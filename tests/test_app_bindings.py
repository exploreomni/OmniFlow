import pytest

from omniflow.app_bindings import MAX_HTML_BYTES, published_app_bindings


def script(body):
    return f"<script>{body}</script>"


def test_native_literal_subscription_requires_a_consumed_handle():
    result = published_app_bindings(script("""
        (() => {
          const handles = {};
          omni.ready.then(() => {
            handles['Sales'] = omni.query('Sales');
            handles['Costs'] = omni.query('Costs');
            handles['Unused'] = omni.query('Unused');
            handles['Sales'].onData(rows => render(rows));
            names.forEach(key => handles[key].onData(update));
          }).catch(error => display(error));
        })();
    """))
    assert result.names == {"Sales"}
    assert result.partial


@pytest.mark.parametrize("body", [
    "omni.query('Sales').onData(rows => update(rows));",
    "const q = omni.query('Sales'); q.onData(function(rows) { update(rows); });",
])
def test_direct_and_variable_subscriptions(body):
    result = published_app_bindings(script(body))
    assert result.names == {"Sales"} and not result.partial


@pytest.mark.parametrize("html", [
    '<button data-omni-query="Sales">Sales</button>',
    '<!-- <script>omni.query("Sales").onData(rows => {});</script> -->',
    '<script type="application/json">omni.query("Sales").onData(rows => {});</script>',
    '<template><script>omni.query("Sales").onData(rows => {});</script></template>',
    '<textarea><script>omni.query("Sales").onData(rows => {});</script></textarea>',
    '<script src="/external.js">omni.query("Sales").onData(rows => {});</script>',
    script("/* omni.query('Sales').onData(rows => {}); */ const text = \"omni.query('Sales').onData(cb)\";"),
    script("const q = omni.query('Sales');"),
    script("omni.query(key).onData(rows => {});"),
    script("const omni = other; omni.query('Sales').onData(cb);"),
    script("function unused(omni) { omni.query('Sales').onData(cb); }"),
    script("window.omni = fake; omni.query('Sales').onData(cb);"),
    script("omni.query = fake; omni.query('Sales').onData(cb);"),
    script("if (false) { omni.query('Sales').onData(cb); }"),
    script("function dormant() { omni.query('Sales').onData(cb); }"),
    script("(() => { return; omni.query('Sales').onData(cb); })();"),
    script("const h={}; h['Sales']=omni.query('Sales'); h['Sales']=other; h['Sales'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); h[k]=other; h['Sales'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); leak(h); h['Sales'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); h['Sales'].onData=fake; h['Sales'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); const hidden={h}; h['Sales'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); h['Costs'].onData(cb);"),
    script("const h={}; h['Sales']=omni.query('Sales'); let a=h['Sales']; a.onData=fake; h['Sales'].onData(cb);"),
    script("omni.query('Sales').onData(cb); const bad = ;"),
    "s" * (MAX_HTML_BYTES + 1),
], ids=lambda s: s[:63])
def test_unsupported_binding_never_confirms_a_query(html):
    result = published_app_bindings(html)
    assert not result.names and result.partial


def loop_source(names="['Sales', 'Costs']", before="", middle="", after="", key="key"):
    return script(f"""
      (function() {{
        const tabs = {names};
        const handles = {{}};
        {before}
        omni.ready.then(() => {{
          try {{
            tabs.forEach(key => {{
              handles[key] = omni.query(key);
              {middle}
              handles[{key}].onData(rows => render(rows));
            }});
          }} catch (e) {{ show(e); }}
        }}).catch(e => show(e));
        {after}
      }})();
    """)


def test_static_native_ready_subscription_over_immutable_names():
    result = published_app_bindings(loop_source(
        before="if (typeof omni === 'undefined') return; function rows(handle) { return handle.data; }",
        middle="if (!handles[key]) return;",
        after="function read(key) { return rows(handles[key]); }",
    ))
    assert result.names == {"Sales", "Costs"} and not result.partial


@pytest.mark.parametrize("options", [
    {"names": "['Sales', name]"},
    {"names": "['Sales',,'Costs']"},
    {"names": "['Sales', 'Sales']"},
    {"before": "tabs.pop();"},
    {"before": "const alias = tabs; alias.splice(0);"},
    {"before": "mutate(tabs);"},
    {"before": "const alias = handles;"},
    {"middle": "return;"},
    {"middle": "key = 'Other';"},
    {"middle": "delete handles[key];"},
    {"middle": "handles[key].onData = fake;"},
    {"middle": "leak(handles[key]);"},
    {"key": "'different'"},
])
def test_uncertain_native_list_subscription_is_candidate_only(options):
    result = published_app_bindings(loop_source(**options))
    assert not result.names and result.partial


@pytest.mark.parametrize("guard", [
    "if (true) return;",
    "if (unknown) throw Error();",
    "try { return; } catch (e) {}",
    "while (true) {}",
])
def test_early_exit_cannot_confirm_code_after_it(guard):
    result = published_app_bindings(loop_source(middle=guard))
    assert not result.names and result.partial


def test_escaped_array_through_a_helper_result_remains_uncertain():
    result = published_app_bindings(loop_source(
        before="function describe(xs) { return {xs}; } const options = describe(tabs);",
    ))
    assert not result.names and result.partial


@pytest.mark.parametrize("options", [
    {"middle": "for (key of replacements) { }"},
    {"middle": "[key] = replacements;"},
    {"before": "tabs.map((value, ...args) => args[1].pop());"},
    {"before": "omni.ready.then = replacement;"},
    {"before": "window.omni.query = replacement;"},
])
def test_rebound_sdk_or_key_is_never_confirmed(options):
    result = published_app_bindings(loop_source(**options))
    assert not result.names and result.partial


def test_readiness_guard_for_global_sdk_can_reach_literal_subscription():
    result = published_app_bindings(script("""
      (() => {
        if (!window.omni) return;
        omni.ready.then(() => { omni.query('Sales').onData(rows => render(rows)); });
      })();
    """))
    assert result.names == {"Sales"} and not result.partial
