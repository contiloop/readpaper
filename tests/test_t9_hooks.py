from __future__ import annotations

import json
import shlex
from pathlib import Path

from reportlab.pdfgen import canvas

from readpaper.canonical import digest, digest_text
from readpaper.authority import bound_request_document
from readpaper.audits import AuditStage, ContentRole, reserve_content_audit
from readpaper.commands import CommandRuntime
from readpaper.models import Actor, HostEventKind, RunState
from readpaper.observer import DesktopObserver
from readpaper.parse_invocation import parse_argv
from readpaper.stop import StopCoordinator


def quote(words: list[str]) -> str:
    return " ".join("'" + word.replace("'", "'\"'\"'") + "'" for word in words)


def make_pdf(path: Path) -> None:
    doc = canvas.Canvas(str(path))
    doc.drawString(72, 720, "T9 observer fixture")
    doc.showPage()
    doc.save()


def execute_authorized(runtime: CommandRuntime, argv: list[str], tool: str) -> dict:
    invocation = parse_argv(argv)
    assert invocation is not None
    runtime.authority.issue(
        pretool_semantic_key=digest(["bootstrap", tool]),
        client_request_id=str(invocation.flags["--client-request-id"]),
        request_digest=digest(bound_request_document(invocation)),
        argv_sha256=digest_text("bootstrap"), hook_definition_hash="a" * 64,
        task_id=str(invocation.flags.get("--task-id", "task")), session_id="session", turn_id="turn-0",
        tool_use_id=tool, agent_id="root", agent_execution_id="ae_" + "1" * 64,
        context_stream_id="ctx_" + "2" * 64, context_epoch=0,
    )
    return json.loads(runtime.execute(invocation))


def prepared_run(tmp_path: Path) -> tuple[CommandRuntime, dict, str, str]:
    runtime = CommandRuntime(tmp_path)
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    runtime.state.append_host_event(
        task_id="task", event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key="user-turn-0", subject_id="turn-0",
        payload={"prompt_sha256": digest_text("paper request"), "byte_length": 13},
    )
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    prepared = execute_authorized(runtime, ["prepare", str(source), "--task-id", "task", "--user-turn-id", "turn-0", "--client-request-id", "cr_" + "1" * 32], "bootstrap-prepare")
    run_id = prepared["run_id"]
    execute_authorized(runtime, ["answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", "turn-0", "--client-request-id", "cr_" + "2" * 32], "bootstrap-answer")
    ref = prepared["data"]["artifacts"][0]["artifact_ref_id"]
    payload_dir = tmp_path / "payloads"
    payload_dir.mkdir(mode=0o700)
    scope = payload_dir / "scope.json"
    scope.write_text(json.dumps({"scope_kind": "full", "required_artifact_ref_ids": [ref], "excluded_artifacts": [], "user_turn_id": "turn-0"}))
    execute_authorized(runtime, ["record", run_id, "--kind", "scope_confirmation", "--payload", str(scope), "--client-request-id", "cr_" + "3" * 32], "bootstrap-scope")
    return runtime, prepared, prepared["data"]["transport_frames"][0]["frame_id"], prepared["data"]["visual_units"][0]["unit_id"]


def test_pretool_issues_capability_and_posttool_is_only_read_coverage_authority(tmp_path: Path) -> None:
    runtime, prepared, frame_id, _ = prepared_run(tmp_path)
    observer = DesktopObserver(tmp_path)
    client = "cr_" + "4" * 32
    words = [str((tmp_path / ".venv/bin/python").resolve()), str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").resolve()), "read", prepared["run_id"], "--frame-id", frame_id, "--client-request-id", client]
    command = quote(words)
    base = {"session_id": "session", "turn_id": "turn-1", "cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "tool-read", "tool_input": {"command": command}}
    allowed = json.loads(observer.pre_tool({"hook_event_name": "PreToolUse", **base}))
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    invocation = parse_argv(words[2:])
    assert invocation is not None
    response = runtime.execute(invocation).decode()
    before = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))
    assert frame_id in before["data"]["missing_resident_frame_ids"]
    observer.post_tool({"hook_event_name": "PostToolUse", **base, "tool_response": {"output": response}})
    after = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))
    assert frame_id not in after["data"]["missing_resident_frame_ids"]


