from __future__ import annotations

import json
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from readpaper.authority import bound_request_document
from readpaper.canonical import digest, digest_text
from readpaper.commands import CommandRuntime
from readpaper.documents import estimate_tokens
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.models import Actor, EventKind, EventResult, HostEventKind, RunState
from readpaper.observer import DesktopObserver
from readpaper.ids import sequence_id
from readpaper.parse_invocation import Invocation, parse_argv


def pdf(path: Path) -> None:
    document = canvas.Canvas(str(path))
    document.drawString(72, 720, "ReadPaper integration fixture")
    document.showPage()
    document.save()


def authorized(
    runtime: CommandRuntime,
    argv: list[str],
    *,
    tool: int,
    turn_id: str = "turn-1",
) -> dict:
    invocation = parse_argv(argv)
    assert invocation is not None
    client = str(invocation.flags["--client-request-id"])
    runtime.authority.issue(
        pretool_semantic_key=digest(["PreToolUse/v1", tool]),
        client_request_id=client,
        request_digest=digest(bound_request_document(invocation)),
        argv_sha256=digest_text("argv"),
        hook_definition_hash="h" * 64,
        task_id=str(invocation.flags.get("--task-id", "task")),
        session_id="session",
        turn_id=turn_id,
        tool_use_id=f"tool-{tool}",
        agent_id="root",
        agent_execution_id="ae_" + f"{tool:064x}",
        context_stream_id="cs_" + "1" * 64,
        context_epoch=0,
    )
    return json.loads(runtime.execute(invocation))


