from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from readpaper.canonical import digest_text
from readpaper.grounding import validate_answer_grounding
from readpaper.locators import ImageRegionLocator, PdfObjectLocator, TextSpanLocator, validate_locator_confirmation
from readpaper.observer import DesktopObserver
from readpaper.parse_invocation import parse_argv
from test_t9_hooks import prepared_run, quote


def evidence() -> dict:
    text = "Canonical paper evidence."
    locator = TextSpanLocator(bundle_id="bundle", artifact_ref_id="ref", artifact_id="artifact",
                              pdf_page=1, char_start=0, char_end=len(text), content_sha256=digest_text(text))
    final = {"record_id": "final", "record_kind": "explanation_finalized", "payload": {
        "answer_id": "answer", "response_attempt_id": "attempt", "final_content": text,
        "final_content_sha256": digest_text(text), "paper_claims": [{"claim_id": "claim", "char_start": 0,
        "char_end": len(text), "claim_text_sha256": digest_text(text)}],
    }}
    confirmation = {"record_id": "confirmation", "record_kind": "locator_confirmation",
                    "payload": {"locator_id": locator.locator_id, "locator": locator.model_dump()}}
    grounding = {"record_id": "grounding", "record_kind": "answer_grounding", "payload": {
        "answer_id": "answer", "response_attempt_id": "attempt", "finalization_record_id": "final",
        "final_content_sha256": digest_text(text), "claim_bindings": [{"claim_id": "claim", "claim_text_sha256": digest_text(text),
        "confirmed_locator_ids": [locator.locator_id], "source_reopen_event_ids": ["reopen"]}],
    }}
    def event(key, seq, kind, payload):
        return {"event_id": key, "event_seq": seq, "event_kind": kind, "actor": "root_main", "result": "succeeded",
                "agent_execution_id": "execution", "context_stream_id": "main", "context_epoch": 1, "payload": payload}
    events = [event("start", 1, "answer_started", {"answer_id": "answer", "response_attempt_id": "attempt"}),
              event("confirm", 2, "locator_confirmed", {"record_id": "confirmation"}),
              event("finalize", 3, "explanation_finalized", {"record_id": "final"}),
              event("reopen", 4, "source_frame_emitted", {"content_sha256": "a" * 64, "answer_id": "answer", "response_attempt_id": "attempt"}) | {"subject_id": "frame"},
              event("ground", 5, "answer_grounded", {"record_id": "grounding"})]
    inventory = {"bundle_id": "bundle", "required_artifact_ref_ids": ["ref"],
                 "pages": [{"artifact_ref_id": "ref", "artifact_id": "artifact", "pdf_page": 1, "text": text}],
                 "visual_units": [], "frames": [{"frame_id": "frame", "content_sha256": "a" * 64, "source_ranges": [{
                     "artifact_ref_id": "ref", "artifact_id": "artifact", "pdf_page": 1, "char_start": 0, "char_end": len(text)}]}]}
    return {"grounding_record": grounding, "finalization_record": final, "answer": {
        "answer_id": "answer", "current_response_attempt_id": "attempt", "attempts": {"attempt": {"root_main_agent_execution_id": "execution"}}},
        "records": [confirmation, final, grounding], "events": events, "inventory": inventory,
        "current_context_stream_id": "main", "current_context_epoch": 1}