def test_observer_rejects_frame_with_wrong_content_hash(tmp_path: Path) -> None:
    runtime, prepared, frame_id, _ = prepared_run(tmp_path)
    observer = DesktopObserver(tmp_path)
    client = "cr_" + "5" * 32
    words = [
        str((tmp_path / ".venv/bin/python").resolve()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").resolve()),
        "read",
        prepared["run_id"],
        "--frame-id",
        frame_id,
        "--client-request-id",
        client,
    ]
    command = quote(words)
    base = {
        "session_id": "session",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-truncated-read",
        "tool_input": {"command": command},
    }
    observer.pre_tool({"hook_event_name": "PreToolUse", **base})
    invocation = parse_argv(words[2:])
    assert invocation is not None
    response = json.loads(runtime.execute(invocation))
    response["data"]["content"] = response["data"]["content"][:-1]
    observer.post_tool({
        "hook_event_name": "PostToolUse",
        **base,
        "tool_response": {"output": json.dumps(response)},
    })
    checked = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))
    assert frame_id in checked["data"]["missing_historical_frame_ids"]


def test_previous_epoch_frames_remain_historical_but_not_resident(tmp_path: Path) -> None:
    runtime, prepared, frame_id, _ = prepared_run(tmp_path)
    observer = DesktopObserver(tmp_path)
    client = "cr_" + "6" * 32
    words = [
        str((tmp_path / ".venv/bin/python").resolve()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").resolve()),
        "read",
        prepared["run_id"],
        "--frame-id",
        frame_id,
        "--client-request-id",
        client,
    ]
    command = quote(words)
    base = {
        "session_id": "session",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-epoch-read",
        "tool_input": {"command": command},
    }
    observer.pre_tool({"hook_event_name": "PreToolUse", **base})
    invocation = parse_argv(words[2:])
    assert invocation is not None
    response = runtime.execute(invocation).decode()
    observer.post_tool({"hook_event_name": "PostToolUse", **base, "tool_response": {"output": response}})
    compact_base = {
        "session_id": "session",
        "cwd": str(tmp_path),
        "trigger": "auto",
        "task_id": "task",
    }
    observer.compact({"hook_event_name": "PreCompact", **compact_base})
    observer.compact({"hook_event_name": "PostCompact", **compact_base})
    checked = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))
    assert checked["data"]["historical_coverage"]["frames"] == 1
    assert checked["data"]["resident_coverage"]["frames"] == 0
    assert checked["data"]["missing_historical_frame_ids"] == []
    assert checked["data"]["missing_resident_frame_ids"] == [frame_id]
    assert checked["data"]["full_source_currently_resident"] is False


def test_pretool_allows_canonical_read_only_check_without_capability(tmp_path: Path) -> None:
    _, prepared, _, _ = prepared_run(tmp_path)
    observer = DesktopObserver(tmp_path)
    words = [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
        "check",
        prepared["run_id"],
    ]
    capabilities = observer.state.layout.runtime / "invocation-capabilities"
    before = sorted(capabilities.glob("cap_*.json"))
    allowed = json.loads(observer.pre_tool({
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "turn_id": "turn-check",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-check",
        "tool_input": {"command": quote(words)},
    }))
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert sorted(capabilities.glob("cap_*.json")) == before


