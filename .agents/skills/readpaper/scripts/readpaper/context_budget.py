"""One source of truth for startup presets and per-scope context budgets."""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import ErrorCode, ReadPaperError


POLICY_PATH = Path(".codex/readpaper-context.toml")
PACKAGE_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class ContextBudgetPolicy:
    profile: str
    model: str
    model_context_window: int
    auto_compact_limit: int
    output_reserve: int
    workflow_reserve: int
    tool_output_token_limit: int
    visual_tokens_per_unit: int
    fixed_source_reserve: int

    def __post_init__(self) -> None:
        values = asdict(self)
        if any(type(value) is not int or value <= 0 for key, value in values.items() if key not in {"profile", "model"}):
            raise ReadPaperError(ErrorCode.UNSUPPORTED_MODEL_CONFIG, "context budgets must be positive integers")
        if not 0 < self.source_limit < self.auto_compact_limit < self.model_context_window:
            raise ReadPaperError(ErrorCode.UNSUPPORTED_MODEL_CONFIG, "context reserves exceed the selected preset")

    @property
    def source_limit(self) -> int:
        return self.auto_compact_limit - self.output_reserve - self.workflow_reserve

    def text_limit(self, visual_count: int) -> int:
        return max(0, self.source_limit - visual_count * self.visual_tokens_per_unit - self.fixed_source_reserve)

    def scope_estimate(self, text_tokens: int, visual_count: int) -> int:
        return text_tokens + visual_count * self.visual_tokens_per_unit + self.fixed_source_reserve

    def codex_settings(self) -> dict[str, str | int]:
        return {
            "model": self.model,
            "model_context_window": self.model_context_window,
            "model_auto_compact_token_limit": self.auto_compact_limit,
            "model_auto_compact_token_limit_scope": "total",
            "tool_output_token_limit": self.tool_output_token_limit,
        }

    @classmethod
    def presets(cls, root: Path) -> dict[str, "ContextBudgetPolicy"]:
        path = root / POLICY_PATH
        if not path.exists():
            path = PACKAGE_ROOT / POLICY_PATH
        value = tomllib.loads(path.read_text())
        common = {key: item for key, item in value.items() if key != "profiles"}
        return {name: cls(profile=name, **common, **settings) for name, settings in value["profiles"].items()}

    @classmethod
    def load(cls, root: Path) -> "ContextBudgetPolicy":
        path = root / ".codex/config.toml"
        if not path.exists():
            path = PACKAGE_ROOT / ".codex/config.toml"
        config = tomllib.loads(path.read_text())
        matches = [policy for policy in cls.presets(root).values() if all(config.get(key) == value for key, value in policy.codex_settings().items())]
        if len(matches) != 1:
            raise ReadPaperError(
                ErrorCode.UNSUPPORTED_MODEL_CONFIG,
                "Codex settings do not match a ReadPaper context preset; configure a preset before starting a new session",
            )
        return matches[0]


def render_codex_config(existing: str, policy: ContextBudgetPolicy) -> str:
    """Update top-level budget settings while preserving unrelated TOML."""
    settings = policy.codex_settings()
    lines = existing.splitlines(keepends=True)
    boundary = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    top = [line for line in lines[:boundary] if not any(re.match(rf"\s*{re.escape(key)}\s*=", line) for key in settings)]
    generated = [f'{key} = {value!r}\n' if isinstance(value, str) else f"{key} = {value}\n" for key, value in settings.items()]
    return "".join(generated + top + lines[boundary:])