@pytest.mark.parametrize("mutation,blocker", [
    ("empty", "answer_grounding_empty"), ("no_locator", "answer_grounding_payload_invalid"),
    ("unconfirmed", "answer_grounding_locator_unconfirmed:"), ("invalid_locator", "answer_grounding_locator_invalid:"),
    ("before_begin", "answer_grounding_reopen_wrong_attempt:"), ("other_attempt", "answer_grounding_reopen_wrong_attempt:"),
    ("other_execution", "answer_grounding_reopen_wrong_attempt:"), ("old_epoch", "answer_grounding_reopen_context_mismatch:"),
    ("after_grounding", "answer_grounding_reopen_wrong_attempt:"), ("wrong_frame", "answer_grounding_reopen_does_not_cover:"),
    ("other_finalization", "answer_grounding_finalization_mismatch"), ("claim_hash", "answer_grounding_claim_mismatch:"),
    ("claim_omitted", "answer_grounding_claim_mismatch"), ("final_text_hash", "answer_grounding_finalization_invalid"),
    ("reviewer_reopen", "answer_grounding_reopen_invalid:"),
])
def test_invalid_grounding_cannot_pass(mutation: str, blocker: str) -> None:
    data = evidence()
    payload = data["grounding_record"]["payload"]
    claim = payload["claim_bindings"][0]
    reopen = data["events"][3]
    if mutation == "empty":
        payload["claim_bindings"] = []
    elif mutation == "no_locator":
        claim["confirmed_locator_ids"] = []
    elif mutation == "unconfirmed":
        data["records"].pop(0)
    elif mutation == "invalid_locator":
        data["inventory"]["pages"][0]["text"] = "wrong canonical text"
    elif mutation == "before_begin":
        reopen["event_seq"] = 0
    elif mutation == "other_attempt":
        reopen["payload"]["response_attempt_id"] = "previous-attempt"
    elif mutation == "other_execution":
        reopen["agent_execution_id"] = "other-main"
    elif mutation == "old_epoch":
        reopen["context_epoch"] = 0
    elif mutation == "after_grounding":
        reopen["event_seq"] = 6
    elif mutation == "wrong_frame":
        data["inventory"]["frames"][0]["source_ranges"][0]["char_end"] = 1
    elif mutation == "other_finalization":
        payload["finalization_record_id"] = "another-record-with-the-same-hash"
    elif mutation == "claim_hash":
        claim["claim_text_sha256"] = "b" * 64
    elif mutation == "claim_omitted":
        final_claim = data["finalization_record"]["payload"]["paper_claims"][0]
        data["finalization_record"]["payload"]["paper_claims"].append(final_claim | {"claim_id": "second-claim"})
    elif mutation == "final_text_hash":
        data["finalization_record"]["payload"]["final_content"] += " changed"
    elif mutation == "reviewer_reopen":
        reopen["actor"] = "subagent"
    validation = validate_answer_grounding(**data)
    assert not validation.valid
    assert any(item.startswith(blocker) for item in validation.blocker_ids)


def test_grounding_accepts_contiguous_cross_frame_span_but_not_a_gap() -> None:
    data = evidence()
    assert validate_answer_grounding(**data).valid
    frame = data["inventory"]["frames"][0]
    second = deepcopy(frame)
    second["frame_id"] = "frame-2"
    frame["source_ranges"][0]["char_end"] = 10
    second["source_ranges"][0]["char_start"] = 10
    data["inventory"]["frames"].append(second)
    data["events"].append(data["events"][3] | {"event_id": "reopen-2", "subject_id": "frame-2"})
    data["grounding_record"]["payload"]["claim_bindings"][0]["source_reopen_event_ids"].append("reopen-2")
    assert validate_answer_grounding(**data).valid
    second["source_ranges"][0]["char_start"] = 11
    assert not validate_answer_grounding(**data).valid


def test_later_confirmation_reobservation_preserves_earlier_grounding() -> None:
    data = evidence()
    data["events"].append(data["events"][1] | {"event_id": "confirmation-again", "event_seq": 100})
    assert validate_answer_grounding(**data).valid


@pytest.mark.parametrize("image", [False, True])
def test_visual_locators_require_matching_image_open(image: bool) -> None:
    data = evidence()
    ref, artifact = "ref", "a_" + "d" * 64
    common = {"bundle_id": "bundle", "artifact_ref_id": ref, "artifact_id": artifact, "bbox_ppm": (0, 0, 500000, 500000)}
    locator = (ImageRegionLocator(**common, image_sha256="d" * 64) if image else
               PdfObjectLocator(**common, pdf_page=1, object_kind="equation"))
    data["inventory"]["pages"][0]["artifact_id"] = artifact
    data["inventory"]["visual_units"] = [{"unit_id": "visual", "artifact_ref_id": ref, "artifact_id": artifact,
                                           "media_kind": "image" if image else "pdf", "pdf_page": None if image else 1}]
    data["records"][0]["payload"] = {"locator_id": locator.locator_id, "locator": locator.model_dump()}
    data["grounding_record"]["payload"]["claim_bindings"][0]["confirmed_locator_ids"] = [locator.locator_id]
    assert not validate_answer_grounding(**data).valid  # A text frame cannot cover an image/object.
    data["events"][3].update({"event_kind": "visual_open_observed", "subject_id": "visual"})
    assert validate_answer_grounding(**data).valid
    data["events"][3]["subject_id"] = "unrelated-visual"
    assert not validate_answer_grounding(**data).valid


@pytest.mark.parametrize("mutation", ["bounds", "hash", "artifact", "bundle", "scope"])
def test_locator_confirmation_rejects_noncanonical_source(mutation: str) -> None:
    data = evidence()
    payload = data["records"][0]["payload"]
    locator = payload["locator"]
    if mutation == "bounds":
        locator["char_end"] = 999
    elif mutation == "hash":
        locator["content_sha256"] = "c" * 64
    elif mutation == "artifact":
        locator["artifact_id"] = "other"
    elif mutation == "bundle":
        locator["bundle_id"] = "other"
    else:
        data["inventory"]["required_artifact_ref_ids"] = []
    payload["locator_id"] = TextSpanLocator.model_validate(locator).locator_id
    with pytest.raises(ValueError):
        validate_locator_confirmation(payload, data["inventory"])