def test_reviewer_start_and_protected_challenge_bind_reserved_execution(tmp_path: Path) -> None:
    runtime, prepared, _, _ = prepared_run(tmp_path)
    run_id = prepared["run_id"]
    reserved_execution = "ae_" + "7" * 64
    reservation = reserve_content_audit(
        run_id=run_id,
        role=ContentRole.MATH_VISUAL,
        audit_seq=1,
        stage=AuditStage.SOURCE_FIRST,
        attempt_no=1,
        agent_execution_id=reserved_execution,
        input_digest="8" * 64,
    ) | {
        "entity_id": "audit-source-first-attempt-1",
        "bundle_id": prepared["bundle_id"],
        "run_id": run_id,
        "expected_reviewer_agent_id": None,
    }
    payload_dir = tmp_path / "payloads"
    start_path = payload_dir / "audit-start.json"
    start_path.write_text(json.dumps(reservation), encoding="utf-8")
    execute_authorized(
        runtime,
        [
            "record", run_id, "--kind", "audit_start", "--payload", str(start_path),
            "--client-request-id", "cr_" + "5" * 32,
        ],
        "bootstrap-audit-start",
    )
    start_record = next(
        json.loads(path.read_text())
        for path in runtime.state.layout.run_records(prepared["paper_id"], run_id).glob("rec_*.json")
        if json.loads(path.read_text())["record_kind"] == "audit_start"
    )
    assert start_record["payload"]["reservation_host_event_seq_floor"] >= 1

    observer = DesktopObserver(tmp_path)
    reviewer_id = "reviewer-native-id"
    observer.agent_event({
        "hook_event_name": "SubagentStart",
        "task_id": "task",
        "session_id": "session",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "agent_id": reviewer_id,
        "agent_type": "math_visual",
    })
    result = start_record["payload"] | {
        "entity_id": "audit-source-first-attempt-1-result",
        "reviewer_agent_id": reviewer_id,
        "reviewer_synthesis_epoch": 0,
        "status": "returned",
        "read_frame_ids": [],
        "opened_visual_unit_ids": [],
        "unverified_scope": [],
        "findings": [],
        "recheck_finding_ids": [],
        "recheck_results": [],
    }
    result_path = payload_dir / "audit-result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    words = [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
        "record", run_id, "--kind", "audit_result", "--payload", str(result_path),
        "--client-request-id", "cr_" + "6" * 32,
    ]
    pretool = {
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-review-result",
        "agent_id": reviewer_id,
        "tool_input": {"command": quote(words)},
    }
    allowed = json.loads(observer.pre_tool(pretool))
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    invocation = parse_argv(words[2:])
    assert invocation is not None
    response = json.loads(runtime.execute(invocation))
    assert response["ok"] is True
    capability = observer._capability_for_tool("tool-review-result")
    assert capability is not None
    assert capability["agent_execution_id"] == reserved_execution
    host_state = json.loads(runtime.state.layout.host_state("task").read_text())
    claim = host_state["reviewer_bindings"][reservation["reviewer_assignment_id"]]
    assert claim["agent_id"] == reviewer_id
    assert claim["evidence_kind"] == "agent_start_plus_protected_challenge_v1"
    check = json.loads(runtime.execute(parse_argv(["check", run_id])))
    assert check["data"]["invalid_agent_execution_ids"] == []

    followup_execution = "ae_" + "d" * 64
    followup = reserve_content_audit(
        run_id=run_id,
        role=ContentRole.MATH_VISUAL,
        audit_seq=1,
        stage=AuditStage.NOTE_COMPARISON,
        attempt_no=1,
        agent_execution_id=followup_execution,
        input_digest="e" * 64,
    ) | {
        "entity_id": "audit-note-comparison-attempt-1",
        "bundle_id": prepared["bundle_id"],
        "run_id": run_id,
        "expected_reviewer_agent_id": reviewer_id,
        "note_version_id": "nv_" + "f" * 64,
    }
    followup_start_path = payload_dir / "audit-followup-start.json"
    followup_start_path.write_text(json.dumps(followup), encoding="utf-8")
    execute_authorized(
        runtime,
        [
            "record", run_id, "--kind", "audit_start", "--payload", str(followup_start_path),
            "--client-request-id", "cr_" + "d" * 32,
        ],
        "bootstrap-audit-followup-start",
    )
    followup_start_record = next(
        value
        for path in runtime.state.layout.run_records(prepared["paper_id"], run_id).glob("rec_*.json")
        if (value := json.loads(path.read_text()))["record_kind"] == "audit_start"
        and value["payload"].get("stage") == "note_comparison"
    )
    followup_result = followup_start_record["payload"] | {
        "entity_id": "audit-note-comparison-attempt-1-result",
        "reviewer_agent_id": reviewer_id,
        "reviewer_synthesis_epoch": 0,
        "status": "returned",
        "read_frame_ids": [],
        "opened_visual_unit_ids": [],
        "unverified_scope": [],
        "findings": [],
        "recheck_finding_ids": [],
        "recheck_results": [],
    }
    followup_result_path = payload_dir / "audit-followup-result.json"
    followup_result_path.write_text(json.dumps(followup_result), encoding="utf-8")
    followup_words = [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
        "record", run_id, "--kind", "audit_result", "--payload", str(followup_result_path),
        "--client-request-id", "cr_" + "e" * 32,
    ]
    followup_allowed = json.loads(observer.pre_tool({
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-review-followup",
        "agent_id": reviewer_id,
        "tool_input": {"command": quote(followup_words)},
    }))
    assert followup_allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    followup_capability = observer._capability_for_tool("tool-review-followup")
    assert followup_capability is not None
    assert followup_capability["agent_execution_id"] == followup_execution


