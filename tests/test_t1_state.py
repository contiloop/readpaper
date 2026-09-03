from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from readpaper.canonical import digest
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.ids import artifact_id, bundle_id, context_stream_id, paper_id
from readpaper.models import (
    Actor,
    EventKind,
    EventResult,
    HostEventKind,
    RunSnapshot,
    RunState,
    ScopeKind,
    TaskBinding,
)
from readpaper.state import StateService
from readpaper.storage import atomic_write_json, read_json


PAPER = paper_id(b"main pdf")
BUNDLE = bundle_id(schema_version=1, paper_id=PAPER, landing_url=None, artifacts=[])


def create(service: StateService, task: str = "task-local") -> RunSnapshot:
    return service.create_run(task_id=task, paper_id=PAPER, bundle_id=BUNDLE)


def append_in_process(root: str, run_id: str, ordinal: int) -> str:
    service = StateService(Path(root))
    event = service.append_event(
        paper_id=PAPER,
        run_id=run_id,
        event_kind=EventKind.SOURCE_PREPARED,
        subject_id=f"source-{ordinal}",
        result=EventResult.SUCCEEDED,
        actor=Actor.STATE_SERVICE,
        payload={"ordinal": ordinal},
        idempotency_key=f"source:{ordinal}",
    )
    return event.event_id


def test_content_and_bundle_ids_are_immutable_and_order_independent() -> None:
    assert paper_id(b"x") == paper_id(b"x")
    assert paper_id(b"x") != paper_id(b"y")
    assert artifact_id(b"x").startswith("a_")
    artifacts = [
        {"artifact_ref_id": "r_" + "b" * 64, "artifact_id": "a_" + "1" * 64},
        {"artifact_ref_id": "r_" + "a" * 64, "artifact_id": "a_" + "2" * 64},
    ]
    first = bundle_id(schema_version=1, paper_id=PAPER, landing_url=None, artifacts=artifacts)
    second = bundle_id(schema_version=1, paper_id=PAPER, landing_url=None, artifacts=reversed(artifacts))
    assert first == second
    assert context_stream_id("session", "root") != context_stream_id("session", "agent")


def test_closed_models_reject_unknown_fields_and_incoherent_binding() -> None:
    with pytest.raises(ValidationError):
        RunSnapshot(
            paper_id=PAPER,
            bundle_id=BUNDLE,
            run_id="run_" + "1" * 32,
            task_id="task",
            understanding_verified=True,
        )
    with pytest.raises(ValidationError):
        TaskBinding(task_id="task", current_run_id="run_" + "1" * 32)


def test_active_run_cardinality_and_task_path_privacy(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service, "secret desktop task")
    with pytest.raises(ReadPaperError) as error:
        create(service, "secret desktop task")
    assert error.value.code is ErrorCode.ACTIVE_RUN_CONFLICT
    binding_paths = list((tmp_path / ".readpaper/task-bindings").glob("*.json"))
    assert len(binding_paths) == 1
    assert "secret desktop task" not in binding_paths[0].name
    assert service.get_binding("secret desktop task").active_run_id == run.run_id


def test_object_store_is_content_addressed_write_once(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    identity, path = service.put_object(b"immutable")
    assert identity == artifact_id(b"immutable")
    assert path.read_bytes() == b"immutable"
    assert service.put_object(b"immutable") == (identity, path)
    assert path.parts[-3:] == (identity[2:4], identity, "source")


def test_scope_lock_and_run_transition_authority(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    with pytest.raises(ReadPaperError):
        service.transition(
            task_id=run.task_id,
            paper_id=PAPER,
            run_id=run.run_id,
            to_state=RunState.READING,
            actor=Actor.ROOT_MAIN,
            reason_code="first_read",
        )
    scope = service.lock_scope(
        paper_id=PAPER,
        run_id=run.run_id,
        scope_kind=ScopeKind.FULL,
        required_artifact_ref_ids=["r_" + "1" * 64],
        excluded_artifacts=[],
        authority_event_id="hev_" + "1" * 64,
    )
    assert service.lock_scope(
        paper_id=PAPER,
        run_id=run.run_id,
        scope_kind=ScopeKind.FULL,
        required_artifact_ref_ids=["r_" + "1" * 64],
        excluded_artifacts=[],
        authority_event_id="hev_" + "1" * 64,
    ).event_id == scope.event_id
    service.transition(
        task_id=run.task_id,
        paper_id=PAPER,
        run_id=run.run_id,
        to_state=RunState.READING,
        actor=Actor.ROOT_MAIN,
        reason_code="first_read",
    )
    with pytest.raises(ReadPaperError) as error:
        service.transition(
            task_id=run.task_id,
            paper_id=PAPER,
            run_id=run.run_id,
            to_state=RunState.REVIEWING,
            actor=Actor.SUBAGENT,
            reason_code="forbidden",
        )
    assert error.value.code is ErrorCode.OBSERVER_UNAVAILABLE


def test_pause_resume_uses_saved_phase_and_terminal_states_do_not_reopen(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    service.lock_scope(
        paper_id=PAPER,
        run_id=run.run_id,
        scope_kind=ScopeKind.FULL,
        required_artifact_ref_ids=[],
        excluded_artifacts=[],
        authority_event_id="hev_" + "2" * 64,
    )
    service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                       to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="read")
    service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                       to_state=RunState.PAUSED, actor=Actor.USER, reason_code="pause")
    with pytest.raises(ReadPaperError):
        service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                           to_state=RunState.REVIEWING, actor=Actor.ROOT_MAIN, reason_code="wrong_resume")
    service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                       to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="resume")
    service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                       to_state=RunState.BLOCKED, actor=Actor.HOOK, reason_code="fatal")
    with pytest.raises(ReadPaperError):
        service.transition(task_id=run.task_id, paper_id=PAPER, run_id=run.run_id,
                           to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="reopen")


