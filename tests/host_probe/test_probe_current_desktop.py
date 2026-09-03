from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from probe_current_desktop import (
    CODEX,
    HOOK_EVENT_NAMES,
    SEMANTIC_IDENTITY_FIELD_CONTRACTS,
    SEMANTIC_IDENTITY_PRESENT_FIELDS,
    SEMANTIC_IDENTITY_REQUIRED_FIELDS,
    _extract_json_after_label,
    _find_titled_object,
    _path_state,
    collect,
    extract_command_input_schemas,
    semantic_identity_summary,
)
from run_verification import _run as run_verification_command


def test_embedded_schema_extractor_uses_the_named_object() -> None:
    source = (
        'subagent-stop.command.input{"properties":{"hook_event_name":'
        '{"const":"SubagentStop"}},"required":[]}'
        'stop.command.input{"properties":{"hook_event_name":{"const":"Stop"},'
        '"turn_id":{}},"required":[]}'
    )

    assert _extract_json_after_label(
        source,
        "stop.command.input",
        expected_hook_event_name="Stop",
    ) == {
        "properties": {
            "hook_event_name": {"const": "Stop"},
            "turn_id": {},
        },
        "required": [],
    }


@pytest.mark.skipif(not CODEX.is_file(), reason="Codex Desktop is not installed")
def test_current_desktop_command_hook_schemas_are_machine_readable() -> None:
    schemas = extract_command_input_schemas(CODEX)

    assert set(schemas) == {
        "session_start",
        "user_prompt_submit",
        "subagent_start",
        "subagent_stop",
        "pre_tool_use",
        "post_tool_use",
        "pre_compact",
        "post_compact",
        "stop",
    }
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert {
        event: schema["properties"]["hook_event_name"]["const"]
        for event, schema in schemas.items()
    } == HOOK_EVENT_NAMES
    assert schemas["stop"]["properties"]["hook_event_name"]["const"] == "Stop"
    assert "agent_id" not in schemas["stop"]["properties"]
    assert schemas["stop"]["properties"]["stop_hook_active"]["type"] == "boolean"
    assert all("session_id" in schema["properties"] for schema in schemas.values())
    assert semantic_identity_summary(schemas)["all_events_ready"] is True


def test_semantic_identity_summary_reports_missing_required_input() -> None:
    schemas = {
        event: {
            "properties": {
                field: deepcopy(SEMANTIC_IDENTITY_FIELD_CONTRACTS[field])
                for field in (
                    required_fields
                    | SEMANTIC_IDENTITY_PRESENT_FIELDS.get(event, set())
                )
            },
            "required": sorted(required_fields),
        }
        for event, required_fields in SEMANTIC_IDENTITY_REQUIRED_FIELDS.items()
    }
    schemas["stop"]["required"].remove("turn_id")

    summary = semantic_identity_summary(schemas)

    assert summary["all_events_ready"] is False
    assert summary["ready_by_event"]["stop"] is False
    assert summary["missing_required_fields_by_event"]["stop"] == ["turn_id"]


def test_semantic_identity_summary_rejects_wrong_type_and_missing_actor_field() -> None:
    schemas = {
        event: {
            "properties": {
                field: deepcopy(SEMANTIC_IDENTITY_FIELD_CONTRACTS[field])
                for field in (
                    required_fields
                    | SEMANTIC_IDENTITY_PRESENT_FIELDS.get(event, set())
                )
            },
            "required": sorted(required_fields),
        }
        for event, required_fields in SEMANTIC_IDENTITY_REQUIRED_FIELDS.items()
    }
    schemas["stop"]["properties"]["stop_hook_active"] = {"type": "string"}
    del schemas["pre_compact"]["properties"]["agent_id"]

    summary = semantic_identity_summary(schemas)

    assert summary["all_events_ready"] is False
    assert summary["mismatched_field_contracts_by_event"]["stop"][
        "stop_hook_active"
    ]["expected"] == {"type": "boolean"}
    assert summary["missing_presence_only_fields_by_event"]["pre_compact"] == [
        "agent_id"
    ]


def test_titled_object_search_finds_nested_schema() -> None:
    schema = {"oneOf": [{"title": "Target", "properties": {"id": {}}}]}

    assert _find_titled_object(schema, "Target") == {
        "title": "Target",
        "properties": {"id": {}},
    }


@pytest.mark.skipif(not CODEX.is_file(), reason="Codex Desktop is not installed")
def test_probe_output_contains_metadata_not_conversation_values() -> None:
    report = collect()
    keys: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(report)
    assert {
        "prompt",
        "last_assistant_message",
        "tool_input",
        "tool_response",
        "process_command",
        "rollout_path",
    }.isdisjoint(keys)
    assert report["status"] == "requires_live_desktop_probe"
    assert report["blockers"] == []
    assert report["checks"]["semantic_identity_inputs_ready"] is True
    assert report["live_probe"]["same_main_stop_continuation_proven"] is False
    assert report["live_probe"]["host_prompt_exactly_once_claimed"] is False
    assert all(
        boundary["release_blocker"] is False
        for boundary in report["capability_boundaries"]
    )


def test_absent_path_state_is_explicit() -> None:
    state = _path_state(Path("this-g0-probe-path-must-not-exist"))

    assert state == {
        "path": "this-g0-probe-path-must-not-exist",
        "exists": False,
        "mode": None,
        "sha256": None,
    }


def test_verification_record_does_not_persist_argv_or_output_text() -> None:
    secret = "must-not-be-recorded"

    result = run_verification_command(
        "privacy_regression",
        [sys.executable, "-c", f"print({secret!r})"],
        0,
    )

    assert result["matched_expectation"] is True
    assert {"argv", "stdout_tail", "stderr_tail"}.isdisjoint(result)
    assert secret not in json.dumps(result)