def test_copied_reviewer_challenge_from_another_agent_fails_closed(tmp_path: Path) -> None:
    runtime, prepared, _, _ = prepared_run(tmp_path)
    run_id = prepared["run_id"]
    reservation = reserve_content_audit(
        run_id=run_id,
        role=ContentRole.CLAIM_EXPERIMENT,
        audit_seq=1,
        stage=AuditStage.SOURCE_FIRST,
        attempt_no=1,
        agent_execution_id="ae_" + "9" * 64,
        input_digest="a" * 64,
    ) | {
        "entity_id": "claim-source-first-attempt-1",
        "bundle_id": prepared["bundle_id"],
        "run_id": run_id,
        "expected_reviewer_agent_id": None,
    }
    payload_dir = tmp_path / "payloads"
    start_path = payload_dir / "claim-start.json"
    start_path.write_text(json.dumps(reservation), encoding="utf-8")
    execute_authorized(
        runtime,
        [
            "record", run_id, "--kind", "audit_start", "--payload", str(start_path),
            "--client-request-id", "cr_" + "b" * 32,
        ],
        "bootstrap-claim-start",
    )
    start_record = next(
        json.loads(path.read_text())
        for path in runtime.state.layout.run_records(prepared["paper_id"], run_id).glob("rec_*.json")
        if json.loads(path.read_text())["record_kind"] == "audit_start"
    )
    observer = DesktopObserver(tmp_path)
    for reviewer_id in ("intended-reviewer", "copying-reviewer"):
        observer.agent_event({
            "hook_event_name": "SubagentStart",
            "task_id": "task",
            "session_id": "session",
            "turn_id": "turn-2",
            "cwd": str(tmp_path),
            "agent_id": reviewer_id,
            "agent_type": "claim_experiment",
        })
    copied = start_record["payload"] | {
        "entity_id": "claim-result-copy",
        "reviewer_agent_id": "copying-reviewer",
        "reviewer_synthesis_epoch": 0,
        "status": "returned",
        "read_frame_ids": [],
        "opened_visual_unit_ids": [],
        "unverified_scope": [],
        "findings": [],
        "recheck_finding_ids": [],
        "recheck_results": [],
    }
    copied_path = payload_dir / "claim-result-copy.json"
    copied_path.write_text(json.dumps(copied), encoding="utf-8")
    words = [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
        "record", run_id, "--kind", "audit_result", "--payload", str(copied_path),
        "--client-request-id", "cr_" + "c" * 32,
    ]
    denied = json.loads(observer.pre_tool({
        "hook_event_name": "PreToolUse",
        "session_id": "session",
        "turn_id": "turn-2",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-copy",
        "agent_id": "copying-reviewer",
        "tool_input": {"command": quote(words)},
    }))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exactly one matching semantic agent start" in denied["hookSpecificOutput"]["permissionDecisionReason"]


