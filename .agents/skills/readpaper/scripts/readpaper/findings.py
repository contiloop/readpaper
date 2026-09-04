"""Fail-closed validation of Main's responses to content-audit findings."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from .locators import reopened_sources_cover, validate_locator_confirmation


REMEDIATING = {"accepted", "partially_accepted", "modified"}


def _root(event: dict[str, Any] | None, kind: str) -> bool:
    return bool(event and event.get("actor") == "root_main" and event.get("result") == "succeeded" and event.get("event_kind") == kind)


def pending_content_findings(
    *, records: list[dict[str, Any]], events: list[dict[str, Any]],
    inventory: dict[str, Any], latest_results: list[dict[str, Any]],
    trusted_results: list[dict[str, Any]],
) -> dict[str, str]:
    """Return each active finding that lacks a source-grounded, verified disposition."""
    by_id = {item["record_id"]: item for item in records}
    record_events = {item.get("payload", {}).get("record_id"): item for item in events if item.get("result") == "succeeded"}
    by_event = {item["event_id"]: item for item in events}
    origins = {
        finding["finding_id"]: (finding, record)
        for record in records if record["record_kind"] == "audit_result"
        for finding in record["payload"].get("findings", [])
    }
    # A later empty audit must not silently erase an earlier unresolved finding.
    active = set(origins) | {
        finding_id
        for record in latest_results for finding_id in record["payload"].get("recheck_finding_ids", [])
    }
    dispositions: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: int(record_events.get(item["record_id"], {}).get("event_seq", 0))):
        if record["record_kind"] == "finding_disposition":
            dispositions[str(record["payload"].get("finding_id"))] = record
    pending = {}
    for finding_id in sorted(active):
        origin = origins.get(finding_id)
        disposition = dispositions.get(finding_id)
        if origin is None or disposition is None:
            pending[finding_id] = "disposition_missing"
            continue
        try:
            reason = _validate_disposition(
                finding=origin[0], result=origin[1], disposition=disposition,
                by_id=by_id, record_events=record_events, by_event=by_event,
                inventory=inventory, trusted_results=trusted_results,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            reason = "invalid_disposition_evidence"
        if reason:
            pending[finding_id] = reason
    return pending


def _validate_disposition(
    *, finding: dict[str, Any], result: dict[str, Any], disposition: dict[str, Any],
    by_id: dict[str, dict[str, Any]], record_events: dict[str, dict[str, Any]],
    by_event: dict[str, dict[str, Any]], inventory: dict[str, Any],
    trusted_results: list[dict[str, Any]],
) -> str | None:
    payload = disposition["payload"]
    event = record_events.get(disposition["record_id"])
    origin_event = record_events.get(result["record_id"])
    if not _root(event, "finding_dispositioned") or not origin_event:
        return "unobserved_disposition"
    origin_seq, disposition_seq = int(origin_event["event_seq"]), int(event["event_seq"])
    if disposition_seq <= origin_seq:
        return "disposition_predates_finding"
    outcome = payload.get("disposition")
    if outcome not in REMEDIATING | {"rejected", "interpretive"}:
        return "finding_unresolved"
    if outcome == "interpretive" and finding.get("category") != "interpretive_ambiguity":
        return "noninterpretive_finding_unresolved"
    if not isinstance(payload.get("rationale"), str) or not payload["rationale"].strip():
        return "rationale_missing"
    locator_ids = set(payload.get("confirmed_locator_ids", []))
    if not locator_ids or not set(finding.get("locator_ids", [])) <= locator_ids:
        return "finding_locators_missing"
    confirmations = {}
    for record in by_id.values():
        if record["record_kind"] != "locator_confirmation":
            continue
        confirmation = record_events.get(record["record_id"])
        if not _root(confirmation, "locator_confirmed") or confirmation["event_seq"] >= disposition_seq:
            continue
        if record["payload"].get("locator_id") not in locator_ids:
            continue
        try:
            locator = validate_locator_confirmation(record["payload"], inventory)
        except (ValueError, KeyError, TypeError):
            continue
        if locator.locator_id != record["payload"].get("locator_id") or locator.bundle_id != inventory["bundle_id"]:
            continue
        confirmations[locator.locator_id] = locator
    if not locator_ids <= confirmations.keys():
        return "confirmed_locator_missing"
    reopens = [by_event.get(key, {}) for key in payload.get("source_reopen_event_ids", [])]
    fresh = [item for item in reopens if (
        (_root(item, "source_frame_emitted") or _root(item, "visual_open_observed"))
        and origin_seq < item["event_seq"] < disposition_seq
        and item.get("context_stream_id") == event.get("context_stream_id")
        and item.get("context_epoch") == event.get("context_epoch")
    )]
    if not all(reopened_sources_cover(confirmations[key], fresh, inventory, all_events=list(by_event.values()))
               for key in locator_ids):
        return "post_finding_source_reopen_missing"
    if outcome not in REMEDIATING:
        return None
    remediation_ids = payload.get("remediation_record_ids", [])
    if not remediation_ids:
        return "remediation_missing"
    for record_id in remediation_ids:
        record = by_id.get(record_id, {})
        kind = record.get("record_kind")
        expected_event = {"understanding_note": "note_versioned", "explanation_draft": "draft_versioned"}.get(kind)
        remediation_event = record_events.get(record_id)
        if expected_event is None or not _root(remediation_event, expected_event):
            return "unobserved_remediation"
        if not max(item["event_seq"] for item in fresh) < remediation_event["event_seq"] < disposition_seq:
            return "remediation_not_after_reopen"
        if (remediation_event.get("context_stream_id"), remediation_event.get("context_epoch")) != (event.get("context_stream_id"), event.get("context_epoch")):
            return "remediation_context_mismatch"
        parent = next((item for item in by_id.values() if (
            item["record_kind"] == kind and item["entity_id"] == record["entity_id"]
            and item.get("version_id") == record.get("parent_version_id")
            and item.get("version_id") is not None
        )), None)
        if parent is None:
            return "remediation_is_not_a_descendant"
        parent_event = record_events.get(parent["record_id"])
        if not _root(parent_event, expected_event) or parent_event["event_seq"] >= remediation_event["event_seq"]:
            return "remediation_parent_unobserved"
        audited_version = result["payload"].get("note_version_id") if kind == "understanding_note" else None
        if audited_version:
            ancestor, visited = parent, set()
            while ancestor.get("version_id") != audited_version:
                ancestor_id = ancestor["record_id"]
                if ancestor_id in visited:
                    return "remediation_not_from_audited_version"
                visited.add(ancestor_id)
                ancestor = next((item for item in by_id.values() if (
                    item["record_kind"] == kind and item["entity_id"] == record["entity_id"]
                    and item.get("version_id") == ancestor.get("parent_version_id")
                    and item.get("version_id") is not None
                )), {})
                if not ancestor:
                    return "remediation_not_from_audited_version"
        content_hash = record["payload"].get("content_sha256")
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash) or content_hash == parent["payload"].get("content_sha256"):
            return "remediation_content_unchanged"
        # The same reviewer role must recheck this finding against the child version.
        if not any(
            item["payload"].get("role") == result["payload"].get("role")
            and record_events.get(item["record_id"], {}).get("event_seq", 0) > remediation_event["event_seq"]
            and any(recheck.get("finding_id") == finding["finding_id"] and recheck.get("status") == "resolved"
                    and recheck.get("remediation_record_id") == record_id
                    for recheck in item["payload"].get("recheck_results", []))
            for item in trusted_results
        ):
            return "reviewer_remediation_recheck_missing"
    return None
