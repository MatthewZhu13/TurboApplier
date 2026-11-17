"""Append-only local audit log (data/log.jsonl): what was read, filled and sent where."""
import json
from datetime import datetime, timezone

from . import config


def log(event: str, **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with config.LOG.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    print(f"[{event}]", {k: v for k, v in fields.items() if k != "values"})
