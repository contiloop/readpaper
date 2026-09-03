#!/usr/bin/env python3
"""Dedicated compact observer entrypoint used by production hook wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/skills/readpaper/scripts"))

from readpaper.observer import DesktopObserver  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    sys.stdout.buffer.write(DesktopObserver(ROOT).compact(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
