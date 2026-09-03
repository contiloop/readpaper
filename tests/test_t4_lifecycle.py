from __future__ import annotations

from pathlib import Path

import pytest

from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.ids import bundle_id, paper_id
from readpaper.models import Actor, AnswerStatus, ResponseAttemptStatus, RunState, ScopeKind
from readpaper.state import StateService


PAPER = paper_id(b"lifecycle")
BUNDLE = bundle_id(schema_version=1, paper_id=PAPER, landing_url=None, artifacts=[])


def begin(service: StateService, task: str = "task") -> tuple[str, str, str]:
    run = service.create_run(task_id=task, paper_id=PAPER, bundle_id=BUNDLE)
    make_reviewable(service, run.run_id, task=task)
    answer = service.begin_answer(
        task_id=task,
        paper_id=PAPER,
        run_id=run.run_id,
        question_event_id="hev_" + "1" * 64,
        question_turn_id="turn-question",
        question_hash="a" * 64,
        authority_turn_event_id="hev_" + "1" * 64,
        root_main_agent_execution_id="ae_" + "1" * 64,
        client_request_id="cr_" + "1" * 32,
    )
    return run.run_id, answer["answer_id"], answer["current_response_attempt_id"]


def make_reviewable(service: StateService, run_id: str, *, task: str = "task") -> None:
    service.lock_scope(
        paper_id=PAPER,
        run_id=run_id,
        scope_kind=ScopeKind.FULL,
        required_artifact_ref_ids=[],
        excluded_artifacts=[],
        authority_event_id="hev_" + "9" * 64,
    )
    service.transition(
        task_id=task, paper_id=PAPER, run_id=run_id,
        to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="scope_locked",
    )
    service.transition(
        task_id=task, paper_id=PAPER, run_id=run_id,
        to_state=RunState.REVIEWING, actor=Actor.ROOT_MAIN, reason_code="note_recorded",
    )
    current = service.get_run(PAPER, run_id)
    service.finalize_reading(
        task_id=task,
        paper_id=PAPER,
        run_id=run_id,
        expected_event_seq=current.event_seq,
        authority_host_event_id="hev_" + "8" * 64,
        committed_by_agent_execution_id="ae_" + "8" * 64,
        client_request_id="cr_" + "8" * 32,
    )


