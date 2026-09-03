"""Generate project-local Codex Desktop wiring for ReadPaper.

The committed workflow should be portable.  Codex hook commands, however,
must point at the clone's local absolute paths, so `.codex/hooks.json` and the
wiring manifest are generated on each machine instead of being committed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .parse_invocation import SCHEMA_SHA256


MANIFEST_FILES = (
    ".codex/hooks/readpaper_observer.py",
    ".codex/hooks/readpaper_stop_hook.py",
    ".codex/hooks/readpaper_compact_hook.py",
    ".codex/config.toml",
    ".agents/skills/readpaper/scripts/readpaper/parse_invocation.py",
    ".agents/skills/readpaper/scripts/paper.py",
    ".agents/skills/readpaper/SKILL.md",
)

REVIEWER_ROLES = ("math_visual", "claim_experiment", "explanation_flow")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _command(root: Path, hook_script: str) -> str:
    return f"{root / '.venv/bin/python'} {root / hook_script}"


def _command_hook(command: str, status_message: str) -> dict[str, Any]:
    return {
        "type": "command",
        "command": command,
        "timeout": 10,
        "statusMessage": status_message,
    }


def build_hooks_config(root: Path) -> dict[str, Any]:
    """Return the expected `.codex/hooks.json` content for this checkout."""

    root = root.resolve()
    observer = _command(root, ".codex/hooks/readpaper_observer.py")
    compact = _command(root, ".codex/hooks/readpaper_compact_hook.py")
    stop = _command(root, ".codex/hooks/readpaper_stop_hook.py")
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "^(startup|resume|clear|compact)$",
                    "hooks": [
                        _command_hook(observer, "Recording ReadPaper session boundary"),
                    ],
                },
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        _command_hook(observer, "Binding ReadPaper user turn"),
                    ],
                },
            ],
            "SubagentStart": [
                {
                    "matcher": ".*",
                    "hooks": [
                        _command_hook(observer, "Binding ReadPaper reviewer actor"),
                    ],
                },
            ],
            "SubagentStop": [
                {
                    "matcher": ".*",
                    "hooks": [
                        _command_hook(observer, "Closing ReadPaper reviewer actor"),
                    ],
                },
            ],
            "PreToolUse": [
                {
                    "matcher": "^Bash$",
                    "hooks": [
                        _command_hook(observer, "Authorizing exact ReadPaper command"),
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "^(Bash|view_image)$",
                    "hooks": [
                        _command_hook(observer, "Correlating ReadPaper tool evidence"),
                    ],
                },
            ],
            "PreCompact": [
                {
                    "matcher": "^(manual|auto)$",
                    "hooks": [
                        _command_hook(compact, "Opening ReadPaper compact epoch"),
                    ],
                },
            ],
            "PostCompact": [
                {
                    "matcher": "^(manual|auto)$",
                    "hooks": [
                        _command_hook(compact, "Closing ReadPaper compact epoch"),
                    ],
                },
            ],
            "Stop": [
                {
                    "hooks": [
                        _command_hook(stop, "Checking ReadPaper delivery and repair"),
                    ],
                },
            ],
        },
    }


def build_wiring_manifest(root: Path) -> dict[str, Any]:
    """Return the expected local wiring manifest for this checkout."""

    root = root.resolve()
    return {
        "schema_version": 1,
        "project_root": str(root),
        "python_path": str(root / ".venv/bin/python"),
        "paper_script_path": str(root / ".agents/skills/readpaper/scripts/paper.py"),
        "parser_schema_sha256": SCHEMA_SHA256,
        "files": {relative: _sha256(root / relative) for relative in MANIFEST_FILES},
        "roles": {
            role: _sha256(root / ".codex/agents" / f"{role}.toml")
            for role in REVIEWER_ROLES
        },
    }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
