from __future__ import annotations

from typing import Any

from .exceptions import OmniAPIError


def normalize_model_response(payload: Any) -> list[dict[str, Any]]:
    """Validate evidence before severity classification and discard unknown metadata.

    The model validation API documents string messages/paths, a boolean warning
    flag, and auto-fix description strings. An omitted warning flag remains a
    conservative error for compatibility; present values are never coerced.
    """
    if not isinstance(payload, list):
        raise OmniAPIError("Model validation returned an unexpected response shape")
    normalized = []
    for item in payload:
        if not isinstance(item, dict):
            raise OmniAPIError("Model validation returned an invalid issue row")
        message = item.get("message")
        if not isinstance(message, str):
            raise OmniAPIError("Model validation returned an invalid issue message")
        is_warning = item.get("is_warning", False)
        if not isinstance(is_warning, bool):
            raise OmniAPIError("Model validation returned an invalid warning flag")
        yaml_path = item.get("yaml_path")
        if yaml_path is not None and not isinstance(yaml_path, str):
            raise OmniAPIError("Model validation returned an invalid YAML path")
        auto_fix = item.get("auto_fix")
        if auto_fix is not None:
            if not isinstance(auto_fix, dict):
                raise OmniAPIError("Model validation returned invalid auto-fix metadata")
            allowed_fix = {}
            for key in ("description_short", "description_unique"):
                if key in auto_fix:
                    if not isinstance(auto_fix[key], str):
                        raise OmniAPIError("Model validation returned an invalid auto-fix description")
                    allowed_fix[key] = auto_fix[key]
            auto_fix = allowed_fix
        normalized.append(
            {"message": message, "is_warning": is_warning, "yaml_path": yaml_path, "auto_fix": auto_fix}
        )
    return normalized
