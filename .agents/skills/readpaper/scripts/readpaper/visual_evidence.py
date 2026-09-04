"""Bind visual observations to the exact protected render that produced the file."""

from __future__ import annotations

import re
from typing import Any


def matching_render(
    events: list[dict[str, Any]], *, path_sha256: str, image_sha256: str,
    before_seq: int | None = None,
) -> dict[str, Any] | None:
    """Use the latest successful render at this exact path, never a filename guess."""
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (path_sha256, image_sha256)):
        return None
    candidates = [event for event in events if (
        event.get("event_kind") == "render_created" and event.get("result") == "succeeded"
        and event.get("actor") == "state_service"
        and event.get("payload", {}).get("path_sha256") == path_sha256
        and (before_seq is None or event["event_seq"] < before_seq)
    )]
    render = max(candidates, key=lambda event: event["event_seq"], default=None)
    if render is None or render["payload"].get("image_sha256") != image_sha256:
        return None
    return render


def validated_visual_unit(
    event: dict[str, Any], all_events: list[dict[str, Any]], inventory: dict[str, Any],
) -> dict[str, Any] | None:
    """Recheck the render/open chain for coverage, grounding and finding disposition."""
    if (event.get("event_kind") != "visual_open_observed" or event.get("result") != "succeeded"
        or event.get("actor") != "root_main"):
        return None
    payload = event.get("payload", {})
    render = matching_render(
        all_events, path_sha256=payload.get("path_sha256"), image_sha256=payload.get("image_sha256"),
        before_seq=event["event_seq"],
    )
    if (render is None or payload.get("render_id") != render.get("subject_id")
        or payload.get("render_event_id") != render.get("event_id")
        or event.get("subject_id") != render["payload"].get("unit_id")
        or any(event.get(key) != render.get(key) for key in ("paper_id", "run_id", "bundle_id"))):
        return None
    return next((unit for unit in inventory["visual_units"] if unit["unit_id"] == event["subject_id"]), None)
