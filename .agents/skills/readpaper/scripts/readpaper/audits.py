"""Deterministic reviewer reservations and finding identity validation."""

from __future__ import annotations

import secrets
from enum import StrEnum
from typing import Any

from .canonical import digest
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id


class ContentRole(StrEnum):
    MATH_VISUAL = "math_visual"
    CLAIM_EXPERIMENT = "claim_experiment"


class AuditStage(StrEnum):
    SOURCE_FIRST = "source_first"
    NOTE_COMPARISON = "note_comparison"


class FindingCategory(StrEnum):
    COVERAGE_GAP = "coverage_gap"
    DEFINITION_EQUATION_ERROR = "definition_equation_error"
    VISUAL_MISREAD = "visual_misread"
    CLAIM_EVIDENCE_MISMATCH = "claim_evidence_mismatch"
    EXPERIMENT_DESIGN_ERROR = "experiment_design_error"
    RESULT_SCOPE_OVERREACH = "result_scope_overreach"
    LIMITATION_APPENDIX_OMISSION = "limitation_appendix_omission"
    SOURCE_CONFLICT = "source_conflict"
    INTERPRETIVE_AMBIGUITY = "interpretive_ambiguity"
    OTHER = "other"


def reserve_content_audit(
    *, run_id: str, role: ContentRole, audit_seq: int, stage: AuditStage,
    attempt_no: int, agent_execution_id: str, input_digest: str,
) -> dict[str, Any]:
    audit_id = sequence_id("ca", run_id, role.value, audit_seq)
    stage_id = sequence_id("cas", audit_id, stage.value)
    assignment_id = sequence_id(
        "rva", run_id, "content_stage", stage_id, attempt_no, agent_execution_id
    )
    return {
        "audit_id": audit_id,
        "audit_stage_id": stage_id,
        "attempt_no": attempt_no,
        "stage": stage.value,
        "role": role.value,
        "reviewer_assignment_id": assignment_id,
        "assignment_nonce": secrets.token_hex(32),
        "assignment_input_digest": input_digest,
        "agent_execution_id": agent_execution_id,
        "reviewer_agent_id": None,
        "reviewer_synthesis_epoch": None,
        "status": "requested",
        "read_frame_ids": [],
        "opened_visual_unit_ids": [],
        "unverified_scope": [],
        "findings": [],
        "recheck_finding_ids": [],
        "recheck_results": [],
    }


def validate_content_findings(
    *, audit_stage_id: str, attempt_no: int, stage: AuditStage, findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(findings, start=1):
        item = dict(raw)
        if item.get("finding_ordinal") != ordinal:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "finding ordinals must be gapless")
        try:
            FindingCategory(item["category"])
        except (KeyError, ValueError) as error:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid finding category") from error
        if stage is AuditStage.SOURCE_FIRST and item.get("comparison_state") is not None:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "source-first finding cannot compare the note")
        body = {key: value for key, value in item.items() if key not in {"finding_id", "finding_ordinal"}}
        expected = sequence_id("cf", audit_stage_id, attempt_no, ordinal, body)
        if item.get("finding_id") != expected:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "finding ID does not match canonical body")
        validated.append(item)
    return validated


def content_audit_stage_returned(
    records: list[dict[str, Any]], *, role: ContentRole, stage: AuditStage
) -> bool:
    """Return true only when the latest attempt has one resolved returned result.

    ``records`` may contain both audit-start and audit-result records.  A newer
    requested/partial attempt must therefore supersede an older returned one.
    """
    matching = [
        item.get("payload", {})
        for item in records
        if item.get("payload", {}).get("role") == role.value
        and item.get("payload", {}).get("stage") == stage.value
    ]
    if not matching:
        return False
    try:
        latest_attempt = max(int(item["attempt_no"]) for item in matching)
    except (KeyError, TypeError, ValueError):
        return False
    latest = [item for item in matching if item.get("attempt_no") == latest_attempt]
    returned = [item for item in latest if item.get("status") == "returned"]
    if len(returned) != 1:
        return False
    result = returned[0]
    requested_ids = list(result.get("recheck_finding_ids", []))
    rechecks = list(result.get("recheck_results", []))
    if [item.get("finding_id") for item in rechecks] != requested_ids:
        return False
    return all(item.get("status") == "resolved" for item in rechecks)


def flow_review_reasons(
    *, explicit_user_request: bool, requested_level: str,
    contentious_interpretation: bool, estimated_tokens: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if explicit_user_request:
        reasons.append("explicit_user_request")
    if requested_level == "tutorial":
        reasons.append("tutorial")
    if contentious_interpretation:
        reasons.append("contentious_interpretation")
    if estimated_tokens >= 1200:
        reasons.append("length_threshold")
    return tuple(reasons)


def flow_finding_id(*, flow_audit_id: str, attempt_no: int, ordinal: int, body: dict[str, Any]) -> str:
    return sequence_id("ff", flow_audit_id, attempt_no, ordinal, body)
