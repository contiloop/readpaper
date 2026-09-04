"""Structural answer-grounding evidence, not a semantic truth verifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

from .canonical import digest_text
from .locators import reopened_sources_cover, validate_locator_confirmation
from .models import StrictModel


SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1)]


class PaperClaim(StrictModel):
    claim_id: Identifier
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    claim_text_sha256: SHA256


class ClaimBinding(StrictModel):
    claim_id: Identifier
    claim_text_sha256: SHA256
    confirmed_locator_ids: list[Identifier] = Field(min_length=1)
    source_reopen_event_ids: list[Identifier] = Field(min_length=1)


class GroundingPayload(StrictModel):
    entity_id: Identifier | None = None
    answer_id: Identifier
    response_attempt_id: Identifier
    finalization_record_id: Identifier
    final_content_sha256: SHA256
    claim_bindings: list[ClaimBinding] = Field(min_length=1)


@dataclass(frozen=True)
class GroundingValidation:
    valid: bool
    blocker_ids: tuple[str, ...]
    confirmed_locator_ids: tuple[str, ...] = ()
    reopen_event_ids: tuple[str, ...] = ()


def finalization_claims(payload: dict[str, Any]) -> dict[str, PaperClaim]:
    """Bind the declared paper-derived claim groups to the exact final text."""
    content = payload.get("final_content")
    if not isinstance(content, str) or not content.strip() or digest_text(content) != payload.get("final_content_sha256"):
        raise ValueError("final content is missing or its hash does not match")
    claims = TypeAdapter(list[PaperClaim]).validate_python(payload.get("paper_claims"))
    if not claims or len({item.claim_id for item in claims}) != len(claims):
        raise ValueError("paper claim IDs must be nonempty and unique")
    for claim in claims:
        text = content[claim.char_start:claim.char_end]
        if not 0 <= claim.char_start < claim.char_end <= len(content) or not text.strip() or digest_text(text) != claim.claim_text_sha256:
            raise ValueError("paper claim span/hash does not match final content")
    return {item.claim_id: item for item in claims}


def _root(event: dict[str, Any] | None, kind: str) -> bool:
    return bool(event and event.get("event_kind") == kind and event.get("actor") == "root_main" and event.get("result") == "succeeded")


def observed_record_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The first successful observation fixes an immutable record's chronology."""
    return {
        item["payload"]["record_id"]: item
        for item in sorted(events, key=lambda value: value["event_seq"], reverse=True)
        if item.get("result") == "succeeded" and isinstance(item.get("payload", {}).get("record_id"), str)
    }


