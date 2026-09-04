from __future__ import annotations

import json
from pathlib import Path

import pytest

from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.observer import DesktopObserver
from readpaper.parse_invocation import parse_argv
from test_t9_hooks import prepared_run, quote


@pytest.mark.parametrize("field,value", [
    ("status", "issued"), ("task_id", "other"), ("agent_id", "reviewer"), ("session_id", "old-session"),
    ("turn_id", "old-turn"), ("pretool_semantic_key", "other"), ("request_digest", "other"),
    ("tool_use_id", "other"), ("argv_sha256", "other"),
])
def test_automatic_workflow_requires_exact_current_root_receipt(tmp_path: Path, field: str, value: str) -> None:
    runtime, prepared, _, _ = prepared_run(tmp_path, observed_prompt=False)
    run = runtime.state.get_run(prepared["paper_id"], prepared["run_id"])
    capability = next(json.loads(path.read_text()) for path in runtime.state.layout.runtime.joinpath("invocation-capabilities").glob("*.json")
                      if json.loads(path.read_text())["tool_use_id"] == "bootstrap-scope")
    receipt = runtime._workflow_authority(run, capability, "turn-0")
    assert receipt.event_kind.value == "pretool_authorized"
    assert receipt.subject_id == capability["tool_use_id"]
    with pytest.raises(ReadPaperError) as error:
        runtime._workflow_authority(run, capability | {field: value}, "turn-0")
    assert error.value.code == ErrorCode.OBSERVER_UNAVAILABLE


@pytest.mark.parametrize("scope_kind", ["full", "user_reduced"])
def test_automatic_command_cannot_drop_artifacts_without_user_approval(tmp_path: Path, scope_kind: str) -> None:
    runtime, prepared, _, _ = prepared_run(tmp_path, observed_prompt=False)
    ref = prepared["data"]["artifacts"][0]["artifact_ref_id"]
    payload = tmp_path / "scope-attempt.json"
    payload.write_text(json.dumps({"scope_kind": scope_kind, "required_artifact_ref_ids": [],
                                   "excluded_artifacts": [] if scope_kind == "full" else [{
                                       "artifact_ref_id": ref, "reason_code": "user_excluded",
                                       "user_confirmation_event_id": "not-a-user-event"}],
                                   "user_turn_id": "turn-0"}))
    words = [str(tmp_path / ".venv/bin/python"), str(tmp_path / ".agents/skills/readpaper/scripts/paper.py"),
             "record", prepared["run_id"], "--kind", "scope_confirmation", "--payload", str(payload),
             "--client-request-id", "cr_" + "9" * 32]
    base = {"session_id": "session", "turn_id": "turn-0", "cwd": str(tmp_path), "tool_name": "Bash",
            "tool_use_id": "scope-attempt", "tool_input": {"command": quote(words)}}
    observer = DesktopObserver(tmp_path)
    assert json.loads(observer.pre_tool({"hook_event_name": "PreToolUse", **base}))["hookSpecificOutput"]["permissionDecision"] == "allow"
    response = json.loads(runtime.execute(parse_argv(words[2:])))
    assert not response["ok"]
    assert response["error"]["code"] == ("INVALID_ARGUMENT" if scope_kind == "full" else "OBSERVER_UNAVAILABLE")
    assert runtime.state.get_run(prepared["paper_id"], prepared["run_id"]).required_artifact_ref_ids == (ref,)
