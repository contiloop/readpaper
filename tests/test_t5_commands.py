from __future__ import annotations

import json
from pathlib import Path

from reportlab.pdfgen import canvas

from readpaper.authority import bound_request_document
from readpaper.canonical import digest, digest_text
from readpaper.commands import CommandRuntime
from readpaper.models import Actor, EventKind, EventResult, HostEventKind
from readpaper.ids import sequence_id
from readpaper.parse_invocation import Invocation, parse_argv


def pdf(path: Path) -> None:
    document = canvas.Canvas(str(path))
    document.drawString(72, 720, "ReadPaper integration fixture")
    document.showPage()
    document.save()


def authorized(runtime: CommandRuntime, argv: list[str], *, tool: int) -> dict:
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
        turn_id="turn-1",
        tool_use_id=f"tool-{tool}",
        agent_id="root",
        agent_execution_id="ae_" + f"{tool:064x}",
        context_stream_id="cs_" + "1" * 64,
        context_epoch=0,
    )
    return json.loads(runtime.execute(invocation))


def test_prepare_scope_read_without_answer_render_and_check_contract(tmp_path: Path) -> None:
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
    assert read["data"]["tool_use_id"] == "tool-4"

    begun = authorized(
        runtime,
        [
            "answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", "turn-1",
            "--client-request-id", "cr_" + "2" * 32,
        ],
        tool=5,
    )
    assert begun["data"]["answer_status"] == "drafting"

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


def test_run_only_check_can_report_reading_ready_without_answer(tmp_path: Path, monkeypatch) -> None:
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
            "--client-request-id", "cr_" + "6" * 32,
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
    for frame in prepared["data"]["transport_frames"]:
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
    monkeypatch.setattr("readpaper.commands.content_audit_stage_returned", lambda *args, **kwargs: True)
    checked = json.loads(runtime.execute(parse_argv(["check", run_id])))
    assert checked["data"]["answer_id"] is None
    assert checked["data"]["decision"] == "reading_ready"
    assert checked["data"]["full_source_currently_resident"] is True