def test_event_idempotency_context_binding_and_conflict(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    args = dict(
        paper_id=PAPER,
        run_id=run.run_id,
        event_kind=EventKind.UNIT_EMITTED,
        subject_id="unit_1",
        result=EventResult.SUCCEEDED,
        actor=Actor.ROOT_MAIN,
        payload={"content_sha256": "a" * 64},
        idempotency_key="tool:1:unit_1",
        session_id="session",
        turn_id="turn",
        agent_execution_id="ae_" + "1" * 64,
        context_stream_id=context_stream_id("session", "root"),
    )
    first = service.append_event(**args)
    assert service.append_event(**args).event_id == first.event_id
    with pytest.raises(ReadPaperError) as error:
        service.append_event(**{**args, "payload": {"content_sha256": "b" * 64}})
    assert error.value.code is ErrorCode.STATE_CONFLICT
    with pytest.raises(ReadPaperError) as error2:
        service.append_event(**{**args, "idempotency_key": "tool:2", "context_stream_id": None})
    assert error2.value.code is ErrorCode.OBSERVER_UNAVAILABLE


def test_host_event_semantic_dedupe(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    first = service.append_host_event(
        task_id="task",
        event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key=digest(["session", "turn", "prompt-hash"]),
        subject_id="turn",
        payload={"prompt_sha256": "1" * 64},
    )
    again = service.append_host_event(
        task_id="task",
        event_kind=HostEventKind.USER_TURN_STARTED,
        semantic_key=digest(["session", "turn", "prompt-hash"]),
        subject_id="turn",
        payload={"prompt_sha256": "1" * 64},
    )
    assert first.host_event_id == again.host_event_id
    with pytest.raises(ReadPaperError):
        service.append_host_event(
            task_id="task",
            event_kind=HostEventKind.USER_TURN_STARTED,
            semantic_key=first.semantic_key,
            subject_id="turn",
            payload={"prompt_sha256": "2" * 64},
        )


def test_version_chain_rejects_stale_parent(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    first = service.put_versioned_record(
        paper_id=PAPER, run_id=run.run_id, record_kind="understanding_note",
        entity_id="note", version_id="nv_" + "1" * 64, payload={"content": "v1"},
    )
    second = service.put_versioned_record(
        paper_id=PAPER, run_id=run.run_id, record_kind="understanding_note",
        entity_id="note", version_id="nv_" + "2" * 64,
        parent_version_id=first.version_id, payload={"content": "v2"},
    )
    assert second.parent_version_id == first.version_id
    with pytest.raises(ReadPaperError) as error:
        service.put_versioned_record(
            paper_id=PAPER, run_id=run.run_id, record_kind="understanding_note",
            entity_id="note", version_id="nv_" + "3" * 64,
            parent_version_id=first.version_id, payload={"content": "stale"},
        )
    assert error.value.code is ErrorCode.STATE_CONFLICT


def test_transaction_intent_recovers_deleted_state(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    intent_path = service.layout.run_transaction(PAPER, run.run_id)
    intent = read_json(intent_path)
    intent["status"] = "prepared"
    atomic_write_json(intent_path, intent)
    service.layout.run_state(PAPER, run.run_id).unlink()
    recovered = service.get_run(PAPER, run.run_id)
    assert recovered.run_id == run.run_id
    assert read_json(intent_path)["status"] == "completed"


def test_run_index_and_human_summary_are_derived_automatically(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    index = read_json(service.layout.run_index(PAPER, run.run_id))
    summary = service.layout.run_summary(PAPER, run.run_id).read_text(encoding="utf-8")

    assert index["run_id"] == run.run_id
    assert index["event_counts"] == {"run_created": 1}
    assert index["record_count"] == 0
    assert index["paths"]["records"].endswith("/records")
    assert "derived navigation view" in summary


def test_concurrent_process_appends_have_unique_monotonic_sequences(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = create(service)
    with ProcessPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(append_in_process, [str(tmp_path)] * 8, [run.run_id] * 8, range(8)))
    assert len(set(ids)) == 8
    events = [json.loads(line) for line in service.layout.run_events(PAPER, run.run_id).read_text().splitlines()]
    sequences = [item["event_seq"] for item in events]
    assert sequences == list(range(1, 10))
