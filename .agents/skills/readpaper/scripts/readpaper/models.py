"""Closed-set durable models for T1."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunState(StrEnum):
    PREPARED = "prepared"
    READING = "reading"
    REVIEWING = "reviewing"
    NEEDS_WORK = "needs_work"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class ScopeKind(StrEnum):
    FULL = "full"
    USER_REDUCED = "user_reduced"


class InterpretationState(StrEnum):
    NONE = "none"
    OPEN = "open"


class EvidenceLevel(StrEnum):
    PREPARED = "prepared"
    EMITTED = "emitted"
    TOOL_OBSERVED = "tool_observed"
    MAIN_REVIEW_RECORDED = "main_review_recorded"
    UNKNOWN = "unknown"


class Actor(StrEnum):
    ROOT_MAIN = "root_main"
    SUBAGENT = "subagent"
    USER = "user"
    HOOK = "hook"
    HOST_OBSERVER = "host_observer"
    STATE_SERVICE = "state_service"
    UNKNOWN = "unknown"


class EventResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutionStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    RETURNED = "returned"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ObserverState(StrEnum):
    VERIFIED = "verified"
    REQUEST_ACCEPTED = "request_accepted"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"


class Role(StrEnum):
    ROOT_MAIN = "root_main"
    MATH_VISUAL = "math_visual"
    CLAIM_EXPERIMENT = "claim_experiment"
    EXPLANATION_FLOW = "explanation_flow"


class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class SelectionSource(StrEnum):
    ACTIVE_TASK = "active_task"
    INVOCATION_OVERRIDE = "invocation_override"
    READPAPER_ROLE_DEFAULT = "readpaper_role_default"
    AGENTS_DEFAULT = "agents_default"
    PARENT_INHERIT = "parent_inherit"


class RecordKind(StrEnum):
    SCOPE_CONFIRMATION = "scope_confirmation"
    PRINTED_LABEL = "printed_label"
    LOCATOR_CANDIDATE = "locator_candidate"
    LOCATOR_CONFIRMATION = "locator_confirmation"
    UNDERSTANDING_NOTE = "understanding_note"
    MODEL_REQUEST = "model_request"
    AGENT_EXECUTION = "agent_execution"
    MODEL_OBSERVATION = "model_observation"
    AUDIT_START = "audit_start"
    AUDIT_RESULT = "audit_result"
    FINDING_DISPOSITION = "finding_disposition"
    EXPLANATION_DRAFT = "explanation_draft"
    FLOW_START = "flow_start"
    FLOW_RESULT = "flow_result"
    FLOW_FINDING_DISPOSITION = "flow_finding_disposition"
    EXPLANATION_FINALIZED = "explanation_finalized"
    USER_PAUSE = "user_pause"
    ANSWER_GROUNDING = "answer_grounding"
    AUDIT_FINDING = "audit_finding"
    FLOW_FINDING = "flow_finding"


class AnswerStatus(StrEnum):
    DRAFTING = "drafting"
    FINALIZED_PENDING_STOP = "finalized_pending_stop"
    CONTENT_FINALIZED = "content_finalized"
    REPAIR_REQUESTED = "repair_requested"
    INTERRUPTED = "interrupted"
    SENT_VERIFIED = "sent_verified"
    DELIVERY_UNKNOWN = "delivery_unknown"
    ABANDONED = "abandoned"


class ResponseAttemptStatus(StrEnum):
    ACTIVE = "active"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    CONTENT_FINALIZED = "content_finalized"
    DELIVERED = "delivered"
    DELIVERY_UNKNOWN = "delivery_unknown"
    ABANDONED = "abandoned"


class HostEventKind(StrEnum):
    SESSION_STARTED = "session_started"
    USER_TURN_STARTED = "user_turn_started"
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    PRETOOL_AUTHORIZED = "pretool_authorized"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    HOOK_STARTED = "hook_started"
    HOOK_COMPLETED = "hook_completed"
    COMPACT_STARTED = "compact_started"
    COMPACT_FINISHED = "compact_finished"
    ASSISTANT_MESSAGE_OBSERVED = "assistant_message_observed"
    STOP_OBSERVED = "stop_observed"
    OBSERVER_ERROR = "observer_error"


class EventKind(StrEnum):
    RUN_CREATED = "run_created"
    STATE_TRANSITION = "state_transition"
    SOURCE_PREPARED = "source_prepared"
    SESSION_STARTED = "session_started"
    USER_TURN_STARTED = "user_turn_started"
    SOURCE_FRAME_EMITTED = "source_frame_emitted"
    RENDER_CREATED = "render_created"
    VISUAL_OPEN_OBSERVED = "visual_open_observed"
    COMPACT_STARTED = "compact_started"
    COMPACT_FINISHED = "compact_finished"
    SCOPE_CONFIRMED = "scope_confirmed"
    PRINTED_LABEL_RECORDED = "printed_label_recorded"
    LOCATOR_CANDIDATE_RECORDED = "locator_candidate_recorded"
    LOCATOR_CONFIRMED = "locator_confirmed"
    MODEL_REQUESTED = "model_requested"
    AGENT_EXECUTION_STATUSED = "agent_execution_statused"
    MODEL_OBSERVED = "model_observed"
    AUDIT_STARTED = "audit_started"
    AUDIT_RESULT_RECORDED = "audit_result_recorded"
    FINDING_RECORDED = "finding_recorded"
    FINDING_DISPOSITIONED = "finding_dispositioned"
    NOTE_VERSIONED = "note_versioned"
    DRAFT_VERSIONED = "draft_versioned"
    FLOW_AUDIT_STARTED = "flow_audit_started"
    FLOW_RESULT_RECORDED = "flow_result_recorded"
    FLOW_FINDING_RECORDED = "flow_finding_recorded"
    FLOW_FINDING_DISPOSITIONED = "flow_finding_dispositioned"
    EXPLANATION_FINALIZED = "explanation_finalized"
    ANSWER_GROUNDED = "answer_grounded"
    ANSWER_CONTENT_FINALIZED = "answer_content_finalized"
    ANSWER_SENT_OBSERVED = "answer_sent_observed"
    ANSWER_DELIVERY_UNKNOWN = "answer_delivery_unknown"
    AUTO_RESUME_REQUESTED = "auto_resume_requested"
    USER_RESUMED = "user_resumed"
    USER_PAUSED = "user_paused"
    OBSERVER_ERROR = "observer_error"
    ANSWER_STARTED = "answer_started"
    ANSWER_RESUMED = "answer_resumed"
    ANSWER_INTERRUPTED = "answer_interrupted"
    ANSWER_ABANDONED = "answer_abandoned"
    AUTO_RESUME_STATUSED = "auto_resume_statused"


class TaskBinding(StrictModel):
    task_id: str
    active_run_id: str | None = None
    current_run_id: str | None = None
    current_paper_id: str | None = None
    current_bundle_id: str | None = None
    pending_answer_id: str | None = None
    pending_answer_status: str | None = None
    current_response_attempt_id: str | None = None
    delivery_candidate_answer_id: str | None = None
    delivery_candidate_status: str | None = None
    delivery_candidate_response_attempt_id: str | None = None
    delivery_candidate_run_id: str | None = None
    delivery_candidate_paper_id: str | None = None
    session_id: str | None = None
    session_epoch: int = Field(default=0, ge=0)
    run_auto_resume_count: int = Field(default=0, ge=0, le=1)
    answer_auto_resume_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def binding_is_coherent(self) -> "TaskBinding":
        current = (self.current_run_id, self.current_paper_id, self.current_bundle_id)
        if any(value is None for value in current) and any(value is not None for value in current):
            raise ValueError("current run/paper/bundle must be set together")
        pending = (self.pending_answer_id, self.pending_answer_status, self.current_response_attempt_id)
        if any(value is None for value in pending) and any(value is not None for value in pending):
            raise ValueError("pending answer fields must be set together")
        delivery = (
            self.delivery_candidate_answer_id,
            self.delivery_candidate_status,
            self.delivery_candidate_response_attempt_id,
            self.delivery_candidate_run_id,
            self.delivery_candidate_paper_id,
        )
        if any(value is None for value in delivery) and any(value is not None for value in delivery):
            raise ValueError("delivery candidate fields must be set together")
        return self


class RunSnapshot(StrictModel):
    schema_version: int = 1
    paper_id: str
    bundle_id: str
    run_id: str
    task_id: str
    state: RunState = RunState.PREPARED
    resume_phase: RunState | None = None
    scope_kind: ScopeKind = ScopeKind.FULL
    interpretation_state: InterpretationState = InterpretationState.NONE
    scope_locked: bool = False
    required_artifact_ref_ids: tuple[str, ...] = ()
    excluded_artifacts: tuple[dict[str, Any], ...] = ()
    scope_disclosure_markdown: str = ""
    scope_disclosure_sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    event_seq: int = Field(default=0, ge=0)
    event_dedupe: dict[str, dict[str, Any]] = Field(default_factory=dict)
    record_heads: dict[str, dict[str, str]] = Field(default_factory=dict)
    context_epochs: dict[str, int] = Field(default_factory=dict)
    answers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class RunEvent(StrictModel):
    event_id: str
    event_seq: int = Field(ge=1)
    occurred_at: str
    source_host_event_id: str | None = None
    client_request_id: str | None = None
    task_id: str
    session_id: str | None = None
    session_epoch: int = Field(ge=0)
    turn_id: str | None = None
    agent_id: str | None = None
    agent_execution_id: str | None = None
    context_stream_id: str | None = None
    context_epoch: int = Field(ge=0)
    actor: Actor
    tool_use_id: str | None = None
    paper_id: str
    bundle_id: str
    run_id: str
    event_kind: EventKind
    subject_id: str
    result: EventResult
    payload: dict[str, Any]
    payload_sha256: str
    idempotency_key: str


class HostEvent(StrictModel):
    host_event_id: str
    host_event_seq: int = Field(ge=1)
    occurred_at: str
    task_id: str
    event_kind: HostEventKind
    semantic_key: str
    subject_id: str
    payload_sha256: str
    payload: dict[str, Any]


class HostLedgerState(StrictModel):
    schema_version: int = 1
    task_id: str
    host_event_seq: int = 0
    dedupe: dict[str, dict[str, str | int]] = Field(default_factory=dict)
    compact_streams: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reviewer_bindings: dict[str, dict[str, Any]] = Field(default_factory=dict)


class VersionedRecord(StrictModel):
    schema_version: int = 1
    record_id: str
    record_kind: str
    entity_id: str
    version_id: str | None = None
    parent_version_id: str | None = None
    parent_record_id: str | None = None
    payload_sha256: str
    payload: dict[str, Any]
    created_at: str
