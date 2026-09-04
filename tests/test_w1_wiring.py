from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from readpaper.parse_invocation import SCHEMA_SHA256
from readpaper.context_budget import ContextBudgetPolicy
from readpaper.wiring import build_hooks_config, build_wiring_manifest


ROOT = Path(__file__).resolve().parents[1]


def test_wiring_manifest_and_hook_cardinality() -> None:
    manifest = build_wiring_manifest(ROOT)
    hooks = build_hooks_config(ROOT)["hooks"]
    assert manifest["parser_schema_sha256"] == SCHEMA_SHA256
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "Stop"}
    assert len(hooks["Stop"]) == len(hooks["PreCompact"]) == len(hooks["PostCompact"]) == 1
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    for role, expected in manifest["roles"].items():
        assert hashlib.sha256((ROOT / ".codex/agents" / f"{role}.toml").read_bytes()).hexdigest() == expected
    with (ROOT / ".codex/config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert config["model"] == "gpt-5.6-sol"
    assert config == ContextBudgetPolicy.load(ROOT).codex_settings()
    assert config["tool_output_token_limit"] == 65536


def test_local_generated_wiring_matches_when_present() -> None:
    hooks_path = ROOT / ".codex/hooks.json"
    manifest_path = ROOT / ".dryforge/wiring-manifest.json"
    if hooks_path.exists():
        assert json.loads(hooks_path.read_text()) == build_hooks_config(ROOT)
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text()) == build_wiring_manifest(ROOT)


def test_gitignore_managed_block_is_exact_and_unique() -> None:
    content = (ROOT / ".gitignore").read_text()
    assert content.count("# BEGIN READPAPER MANAGED") == 1
    assert content.count("# END READPAPER MANAGED") == 1
