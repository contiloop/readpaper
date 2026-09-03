from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.observer import DesktopObserver
from readpaper.state import StateService
from readpaper.storage import read_json


def payload(root: Path, event: str, *, agent_id: str | None = None, trigger: str = "auto") -> dict:
    value = {"hook_event_name": event, "session_id": "session", "cwd": str(root), "trigger": trigger, "task_id": "task"}
    if agent_id is not None:
        value["agent_id"] = agent_id
    return value


def test_long_context_configuration() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / ".codex/config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert config == {
        "model_auto_compact_token_limit": 850000,
        "model_auto_compact_token_limit_scope": "total",
        "tool_output_token_limit": 65536,
    }
    model_context = 1_050_000
    assert model_context - config["model_auto_compact_token_limit"] >= 200_000
    assert 48_000 < config["tool_output_token_limit"]


def test_compact_epochs_are_stream_local_and_pair_checked(tmp_path: Path) -> None:
    state = StateService(tmp_path)
    state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    observer = DesktopObserver(tmp_path)
    observer.compact(payload(tmp_path, "PreCompact"))
    observer.compact(payload(tmp_path, "PostCompact"))
    observer.compact(payload(tmp_path, "PreCompact", agent_id="reviewer"))
    observer.compact(payload(tmp_path, "PostCompact", agent_id="reviewer"))
    host = read_json(state.layout.host_state("task"))
    epochs = sorted(stream["context_epoch"] for stream in host["compact_streams"].values())
    assert epochs == [1, 1]
    observer.compact(payload(tmp_path, "PreCompact"))
    observer.compact(payload(tmp_path, "PostCompact"))
    host = read_json(state.layout.host_state("task"))
    assert sorted(stream["context_epoch"] for stream in host["compact_streams"].values()) == [1, 2]

    with pytest.raises(ReadPaperError) as mismatch:
        observer.compact(payload(tmp_path, "PostCompact", trigger="manual"))
    assert mismatch.value.code is ErrorCode.OBSERVER_UNAVAILABLE
