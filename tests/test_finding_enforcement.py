from __future__ import annotations

import pytest

from readpaper.findings import pending_content_findings
from readpaper.locators import PdfPageLocator


def evidence(outcome: str = "rejected") -> dict:
    locator = PdfPageLocator(bundle_id="bundle", artifact_ref_id="ref", artifact_id="artifact", pdf_page=1)
    finding = {"finding_id": "finding", "category": "definition_equation_error", "locator_ids": [locator.locator_id]}
    records = []
    events = []
    def record(key, kind, payload, seq, event_kind, actor="root_main", **versions):
        item = {"record_id": key, "record_kind": kind, "entity_id": "note", "payload": payload, **versions}
        records.append(item)
        events.append({"event_id": f"event-{key}", "event_seq": seq, "event_kind": event_kind,
                       "actor": actor, "result": "succeeded", "context_stream_id": "main", "context_epoch": 0,
                       "payload": {"record_id": key}})
        return item
    record("parent", "understanding_note", {"content_sha256": "a" * 64}, 1, "note_versioned", version_id="v1")
    origin = record("origin", "audit_result", {"role": "math_visual", "findings": [finding], "note_version_id": "v1"}, 2, "audit_result_recorded", "reviewer")
    record("confirm", "locator_confirmation", {"locator_id": locator.locator_id, "locator": locator.model_dump()}, 3, "locator_confirmed")
    events.append({"event_id": "reopen", "event_seq": 4, "event_kind": "source_frame_emitted", "subject_id": "frame",
                   "actor": "root_main", "result": "succeeded", "context_stream_id": "main", "context_epoch": 0,
                   "payload": {"content_sha256": "c" * 64}})
    record("child", "understanding_note", {"content_sha256": "b" * 64}, 5, "note_versioned", version_id="v2", parent_version_id="v1")
    record("disposition", "finding_disposition", {
        "finding_id": "finding", "disposition": outcome, "rationale": "Verified against the equation on page 1.",
        "confirmed_locator_ids": [locator.locator_id], "source_reopen_event_ids": ["reopen"], "remediation_record_ids": ["child"],
    }, 6, "finding_dispositioned")
    recheck = record("recheck", "audit_result", {
        "role": "math_visual", "findings": [], "recheck_finding_ids": ["finding"],
        "recheck_results": [{"finding_id": "finding", "status": "resolved", "remediation_record_id": "child"}],
    }, 7, "audit_result_recorded", "reviewer")
    inventory = {"bundle_id": "bundle", "pages": [{"artifact_ref_id": "ref", "artifact_id": "artifact", "pdf_page": 1, "text": "x" * 100}],
                 "visual_units": [{"unit_id": "visual", "artifact_ref_id": "ref", "artifact_id": "artifact", "pdf_page": 1}], "frames": [{
        "frame_id": "frame", "content_sha256": "c" * 64,
        "source_ranges": [{"artifact_ref_id": "ref", "artifact_id": "artifact", "pdf_page": 1, "char_start": 0, "char_end": 100}],
    }]}
    return {"records": records, "events": events, "inventory": inventory, "latest_results": [origin, recheck], "trusted_results": [origin, recheck]}


@pytest.mark.parametrize("outcome", ["rejected", "accepted", "partially_accepted", "modified"])
def test_source_grounded_disposition_is_resolved(outcome: str) -> None:
    assert pending_content_findings(**evidence(outcome)) == {}


@pytest.mark.parametrize("mutation,reason", [
    ("missing_disposition", "disposition_missing"),
    ("old_reopen", "post_finding_source_reopen_missing"),
    ("reviewer_reopen", "post_finding_source_reopen_missing"),
    ("wrong_page", "post_finding_source_reopen_missing"),
    ("old_epoch", "post_finding_source_reopen_missing"),
    ("unconfirmed", "confirmed_locator_missing"),
    ("unchanged_child", "remediation_content_unchanged"),
    ("unrelated_child", "remediation_not_from_audited_version"),
    ("missing_recheck", "reviewer_remediation_recheck_missing"),
    ("unbound_recheck", "reviewer_remediation_recheck_missing"),
    ("false_interpretive", "noninterpretive_finding_unresolved"),
])
def test_incomplete_or_unrelated_evidence_stays_blocking(mutation: str, reason: str) -> None:
    data = evidence("accepted")
    records = {item["record_id"]: item for item in data["records"]}
    reopen = next(item for item in data["events"] if item["event_id"] == "reopen")
    if mutation == "missing_disposition":
        data["records"].remove(records["disposition"])
    elif mutation == "old_reopen":
        reopen["event_seq"] = 1
    elif mutation == "reviewer_reopen":
        reopen["actor"] = "reviewer"
    elif mutation == "wrong_page":
        data["inventory"]["frames"][0]["source_ranges"][0]["pdf_page"] = 2
    elif mutation == "old_epoch":
        reopen["context_epoch"] = 1
    elif mutation == "unconfirmed":
        records["confirm"]["payload"]["locator_id"] = "wrong"
    elif mutation == "unchanged_child":
        records["child"]["payload"]["content_sha256"] = "a" * 64
    elif mutation == "unrelated_child":
        records["origin"]["payload"]["note_version_id"] = "unrelated-version"
    elif mutation == "missing_recheck":
        records["recheck"]["payload"]["recheck_results"] = []
    elif mutation == "unbound_recheck":
        data["trusted_results"] = [records["origin"]]
    elif mutation == "false_interpretive":
        records["disposition"]["payload"]["disposition"] = "interpretive"
    assert pending_content_findings(**data) == {"finding": reason}


def test_new_empty_audit_does_not_erase_undisposed_findings() -> None:
    data = evidence()
    data["records"] = [item for item in data["records"] if item["record_kind"] != "finding_disposition"]
    data["latest_results"] = [data["latest_results"][-1]]
    data["latest_results"][0]["payload"]["recheck_finding_ids"] = []
    assert pending_content_findings(**data) == {"finding": "disposition_missing"}
