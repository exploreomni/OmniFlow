from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import redact, secure_write_text


def write_json_report(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    secure_write_text(target, json.dumps(redact(payload), indent=2, sort_keys=True) + "\n")