def test_first_turn_events_bind_when_prepare_reveals_task_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("READPAPER_TASK_ID", raising=False)
    observer = DesktopObserver(tmp_path)
    observer.session_start({
        "hook_event_name": "SessionStart",
        "session_id": "session-first",
        "source": "startup",
        "cwd": str(tmp_path),
    })
    observer.user_prompt({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-first",
        "turn_id": "turn-first",
        "prompt": "read this paper",
        "cwd": str(tmp_path),
    })
    words = [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
        "prepare",
        str(tmp_path / "paper.pdf"),
        "--task-id",
        "task-first",
        "--user-turn-id",
        "turn-first",
        "--client-request-id",
        "cr_" + "a" * 32,
    ]
    allowed = observer.pre_tool({
        "hook_event_name": "PreToolUse",
        "session_id": "session-first",
        "turn_id": "turn-first",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_use_id": "tool-first",
        "tool_input": {"command": quote(words)},
    })

    assert json.loads(allowed)["hookSpecificOutput"]["permissionDecision"] == "allow"
    turn = observer.state.find_user_turn(task_id="task-first", turn_or_event_id="turn-first")
    assert turn.payload["prompt_sha256"] == digest_text("read this paper")
    kinds = [
        json.loads(line)["event_kind"]
        for line in observer.state.layout.host_ledger("task-first").read_text().splitlines()
    ]
    assert kinds == ["session_started", "user_turn_started", "pretool_authorized"]
    pending = [json.loads(path.read_text()) for path in observer._unbound_host_dir.glob("*.json")]
    assert len(pending) == 2
    assert all(item["bound_task_sha256"] == digest_text("task-first") for item in pending)


