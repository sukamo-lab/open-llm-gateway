from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from llm_gateway.audit import GENESIS_HASH, verify_events


def load_events(path: Path) -> list[dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get("events")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("export must be a JSON event list, an object with events, or JSON Lines")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Independently verify an LLM Gateway audit export")
    parser.add_argument("path", type=Path)
    parser.add_argument("--checkpoint-sequence", type=int, default=0)
    parser.add_argument("--checkpoint-hash", default=GENESIS_HASH)
    args = parser.parse_args()
    try:
        events = load_events(args.path)
        valid, last_hash = verify_events(events, args.checkpoint_hash, args.checkpoint_sequence)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"invalid audit export: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not valid:
        print("audit chain verification failed", file=sys.stderr)
        raise SystemExit(1)
    sequence = events[-1]["sequence"] if events else args.checkpoint_sequence
    print(json.dumps({"valid": True, "sequence": sequence, "last_hash": last_hash}))


if __name__ == "__main__":
    main()
