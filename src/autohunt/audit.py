"""Append-only structured audit records. No browser or network dependencies."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def append_audit_event(path: Path, event_type: str, payload: dict[str, Any]) -> None:
    """Append one UTF-8 JSON object; callers decide which events are safe to record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
