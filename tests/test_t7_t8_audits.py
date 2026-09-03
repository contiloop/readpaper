from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from readpaper.audits import (
    AuditStage,
    ContentRole,
    content_audit_stage_returned,
    flow_finding_id,
    flow_review_reasons,
    reserve_content_audit,
    validate_content_findings,
)
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.ids import sequence_id


ROOT = Path(__file__).resolve().parents[1]


def test_reviewer_role_files_do_not_override_model_or_effort() -> None:
    for name in ("math_visual", "claim_experiment", "explanation_flow"):
        value = tomllib.loads((ROOT / f".codex/agents/{name}.toml").read_text())
        assert value["name"] == name
        assert "model" not in value
        assert "model_reasoning_effort" not in value
        assert "as content" in value["developer_instructions"]


def test_audit_reservation_has_nonce_and_null_pre_spawn_binding() -> None:
    reservation = reserve_content_audit(
        run_id="run_" + "1" * 32,
        role=ContentRole.MATH_VISUAL,
        audit_seq=1,
        stage=AuditStage.SOURCE_FIRST,
        attempt_no=1,
        agent_execution_id="ae_" + "2" * 64,
        input_digest="3" * 64,
    )
    assert reservation["reviewer_assignment_id"].startswith("rva_")
    assert len(reservation["assignment_nonce"]) == 64
    assert reservation["reviewer_agent_id"] is None
    assert reservation["read_frame_ids"] == reservation["findings"] == []


def test_content_finding_ids_are_gapless_and_body_bound() -> None:
    stage_id = "cas_" + "1" * 64
    body = {
        "category": "visual_misread",
        "statement": "axis mismatch",
        "evidence": [],
        "locator_ids": ["loc_" + "2" * 64],
        "related_finding_ids": [],
        "comparison_state": None,
        "unverified_scope": [],
    }
    finding = {
        "finding_ordinal": 1,
        "finding_id": sequence_id("cf", stage_id, 1, 1, body),
        **body,
    }
    assert validate_content_findings(
        audit_stage_id=stage_id,
        attempt_no=1,
        stage=AuditStage.SOURCE_FIRST,
        findings=[finding],
    ) == [finding]
    with pytest.raises(ReadPaperError) as error:
        validate_content_findings(
            audit_stage_id=stage_id,
            attempt_no=1,
            stage=AuditStage.SOURCE_FIRST,
            findings=[finding | {"finding_ordinal": 2}],
        )
    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ReadPaperError) as mismatch:
        validate_content_findings(
            audit_stage_id=stage_id,
            attempt_no=1,
            stage=AuditStage.SOURCE_FIRST,
            findings=[finding | {"statement": "changed"}],
        )
    assert mismatch.value.code is ErrorCode.ID_MISMATCH


def test_only_returned_note_comparison_completes_content_audit() -> None:
    base = {"role": "math_visual", "stage": "note_comparison", "attempt_no": 1}
    for status in ("requested", "running", "partial", "failed", "cancelled", "completed"):
        assert not content_audit_stage_returned(
            [{"payload": base | {"status": status}}],
            role=ContentRole.MATH_VISUAL,
            stage=AuditStage.NOTE_COMPARISON,
        )
    assert content_audit_stage_returned(
        [{"payload": base | {"status": "returned"}}],
        role=ContentRole.MATH_VISUAL,
        stage=AuditStage.NOTE_COMPARISON,
    )


def test_newer_attempt_and_recheck_bijection_control_audit_completion() -> None:
    base = {"role": "math_visual", "stage": "note_comparison"}
    old = {"payload": base | {"attempt_no": 1, "status": "returned"}}
    pending = {"payload": base | {"attempt_no": 2, "status": "requested"}}
    assert not content_audit_stage_returned(
        [old, pending], role=ContentRole.MATH_VISUAL, stage=AuditStage.NOTE_COMPARISON
    )
    unresolved = {
        "payload": base | {
            "attempt_no": 2,
            "status": "returned",
            "recheck_finding_ids": ["cf_1"],
            "recheck_results": [{"finding_id": "cf_1", "status": "still_present"}],
        }
    }
    assert not content_audit_stage_returned(
        [old, unresolved], role=ContentRole.MATH_VISUAL, stage=AuditStage.NOTE_COMPARISON
    )
    resolved = {
        "payload": unresolved["payload"] | {
            "recheck_results": [{"finding_id": "cf_1", "status": "resolved"}],
        }
    }
    assert content_audit_stage_returned(
        [old, resolved], role=ContentRole.MATH_VISUAL, stage=AuditStage.NOTE_COMPARISON
    )


def test_flow_review_threshold_reasons_are_complete() -> None:
    assert flow_review_reasons(
        explicit_user_request=False,
        requested_level="standard",
        contentious_interpretation=False,
        estimated_tokens=1199,
    ) == ()
    assert flow_review_reasons(
        explicit_user_request=True,
        requested_level="tutorial",
        contentious_interpretation=True,
        estimated_tokens=1200,
    ) == (
        "explicit_user_request",
        "tutorial",
        "contentious_interpretation",
        "length_threshold",
    )
    first = flow_finding_id(flow_audit_id="fa_" + "1" * 64, attempt_no=1, ordinal=1, body={"x": 1})
    second = flow_finding_id(flow_audit_id="fa_" + "1" * 64, attempt_no=1, ordinal=1, body={"x": 2})
    assert first != second
