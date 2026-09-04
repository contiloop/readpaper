from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from readpaper.canonical import digest_text
from readpaper.ids import sequence_id
from readpaper.models import Actor, EventKind, EventResult
from readpaper.observer import DesktopObserver
from readpaper.parse_invocation import parse_argv
from readpaper.visual_evidence import validated_visual_unit
from test_t9_hooks import execute_authorized, prepared_run


@pytest.mark.parametrize("case", ["original", "replaced", "fake_name", "copied", "missing", "error", "reviewer", "opaque_name"])
def test_observer_requires_exact_rendered_file(tmp_path: Path, monkeypatch, case: str) -> None:
    runtime, prepared, _, unit_id = prepared_run(tmp_path)
    if case == "opaque_name":
        from readpaper.commands import render_pdf_page
        def render_with_opaque_name(source, *, output, **kwargs):
            return render_pdf_page(source, output=output.with_name("opaque.png"), **kwargs)
        monkeypatch.setattr("readpaper.commands.render_pdf_page", render_with_opaque_name)
    rendered = execute_authorized(runtime, ["render", prepared["run_id"], "--unit-id", unit_id,
                                  "--client-request-id", "cr_" + "4" * 32], "render")
    assert rendered["ok"], rendered
    path = Path(rendered["data"]["path"])
    if case == "replaced":
        Image.new("RGB", (8, 8), "red").save(path)
    elif case in {"fake_name", "copied"}:
        copy = path.with_name(f"{unit_id}-fake.png")
        if case == "copied":
            copy.write_bytes(path.read_bytes())
        else:
            Image.new("RGB", (8, 8), "red").save(copy)
        path = copy
    elif case == "missing":
        path.unlink()
    payload = {"hook_event_name": "PostToolUse", "session_id": "session", "turn_id": "turn-0", "cwd": str(tmp_path),
               "tool_name": "view_image", "tool_use_id": "open", "tool_input": {"path": str(path)},
               "tool_response": {"isError": True} if case == "error" else {}}
    if case == "reviewer":
        payload["agent_id"] = "reviewer"
    observer = DesktopObserver(tmp_path)
    observer.post_tool(payload)
    events = [json.loads(line) for line in runtime.state.layout.run_events(prepared["paper_id"], prepared["run_id"]).read_text().splitlines()]
    opens = [event for event in events if event["event_kind"] == "visual_open_observed"]
    checked = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))["data"]
    accepted = case in {"original", "opaque_name"}
    assert len(opens) == int(accepted)
    for key in ("missing_historical_visual_unit_ids", "missing_resident_visual_unit_ids"):
        assert (unit_id not in checked[key]) == accepted
    if accepted:
        opened = opens[0]
        render = next(event for event in events if event["event_kind"] == "render_created")
        assert opened["subject_id"] == unit_id
        assert opened["payload"]["render_id"] == rendered["data"]["render_id"] == render["subject_id"]
        assert opened["payload"]["render_event_id"] == render["event_id"]
        assert opened["payload"]["path_sha256"] == render["payload"]["path_sha256"] == digest_text(str(path.resolve()))
        assert opened["payload"]["image_sha256"] == render["payload"]["image_sha256"] == rendered["data"]["image_sha256"]
        # Replayed hook delivery is idempotent.
        observer.post_tool(payload)


def test_check_rejects_legacy_visual_observation_without_render_binding(tmp_path: Path) -> None:
    runtime, prepared, _, unit_id = prepared_run(tmp_path)
    runtime.state.append_event(
        paper_id=prepared["paper_id"], run_id=prepared["run_id"], event_kind=EventKind.VISUAL_OPEN_OBSERVED,
        subject_id=unit_id, actor=Actor.ROOT_MAIN, result=EventResult.SUCCEEDED,
        payload={"path_sha256": "a" * 64, "image_sha256": "b" * 64},
        idempotency_key="legacy-open", session_id="session", turn_id="turn-0",
        agent_execution_id=sequence_id("ae", "task", "session", "turn-0", "root"),
        context_stream_id=sequence_id("ctx", "task", "session", "root"), context_epoch=0,
    )
    checked = json.loads(runtime.execute(parse_argv(["check", prepared["run_id"]])))["data"]
    assert unit_id in checked["missing_historical_visual_unit_ids"]
    assert unit_id in checked["missing_resident_visual_unit_ids"]


@pytest.mark.parametrize("mutation", ["image", "path", "unit", "render_id", "event_id", "late", "actor", "failed", "run", "missing"])
def test_persisted_visual_chain_is_revalidated(mutation: str) -> None:
    render = {"event_id": "render-event", "event_seq": 1, "event_kind": "render_created", "subject_id": "render",
              "actor": "state_service", "result": "succeeded", "run_id": "run", "payload": {
                  "unit_id": "visual", "path_sha256": "a" * 64, "image_sha256": "b" * 64}}
    opened = {"event_id": "open", "event_seq": 2, "event_kind": "visual_open_observed", "subject_id": "visual",
              "actor": "root_main", "result": "succeeded", "run_id": "run", "payload": {
                  "path_sha256": "a" * 64, "image_sha256": "b" * 64, "render_id": "render", "render_event_id": "render-event"}}
    inventory = {"visual_units": [{"unit_id": "visual"}]}
    events = [render, opened]
    assert validated_visual_unit(opened, events, inventory) is not None
    if mutation in {"image", "path", "unit"}:
        render["payload"][{"image": "image_sha256", "path": "path_sha256", "unit": "unit_id"}[mutation]] = "c" * 64
    elif mutation in {"render_id", "event_id"}:
        render["subject_id" if mutation == "render_id" else "event_id"] = "other"
    elif mutation == "late":
        render["event_seq"] = 3
    elif mutation == "actor":
        render["actor"] = "root_main"
    elif mutation == "failed":
        render["result"] = "failed"
    elif mutation == "run":
        render["run_id"] = "other"
    else:
        events.remove(render)
    assert validated_visual_unit(opened, events, inventory) is None


def test_rerender_does_not_invalidate_historical_open_or_allow_old_file_restore() -> None:
    render = {"event_id": "r1", "event_seq": 1, "event_kind": "render_created", "subject_id": "render-1",
              "actor": "state_service", "result": "succeeded",
              "payload": {"unit_id": "visual", "path_sha256": "a" * 64, "image_sha256": "b" * 64}}
    opened = {"event_id": "o1", "event_seq": 2, "event_kind": "visual_open_observed", "subject_id": "visual",
              "actor": "root_main", "result": "succeeded", "payload": {
                  "path_sha256": "a" * 64, "image_sha256": "b" * 64, "render_id": "render-1", "render_event_id": "r1"}}
    rerender = render | {"event_id": "r2", "event_seq": 3, "subject_id": "render-2",
                         "payload": render["payload"] | {"image_sha256": "c" * 64}}
    inventory = {"visual_units": [{"unit_id": "visual"}]}
    events = [render, opened, rerender]
    assert validated_visual_unit(opened, events, inventory) is not None
    restored_open = opened | {"event_id": "o2", "event_seq": 4}
    assert validated_visual_unit(restored_open, events, inventory) is None
