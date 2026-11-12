"""Recognize a bounded subset of native Omni App subscriptions without executing App code."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser

import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_javascript.language())
MAX_HTML_BYTES = 1024 * 1024
MAX_AST_NODES = 100_000
MAX_ANALYSIS_STEPS = 500_000
MAX_SCRIPTS = 64
_UNKNOWN = frozenset()
_FUNCTIONS = {"arrow_function", "function_expression"}
_CLASSIC_TYPES = {"", "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript"}


@dataclass(frozen=True)
class AppBindings:
    names: frozenset[str]
    partial: bool


@dataclass(frozen=True)
class _FixedLoop:
    call: Node
    declaration: Node
    parameter: bytes
    names: tuple[str, ...]


class _Scripts(HTMLParser):
    # These elements treat their contents as text when browser scripting is enabled.
    CDATA_CONTENT_ELEMENTS = ("script", "style", "title", "textarea", "xmp", "iframe", "noembed", "noframes", "noscript")

    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.active: list[str] | None = None
        self.inert: list[str] = []
        self.partial = False
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"template", "svg", "math", "plaintext"}:
            self.inert.append(tag)
        if self.inert:
            return
        attributes = dict(attrs)
        if "data-omni-query" in attributes or any(key.startswith("on") for key in attributes):
            self.partial = True
        if tag != "script":
            return
        self.script_count += 1
        if self.script_count > MAX_SCRIPTS or len(attributes) != len(attrs):
            raise ValueError("Unsupported script inventory")
        kind = (attributes.get("type") or "").strip().lower()
        if "src" in attributes or "nomodule" in attributes or "language" in attributes or kind == "module":
            self.partial = True
        elif kind in _CLASSIC_TYPES:
            self.active = []

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1] and tag != "plaintext":
                self.inert.pop()
            return
        if tag == "script" and self.active is not None:
            self.scripts.append("".join(self.active))
            self.active = None

    def handle_data(self, data):
        if self.active is not None:
            self.active.append(data)

    def handle_startendtag(self, tag, attrs):
        if tag in {"script", *self.CDATA_CONTENT_ELEMENTS, "template", "plaintext"}:
            raise ValueError("Ambiguous self-closing script or text container")


def _field(node: Node | None, name: str) -> Node | None:
    return node.child_by_field_name(name) if node is not None else None


def _text(node: Node | None) -> bytes:
    return node.text if node is not None else b""


def _children(node: Node | None) -> list[Node]:
    return [child for child in node.named_children if not child.is_extra] if node is not None else []


def _member(node: Node | None, name: bytes) -> Node | None:
    if node is not None and node.type == "member_expression" and _text(_field(node, "property")) == name:
        return _field(node, "object")
    return None


def _literal(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    raw = node.text
    if not 2 < len(raw) <= 514 or b"\\" in raw:
        return None
    name = raw[1:-1].decode()
    return name if name.isprintable() else None


def _nodes(root: Node):
    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 160:
            raise ValueError("App syntax exceeds analysis depth")
        yield node
        pending.extend((child, depth + 1) for child in _children(node))


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type == "parenthesized_expression":
        contents = _children(node)
        node = contents[0] if len(contents) == 1 else None
    return node


def _written(node: Node) -> bool:
    parent = node.parent
    while parent is not None and parent.type in {"parenthesized_expression", "array_pattern", "object_pattern", "pair_pattern", "rest_pattern"}:
        node, parent = parent, parent.parent
    if parent is None:
        return False
    return ((_field(parent, "left") == node and parent.type in {"assignment_expression", "augmented_assignment_expression", "for_in_statement", "assignment_pattern"})
            or (parent.type == "variable_declarator" and _field(parent, "name") == node)
            or parent.type == "update_expression"
            or (parent.type == "unary_expression" and _text(_field(parent, "operator")) == b"delete"))


def _handle_read(handle: Node) -> bool:
    parent = handle.parent
    if parent.type == "member_expression" and _field(parent, "object") == handle and not _written(parent):
        return True
    # A truth test is a read. Passing or assigning the handle itself could
    # expose its subscription method to a later replacement.
    while parent.type == "parenthesized_expression":
        handle, parent = parent, parent.parent
    if parent.type == "if_statement" and _field(parent, "condition") == handle:
        return True
    return parent.type == "unary_expression" and _text(_field(parent, "operator")) in {b"!", b"typeof"}


def _query_argument(call: Node | None) -> Node | None:
    if call is None or call.type != "call_expression" or _text(_member(_field(call, "function"), b"query")) != b"omni":
        return None
    args = _children(_field(call, "arguments"))
    return args[0] if len(args) == 1 else None


def _query_name(call: Node | None) -> str | None:
    return _literal(_query_argument(call))


class _Analysis:
    def __init__(self, roots: list[Node]):
        self.names: set[str] = set()
        self.created: set[Node] = set()
        self.consumed: set[Node] = set()
        self.references: dict[bytes, list[Node]] = defaultdict(list)
        self.unsafe_sdk = False
        self.shadowed_globals = set()
        self.steps = 0
        self.fixed_writes: dict[Node, _FixedLoop] = {}
        self.fixed_loops: dict[Node, _FixedLoop] = {}
        self.subscriptions: set[Node] = set()
        self.bound_subscriptions: set[Node] = set()
        for root in roots:
            for node in _nodes(root):
                if node.type in {"identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"}:
                    self.references[node.text].append(node)
                    if node.text in {b"window", b"globalThis", b"self"} and not (
                            node.parent.type == "member_expression" and _field(node.parent, "object") == node):
                        self.shadowed_globals.add(node.text)
                    if node.text == b"omni" and not (
                        node.parent.type == "member_expression" and _field(node.parent, "object") == node
                        and not _written(node.parent)
                        or node.parent.type == "unary_expression" and _text(_field(node.parent, "operator")) == b"typeof"
                    ):
                        self.unsafe_sdk = True
                if node.type in {"member_expression", "subscript_expression"} and _written(node):
                    obj = _text(_field(node, "object"))
                    prop = _text(_field(node, "property")) or _text(_field(node, "index")).strip(b"'\x22")
                    if obj in {b"window", b"globalThis", b"self"} and prop == b"omni":
                        self.unsafe_sdk = True
                    obj_node = _field(node, "object")
                    while obj_node is not None and obj_node.type in {"member_expression", "subscript_expression"}:
                        if _text(_member(obj_node, b"omni")) in {b"window", b"globalThis", b"self"}:
                            self.unsafe_sdk = True
                        obj_node = _field(obj_node, "object")
                    if _text(obj_node) == b"omni":
                        self.unsafe_sdk = True
                if node.type == "call_expression":
                    callee = _field(node, "function")
                    if _text(_member(callee, b"query")) == b"omni":
                        self.created.add(node)
                    elif _member(callee, b"onData") is not None:
                        # An unrecognized subscription (e.g. a dynamic handle) limits coverage.
                        self.subscriptions.add(node)

    def _spend(self, steps=1):
        self.steps += steps
        if self.steps > MAX_ANALYSIS_STEPS:
            raise ValueError("App exceeds analysis budget")

    def _refs(self, name):
        refs = self.references[name]
        self._spend(len(refs) + 1)
        return refs

    def _read_only(self, value: Node, kind: str, depth: int = 0) -> bool:
        """Follow tracked values through local constant aliases and known helper parameters."""
        self._spend()
        if depth > 12:
            return False
        parent = value.parent
        if parent is None:
            return False
        if _handle_read(value):
            if parent.type != "member_expression" or kind == "handle":
                return True
            member = _text(_field(parent, "property"))
            if member == b"length" or member in {b"includes", b"indexOf", b"join", b"slice", b"at"}:
                return True
            if member in {b"forEach", b"map", b"filter", b"some", b"every", b"find"}:
                call = parent.parent
                args = _children(_field(call, "arguments"))
                if call.type != "call_expression" or len(args) != 1 or args[0].type != "arrow_function":
                    return False
                parameters = _children(_field(args[0], "parameters"))
                return len(parameters) < 3 and all(param.type == "identifier" for param in parameters)
            return False
        if parent.type == "for_in_statement":
            return kind == "array" and _field(parent, "right") == value
        if parent.type == "ternary_expression":
            if _field(parent, "condition") == value:
                return True
            return self._read_only(parent, kind, depth + 1)
        binder = scope = None
        if parent.type == "variable_declarator" and _field(parent, "value") == value and parent.parent.children[0].type == "const":
            binder, scope = _field(parent, "name"), parent.parent.parent
        elif parent.type == "assignment_pattern" and _field(parent, "right") == value and parent.parent.type == "formal_parameters":
            binder, scope = _field(parent, "left"), _field(parent.parent.parent, "body")
        elif parent.type == "arguments":
            call = parent.parent
            fn = _field(call, "function")
            if fn is None or fn.type != "identifier":
                return False
            refs = self._refs(fn.text)
            defs = [r for r in refs if r.parent.type == "function_declaration" and _field(r.parent, "name") == r]
            if len(defs) != 1:
                return False
            scope = defs[0].parent.parent
            if not scope.start_byte <= fn.start_byte < scope.end_byte:
                return False
            for ref in refs:
                if ref == defs[0] or ref.parent.type == "call_expression" and _field(ref.parent, "function") == ref:
                    continue
                # Ignore unrelated same-name references in siblings, e.g., render callback parameters.
                owner = ref.parent
                while owner.parent and owner.type not in _FUNCTIONS | {"function_declaration", "program"}:
                    owner = owner.parent
                if owner.start_byte <= fn.start_byte < owner.end_byte:
                    return False
            definition = defs[0].parent
            parameters = _children(_field(definition, "parameters"))
            pos = _children(parent).index(value)
            if pos < len(parameters):
                binder, scope = parameters[pos], _field(definition, "body")
                if binder.type == "assignment_pattern":
                    binder = _field(binder, "left")
        if binder is None or scope is None or binder.type != "identifier":
            return False
        return all(self._read_only(ref, kind, depth + 1) for ref in self._refs(binder.text)
                   if ref != binder and scope.start_byte <= ref.start_byte < scope.end_byte)

    def _constant_names(self, declaration: Node) -> tuple[str, ...]:
        value = _field(declaration, "value")
        name = _field(declaration, "name")
        if value is None or value.type != "array" or name is None or name.type != "identifier":
            return ()
        entries = _children(value)
        if not entries or len(entries) > 256:
            return ()
        # Inspect punctuation as well, since array holes have no named AST node.
        elements = [c.type for c in value.children if not c.is_extra][1:-1]
        if any(t != ("string" if i % 2 == 0 else ",") for i, t in enumerate(elements)):
            return ()
        names = tuple(_literal(entry) for entry in entries)
        if None in names or len(set(names)) != len(names) or any(n in {"__proto__", "constructor", "prototype"} for n in names):
            return ()
        if declaration.parent.children[0].type != "const" or not all(self._read_only(ref, "array")
                for ref in self._refs(name.text) if ref != name):
            return ()
        return names

    def _fixed_loop_writes(self, roots):
        for root in roots:
            for decl in _nodes(root):
                if decl.type != "variable_declarator":
                    continue
                names = self._constant_names(decl)
                if not names:
                    continue
                array = _field(decl, "name")
                for usage in self._refs(array.text):
                    obj = usage.parent
                    call = obj.parent
                    if _member(obj, b"forEach") != usage or call.type != "call_expression":
                        continue
                    args = _children(_field(call, "arguments"))
                    callback = args[0] if len(args) == 1 else None
                    if callback is None or callback.type != "arrow_function":
                        continue
                    parameters = _children(_field(callback, "parameters")) or [_field(callback, "parameter")]
                    if len(parameters) != 1 or parameters[0] is None or parameters[0].type != "identifier":
                        continue
                    key = parameters[0]
                    body = _field(callback, "body")
                    if body.type != "statement_block" or any(_written(r) or r.parent.type in {"variable_declarator","formal_parameters","required_parameter"}
                            for r in self._refs(key.text) if body.start_byte <= r.start_byte < body.end_byte):
                        continue
                    for stmt in _children(body):
                        exprs = _children(stmt)
                        if stmt.type != "expression_statement" or len(exprs) != 1 or exprs[0].type != "assignment_expression":
                            continue
                        expr = exprs[0]
                        left = _field(expr, "left")
                        arg = _query_argument(_field(expr, "right"))
                        if left.type == "subscript_expression" and arg is not None and arg.type == "identifier" and arg.text == key.text and _text(_field(left, "index")) == key.text:
                            loop = _FixedLoop(call, decl, key.text, names)
                            self.fixed_loops[call] = self.fixed_writes[expr] = loop

    def _stable_handle(self, declaration: Node, *, container: bool) -> bool:
        name = _field(declaration, "name")
        if name is None or name.type != "identifier" or declaration.parent.children[0].type != "const":
            return False
        assigned_keys = set()
        for ref in self._refs(name.text):
            if ref == name:
                continue
            parent = ref.parent
            if container and parent.type == "subscript_expression" and _field(parent, "object") == ref:
                if _written(parent):
                    left, right = _field(parent.parent, "left"), _field(parent.parent, "right")
                    if parent.parent in self.fixed_writes:
                        keys = self.fixed_writes[parent.parent].names
                        if assigned_keys.intersection(keys):
                            return False
                        assigned_keys.update(keys)
                        continue
                    if left != parent or parent.parent.type != "assignment_expression" or _query_name(right) is None:
                        return False
                    key = _literal(_field(parent, "index"))
                    if key is None or key in assigned_keys or key in {"__proto__", "constructor", "prototype"}:
                        return False
                    assigned_keys.add(key)
                elif not self._read_only(parent, "handle"):
                    return False
                continue
            if not container and _handle_read(ref):
                continue
            if (container and parent.type == "arguments" and _text(_field(parent.parent, "function")) == b"Object.keys"):
                continue
            return False
        return True

    def _may_exit(self, node):
        pending = [node]
        while pending:
            item = pending.pop()
            self._spend()
            if item.type in _FUNCTIONS | {"function_declaration", "method_definition", "class_declaration"}:
                continue
            if item.type in {"return_statement", "throw_statement", "while_statement", "do_statement", "for_statement"}:
                return True
            pending.extend(_children(item))
        return False

    def _condition(self, node, handles, loop):
        condition = _unwrap(node)
        if condition is None:
            return None
        if condition.type in {"true", "false"}:
            return condition.type == "true"
        if condition.type == "binary_expression":
            left, right = _field(condition, "left"), _field(condition, "right")
            op = _text(_field(condition, "operator"))
            if left.type == "unary_expression" and _text(_field(left, "operator")) == b"typeof" and _text(_field(left, "argument")) == b"omni" and _literal(right) == "undefined":
                if op in {b"===", b"==", b"!==", b"!="}:
                    return op in {b"!==", b"!="}
        if condition.type == "unary_expression" and _text(_field(condition, "operator")) == b"!":
            obj = _field(condition, "argument")
            if obj.type == "identifier" and obj.text == b"omni":
                return False
            glob = _text(_member(obj, b"omni"))
            if glob in {b"window", b"globalThis", b"self"} - self.shadowed_globals:
                return False
            if obj.type == "subscript_expression":
                index = _field(obj, "index")
                key = loop.parameter if loop and _text(index) == loop.parameter else _literal(index)
                if key is not None and (_text(_field(obj, "object")), key) in handles:
                    return False
        return None

    def run(self, node: Node | None, handles: dict, maps: set[bytes], depth: int = 0, arrays=None, keys=None):
        arrays = set() if arrays is None else arrays
        if node is None or depth > 40:
            return
        self._spend()
        if node.type in {"program", "statement_block"}:
            for stmt in _children(node):
                if self.run(stmt, handles, maps, depth + 1, arrays, keys) is False:
                    return False
            return
        if node.type in {"return_statement", "throw_statement", "break_statement", "continue_statement", "while_statement", "do_statement", "for_statement"}:
            return False
        if node.type == "if_statement":
            outcome = self._condition(_field(node, "condition"), handles, keys)
            if outcome is None:
                return not self._may_exit(node)
            branch = _field(node, "consequence" if outcome else "alternative")
            if branch is not None and branch.type == "else_clause":
                branch = _children(branch)[0]
            return self.run(branch, handles, maps, depth + 1, arrays, keys)
        if node.type == "try_statement":
            falls_through = self.run(_field(node, "body"), handles, maps, depth + 1, arrays, keys)
            return falls_through is not False and not any(self._may_exit(part) for part in _children(node) if part.type in {"catch_clause", "finally_clause"})
        if node.type in {"for_in_statement", "switch_statement", "labeled_statement", "with_statement"}:
            return not self._may_exit(node)
        if node.type == "expression_statement":
            self.run(_children(node)[0], handles, maps, depth + 1, arrays, keys)
            return
        if node.type == "lexical_declaration":
            for decl in _children(node):
                name = _field(decl, "name")
                value = _field(decl, "value")
                if name is None or value is None:
                    continue
                if value.type == "array":
                    arrays.add(decl)
                if value.type == "object" and not _children(value) and self._stable_handle(decl, container=True):
                    maps.add(name.text)
                elif _query_name(value) and self._stable_handle(decl, container=False):
                    handles[(name.text, None)] = value
            return
        if node.type == "assignment_expression":
            left, right = _field(node, "left"), _field(node, "right")
            if node in self.fixed_writes and keys is not None and keys == self.fixed_writes[node]:
                obj = _text(_field(left, "object"))
                if obj in maps:
                    handles[(obj, keys.parameter)] = right
                return
            if left is not None and left.type == "subscript_expression" and _query_name(right):
                obj, key = _text(_field(left, "object")), _literal(_field(left, "index"))
                if key and obj in maps:
                    reference = (obj, key)
                    handles[reference] = right
            return
        if node.type != "call_expression":
            return
        callee = _unwrap(_field(node, "function"))
        args = _children(_field(node, "arguments"))
        if callee is None:
            return
        if callee.type in _FUNCTIONS and not args and not _children(_field(callee, "parameters")) and not _field(callee, "parameter"):
            self.run(_field(callee, "body"), handles.copy(), maps.copy(), depth + 1, arrays.copy(), keys)
            return
        chain = _member(callee, b"catch")
        if chain is not None:
            self.run(chain, handles, maps, depth + 1, arrays, keys)
            return
        if _text(_member(_member(callee, b"then"), b"ready")) == b"omni" and len(args) == 1 and args[0].type in _FUNCTIONS:
            self.run(_field(args[0], "body"), handles.copy(), maps.copy(), depth + 1, arrays.copy(), keys)
            return
        if _member(callee, b"forEach") is not None:
            loop = self.fixed_loops.get(node)
            if loop is not None and loop.declaration in arrays:
                self.run(_field(args[0], "body"), handles.copy(), maps.copy(), depth + 1, arrays.copy(), loop)
            return
        obj = _member(callee, b"onData")
        if obj is None or len(args) != 1 or args[0].type not in _FUNCTIONS:
            return
        if obj.type == "identifier":
            source = handles.get((obj.text, None))
        elif obj.type == "subscript_expression":
            key = _literal(_field(obj, "index"))
            if keys and _text(_field(obj, "index")) == keys.parameter:
                key = keys.parameter
            source = handles.get((_text(_field(obj, "object")), key)) if key is not None else None
        else:
            source = obj
        query = _query_name(source)
        names = (query,) if query else ()
        if keys and source is not None and self.fixed_writes.get(source.parent) == keys:
            names = keys.names
        if names:
            self.names.update(names)
            self.consumed.add(source)
            self.bound_subscriptions.add(node)


def published_app_bindings(html: str) -> AppBindings:
    """Report literal SDK subscriptions in supported inline scripts; names alone never establish usage."""
    if not isinstance(html, str) or len(html) > MAX_HTML_BYTES:
        return AppBindings(_UNKNOWN, True)
    try:
        if len(html.encode()) > MAX_HTML_BYTES:
            return AppBindings(_UNKNOWN, True)
        document = _Scripts()
        document.feed(html)
        document.close()
        if document.active is not None or document.inert:
            return AppBindings(_UNKNOWN, True)
        parser = Parser(_LANGUAGE)
        roots = [parser.parse(source.encode()).root_node for source in document.scripts]
        if any(root.has_error for root in roots) or sum(root.descendant_count for root in roots) > MAX_AST_NODES:
            return AppBindings(_UNKNOWN, True)
        if len(roots) != 1:
            return AppBindings(_UNKNOWN, True)
        analysis = _Analysis(roots)
        if analysis.unsafe_sdk:
            return AppBindings(_UNKNOWN, True)
        analysis._fixed_loop_writes(roots)
        analysis.run(roots[0], {}, set())
        partial = (document.partial or not analysis.names or analysis.subscriptions != analysis.bound_subscriptions
                   or analysis.created != analysis.consumed)
        return AppBindings(frozenset(analysis.names), partial)
    except (ValueError, UnicodeError, RecursionError, AssertionError):
        return AppBindings(_UNKNOWN, True)
