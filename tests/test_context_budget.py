from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from readpaper.context_budget import ContextBudgetPolicy, render_codex_config
from readpaper.errors import ReadPaperError


@pytest.mark.parametrize("name,source_limit", [("long-paper", 650_000), ("cost-controlled", 150_000)])
def test_preset_budget_and_config_roundtrip(tmp_path: Path, name: str, source_limit: int) -> None:
    policy = ContextBudgetPolicy.presets(tmp_path)[name]
    assert policy.source_limit == source_limit
    assert policy.text_limit(10) == source_limit - 24_000
    assert policy.scope_estimate(policy.text_limit(10), 10) == source_limit
    assert policy.text_limit(1_000_000) == 0
    rendered = render_codex_config('model = "old"\nother = true\n[features]\nhooks = true\n', policy)
    assert tomllib.loads(rendered) == policy.codex_settings() | {"other": True, "features": {"hooks": True}}
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(rendered)
    assert ContextBudgetPolicy.load(tmp_path) == policy
    config.write_text(rendered.replace(str(policy.auto_compact_limit), "100000"))
    with pytest.raises(ReadPaperError, match="do not match"):
        ContextBudgetPolicy.load(tmp_path)