def test_prepare_scope_read_without_answer_render_and_check_contract(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = CommandRuntime(tmp_path)
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    runtime.state.append_host_event(
        task_id="task", event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key="user-turn-1", subject_id="turn-1",
        payload={"prompt_sha256": digest_text("read this paper"), "byte_length": 15},
    )
    source = tmp_path / "paper.pdf"
    pdf(source)
    prepare_client = "cr_" + "1" * 32
    prepared = authorized(
        runtime,
        [
            "prepare", str(source), "--task-id", "task", "--user-turn-id", "turn-1",
            "--client-request-id", prepare_client,
        ],
        tool=1,
    )
    assert prepared["ok"] is True
    run_id = prepared["run_id"]
    ref = prepared["data"]["artifacts"][0]["artifact_ref_id"]
    frame_id = prepared["data"]["transport_frames"][0]["frame_id"]
    visual_id = prepared["data"]["visual_units"][0]["unit_id"]
    assert prepared["data"]["sections"][0]["frame_ids"] == [frame_id]
    assert "source_ranges" not in prepared["data"]["sections"][0]
    assert "content_sha256" not in prepared["data"]["transport_frames"][0]
    assert prepared["data"]["residency_plan"]["estimated_to_fit"] is True

    replayed = authorized(
        runtime,
        [
            "prepare", str(source), "--task-id", "task", "--user-turn-id", "turn-1",
            "--client-request-id", prepare_client,
        ],
        tool=2,
    )
    assert replayed == prepared

    payload_dir = tmp_path / "papers" / prepared["paper_id"] / "runs" / run_id / "pending-inputs"
    payload_dir.mkdir(mode=0o700)
    payload = payload_dir / "scope.json"
    payload.write_text(
        json.dumps(
            {
                "scope_kind": "full",
                "required_artifact_ref_ids": [ref],
                "excluded_artifacts": [],
                "user_turn_id": "turn-1",
            }
        )
    )
    scoped = authorized(
        runtime,
        [
            "record", run_id, "--kind", "scope_confirmation", "--payload", str(payload),
            "--client-request-id", "cr_" + "3" * 32,
        ],
        tool=3,
    )
    assert scoped["data"]["primary_event_id"].startswith("ev_")

    read = authorized(
        runtime,
        ["read", run_id, "--frame-id", frame_id, "--client-request-id", "cr_" + "4" * 32],
        tool=4,
    )
    assert "ReadPaper integration fixture" in read["data"]["content"]
    assert read["data"]["content"].startswith("<readpaper-section ")
    assert "[PDF PAGE 1]" in read["data"]["content"]
    assert read["data"]["frame"]["frame_id"] == frame_id
    assert "source_ranges" not in read["data"]["frame"]
    assert read["data"]["tool_use_id"] == "tool-4"
    assert estimate_tokens(json.dumps(read, ensure_ascii=False)) <= runtime.context_policy.tool_output_token_limit

    monkeypatch.setattr(
        "readpaper.commands.estimate_tokens",
        lambda _: runtime.context_policy.tool_output_token_limit + 1,
    )
    oversized = authorized(
        runtime,
        ["read", run_id, "--frame-id", frame_id, "--client-request-id", "cr_" + "8" * 32],
        tool=8,
    )
    assert oversized["ok"] is False
    assert oversized["error"]["code"] == ErrorCode.OUTPUT_BUDGET_EXCEEDED.value
    monkeypatch.undo()

    begun_too_early = authorized(
        runtime,
        [
            "answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", "turn-1",
            "--client-request-id", "cr_" + "2" * 32,
        ],
        tool=5,
    )
    assert begun_too_early["ok"] is False
    assert begun_too_early["error"]["code"] == ErrorCode.STATE_CONFLICT.value

    rendered = authorized(
        runtime,
        ["render", run_id, "--unit-id", visual_id, "--client-request-id", "cr_" + "5" * 32],
        tool=6,
    )
    assert Path(rendered["data"]["path"]).is_file()
    assert rendered["data"]["render_id"].startswith("ren_")

    checked = json.loads(runtime.execute(parse_argv(["check", run_id])))
    assert checked["ok"] is True
    assert checked["data"]["decision"] == "block"
    assert frame_id in checked["data"]["missing_resident_frame_ids"]
    assert checked["data"]["main_context_stream_id"] == sequence_id(
        "ctx", "task", "session", "root"
    )
    assert checked["data"]["main_context_epoch"] == 0


@pytest.mark.parametrize("ingest_only", [True, False])
def test_run_only_check_can_report_reading_ready_without_answer(tmp_path: Path, monkeypatch, ingest_only: bool) -> None:
    runtime = CommandRuntime(tmp_path)
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    runtime.state.append_host_event(
        task_id="task",
        event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key="user-turn-ready",
        subject_id="turn-ready",
        payload={"prompt_sha256": digest_text("read this paper"), "byte_length": 15},
    )
    source = tmp_path / "ready.pdf"
    pdf(source)
    prepared = authorized(
        runtime,
        [
            "prepare", str(source), "--task-id", "task", "--user-turn-id", "turn-ready",
            "--client-request-id", "cr_" + "6" * 32, *(["--ingest-only"] if ingest_only else []),
        ],
        tool=10,
    )
    run_id = prepared["run_id"]
    ref = prepared["data"]["artifacts"][0]["artifact_ref_id"]
    payload_dir = tmp_path / "ready-payloads"
    payload_dir.mkdir(mode=0o700)
    scope = payload_dir / "scope.json"
    scope.write_text(json.dumps({
        "scope_kind": "full",
        "required_artifact_ref_ids": [ref],
        "excluded_artifacts": [],
        "user_turn_id": "turn-ready",
    }))
    authorized(
        runtime,
        [
            "record", run_id, "--kind", "scope_confirmation", "--payload", str(scope),
            "--client-request-id", "cr_" + "7" * 32,
        ],
        tool=11,
    )
    stream = sequence_id("ctx", "task", "session", "root")
    common = {
        "paper_id": prepared["paper_id"],
        "run_id": run_id,
        "result": EventResult.SUCCEEDED,
        "actor": Actor.ROOT_MAIN,
        "session_id": "session",
        "turn_id": "turn-ready",
        "agent_execution_id": "ae_" + "9" * 64,
        "context_stream_id": stream,
        "context_epoch": 0,
    }
    inventory = json.loads(Path(prepared["data"]["inventory_path"]).read_text())
    for frame in inventory["frames"]:
        runtime.state.append_event(
            **common,
            event_kind=EventKind.SOURCE_FRAME_EMITTED,
            subject_id=frame["frame_id"],
            payload={"complete": True, "content_sha256": frame["content_sha256"]},
            idempotency_key=f"ready-frame:{frame['frame_id']}",
        )
    for visual in prepared["data"]["visual_units"]:
        runtime.state.append_event(
            **common,
            event_kind=EventKind.VISUAL_OPEN_OBSERVED,
            subject_id=visual["unit_id"],
            payload={"complete": True},
            idempotency_key=f"ready-visual:{visual['unit_id']}",
        )
    runtime.state.put_versioned_record(
        paper_id=prepared["paper_id"],
        run_id=run_id,
        record_kind="understanding_note",
        entity_id=run_id,
        version_id="note-v1",
        payload={"content_sha256": "a" * 64},
    )
    runtime.state.transition(
        task_id="task",
        paper_id=prepared["paper_id"],
        run_id=run_id,
        to_state=RunState.REVIEWING,
        actor=Actor.ROOT_MAIN,
        reason_code="understanding_note_recorded",
    )
    monkeypatch.setattr("readpaper.commands.content_audit_stage_returned", lambda *args, **kwargs: True)
    checked = json.loads(runtime.execute(parse_argv(["check", run_id])))
    assert checked["data"]["answer_id"] is None
    assert checked["data"]["decision"] == "reading_ready"
    assert checked["data"]["all_source_frames_emitted_in_current_epoch"] is True

    finalized = authorized(
        runtime,
        [
            "run", run_id, "--finalize-reading", "--task-id", "task",
            "--user-turn-id", "turn-ready", "--client-request-id", "cr_" + "9" * 32,
        ],
        tool=12,
        turn_id="turn-ready",
    )
    assert finalized["data"]["run_state"] == "read_complete"
    assert finalized["data"]["completion_mode"] == ("ingest_only" if ingest_only else "answer_required")
    assert finalized["data"]["active_run_released"] is True
    binding = runtime.state.get_binding("task")
    assert binding.active_run_id is None
    assert binding.current_run_id == run_id

    stored = runtime.state.get_run(prepared["paper_id"], run_id)
    assert (stored.reading_finalized_context_stream_id, stored.reading_finalized_context_epoch) == (stream, 0)
    observer = DesktopObserver(tmp_path)
    compact = {"session_id": "session", "cwd": str(tmp_path), "trigger": "auto", "task_id": "task"}
    observer.compact({"hook_event_name": "PreCompact", **compact})
    during = json.loads(runtime.execute(parse_argv(["check", run_id])))["data"]
    assert "main_context_compaction_in_progress" in during["blocking_ids"]
    observer.compact({"hook_event_name": "PostCompact", **compact})
    after = json.loads(runtime.execute(parse_argv(["check", run_id])))["data"]
    assert after["reading_context_refresh_required"] is (not ingest_only)
    assert after["missing_resident_frame_ids"]
    assert after["decision"] == ("reading_complete" if ingest_only else "block")
    if not ingest_only:
        denied = authorized(runtime, [
            "answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", "turn-ready",
            "--client-request-id", "cr_" + "b" * 32,
        ], tool=14, turn_id="turn-ready")
        assert denied["error"]["code"] == ErrorCode.STATE_CONFLICT.value
        for event in [json.loads(line) for line in runtime.state.layout.run_events(prepared["paper_id"], run_id).read_text().splitlines()]:
            if event["event_kind"] in {"source_frame_emitted", "visual_open_observed"}:
                runtime.state.append_event(
                    **(common | {"context_epoch": 1}), event_kind=EventKind(event["event_kind"]),
                    subject_id=event["subject_id"], payload=event["payload"],
                    idempotency_key=f"reload:{event['event_id']}",
                )
        assert json.loads(runtime.execute(parse_argv(["check", run_id])))["data"]["decision"] == "reading_ready"
        refreshed = authorized(runtime, [
            "run", run_id, "--finalize-reading", "--task-id", "task", "--user-turn-id", "turn-ready",
            "--client-request-id", "cr_" + "c" * 32,
        ], tool=15, turn_id="turn-ready")
        assert refreshed["ok"] is True
        assert runtime.state.get_run(prepared["paper_id"], run_id).reading_finalized_context_epoch == 1

    runtime.state.append_host_event(
        task_id="task",
        event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key="user-turn-follow-up",
        subject_id="turn-follow-up",
        payload={"prompt_sha256": digest_text("explain it"), "byte_length": 10},
    )
    begun = authorized(
        runtime,
        [
            "answer", run_id, "--begin", "--task-id", "task",
            "--user-turn-id", "turn-follow-up", "--client-request-id", "cr_" + "a" * 32,
        ],
        tool=13,
        turn_id="turn-follow-up",
    )
    assert begun["data"]["answer_status"] == "drafting"
    if not ingest_only:
        answer_id = begun["data"]["answer_id"]
        stored = runtime.state.get_run(prepared["paper_id"], run_id)
        observer.compact({"hook_event_name": "PreCompact", **compact})
        # Compaction changes the host ledger, not run.event_seq: CAS alone is insufficient.
        with pytest.raises(ReadPaperError, match="compaction is in progress"):
            runtime.state.finalize_answer_content(
                task_id="task", paper_id=prepared["paper_id"], run_id=run_id, answer_id=answer_id,
                final_content_sha256="a" * 64, expected_event_seq=stored.event_seq,
                authority_host_event_id="hev_" + "8" * 64,
                committed_by_agent_execution_id="ae_" + "8" * 64, client_request_id="cr_" + "d" * 32,
            )
        observer.compact({"hook_event_name": "PostCompact", **compact})
        after = json.loads(runtime.execute(parse_argv(["check", run_id, "--answer-id", answer_id])))["data"]
        assert "reading_context_refresh_required" in after["blocking_ids"]


def test_prepare_bounds_inline_inventory_without_losing_manifest(tmp_path: Path, monkeypatch) -> None:
    runtime = CommandRuntime(tmp_path)
    runtime.state.bind_session(task_id="task", session_id="session", hard_boundary=True)
    runtime.state.append_host_event(
        task_id="task", event_kind=HostEventKind.USER_TURN_STARTED, semantic_key="prepare-turn",
        subject_id="turn-1", payload={"prompt_sha256": digest_text("read paper"), "byte_length": 10},
    )
    source = tmp_path / "bounded.pdf"
    pdf(source)
    def oversized_inventory(text: str) -> int:
        value = json.loads(text)
        return runtime.context_policy.tool_output_token_limit + 1 if value["data"]["inventory_inline"] else estimate_tokens(text)
    monkeypatch.setattr("readpaper.commands.estimate_tokens", oversized_inventory)
    prepared = authorized(runtime, [
        "prepare", str(source), "--task-id", "task", "--user-turn-id", "turn-1",
        "--client-request-id", "cr_" + "e" * 32,
    ], tool=16)
    assert prepared["ok"] is True
    assert prepared["data"]["inventory_inline"] is False
    assert "transport_frames" not in prepared["data"]
    inventory = json.loads(Path(prepared["data"]["inventory_path"]).read_text())
    assert inventory["frames"][0]["source_ranges"]
    manifest = json.loads(Path(prepared["data"]["bundle_manifest_path"]).read_text())
    assert manifest["artifacts"][0]["support_state"] == "supported"
    assert estimate_tokens(json.dumps(prepared)) < runtime.context_policy.tool_output_token_limit


def test_schema_v1_inventory_requires_explicit_local_state_migration(tmp_path: Path) -> None:
    runtime = CommandRuntime(tmp_path)
    paper = "p_" + "1" * 64
    bundle = "b_" + "2" * 64
    run = runtime.state.create_run(task_id="task", paper_id=paper, bundle_id=bundle)
    inventory = runtime.state.layout.run_dir(paper, run.run_id) / "inventory.json"
    inventory.write_text(json.dumps({
        "schema_version": 1,
        "paper_id": paper,
        "bundle_id": bundle,
        "run_id": run.run_id,
    }))
    with pytest.raises(ReadPaperError) as error:
        runtime.execute(parse_argv(["check", run.run_id]))
    assert error.value.code is ErrorCode.SCHEMA_MIGRATION_REQUIRED
    assert error.value.details["detected_schema_version"] == 1
    assert error.value.details["recovery_command"].startswith("mv .readpaper ")
