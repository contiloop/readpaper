#!/usr/bin/env python3
"""Install or check local ReadPaper Codex Desktop wiring.

This script writes only machine-specific files that are intentionally ignored
by Git:

- `.codex/hooks.json`
- `.dryforge/wiring-manifest.json`

Run with `--write` after cloning, then review and trust the project hooks from
Codex Desktop's `/hooks` UI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".agents/skills/readpaper/scripts"))

from readpaper.wiring import atomic_write_json, build_hooks_config, build_wiring_manifest  # noqa: E402


HOOKS_PATH = ROOT / ".codex/hooks.json"
MANIFEST_PATH = ROOT / ".dryforge/wiring-manifest.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _status(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    current = _read_json(path)
    return {
        "path": str(path.relative_to(ROOT)),
        "exists": current is not None,
        "matches": current == expected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write local hooks and wiring manifest")
    parser.add_argument("--check", action="store_true", help="verify local generated files already match")
    args = parser.parse_args(argv)
    if not args.write and not args.check:
        args.check = True

    hooks = build_hooks_config(ROOT)
    manifest = build_wiring_manifest(ROOT)

    if args.write:
        atomic_write_json(HOOKS_PATH, hooks)
        atomic_write_json(MANIFEST_PATH, manifest)

    statuses = {
        "hooks": _status(HOOKS_PATH, hooks),
        "wiring_manifest": _status(MANIFEST_PATH, manifest),
        "next_step": "Review and trust the project hooks in Codex Desktop with /hooks.",
    }
    print(json.dumps(statuses, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if statuses["hooks"]["matches"] and statuses["wiring_manifest"]["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