def test_unbound_session_start_never_attaches_to_an_unrelated_active_task(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("READPAPER_TASK_ID", raising=False)
    runtime = CommandRuntime(tmp_path)
    run = runtime.state.create_run(
        task_id="task-existing",
        paper_id="p_" + "1" * 64,
        bundle_id="b_" + "2" * 64,
    )
    runtime.state.bind_session(
        task_id="task-existing", session_id="session-existing", hard_boundary=True
    )

    observer = DesktopObserver(tmp_path)
    observer.session_start({
        "hook_event_name": "SessionStart",
        "session_id": "session-new-task",
        "source": "startup",
        "cwd": str(tmp_path),
    })

    assert runtime.state.get_run(run.paper_id, run.run_id).state is RunState.PREPARED
    assert runtime.state.get_binding("task-existing").session_id == "session-existing"
    pending = [json.loads(path.read_text()) for path in observer._unbound_host_dir.glob("*.json")]
    assert len(pending) == 1
    assert pending[0]["event_name"] == "SessionStart"
    assert pending[0]["session_id"] == "session-new-task"
    assert pending[0]["bound_task_sha256"] is None


def test_stop_transaction_replays_exact_bytes_and_pretool_claims_once(tmp_path: Path) -> None:
    _, prepared, unit_id, visual_id = prepared_run(tmp_path)
    stop = StopCoordinator(tmp_path)
    payload = {"hook_event_name": "Stop", "session_id": "session", "turn_id": "turn-2", "cwd": str(tmp_path), "stop_hook_active": False, "last_assistant_message": "draft"}
    first = stop.handle_stop(payload)
    second = stop.handle_stop(payload)
    assert first == second
    value = json.loads(first)
    assert value["decision"] == "block"
    command = value["reason"].splitlines()[-1]
    assert shlex.split(command)[:2] == [
        str((tmp_path / ".venv/bin/python").absolute()),
        str((tmp_path / ".agents/skills/readpaper/scripts/paper.py").absolute()),
    ]
    assert unit_id in command or visual_id in command
    observer = DesktopObserver(tmp_path)
    pre = {"hook_event_name": "PreToolUse", "session_id": "session", "turn_id": "turn-2", "cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "tool-cont", "tool_input": {"command": command}}
    assert json.loads(observer.pre_tool(pre))["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert json.loads(observer.pre_tool(pre))["hookSpecificOutput"]["permissionDecision"] == "allow"
    duplicate = dict(pre)
    duplicate["tool_use_id"] = "tool-cont-duplicate"
    assert json.loads(observer.pre_tool(duplicate))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_never_synthesizes_read_before_answer_and_scope(tmp_path: Path) -> None:
    runtime = CommandRuntime(tmp_path)
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    runtime.state.append_host_event(
        task_id="task",
        event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key="turn-before-answer",
        subject_id="turn-before-answer",
        payload={"prompt_sha256": digest_text("read this paper"), "byte_length": 15},
    )
    source = tmp_path / "fixture.pdf"
    make_pdf(source)
    prepared = execute_authorized(
        runtime,
        [
            "prepare",
            str(source),
            "--task-id",
            "task",
            "--user-turn-id",
            "turn-before-answer",
            "--client-request-id",
            "cr_" + "7" * 32,
        ],
        "bootstrap-prepare-only",
    )

    output = StopCoordinator(tmp_path).handle_stop({
        "hook_event_name": "Stop",
        "session_id": "session",
        "turn_id": "turn-before-answer",
        "cwd": str(tmp_path),
        "stop_hook_active": False,
        "last_assistant_message": "The live run must restart from a fresh user turn.",
    })

    assert json.loads(output) == {}
    binding = runtime.state.get_binding("task")
    assert binding.run_auto_resume_count == 0
    transaction = next(
        json.loads(path.read_text())
        for path in runtime.state.layout.runtime.joinpath("stop-transactions").glob("stx_*.json")
    )
    assert transaction["attempt_status"] == "not_started"
    assert transaction["target"] == "external"
    assert transaction["expected_command_sha256"] is None
    assert runtime.state.get_run(prepared["paper_id"], prepared["run_id"]).state is RunState.PREPARED


def test_user_prompt_cancels_reserved_continuation_and_nested_stop_never_blocks(tmp_path: Path) -> None:
    _, _, _, _ = prepared_run(tmp_path)
    stop = StopCoordinator(tmp_path)
    payload = {"hook_event_name": "Stop", "session_id": "session", "turn_id": "turn-3", "cwd": str(tmp_path), "stop_hook_active": False, "last_assistant_message": "draft"}
    assert json.loads(stop.handle_stop(payload))["decision"] == "block"
    observer = DesktopObserver(tmp_path)
    observer.user_prompt({"hook_event_name": "UserPromptSubmit", "session_id": "session", "turn_id": "turn-user", "cwd": str(tmp_path), "prompt": "new user question", "task_id": "task"})
    nested = dict(payload)
    nested["stop_hook_active"] = True
    assert json.loads(stop.handle_stop(nested)) == {}


def test_hard_session_boundary_pauses_active_run(tmp_path: Path) -> None:
    state = CommandRuntime(tmp_path).state
    run = state.create_run(task_id="task", paper_id="p_" + "1" * 64, bundle_id="b_" + "2" * 64)
    state.bind_session(task_id="task", session_id="old-session", hard_boundary=True)
    DesktopObserver(tmp_path).session_start({
        "hook_event_name": "SessionStart", "session_id": "new-session", "source": "resume",
        "cwd": str(tmp_path), "task_id": "task",
    })
    assert state.get_run(run.paper_id, run.run_id).state is RunState.PAUSED
    assert state.get_binding("task").active_run_id is None


def test_stop_observes_exact_deletion_preview_without_active_run(tmp_path: Path) -> None:
    runtime = CommandRuntime(tmp_path)
    run = runtime.state.create_run(task_id="task", paper_id="p_" + "3" * 64, bundle_id="b_" + "4" * 64)
    runtime.state.transition(
        task_id="task", paper_id=run.paper_id, run_id=run.run_id,
        to_state=RunState.PAUSED, actor=Actor.ROOT_MAIN, reason_code="user_pause",
    )
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    preview = runtime.deletion.create_preview(
        task_id="task", paper_id=run.paper_id, client_request_id="cr_" + "9" * 32,
    )
    output = StopCoordinator(tmp_path).handle_stop({
        "hook_event_name": "Stop", "session_id": "session", "turn_id": "turn-delete",
        "cwd": str(tmp_path), "stop_hook_active": False,
        "last_assistant_message": preview["preview_text"], "task_id": "task",
    })
    assert json.loads(output) == {}
    request = json.loads(runtime.state.layout.deletion_request(preview["deletion_request_id"]).read_text())
    assert request["state"] == "presented"