def test_answer_begin_binds_pending_immediately(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run_id, answer_id, response_id = begin(service)
    binding = service.get_binding("task")
    assert binding.pending_answer_id == answer_id
    assert binding.pending_answer_status == AnswerStatus.DRAFTING.value
    assert binding.current_response_attempt_id == response_id
    with pytest.raises(ReadPaperError) as error:
        service.begin_answer(
            task_id="task",
            paper_id=PAPER,
            run_id=run_id,
            question_event_id="hev_" + "2" * 64,
            question_turn_id="turn-2",
            question_hash="b" * 64,
            authority_turn_event_id="hev_" + "2" * 64,
            root_main_agent_execution_id="ae_" + "2" * 64,
            client_request_id="cr_" + "2" * 32,
        )
    assert error.value.code is ErrorCode.ANSWER_PENDING


def test_interruption_and_resume_preserve_question_and_replace_attempt(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run_id, answer_id, first_response = begin(service)
    service.interrupt_answer(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        answer_id=answer_id,
        reason_code="session_resume",
        authority_host_event_id="hev_" + "3" * 64,
    )
    resumed = service.resume_answer(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        answer_id=answer_id,
        authority_turn_event_id="hev_" + "4" * 64,
        root_main_agent_execution_id="ae_" + "4" * 64,
        client_request_id="cr_" + "4" * 32,
    )
    second = resumed["current_response_attempt_id"]
    assert second != first_response
    assert resumed["question_hash"] == "a" * 64
    assert resumed["attempts"][first_response]["status"] == ResponseAttemptStatus.SUPERSEDED.value
    assert resumed["attempts"][second]["status"] == ResponseAttemptStatus.ACTIVE.value
    assert service.get_binding("task").current_response_attempt_id == second


def test_answer_abandon_is_the_only_explicit_pending_clear(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run_id, answer_id, response_id = begin(service)
    service.interrupt_answer(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        answer_id=answer_id,
        reason_code="user_intervention",
        authority_host_event_id="hev_" + "5" * 64,
    )
    service.abandon_answer(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        answer_id=answer_id,
        authority_turn_event_id="hev_" + "6" * 64,
        root_main_agent_execution_id="ae_" + "6" * 64,
        client_request_id="cr_" + "6" * 32,
    )
    binding = service.get_binding("task")
    assert binding.pending_answer_id is None
    stored = service.get_run(PAPER, run_id).answers[answer_id]
    assert stored["answer_status"] == AnswerStatus.ABANDONED.value
    assert stored["attempts"][response_id]["status"] == ResponseAttemptStatus.ABANDONED.value


def test_new_run_is_blocked_while_answer_pending(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    begin(service)
    with pytest.raises(ReadPaperError) as error:
        service.create_run(task_id="task", paper_id=paper_id(b"other"), bundle_id=BUNDLE)
    assert error.value.code is ErrorCode.ANSWER_PENDING


def test_content_completion_is_independent_from_delivery_and_does_not_block_next_answer(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run_id, answer_id, response_id = begin(service)
    current = service.get_run(PAPER, run_id)
    service.finalize_answer_content(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        answer_id=answer_id,
        final_content_sha256="f" * 64,
        expected_event_seq=current.event_seq,
        authority_host_event_id="hev_" + "a" * 64,
        committed_by_agent_execution_id="ae_" + "a" * 64,
        client_request_id="cr_" + "a" * 32,
    )

    binding = service.get_binding("task")
    assert binding.pending_answer_id is None
    assert binding.delivery_candidate_answer_id == answer_id
    assert binding.delivery_candidate_status == "pending_observation"
    assert service.get_run(PAPER, run_id).state is RunState.READ_COMPLETE
    stored = service.get_run(PAPER, run_id).answers[answer_id]
    assert stored["answer_status"] == AnswerStatus.CONTENT_FINALIZED.value
    assert stored["attempts"][response_id]["status"] == ResponseAttemptStatus.CONTENT_FINALIZED.value

    next_answer = service.begin_answer(
        task_id="task",
        paper_id=PAPER,
        run_id=run_id,
        question_event_id="hev_" + "b" * 64,
        question_turn_id="turn-next",
        question_hash="b" * 64,
        authority_turn_event_id="hev_" + "b" * 64,
        root_main_agent_execution_id="ae_" + "b" * 64,
        client_request_id="cr_" + "b" * 32,
    )
    assert service.get_binding("task").pending_answer_id == next_answer["answer_id"]
    previous = service.get_run(PAPER, run_id).answers[answer_id]
    assert previous["answer_status"] == AnswerStatus.DELIVERY_UNKNOWN.value
    assert previous["attempts"][response_id]["status"] == ResponseAttemptStatus.DELIVERY_UNKNOWN.value


def test_read_complete_releases_active_run_and_keeps_current_run(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run = service.create_run(task_id="task", paper_id=PAPER, bundle_id=BUNDLE)
    service.lock_scope(
        paper_id=PAPER,
        run_id=run.run_id,
        scope_kind=ScopeKind.FULL,
        required_artifact_ref_ids=[],
        excluded_artifacts=[],
        authority_event_id="hev_" + "7" * 64,
    )
    service.transition(
        task_id="task", paper_id=PAPER, run_id=run.run_id,
        to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="scope_locked",
    )
    service.transition(
        task_id="task", paper_id=PAPER, run_id=run.run_id,
        to_state=RunState.REVIEWING, actor=Actor.ROOT_MAIN, reason_code="note_recorded",
    )
    current = service.get_run(PAPER, run.run_id)
    service.finalize_reading(
        task_id="task", paper_id=PAPER, run_id=run.run_id,
        expected_event_seq=current.event_seq,
        authority_host_event_id="hev_" + "8" * 64,
        committed_by_agent_execution_id="ae_" + "8" * 64,
        client_request_id="cr_" + "8" * 32,
    )
    binding = service.get_binding("task")
    assert service.get_run(PAPER, run.run_id).state is RunState.READ_COMPLETE
    assert binding.active_run_id is None
    assert binding.current_run_id == run.run_id
    next_run = service.create_run(
        task_id="task",
        paper_id=paper_id(b"another paper"),
        bundle_id=BUNDLE,
    )
    assert service.get_binding("task").active_run_id == next_run.run_id


def test_stop_delivery_upgrades_only_delivery_metadata(tmp_path: Path) -> None:
    service = StateService(tmp_path)
    run_id, answer_id, response_id = begin(service)
    current = service.get_run(PAPER, run_id)
    service.finalize_answer_content(
        task_id="task", paper_id=PAPER, run_id=run_id, answer_id=answer_id,
        final_content_sha256="c" * 64, expected_event_seq=current.event_seq,
        authority_host_event_id="hev_" + "c" * 64,
        committed_by_agent_execution_id="ae_" + "c" * 64,
        client_request_id="cr_" + "c" * 32,
    )
    service.commit_stop_delivery(
        task_id="task", paper_id=PAPER, run_id=run_id,
        assistant_message_hash="c" * 64,
        authority_host_event_id="hev_" + "d" * 64,
    )

    binding = service.get_binding("task")
    assert binding.pending_answer_id is None
    assert binding.delivery_candidate_answer_id is None
    stored = service.get_run(PAPER, run_id).answers[answer_id]
    assert stored["answer_status"] == AnswerStatus.SENT_VERIFIED.value
    assert stored["attempts"][response_id]["status"] == ResponseAttemptStatus.DELIVERED.value