def validate_answer_grounding(
    *, grounding_record: dict[str, Any], finalization_record: dict[str, Any] | None,
    answer: dict[str, Any], records: list[dict[str, Any]], events: list[dict[str, Any]],
    inventory: dict[str, Any], current_context_stream_id: str, current_context_epoch: int,
) -> GroundingValidation:
    blockers: set[str] = set()
    locator_ids: set[str] = set()
    reopen_ids: set[str] = set()
    def result() -> GroundingValidation:
        return GroundingValidation(not blockers, tuple(sorted(blockers)), tuple(sorted(locator_ids)), tuple(sorted(reopen_ids)))
    raw = grounding_record.get("payload", {})
    if grounding_record.get("record_kind") != "answer_grounding":
        blockers.add("answer_grounding_payload_invalid")
        return result()
    if not raw.get("claim_bindings"):
        blockers.add("answer_grounding_empty")
    try:
        payload = GroundingPayload.model_validate(raw)
    except ValidationError:
        blockers.add("answer_grounding_payload_invalid")
        return result()
    answer_id = answer.get("answer_id")
    attempt_id = answer.get("current_response_attempt_id")
    execution = answer.get("attempts", {}).get(attempt_id, {}).get("root_main_agent_execution_id")
    if payload.answer_id != answer_id or payload.response_attempt_id != attempt_id or not execution:
        blockers.add("answer_grounding_attempt_mismatch")
        return result()
    record_events = observed_record_events(events)
    starts = [item for item in events if item.get("event_kind") in {"answer_started", "answer_resumed"}
              and item.get("result") == "succeeded" and item.get("actor") in {"root_main", "state_service"}
              and item.get("payload", {}).get("answer_id") == answer_id
              and item.get("payload", {}).get("response_attempt_id") == attempt_id
              and item.get("agent_execution_id") == execution]
    if len(starts) != 1:
        blockers.add("answer_grounding_attempt_unobserved")
        return result()
    start_seq = starts[0]["event_seq"]
    grounding_event = record_events.get(grounding_record.get("record_id"))
    if not _root(grounding_event, "answer_grounded") or grounding_event.get("agent_execution_id") != execution:
        blockers.add("answer_grounding_unobserved")
        return result()
    grounding_seq = grounding_event["event_seq"]
    if grounding_seq <= start_seq:
        blockers.add("answer_grounding_attempt_mismatch")
    if (grounding_event.get("context_stream_id"), grounding_event.get("context_epoch")) != (current_context_stream_id, current_context_epoch):
        blockers.add("answer_grounding_context_mismatch")
    final = finalization_record or {}
    final_payload = final.get("payload", {})
    final_event = record_events.get(final.get("record_id"))
    if (final.get("record_kind") != "explanation_finalized" or payload.finalization_record_id != final.get("record_id")
        or final_payload.get("answer_id") != answer_id or final_payload.get("response_attempt_id") != attempt_id
        or payload.final_content_sha256 != final_payload.get("final_content_sha256")
        or not _root(final_event, "explanation_finalized") or final_event.get("agent_execution_id") != execution
        or not start_seq < final_event["event_seq"] < grounding_seq):
        blockers.add("answer_grounding_finalization_mismatch")
        return result()
    try:
        claims = finalization_claims(final_payload)
    except (ValueError, TypeError):
        blockers.add("answer_grounding_finalization_invalid")
        return result()
    if len({item.claim_id for item in payload.claim_bindings}) != len(payload.claim_bindings) or set(claims) != {item.claim_id for item in payload.claim_bindings}:
        blockers.add("answer_grounding_claim_mismatch")
    by_event = {item["event_id"]: item for item in events}
    for claim in payload.claim_bindings:
        if claim.claim_id not in claims or claims[claim.claim_id].claim_text_sha256 != claim.claim_text_sha256:
            blockers.add(f"answer_grounding_claim_mismatch:{claim.claim_id}")
        fresh = []
        for event_id in claim.source_reopen_event_ids:
            reopen_ids.add(event_id)
            event = by_event.get(event_id)
            if not (_root(event, "source_frame_emitted") or _root(event, "visual_open_observed")):
                blockers.add(f"answer_grounding_reopen_invalid:{event_id}")
                continue
            if (event.get("agent_execution_id") != execution or not start_seq < event["event_seq"] < grounding_seq
                or event.get("payload", {}).get("answer_id") != answer_id
                or event.get("payload", {}).get("response_attempt_id") != attempt_id):
                blockers.add(f"answer_grounding_reopen_wrong_attempt:{event_id}")
                continue
            if (event.get("context_stream_id"), event.get("context_epoch")) != (current_context_stream_id, current_context_epoch):
                blockers.add(f"answer_grounding_reopen_context_mismatch:{event_id}")
                continue
            fresh.append(event)
        for locator_id in claim.confirmed_locator_ids:
            locator_ids.add(locator_id)
            confirmations = [item for item in records if item.get("record_kind") == "locator_confirmation"
                             and item.get("payload", {}).get("locator_id") == locator_id
                             and _root(record_events.get(item["record_id"]), "locator_confirmed")
                             and record_events[item["record_id"]]["event_seq"] < grounding_seq]
            if not confirmations:
                blockers.add(f"answer_grounding_locator_unconfirmed:{locator_id}")
                continue
            confirmed = []
            for record in confirmations:
                try:
                    confirmed.append(validate_locator_confirmation(record["payload"], inventory))
                except (ValueError, KeyError, TypeError):
                    continue
            if not confirmed:
                blockers.add(f"answer_grounding_locator_invalid:{locator_id}")
            elif not fresh:
                blockers.add(f"answer_grounding_reopen_missing:{locator_id}")
            elif not any(reopened_sources_cover(locator, fresh, inventory) for locator in confirmed):
                blockers.add(f"answer_grounding_reopen_does_not_cover:{locator_id}")
    return result()
