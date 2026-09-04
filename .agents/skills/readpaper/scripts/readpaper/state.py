"""T1 state service: immutable IDs, append-only events, CAS transitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest
from .errors import ErrorCode, ReadPaperError
from .ids import artifact_id as make_artifact_id, new_id, sequence_id, validate_id
from .models import (
    Actor,
    AnswerStatus,
    EventKind,
    EventResult,
    HostEvent,
    HostEventKind,
    HostLedgerState,
    RunEvent,
    RunCompletionMode,
    RunSnapshot,
    RunState,
    ResponseAttemptStatus,
    ScopeKind,
    TaskBinding,
    VersionedRecord,
    utc_now,
)
from .storage import FileLock, Layout, append_jsonl_once, atomic_write, atomic_write_json, read_json


ACTIVE_STATES = {RunState.PREPARED, RunState.READING, RunState.REVIEWING, RunState.NEEDS_WORK}
TERMINAL_STATES = {RunState.READ_COMPLETE, RunState.BLOCKED, RunState.COMPLETE}
ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.PREPARED: {RunState.READING, RunState.NEEDS_WORK, RunState.PAUSED, RunState.BLOCKED},
    RunState.READING: {RunState.REVIEWING, RunState.NEEDS_WORK, RunState.PAUSED, RunState.BLOCKED},
    RunState.REVIEWING: {RunState.NEEDS_WORK, RunState.PAUSED, RunState.BLOCKED},
    RunState.NEEDS_WORK: {RunState.PREPARED, RunState.READING, RunState.REVIEWING, RunState.PAUSED, RunState.BLOCKED},
    RunState.PAUSED: {RunState.PREPARED, RunState.READING, RunState.REVIEWING},
    RunState.READ_COMPLETE: set(),
    RunState.BLOCKED: set(),
    RunState.COMPLETE: set(),
}
AGENT_CONTEXT_EVENTS = {
    EventKind.SOURCE_FRAME_EMITTED,
    EventKind.RENDER_CREATED,
    EventKind.VISUAL_OPEN_OBSERVED,
    EventKind.NOTE_VERSIONED,
    EventKind.DRAFT_VERSIONED,
    EventKind.ANSWER_GROUNDED,
}


class StateService:
    def __init__(self, root: Path):
        self.layout = Layout(root)
        self.layout.initialize()

    def _read_binding(self, task_id: str) -> TaskBinding:
        path = self.layout.task_binding(task_id)
        if not path.exists():
            return TaskBinding(task_id=task_id)
        return TaskBinding.model_validate(read_json(path))

    def _write_binding(self, binding: TaskBinding) -> None:
        atomic_write_json(self.layout.task_binding(binding.task_id), binding.model_dump(mode="json"))

    def get_binding(self, task_id: str) -> TaskBinding:
        with FileLock(self.layout.task_lock(task_id)):
            return self._read_binding(task_id)

    def bind_session(self, *, task_id: str, session_id: str, hard_boundary: bool) -> TaskBinding:
        """Bind the current Desktop session without trusting a caller-supplied actor."""
        with FileLock(self.layout.task_lock(task_id)):
            binding = self._read_binding(task_id)
            changed = binding.session_id != session_id
            epoch = binding.session_epoch + (1 if hard_boundary and changed else 0)
            after = binding.model_copy(update={"session_id": session_id, "session_epoch": epoch})
            self._write_binding(after)
            return after

    def update_host_state(self, *, task_id: str, transform: Any) -> HostLedgerState:
        """Serialize observer-only state transitions with the task host ledger."""
        with FileLock(self.layout.task_lock(task_id)):
            path = self.layout.host_state(task_id)
            state = HostLedgerState.model_validate(read_json(path)) if path.exists() else HostLedgerState(task_id=task_id)
            after = transform(state)
            if not isinstance(after, HostLedgerState):
                raise ReadPaperError(ErrorCode.INTERNAL_ERROR, "host state transform returned the wrong type")
            atomic_write_json(path, after.model_dump(mode="json"))
            return after

    def put_object(self, data: bytes) -> tuple[str, Path]:
        artifact = make_artifact_id(data)
        path = self.layout.object_source(artifact)
        with FileLock(self.layout.reference_lock):
            if path.exists():
                if path.read_bytes() != data:
                    raise ReadPaperError(ErrorCode.ID_MISMATCH, "immutable object bytes conflict")
                return artifact, path
            from .storage import atomic_write

            atomic_write(path, data, replace=False)
            return artifact, path

    def get_run(self, paper_id: str, run_id: str) -> RunSnapshot:
        validate_id(paper_id, prefix="p", lengths=(64,))
        validate_id(run_id, prefix="run", lengths=(32,))
        with FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            return RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))

    def create_run(
        self,
        *,
        task_id: str,
        paper_id: str,
        bundle_id: str,
        completion_mode: RunCompletionMode = RunCompletionMode.ANSWER_REQUIRED,
    ) -> RunSnapshot:
        validate_id(paper_id, prefix="p", lengths=(64,))
        validate_id(bundle_id, prefix="b", lengths=(64,))
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)):
            binding = self._read_binding(task_id)
            if binding.pending_answer_id is not None:
                raise ReadPaperError(ErrorCode.ANSWER_PENDING, "task has a pending answer")
            if binding.delivery_candidate_answer_id is not None:
                candidate_paper = str(binding.delivery_candidate_paper_id)
                candidate_run = str(binding.delivery_candidate_run_id)
                with FileLock(self.layout.run_lock(candidate_run)):
                    self._recover_run(candidate_paper, candidate_run)
                    previous = RunSnapshot.model_validate(
                        read_json(self.layout.run_state(candidate_paper, candidate_run))
                    )
                    previous, binding, _ = self._mark_delivery_unknown_locked(
                        previous,
                        binding,
                        reason_code="new_run_started_before_stop_observation",
                        authority_host_event_id=None,
                    )
            if binding.active_run_id is not None:
                raise ReadPaperError(ErrorCode.ACTIVE_RUN_CONFLICT, "task already has an active run")
            for _ in range(8):
                run_id = new_id("run")
                path = self.layout.run_state(paper_id, run_id)
                if not path.exists():
                    break
            else:
                raise ReadPaperError(ErrorCode.INTERNAL_ERROR, "could not allocate run ID")
            snapshot = RunSnapshot(
                paper_id=paper_id,
                bundle_id=bundle_id,
                run_id=run_id,
                task_id=task_id,
                completion_mode=completion_mode,
            )
            event, after = self._plan_event(
                snapshot,
                event_kind=EventKind.RUN_CREATED,
                subject_id=run_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.STATE_SERVICE,
                payload={
                    "state": RunState.PREPARED.value,
                    "completion_mode": completion_mode.value,
                },
                idempotency_key=f"create:{run_id}",
            )
            self._commit_run(after, event)
            binding = binding.model_copy(
                update={
                    "active_run_id": run_id,
                    "current_run_id": run_id,
                    "current_paper_id": paper_id,
                    "current_bundle_id": bundle_id,
                    "run_auto_resume_count": 0,
                }
            )
            self._write_binding(binding)
            return after

    def transition(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        to_state: RunState,
        actor: Actor,
        reason_code: str,
        authority_event_id: str | None = None,
    ) -> RunEvent:
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            current = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            if current.task_id != task_id:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "run belongs to another task")
            if actor in {Actor.SUBAGENT, Actor.UNKNOWN}:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "actor cannot transition run state")
            if to_state not in ALLOWED_TRANSITIONS[current.state]:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"transition {current.state}->{to_state} is forbidden")
            if to_state in {RunState.READ_COMPLETE, RunState.COMPLETE}:
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "terminal reading state requires the dedicated finalization transaction",
                )
            if current.state is RunState.PREPARED and to_state is RunState.READING and not current.scope_locked:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "scope must be locked before reading")
            resume_phase = current.resume_phase
            if to_state in {RunState.NEEDS_WORK, RunState.PAUSED} and current.state in {
                RunState.PREPARED, RunState.READING, RunState.REVIEWING
            }:
                resume_phase = current.state
            if current.state in {RunState.NEEDS_WORK, RunState.PAUSED} and to_state in {
                RunState.PREPARED, RunState.READING, RunState.REVIEWING
            }:
                if current.resume_phase is not to_state:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "resume target does not match saved phase")
                resume_phase = None
            changed = current.model_copy(update={"state": to_state, "resume_phase": resume_phase})
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.STATE_TRANSITION,
                subject_id=run_id,
                result=EventResult.SUCCEEDED,
                actor=actor,
                payload={
                    "from": current.state.value,
                    "to": to_state.value,
                    "reason_code": reason_code,
                    "authority_event_id": authority_event_id,
                },
                idempotency_key=f"transition:{current.event_seq + 1}:{current.state}:{to_state}",
            )
            self._commit_run(after, event)
            binding = self._read_binding(task_id)
            if binding.current_run_id != run_id:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "task binding does not point at run")
            if to_state in ACTIVE_STATES:
                if binding.active_run_id not in {None, run_id}:
                    raise ReadPaperError(ErrorCode.ACTIVE_RUN_CONFLICT, "another run is active")
                binding = binding.model_copy(update={"active_run_id": run_id})
            else:
                binding = binding.model_copy(update={"active_run_id": None})
            self._write_binding(binding)
            return event

    def finalize_reading(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        expected_event_seq: int,
        authority_host_event_id: str,
        committed_by_agent_execution_id: str,
        client_request_id: str,
        context_stream_id: str | None = None,
        context_epoch: int | None = None,
    ) -> RunEvent:
        """Commit paper-reading completion independently from any answer."""

        with (
            FileLock(self.layout.reference_lock),
            FileLock(self.layout.task_lock(task_id)),
            FileLock(self.layout.run_lock(run_id)),
        ):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            if run.task_id != task_id or binding.current_run_id != run_id:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "reading run is not current for task")
            if run.event_seq != expected_event_seq:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "run changed after the reading check")
            if run.state not in {RunState.REVIEWING, RunState.READ_COMPLETE, RunState.COMPLETE}:
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "reading can finalize only after review",
                )
            stream, epoch = self._main_context(binding)
            if (context_stream_id is not None and context_stream_id != stream) or (context_epoch is not None and context_epoch != epoch):
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "Main context changed after the reading check")
            changed = run.model_copy(update={
                "state": RunState.READ_COMPLETE,
                "reading_finalized_context_stream_id": stream,
                "reading_finalized_context_epoch": epoch,
            })
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.READING_FINALIZED,
                subject_id=run_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.ROOT_MAIN,
                payload={
                    "run_from": run.state.value,
                    "run_to": changed.state.value,
                    "completion_mode": run.completion_mode.value,
                    "reading_finalized_context_stream_id": stream,
                    "reading_finalized_context_epoch": epoch,
                    "authority_host_event_id": authority_host_event_id,
                    "committed_by_agent_execution_id": committed_by_agent_execution_id,
                },
                idempotency_key=f"reading-finalize:{client_request_id}",
                source_host_event_id=authority_host_event_id,
                client_request_id=client_request_id,
                agent_execution_id=committed_by_agent_execution_id,
                context_stream_id=stream,
                context_epoch=epoch,
            )
            self._commit_run(after, event)
            self._write_binding(binding.model_copy(update={"active_run_id": None}))
            return event

    def _main_context(self, binding: TaskBinding) -> tuple[str, int]:
        """Read under the task lock shared with compact observers."""
        stream = sequence_id("ctx", binding.task_id, binding.session_id, "root")
        host_path = self.layout.host_state(binding.task_id)
        host = read_json(host_path) if host_path.exists() else {}
        compact = host.get("compact_streams", {}).get(stream, {})
        if compact.get("open") is not None:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "Main context compaction is in progress")
        return stream, int(compact.get("context_epoch", 0))

    def _require_initial_answer_context(self, run: RunSnapshot, binding: TaskBinding, answer_id: str | None = None) -> None:
        if run.requires_initial_answer_context(answer_id):
            if (run.reading_finalized_context_stream_id, run.reading_finalized_context_epoch) != self._main_context(binding):
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "initial answer requires full-source reopening and run --finalize-reading in the current Main context epoch",
                )

    def lock_scope(
        self,
        *,
        paper_id: str,
        run_id: str,
        scope_kind: ScopeKind,
        required_artifact_ref_ids: list[str],
        excluded_artifacts: list[dict[str, Any]],
        authority_event_id: str,
        scope_disclosure_markdown: str = "",
        scope_disclosure_sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ) -> RunEvent:
        with FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            current = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            if current.scope_locked:
                existing = current.event_dedupe.get("scope-lock")
                expected = digest(
                    {
                        "authority_event_id": authority_event_id,
                        "excluded_artifacts": sorted(excluded_artifacts, key=lambda item: item["artifact_ref_id"]),
                        "required_artifact_ref_ids": sorted(required_artifact_ref_ids),
                        "scope_kind": scope_kind.value,
                        "scope_disclosure_markdown": scope_disclosure_markdown,
                        "scope_disclosure_sha256": scope_disclosure_sha256,
                    }
                )
                if existing is None or existing["payload_sha256"] != expected:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "scope is immutable after locking")
                return self._find_event(paper_id, run_id, str(existing["event_id"]))
            payload = {
                "authority_event_id": authority_event_id,
                "excluded_artifacts": sorted(excluded_artifacts, key=lambda item: item["artifact_ref_id"]),
                "required_artifact_ref_ids": sorted(required_artifact_ref_ids),
                "scope_kind": scope_kind.value,
                "scope_disclosure_markdown": scope_disclosure_markdown,
                "scope_disclosure_sha256": scope_disclosure_sha256,
            }
            changed = current.model_copy(update={
                "scope_locked": True, "scope_kind": scope_kind,
                "required_artifact_ref_ids": tuple(sorted(required_artifact_ref_ids)),
                "excluded_artifacts": tuple(sorted(excluded_artifacts, key=lambda item: item["artifact_ref_id"])),
                "scope_disclosure_markdown": scope_disclosure_markdown,
                "scope_disclosure_sha256": scope_disclosure_sha256,
            })
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.SCOPE_CONFIRMED,
                subject_id=run_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.ROOT_MAIN,
                payload=payload,
                idempotency_key="scope-lock",
            )
            self._commit_run(after, event)
            return event

    def begin_answer(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        question_event_id: str,
        question_turn_id: str,
        question_hash: str,
        authority_turn_event_id: str,
        root_main_agent_execution_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            if run.task_id != task_id or binding.current_run_id != run_id:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "answer run is not current for task")
            if run.state not in {RunState.READ_COMPLETE, RunState.COMPLETE}:
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "an answer can begin only after reading is complete",
                )
            self._require_initial_answer_context(run, binding)
            if binding.pending_answer_id is not None:
                raise ReadPaperError(ErrorCode.ANSWER_PENDING, "task already has a pending answer")
            if binding.delivery_candidate_answer_id is not None:
                if (
                    binding.delivery_candidate_paper_id != paper_id
                    or binding.delivery_candidate_run_id != run_id
                ):
                    raise ReadPaperError(
                        ErrorCode.STATE_CONFLICT,
                        "delivery candidate points to another run",
                    )
                run, binding, _ = self._mark_delivery_unknown_locked(
                    run,
                    binding,
                    reason_code="new_answer_started_before_stop_observation",
                    authority_host_event_id=authority_turn_event_id,
                )
            answer_id = sequence_id("ans", run_id, question_event_id)
            response_attempt_id = sequence_id(
                "rsp", answer_id, authority_turn_event_id, root_main_agent_execution_id
            )
            if answer_id in run.answers:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "answer identity already exists")
            answer = {
                "answer_id": answer_id,
                "answer_status": AnswerStatus.DRAFTING.value,
                "question_event_id": question_event_id,
                "question_turn_id": question_turn_id,
                "question_hash": question_hash,
                "current_response_attempt_id": response_attempt_id,
                "answer_auto_resume_count": 0,
                "attempts": {
                    response_attempt_id: {
                        "response_attempt_id": response_attempt_id,
                        "status": ResponseAttemptStatus.ACTIVE.value,
                        "authority_turn_event_id": authority_turn_event_id,
                        "root_main_agent_execution_id": root_main_agent_execution_id,
                        "previous_response_attempt_id": None,
                        "resume_kind": "initial",
                    }
                },
            }
            answers = dict(run.answers)
            answers[answer_id] = answer
            changed = run.model_copy(update={"answers": answers})
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.ANSWER_STARTED,
                subject_id=answer_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.ROOT_MAIN,
                payload={
                    "answer_id": answer_id,
                    "question_event_id": question_event_id,
                    "question_turn_id": question_turn_id,
                    "question_hash": question_hash,
                    "response_attempt_id": response_attempt_id,
                    "response_attempt_status": ResponseAttemptStatus.ACTIVE.value,
                    "answer_status": AnswerStatus.DRAFTING.value,
                    "authority_turn_event_id": authority_turn_event_id,
                    "root_main_agent_execution_id": root_main_agent_execution_id,
                    "resume_kind": "initial",
                },
                idempotency_key=f"answer-begin:{client_request_id}",
                client_request_id=client_request_id,
                agent_execution_id=root_main_agent_execution_id,
            )
            self._commit_run(after, event)
            self._write_binding(
                binding.model_copy(
                    update={
                        "pending_answer_id": answer_id,
                        "pending_answer_status": AnswerStatus.DRAFTING.value,
                        "current_response_attempt_id": response_attempt_id,
                    }
                )
            )
            return answer

    def finalize_answer_content(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        answer_id: str,
        final_content_sha256: str,
        expected_event_seq: int,
        authority_host_event_id: str,
        committed_by_agent_execution_id: str,
        client_request_id: str,
    ) -> RunEvent:
        """Commit content completion without claiming that Desktop delivered it."""

        with (
            FileLock(self.layout.reference_lock),
            FileLock(self.layout.task_lock(task_id)),
            FileLock(self.layout.run_lock(run_id)),
        ):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer = dict(run.answers.get(answer_id) or {})
            if not answer or binding.pending_answer_id != answer_id:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "pending answer not found")
            if run.event_seq != expected_event_seq:
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "run changed after the completion check",
                )
            if run.state not in {RunState.READ_COMPLETE, RunState.COMPLETE}:
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "content can finalize only after reading is complete",
                )
            self._require_initial_answer_context(run, binding, answer_id)
            response_id = answer["current_response_attempt_id"]
            if response_id != binding.current_response_attempt_id:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "response attempt binding changed")
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            if attempts[response_id]["status"] != ResponseAttemptStatus.ACTIVE.value:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "response attempt is not active")
            if answer["answer_status"] not in {
                AnswerStatus.DRAFTING.value,
                AnswerStatus.FINALIZED_PENDING_STOP.value,
            }:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "answer content is not finalizable")

            attempts[response_id].update(
                {
                    "status": ResponseAttemptStatus.CONTENT_FINALIZED.value,
                    "final_content_sha256": final_content_sha256,
                    "delivery_status": "pending_observation",
                }
            )
            answer.update(
                {
                    "answer_status": AnswerStatus.CONTENT_FINALIZED.value,
                    "content_status": "finalized",
                    "delivery_status": "pending_observation",
                    "final_content_sha256": final_content_sha256,
                    "attempts": attempts,
                }
            )
            answers = dict(run.answers)
            answers[answer_id] = answer
            changed = run.model_copy(update={"answers": answers})
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.ANSWER_CONTENT_FINALIZED,
                subject_id=answer_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.ROOT_MAIN,
                payload={
                    "answer_id": answer_id,
                    "response_attempt_id": response_id,
                    "final_content_sha256": final_content_sha256,
                    "content_status": "finalized",
                    "delivery_status": "pending_observation",
                    "run_from": run.state.value,
                    "run_to": changed.state.value,
                    "authority_host_event_id": authority_host_event_id,
                    "committed_by_agent_execution_id": committed_by_agent_execution_id,
                },
                idempotency_key=f"answer-content-finalize:{client_request_id}",
                source_host_event_id=authority_host_event_id,
                client_request_id=client_request_id,
                agent_execution_id=committed_by_agent_execution_id,
            )
            self._commit_run(after, event)
            self._write_binding(
                binding.model_copy(
                    update={
                        "active_run_id": binding.active_run_id,
                        "pending_answer_id": None,
                        "pending_answer_status": None,
                        "current_response_attempt_id": None,
                        "delivery_candidate_answer_id": answer_id,
                        "delivery_candidate_status": "pending_observation",
                        "delivery_candidate_response_attempt_id": response_id,
                        "delivery_candidate_run_id": run_id,
                        "delivery_candidate_paper_id": paper_id,
                    }
                )
            )
            return event

    def resume_answer(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        answer_id: str,
        authority_turn_event_id: str,
        root_main_agent_execution_id: str,
        client_request_id: str,
        resume_kind: str = "explicit_user",
    ) -> dict[str, Any]:
        if resume_kind not in {"explicit_user", "automatic_continuation"}:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid answer resume kind")
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer = dict(run.answers.get(answer_id) or {})
            if not answer or binding.pending_answer_id != answer_id:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "pending answer not found")
            if answer["answer_status"] not in {
                AnswerStatus.INTERRUPTED.value,
                AnswerStatus.REPAIR_REQUESTED.value,
            }:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "answer is not resumable")
            previous_id = answer["current_response_attempt_id"]
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            previous = attempts[previous_id]
            if previous["status"] not in {
                ResponseAttemptStatus.ACTIVE.value,
                ResponseAttemptStatus.INTERRUPTED.value,
            }:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "previous response attempt is terminal")
            previous["status"] = ResponseAttemptStatus.SUPERSEDED.value
            response_id = sequence_id("rsp", answer_id, authority_turn_event_id, root_main_agent_execution_id)
            if response_id in attempts:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "response attempt already exists")
            attempts[response_id] = {
                "response_attempt_id": response_id,
                "status": ResponseAttemptStatus.ACTIVE.value,
                "authority_turn_event_id": authority_turn_event_id,
                "root_main_agent_execution_id": root_main_agent_execution_id,
                "previous_response_attempt_id": previous_id,
                "resume_kind": resume_kind,
            }
            answer.update(
                {
                    "answer_status": AnswerStatus.DRAFTING.value,
                    "current_response_attempt_id": response_id,
                    "attempts": attempts,
                }
            )
            answers = dict(run.answers)
            answers[answer_id] = answer
            changed = run.model_copy(update={"answers": answers})
            event, after = self._plan_event(
                changed,
                event_kind=EventKind.ANSWER_RESUMED,
                subject_id=answer_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.STATE_SERVICE if resume_kind == "automatic_continuation" else Actor.ROOT_MAIN,
                payload={
                    "answer_id": answer_id,
                    "previous_response_attempt_id": previous_id,
                    "response_attempt_id": response_id,
                    "response_attempt_status": ResponseAttemptStatus.ACTIVE.value,
                    "answer_status": AnswerStatus.DRAFTING.value,
                    "authority_turn_event_id": authority_turn_event_id,
                    "root_main_agent_execution_id": root_main_agent_execution_id,
                    "resume_kind": resume_kind,
                },
                idempotency_key=f"answer-resume:{client_request_id}",
                client_request_id=client_request_id,
                agent_execution_id=root_main_agent_execution_id,
            )
            self._commit_run(after, event)
            self._write_binding(
                binding.model_copy(
                    update={
                        "pending_answer_status": AnswerStatus.DRAFTING.value,
                        "current_response_attempt_id": response_id,
                    }
                )
            )
            return answer

    def start_automatic_answer_attempt(
        self, *, task_id: str, paper_id: str, run_id: str, answer_id: str,
        authority_turn_event_id: str, root_main_agent_execution_id: str,
        continuation_attempt_id: str,
    ) -> dict[str, Any]:
        """Atomically supersede the old response when a continuation nonce is claimed."""
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer = dict(run.answers.get(answer_id) or {})
            if not answer or binding.pending_answer_id != answer_id:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "automatic continuation answer is no longer pending")
            existing = run.event_dedupe.get(f"answer-auto-resume:{continuation_attempt_id}")
            if existing is not None:
                return answer
            previous_id = answer["current_response_attempt_id"]
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            if attempts[previous_id]["status"] not in {
                ResponseAttemptStatus.ACTIVE.value, ResponseAttemptStatus.INTERRUPTED.value,
            }:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "automatic continuation previous attempt is terminal")
            attempts[previous_id]["status"] = ResponseAttemptStatus.SUPERSEDED.value
            response_id = sequence_id("rsp", answer_id, authority_turn_event_id, root_main_agent_execution_id)
            attempts[response_id] = {
                "response_attempt_id": response_id, "status": ResponseAttemptStatus.ACTIVE.value,
                "authority_turn_event_id": authority_turn_event_id,
                "root_main_agent_execution_id": root_main_agent_execution_id,
                "previous_response_attempt_id": previous_id, "resume_kind": "automatic_continuation",
            }
            answer.update({
                "answer_status": AnswerStatus.DRAFTING.value,
                "current_response_attempt_id": response_id,
                "answer_auto_resume_count": 1,
                "attempts": attempts,
            })
            answers = dict(run.answers)
            answers[answer_id] = answer
            event, after = self._plan_event(
                run.model_copy(update={"answers": answers}), event_kind=EventKind.ANSWER_RESUMED,
                subject_id=answer_id, result=EventResult.SUCCEEDED, actor=Actor.STATE_SERVICE,
                payload={
                    "answer_id": answer_id, "previous_response_attempt_id": previous_id,
                    "response_attempt_id": response_id, "response_attempt_status": ResponseAttemptStatus.ACTIVE.value,
                    "answer_status": AnswerStatus.DRAFTING.value,
                    "authority_turn_event_id": authority_turn_event_id,
                    "root_main_agent_execution_id": root_main_agent_execution_id,
                    "resume_kind": "automatic_continuation", "continuation_attempt_id": continuation_attempt_id,
                },
                idempotency_key=f"answer-auto-resume:{continuation_attempt_id}",
                source_host_event_id=authority_turn_event_id,
                agent_execution_id=root_main_agent_execution_id,
            )
            self._commit_run(after, event)
            self._write_binding(binding.model_copy(update={
                "pending_answer_status": AnswerStatus.DRAFTING.value,
                "current_response_attempt_id": response_id,
            }))
            return answer

    def interrupt_answer(self, *, task_id: str, paper_id: str, run_id: str, answer_id: str,
                         reason_code: str, authority_host_event_id: str) -> RunEvent:
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer = dict(run.answers.get(answer_id) or {})
            if not answer or binding.pending_answer_id != answer_id:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "pending answer not found")
            response_id = answer["current_response_attempt_id"]
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            before_answer = answer["answer_status"]
            before_attempt = attempts[response_id]["status"]
            if before_attempt != ResponseAttemptStatus.ACTIVE.value:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "only active response attempt can be interrupted")
            attempts[response_id]["status"] = ResponseAttemptStatus.INTERRUPTED.value
            answer.update({"answer_status": AnswerStatus.INTERRUPTED.value, "attempts": attempts})
            answers = dict(run.answers)
            answers[answer_id] = answer
            event, after = self._plan_event(
                run.model_copy(update={"answers": answers}),
                event_kind=EventKind.ANSWER_INTERRUPTED,
                subject_id=answer_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.STATE_SERVICE,
                payload={
                    "answer_from": before_answer,
                    "answer_to": AnswerStatus.INTERRUPTED.value,
                    "attempt_from": before_attempt,
                    "attempt_to": ResponseAttemptStatus.INTERRUPTED.value,
                    "response_attempt_id": response_id,
                    "reason_code": reason_code,
                    "authority_host_event_id": authority_host_event_id,
                },
                idempotency_key=f"answer-interrupt:{authority_host_event_id}:{answer_id}",
                source_host_event_id=authority_host_event_id,
            )
            self._commit_run(after, event)
            self._write_binding(binding.model_copy(update={"pending_answer_status": AnswerStatus.INTERRUPTED.value}))
            return event

    def abandon_answer(self, *, task_id: str, paper_id: str, run_id: str, answer_id: str,
                       authority_turn_event_id: str, root_main_agent_execution_id: str,
                       client_request_id: str) -> RunEvent:
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer = dict(run.answers.get(answer_id) or {})
            if not answer or binding.pending_answer_id != answer_id:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "pending answer not found")
            response_id = answer["current_response_attempt_id"]
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            attempts[response_id]["status"] = ResponseAttemptStatus.ABANDONED.value
            answer.update({"answer_status": AnswerStatus.ABANDONED.value, "attempts": attempts})
            answers = dict(run.answers)
            answers[answer_id] = answer
            event, after = self._plan_event(
                run.model_copy(update={"answers": answers}),
                event_kind=EventKind.ANSWER_ABANDONED,
                subject_id=answer_id,
                result=EventResult.SUCCEEDED,
                actor=Actor.ROOT_MAIN,
                payload={
                    "answer_id": answer_id,
                    "abandoned_response_attempt_id": response_id,
                    "response_attempt_status": ResponseAttemptStatus.ABANDONED.value,
                    "answer_status": AnswerStatus.ABANDONED.value,
                    "authority_turn_event_id": authority_turn_event_id,
                    "root_main_agent_execution_id": root_main_agent_execution_id,
                },
                idempotency_key=f"answer-abandon:{client_request_id}",
                client_request_id=client_request_id,
                agent_execution_id=root_main_agent_execution_id,
            )
            self._commit_run(after, event)
            self._write_binding(
                binding.model_copy(
                    update={
                        "pending_answer_id": None,
                        "pending_answer_status": None,
                        "current_response_attempt_id": None,
                    }
                )
            )
            return event

    def commit_stop_delivery(self, *, task_id: str, paper_id: str, run_id: str,
                             assistant_message_hash: str, authority_host_event_id: str) -> RunEvent:
        """Commit only host-observed delivery; callers cannot self-report this event."""
        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)), FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            binding = self._read_binding(task_id)
            answer_id = binding.delivery_candidate_answer_id
            attempt_id = binding.delivery_candidate_response_attempt_id
            if (
                answer_id is None
                or attempt_id is None
                or binding.delivery_candidate_paper_id != paper_id
                or binding.delivery_candidate_run_id != run_id
            ):
                raise ReadPaperError(ErrorCode.NOT_FOUND, "delivery candidate not found")
            answers = {key: dict(value) for key, value in run.answers.items()}
            answer = answers.get(answer_id)
            if not isinstance(answer, dict):
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery answer is missing")
            if answer.get("final_content_sha256") != assistant_message_hash:
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "observed message hash differs from finalized content")
            attempts = {key: dict(value) for key, value in answer["attempts"].items()}
            if attempts.get(attempt_id, {}).get("status") != ResponseAttemptStatus.CONTENT_FINALIZED.value:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery attempt is not content-finalized")
            attempts[attempt_id].update(
                {"status": ResponseAttemptStatus.DELIVERED.value, "delivery_status": "sent_verified"}
            )
            answer.update(
                {
                    "answer_status": AnswerStatus.SENT_VERIFIED.value,
                    "delivery_status": "sent_verified",
                    "attempts": attempts,
                }
            )
            answers[answer_id] = answer
            changed = run.model_copy(update={"answers": answers})
            event, after = self._plan_event(
                changed, event_kind=EventKind.ANSWER_SENT_OBSERVED,
                subject_id=answer_id or run_id, result=EventResult.SUCCEEDED, actor=Actor.HOOK,
                payload={"assistant_message_sha256": assistant_message_hash, "authority_host_event_id": authority_host_event_id,
                         "answer_id": answer_id, "response_attempt_id": attempt_id,
                         "run_from": run.state.value, "run_to": changed.state.value},
                idempotency_key=f"stop-delivery:{authority_host_event_id}", source_host_event_id=authority_host_event_id,
            )
            self._commit_run(after, event)
            self._write_binding(binding.model_copy(update={
                "delivery_candidate_answer_id": None,
                "delivery_candidate_status": None,
                "delivery_candidate_response_attempt_id": None,
                "delivery_candidate_run_id": None,
                "delivery_candidate_paper_id": None,
            }))
            return event

    def mark_delivery_unknown(
        self,
        *,
        task_id: str,
        reason_code: str,
        authority_host_event_id: str | None,
    ) -> RunEvent | None:
        """Release a stale delivery observation without reopening completed content."""

        with FileLock(self.layout.reference_lock), FileLock(self.layout.task_lock(task_id)):
            binding = self._read_binding(task_id)
            if binding.delivery_candidate_answer_id is None:
                return None
            paper_id = str(binding.delivery_candidate_paper_id)
            run_id = str(binding.delivery_candidate_run_id)
            with FileLock(self.layout.run_lock(run_id)):
                self._recover_run(paper_id, run_id)
                run = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
                _, _, event = self._mark_delivery_unknown_locked(
                    run,
                    binding,
                    reason_code=reason_code,
                    authority_host_event_id=authority_host_event_id,
                )
                return event

    def _mark_delivery_unknown_locked(
        self,
        run: RunSnapshot,
        binding: TaskBinding,
        *,
        reason_code: str,
        authority_host_event_id: str | None,
    ) -> tuple[RunSnapshot, TaskBinding, RunEvent]:
        answer_id = binding.delivery_candidate_answer_id
        attempt_id = binding.delivery_candidate_response_attempt_id
        if answer_id is None or attempt_id is None:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery candidate is incomplete")
        if (
            binding.delivery_candidate_paper_id != run.paper_id
            or binding.delivery_candidate_run_id != run.run_id
        ):
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery candidate run does not match")
        answers = {key: dict(value) for key, value in run.answers.items()}
        answer = answers.get(answer_id)
        if not isinstance(answer, dict):
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery answer is missing")
        attempts = {key: dict(value) for key, value in answer["attempts"].items()}
        if attempts.get(attempt_id, {}).get("status") != ResponseAttemptStatus.CONTENT_FINALIZED.value:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "delivery attempt is not content-finalized")
        attempts[attempt_id].update(
            {"status": ResponseAttemptStatus.DELIVERY_UNKNOWN.value, "delivery_status": "unknown"}
        )
        answer.update(
            {
                "answer_status": AnswerStatus.DELIVERY_UNKNOWN.value,
                "delivery_status": "unknown",
                "attempts": attempts,
            }
        )
        answers[answer_id] = answer
        event, after = self._plan_event(
            run.model_copy(update={"answers": answers}),
            event_kind=EventKind.ANSWER_DELIVERY_UNKNOWN,
            subject_id=answer_id,
            result=EventResult.UNKNOWN,
            actor=Actor.HOST_OBSERVER,
            payload={
                "answer_id": answer_id,
                "response_attempt_id": attempt_id,
                "delivery_status": "unknown",
                "reason_code": reason_code,
                "authority_host_event_id": authority_host_event_id,
            },
            idempotency_key=f"answer-delivery-unknown:{answer_id}:{attempt_id}",
            source_host_event_id=authority_host_event_id,
        )
        self._commit_run(after, event)
        cleared = binding.model_copy(
            update={
                "delivery_candidate_answer_id": None,
                "delivery_candidate_status": None,
                "delivery_candidate_response_attempt_id": None,
                "delivery_candidate_run_id": None,
                "delivery_candidate_paper_id": None,
            }
        )
        self._write_binding(cleared)
        return after, cleared, event

    def append_event(
        self,
        *,
        paper_id: str,
        run_id: str,
        event_kind: EventKind,
        subject_id: str,
        result: EventResult,
        actor: Actor,
        payload: dict[str, Any],
        idempotency_key: str,
        source_host_event_id: str | None = None,
        client_request_id: str | None = None,
        session_id: str | None = None,
        session_epoch: int = 0,
        turn_id: str | None = None,
        agent_id: str | None = None,
        agent_execution_id: str | None = None,
        context_stream_id: str | None = None,
        context_epoch: int = 0,
        tool_use_id: str | None = None,
    ) -> RunEvent:
        with FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            current = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            existing = current.event_dedupe.get(idempotency_key)
            payload_sha = digest(payload)
            if existing is not None:
                if existing["payload_sha256"] != payload_sha:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "idempotency key reused with different payload")
                return self._find_event(paper_id, run_id, str(existing["event_id"]))
            if event_kind in AGENT_CONTEXT_EVENTS and (
                agent_execution_id is None or context_stream_id is None or session_id is None or turn_id is None
            ):
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "agent content event lacks bound execution/context")
            event, after = self._plan_event(
                current,
                event_kind=event_kind,
                subject_id=subject_id,
                result=result,
                actor=actor,
                payload=payload,
                idempotency_key=idempotency_key,
                source_host_event_id=source_host_event_id,
                client_request_id=client_request_id,
                session_id=session_id,
                session_epoch=session_epoch,
                turn_id=turn_id,
                agent_id=agent_id,
                agent_execution_id=agent_execution_id,
                context_stream_id=context_stream_id,
                context_epoch=context_epoch,
                tool_use_id=tool_use_id,
            )
            self._commit_run(after, event)
            return event

    def _plan_event(self, current: RunSnapshot, *, event_kind: EventKind, subject_id: str, result: EventResult,
                    actor: Actor, payload: dict[str, Any], idempotency_key: str,
                    source_host_event_id: str | None = None, client_request_id: str | None = None,
                    session_id: str | None = None, session_epoch: int = 0, turn_id: str | None = None,
                    agent_id: str | None = None, agent_execution_id: str | None = None,
                    context_stream_id: str | None = None, context_epoch: int = 0,
                    tool_use_id: str | None = None) -> tuple[RunEvent, RunSnapshot]:
        payload_sha = digest(payload)
        sequence = current.event_seq + 1
        event_id = sequence_id("ev", current.run_id, sequence, event_kind.value, subject_id, result.value, payload_sha)
        event = RunEvent(
            event_id=event_id, event_seq=sequence, occurred_at=utc_now(), source_host_event_id=source_host_event_id,
            client_request_id=client_request_id, task_id=current.task_id, session_id=session_id,
            session_epoch=session_epoch, turn_id=turn_id, agent_id=agent_id,
            agent_execution_id=agent_execution_id, context_stream_id=context_stream_id,
            context_epoch=context_epoch, actor=actor, tool_use_id=tool_use_id, paper_id=current.paper_id,
            bundle_id=current.bundle_id, run_id=current.run_id, event_kind=event_kind,
            subject_id=subject_id, result=result, payload=payload, payload_sha256=payload_sha,
            idempotency_key=idempotency_key,
        )
        dedupe = dict(current.event_dedupe)
        dedupe[idempotency_key] = {"event_id": event_id, "payload_sha256": payload_sha}
        return event, current.model_copy(update={"event_seq": sequence, "event_dedupe": dedupe})

    def _commit_run(self, after: RunSnapshot, event: RunEvent) -> None:
        run_dir = self.layout.run_dir(after.paper_id, after.run_id)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        intent_path = self.layout.run_transaction(after.paper_id, after.run_id)
        intent = {"schema_version": 1, "status": "prepared", "event": event.model_dump(mode="json"), "after": after.model_dump(mode="json")}
        atomic_write_json(intent_path, intent)
        append_jsonl_once(self.layout.run_events(after.paper_id, after.run_id), intent["event"], identity_field="event_id", identity=event.event_id)
        atomic_write_json(self.layout.run_state(after.paper_id, after.run_id), intent["after"])
        intent["status"] = "completed"
        atomic_write_json(intent_path, intent)
        self._refresh_run_index_locked(after)

    def _recover_run(self, paper_id: str, run_id: str) -> None:
        path = self.layout.run_transaction(paper_id, run_id)
        if not path.exists():
            return
        intent = read_json(path)
        if intent.get("status") == "completed" and self.layout.run_state(paper_id, run_id).exists():
            current = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            self._refresh_run_index_locked(current)
            return
        event = RunEvent.model_validate(intent["event"])
        after = RunSnapshot.model_validate(intent["after"])
        append_jsonl_once(self.layout.run_events(paper_id, run_id), event.model_dump(mode="json"), identity_field="event_id", identity=event.event_id)
        atomic_write_json(self.layout.run_state(paper_id, run_id), after.model_dump(mode="json"))
        if intent.get("status") != "completed":
            intent["status"] = "completed"
            atomic_write_json(path, intent)
        self._refresh_run_index_locked(after)

    def _refresh_run_index_locked(self, run: RunSnapshot) -> None:
        """Write a compact, derived navigation view without changing evidence."""

        event_counts: dict[str, int] = {}
        events_path = self.layout.run_events(run.paper_id, run.run_id)
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                item = json.loads(line)
                kind = str(item.get("event_kind", "unknown"))
                event_counts[kind] = event_counts.get(kind, 0) + 1

        record_counts: dict[str, int] = {}
        records_path = self.layout.run_records(run.paper_id, run.run_id)
        if records_path.exists():
            for path in records_path.glob("rec_*.json"):
                item = read_json(path)
                kind = str(item.get("record_kind", "unknown"))
                record_counts[kind] = record_counts.get(kind, 0) + 1

        answer_states: dict[str, dict[str, Any]] = {}
        for answer_id, answer in sorted(run.answers.items()):
            answer_status = answer.get("answer_status")
            content_status = answer.get("content_status")
            if content_status is None and answer_status in {
                AnswerStatus.CONTENT_FINALIZED.value,
                AnswerStatus.SENT_VERIFIED.value,
                AnswerStatus.DELIVERY_UNKNOWN.value,
            }:
                content_status = "finalized"
            delivery_status = answer.get("delivery_status")
            if delivery_status is None and answer_status == AnswerStatus.SENT_VERIFIED.value:
                delivery_status = "sent_verified"
            answer_states[answer_id] = {
                "answer_status": answer_status,
                "content_status": content_status,
                "delivery_status": delivery_status,
                "current_response_attempt_id": answer.get("current_response_attempt_id"),
            }
        run_dir = self.layout.run_dir(run.paper_id, run.run_id)
        paths = {
            "state": str(self.layout.run_state(run.paper_id, run.run_id).relative_to(self.layout.root)),
            "events": str(events_path.relative_to(self.layout.root)),
            "records": str(records_path.relative_to(self.layout.root)),
            "inventory": str((run_dir / "inventory.json").relative_to(self.layout.root)),
            "summary": str(self.layout.run_summary(run.paper_id, run.run_id).relative_to(self.layout.root)),
        }
        index = {
            "schema_version": 1,
            "paper_id": run.paper_id,
            "bundle_id": run.bundle_id,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "run_state": run.state.value,
            "scope_kind": run.scope_kind.value,
            "event_seq": run.event_seq,
            "event_count": sum(event_counts.values()),
            "event_counts": dict(sorted(event_counts.items())),
            "record_count": sum(record_counts.values()),
            "record_counts": dict(sorted(record_counts.items())),
            "answer_count": len(answer_states),
            "answers": answer_states,
            "record_heads": dict(sorted(run.record_heads.items())),
            "paths": paths,
        }
        index_bytes = canonical_bytes(index) + b"\n"
        index_path = self.layout.run_index(run.paper_id, run.run_id)
        if not index_path.exists() or index_path.read_bytes() != index_bytes:
            atomic_write(index_path, index_bytes)

        lines = [
            f"# ReadPaper run {run.run_id}",
            "",
            f"- Paper: `{run.paper_id}`",
            f"- Bundle: `{run.bundle_id}`",
            f"- State: `{run.state.value}`",
            f"- Scope: `{run.scope_kind.value}`",
            f"- Events: {sum(event_counts.values())}",
            f"- Records: {sum(record_counts.values())}",
            f"- Answers: {len(answer_states)}",
            "",
            "## Record counts",
            "",
        ]
        lines.extend(
            f"- `{kind}`: {count}" for kind, count in sorted(record_counts.items())
        )
        lines.extend(["", "## Answer states", ""])
        if answer_states:
            lines.extend(
                (
                    f"- `{answer_id}`: content=`{value.get('content_status') or 'in_progress'}`, "
                    f"delivery=`{value.get('delivery_status') or value.get('answer_status') or 'unknown'}`"
                )
                for answer_id, value in answer_states.items()
            )
        else:
            lines.append("- None")
        lines.extend(
            [
                "",
                "## Evidence paths",
                "",
                *[f"- {name}: `{path}`" for name, path in paths.items()],
                "",
                "This file is a derived navigation view. Immutable records and events remain authoritative.",
                "",
            ]
        )
        summary_bytes = "\n".join(lines).encode("utf-8")
        summary_path = self.layout.run_summary(run.paper_id, run.run_id)
        if not summary_path.exists() or summary_path.read_bytes() != summary_bytes:
            atomic_write(summary_path, summary_bytes)

    def _find_event(self, paper_id: str, run_id: str, event_id: str) -> RunEvent:
        path = self.layout.run_events(paper_id, run_id)
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if value.get("event_id") == event_id:
                return RunEvent.model_validate(value)
        raise ReadPaperError(ErrorCode.STATE_CONFLICT, "dedupe index points to missing event")

    def append_host_event(self, *, task_id: str, event_kind: HostEventKind, semantic_key: str,
                          subject_id: str, payload: dict[str, Any]) -> HostEvent:
        with FileLock(self.layout.task_lock(task_id)):
            state_path = self.layout.host_state(task_id)
            state = HostLedgerState.model_validate(read_json(state_path)) if state_path.exists() else HostLedgerState(task_id=task_id)
            payload_sha = digest(payload)
            existing = state.dedupe.get(semantic_key)
            if existing is not None:
                if existing["payload_sha256"] != payload_sha:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "host semantic key payload conflict")
                return self._find_host_event(task_id, str(existing["host_event_id"]))
            sequence = state.host_event_seq + 1
            event_id = sequence_id("hev", task_id, sequence, event_kind.value, semantic_key, subject_id, payload_sha)
            event = HostEvent(host_event_id=event_id, host_event_seq=sequence, occurred_at=utc_now(), task_id=task_id,
                              event_kind=event_kind, semantic_key=semantic_key, subject_id=subject_id,
                              payload_sha256=payload_sha, payload=payload)
            append_jsonl_once(self.layout.host_ledger(task_id), event.model_dump(mode="json"), identity_field="host_event_id", identity=event_id)
            dedupe = dict(state.dedupe)
            dedupe[semantic_key] = {"host_event_id": event_id, "payload_sha256": payload_sha, "host_event_seq": sequence}
            atomic_write_json(state_path, state.model_copy(update={"host_event_seq": sequence, "dedupe": dedupe}).model_dump(mode="json"))
            return event

    def _find_host_event(self, task_id: str, event_id: str) -> HostEvent:
        for line in self.layout.host_ledger(task_id).read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if value.get("host_event_id") == event_id:
                return HostEvent.model_validate(value)
        raise ReadPaperError(ErrorCode.STATE_CONFLICT, "host dedupe points to missing event")

    def find_user_turn(self, *, task_id: str, turn_or_event_id: str) -> HostEvent:
        path = self.layout.host_ledger(task_id)
        if not path.exists():
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "user-turn host ledger is unavailable")
        matches: list[HostEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            event = HostEvent.model_validate(json.loads(line))
            if event.event_kind is HostEventKind.USER_TURN_STARTED and (
                event.host_event_id == turn_or_event_id or event.subject_id == turn_or_event_id
            ):
                matches.append(event)
        if len(matches) != 1:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "exactly one observed user turn is required")
        return matches[0]

    def find_agent_starts(
        self,
        *,
        task_id: str,
        agent_id: str | None,
        agent_type: str,
        after_host_event_seq: int | None = None,
    ) -> list[HostEvent]:
        """Return trusted starts for one native reviewer identity in ledger order."""
        path = self.layout.host_ledger(task_id)
        if not path.exists():
            return []
        matches: list[HostEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            event = HostEvent.model_validate(json.loads(line))
            if (
                event.event_kind is HostEventKind.AGENT_STARTED
                and (agent_id is None or event.subject_id == agent_id)
                and event.payload.get("agent_type") == agent_type
                and (
                    after_host_event_seq is None
                    or event.host_event_seq > after_host_event_seq
                )
            ):
                matches.append(event)
        return matches

    def claim_reviewer_binding(
        self,
        *,
        task_id: str,
        reviewer_assignment_id: str,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        """CAS-bind a reservation to one host-observed reviewer challenge."""
        with FileLock(self.layout.task_lock(task_id)):
            path = self.layout.host_state(task_id)
            state = (
                HostLedgerState.model_validate(read_json(path))
                if path.exists()
                else HostLedgerState(task_id=task_id)
            )
            bindings = {key: dict(value) for key, value in state.reviewer_bindings.items()}
            existing = bindings.get(reviewer_assignment_id)
            if existing is not None:
                if existing != binding:
                    raise ReadPaperError(
                        ErrorCode.STATE_CONFLICT,
                        "reviewer reservation was already claimed by different evidence",
                    )
                return existing
            bindings[reviewer_assignment_id] = dict(binding)
            atomic_write_json(
                path,
                state.model_copy(update={"reviewer_bindings": bindings}).model_dump(mode="json"),
            )
            return dict(binding)

    def put_versioned_record(self, *, paper_id: str, run_id: str, record_kind: str, entity_id: str,
                             payload: dict[str, Any], version_id: str | None = None,
                             parent_version_id: str | None = None, parent_record_id: str | None = None) -> VersionedRecord:
        with FileLock(self.layout.run_lock(run_id)):
            self._recover_run(paper_id, run_id)
            current = RunSnapshot.model_validate(read_json(self.layout.run_state(paper_id, run_id)))
            head_key = f"{record_kind}:{entity_id}"
            head = current.record_heads.get(head_key)
            if head is None and parent_version_id is not None:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "first version cannot have a parent")
            if head is not None and parent_version_id != head["version_id"]:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "stale version parent")
            payload_sha = digest(payload)
            record_id = sequence_id("rec", 1, current.paper_id, current.bundle_id, run_id, record_kind, entity_id,
                                    version_id, parent_version_id, parent_record_id, payload_sha)
            record = VersionedRecord(record_id=record_id, record_kind=record_kind, entity_id=entity_id,
                                     version_id=version_id, parent_version_id=parent_version_id,
                                     parent_record_id=parent_record_id, payload_sha256=payload_sha,
                                     payload=payload, created_at=utc_now())
            path = self.layout.run_records(paper_id, run_id) / f"{record_id}.json"
            if path.exists():
                existing = VersionedRecord.model_validate(read_json(path))
                if existing != record:
                    raise ReadPaperError(ErrorCode.ID_MISMATCH, "record ID collision")
                return existing
            atomic_write_json(path, record.model_dump(mode="json"), replace=False)
            if version_id is not None:
                heads = dict(current.record_heads)
                heads[head_key] = {"record_id": record_id, "version_id": version_id}
                atomic_write_json(self.layout.run_state(paper_id, run_id), current.model_copy(update={"record_heads": heads}).model_dump(mode="json"))
            return record
