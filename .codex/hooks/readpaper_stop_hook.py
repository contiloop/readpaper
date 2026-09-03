#!/usr/bin/env python3
"""Project Stop hook entrypoint with durable replay."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/skills/readpaper/scripts"))

from readpaper.stop import StopCoordinator  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    sys.stdout.buffer.write(StopCoordinator(ROOT).handle_stop(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