def test_protected_command_grounding_rejects_bypass_and_finalizes_valid_chain(tmp_path: Path, monkeypatch) -> None:
    runtime, prepared, frame_id, visual_id = prepared_run(tmp_path)
    observer = DesktopObserver(tmp_path)
    run_id, paper_id = prepared["run_id"], prepared["paper_id"]
    count = 100
    turn_id = "turn-0"
    def command(argv: list[str]) -> dict:
        nonlocal count
        count += 1
        words = [str(tmp_path / ".venv/bin/python"), str(tmp_path / ".agents/skills/readpaper/scripts/paper.py"),
                 *argv, "--client-request-id", "cr_" + f"{count:032x}"]
        base = {"session_id": "session", "turn_id": turn_id, "cwd": str(tmp_path), "tool_name": "Bash",
                "tool_use_id": f"tool-{count}", "tool_input": {"command": quote(words)}}
        assert json.loads(observer.pre_tool({"hook_event_name": "PreToolUse", **base}))["hookSpecificOutput"]["permissionDecision"] == "allow"
        result = runtime.execute(parse_argv(words[2:])).decode()
        observer.post_tool({"hook_event_name": "PostToolUse", **base, "tool_response": {"output": result}})
        return json.loads(result)
    def record(kind: str, payload: dict) -> dict:
        path = tmp_path / "payloads" / f"grounding-{count + 1}.json"
        path.write_text(json.dumps(payload))
        return command(["record", run_id, "--kind", kind, "--payload", str(path)])
    def checked() -> dict:
        return json.loads(runtime.execute(parse_argv(["check", run_id, "--answer-id", answer_id])))["data"]
    command(["read", run_id, "--frame-id", frame_id])
    before_begin = [json.loads(line) for line in runtime.state.layout.run_events(paper_id, run_id).read_text().splitlines()
                    if json.loads(line)["event_kind"] == "source_frame_emitted"][-1]["event_id"]
    rendered = command(["render", run_id, "--unit-id", visual_id])
    observer.post_tool({"hook_event_name": "PostToolUse", "session_id": "session", "turn_id": "turn-0", "cwd": str(tmp_path),
                        "tool_name": "view_image", "tool_use_id": "visual", "tool_input": {"path": rendered["data"]["path"]}, "tool_response": {}})
    record("understanding_note", {"version_id": "note-v1", "content_sha256": "a" * 64})
    # Isolate answer-grounding enforcement from the separately tested reviewer protocol.
    monkeypatch.setattr("readpaper.commands.content_audit_stage_returned", lambda *args, **kwargs: True)
    assert command(["run", run_id, "--finalize-reading", "--task-id", "task", "--user-turn-id", "turn-0"])["ok"]
    begun = command(["answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", "turn-0"])
    answer_id, attempt_id = begun["data"]["answer_id"], begun["data"]["response_attempt_id"]
    final_payload = evidence()["finalization_record"]["payload"] | {"answer_id": answer_id, "response_attempt_id": attempt_id}
    assert not record("answer_grounding", {})["ok"]
    minimal = {key: final_payload[key] for key in ("answer_id", "response_attempt_id", "final_content_sha256")}
    assert not record("explanation_finalized", minimal)["ok"]
    assert record("answer_grounding", minimal)["ok"] is False
    finalized = record("explanation_finalized", final_payload)
    assert finalized["ok"]
    inventory = json.loads(Path(prepared["data"]["inventory_path"]).read_text())
    page = inventory["pages"][0]
    locator = TextSpanLocator(bundle_id=prepared["bundle_id"], artifact_ref_id=page["artifact_ref_id"], artifact_id=page["artifact_id"],
                              pdf_page=1, char_start=0, char_end=len(page["text"]), content_sha256=digest_text(page["text"]))
    invalid = locator.model_copy(update={"content_sha256": "f" * 64})
    assert record("locator_confirmation", {"locator_id": invalid.locator_id, "locator": invalid.model_dump()})["error"]["code"] == "INVALID_LOCATOR"
    assert record("locator_confirmation", {"locator_id": locator.locator_id, "locator": locator.model_dump()})["ok"]
    grounding = evidence()["grounding_record"]["payload"] | minimal | {"finalization_record_id": finalized["data"]["record_id"]}
    grounding["claim_bindings"][0].update({"confirmed_locator_ids": [locator.locator_id], "source_reopen_event_ids": [before_begin]})
    assert not record("answer_grounding", grounding)["ok"]
    # Old persisted minimal records must not bypass check even though new admission rejects them.
    runtime.state.put_versioned_record(paper_id=paper_id, run_id=run_id, record_kind="answer_grounding", entity_id="legacy", payload=minimal)
    assert "answer_grounding_empty" in checked()["blocking_ids"]
    assert not command(["answer", run_id, "--finalize", "--answer-id", answer_id, "--task-id", "task", "--user-turn-id", "turn-0"])["ok"]
    command(["read", run_id, "--frame-id", frame_id])
    events = [json.loads(line) for line in runtime.state.layout.run_events(paper_id, run_id).read_text().splitlines()]
    reopen = [item for item in events if item["event_kind"] == "source_frame_emitted"][-1]
    assert reopen["payload"]["response_attempt_id"] == attempt_id
    grounding["claim_bindings"][0]["source_reopen_event_ids"] = [reopen["event_id"]]
    grounded = record("answer_grounding", grounding)
    assert grounded["ok"], grounded
    assert checked()["decision"] == "ready_to_finalize_content"
    # A newer finalization with the SAME content hash still requires its own grounding.
    replacement = record("explanation_finalized", final_payload | {"entity_id": "replacement"})
    assert "answer_grounding_finalization_mismatch" in checked()["blocking_ids"]
    grounding["finalization_record_id"] = replacement["data"]["record_id"]
    assert record("answer_grounding", grounding)["ok"]
    result = command(["answer", run_id, "--finalize", "--answer-id", answer_id, "--task-id", "task", "--user-turn-id", "turn-0"])
    assert result["ok"], result
    stored = runtime.state.get_run(paper_id, run_id).answers[answer_id]
    assert stored["finalization_record_id"] == replacement["data"]["record_id"]
    assert stored["grounding_record_id"] == checked()["grounding_record_id"]
    assert checked()["decision"] == "allow"

    turn_id = "turn-followup"
    observer.user_prompt({"hook_event_name": "UserPromptSubmit", "session_id": "session", "turn_id": turn_id,
                          "cwd": str(tmp_path), "prompt": "Explain this result", "task_id": "task"})
    begun = command(["answer", run_id, "--begin", "--task-id", "task", "--user-turn-id", turn_id])
    answer_id, attempt_id = begun["data"]["answer_id"], begun["data"]["response_attempt_id"]
    final_payload.update({"answer_id": answer_id, "response_attempt_id": attempt_id})
    final = record("explanation_finalized", final_payload)
    grounding.update({"answer_id": answer_id, "response_attempt_id": attempt_id, "finalization_record_id": final["data"]["record_id"]})
    assert not record("answer_grounding", grounding)["ok"]  # Previous answer's reopen is not inherited.

    def reground() -> None:
        assert command(["read", run_id, "--frame-id", frame_id])["ok"]
        events = [json.loads(line) for line in runtime.state.layout.run_events(paper_id, run_id).read_text().splitlines()]
        grounding["claim_bindings"][0]["source_reopen_event_ids"] = [[item for item in events if item["event_kind"] == "source_frame_emitted"][-1]["event_id"]]
        assert record("answer_grounding", grounding)["ok"]
        assert checked()["decision"] == "ready_to_finalize_content"
    reground()
    compact = {"session_id": "session", "cwd": str(tmp_path), "trigger": "auto", "task_id": "task"}
    def compact_main() -> None:
        observer.compact({"hook_event_name": "PreCompact", **compact})
        observer.compact({"hook_event_name": "PostCompact", **compact})
    compact_main()
    assert not checked()["initial_answer_context_required"]
    assert "answer_grounding_context_mismatch" in checked()["blocking_ids"]
    reground()  # Follow-up needs only claim-relevant source, not all visuals again.
    original_finalize = runtime.state.finalize_answer_content
    def compact_between_check_and_commit(**kwargs):
        compact_main()
        return original_finalize(**kwargs)
    with monkeypatch.context() as race:
        race.setattr(runtime.state, "finalize_answer_content", compact_between_check_and_commit)
        failed = command(["answer", run_id, "--finalize", "--answer-id", answer_id, "--task-id", "task", "--user-turn-id", turn_id])
        assert failed["error"]["code"] == "STATE_CONFLICT"
    reground()
    assert command(["answer", run_id, "--finalize", "--answer-id", answer_id, "--task-id", "task", "--user-turn-id", turn_id])["ok"]
    compact_main()
    assert checked()["decision"] == "allow"  # Completed proof is historical, not silently re-bound.
