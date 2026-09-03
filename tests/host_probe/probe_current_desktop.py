#!/usr/bin/env python3
"""Inspect the installed Codex Desktop host contract without mutating config.

The probe uses the current Desktop bundle's own generated schemas and embedded
command-hook schemas. It deliberately records metadata only: no prompt,
assistant-message, tool payload, credential, or full process command is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
APP = Path("/Applications/ChatGPT.app")
CODEX = APP / "Contents/Resources/codex"

HOOK_SCHEMA_NAMES = {
    "session_start": "session-start",
    "user_prompt_submit": "user-prompt-submit",
    "subagent_start": "subagent-start",
    "subagent_stop": "subagent-stop",
    "pre_tool_use": "pre-tool-use",
    "post_tool_use": "post-tool-use",
    "pre_compact": "pre-compact",
    "post_compact": "post-compact",
    "stop": "stop",
}

HOOK_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "subagent_start": "SubagentStart",
    "subagent_stop": "SubagentStop",
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "pre_compact": "PreCompact",
    "post_compact": "PostCompact",
    "stop": "Stop",
}

HOST_CALLBACK_ID_FIELDS = {
    "callback_id",
    "hook_run_id",
    "invocation_id",
    "observation_id",
    "source_observation_id",
}

ROOT_EXECUTION_ID_FIELDS = {
    "execution_id",
    "agent_execution_id",
    "root_execution_id",
    "root_main_execution_id",
}

CONTEXT_STREAM_ID_FIELDS = {
    "context_stream_id",
    "root_main_context_stream_id",
}

SEMANTIC_IDENTITY_REQUIRED_FIELDS = {
    "session_start": {"session_id", "source", "model"},
    "user_prompt_submit": {"session_id", "turn_id", "prompt", "model"},
    "subagent_start": {
        "session_id",
        "turn_id",
        "agent_id",
        "agent_type",
        "model",
    },
    "subagent_stop": {
        "session_id",
        "turn_id",
        "agent_id",
        "agent_type",
        "stop_hook_active",
        "last_assistant_message",
        "model",
    },
    "pre_tool_use": {
        "session_id",
        "turn_id",
        "tool_use_id",
        "tool_name",
        "model",
    },
    "post_tool_use": {
        "session_id",
        "turn_id",
        "tool_use_id",
        "tool_name",
        "model",
    },
    "pre_compact": {"session_id", "turn_id", "trigger", "model"},
    "post_compact": {"session_id", "turn_id", "trigger", "model"},
    "stop": {
        "session_id",
        "turn_id",
        "stop_hook_active",
        "last_assistant_message",
        "model",
    },
}

# These actor fields are intentionally optional in the host JSON schema because
# root-Main events omit them. Their schema presence is still required so the
# live probe can distinguish a subagent tool/compaction stream from root Main.
SEMANTIC_IDENTITY_PRESENT_FIELDS = {
    "pre_tool_use": {"agent_id", "agent_type"},
    "post_tool_use": {"agent_id", "agent_type"},
    "pre_compact": {"agent_id", "agent_type"},
    "post_compact": {"agent_id", "agent_type"},
}

SEMANTIC_IDENTITY_FIELD_CONTRACTS: dict[str, dict[str, Any]] = {
    "session_id": {"type": "string"},
    "source": {
        "type": "string",
        "enum": ["startup", "resume", "clear", "compact"],
    },
    "turn_id": {"type": "string"},
    "prompt": {"type": "string"},
    "agent_id": {"type": "string"},
    "agent_type": {"type": "string"},
    "tool_use_id": {"type": "string"},
    "tool_name": {"type": "string"},
    "trigger": {"type": "string", "enum": ["manual", "auto"]},
    "stop_hook_active": {"type": "boolean"},
    "last_assistant_message": {"$ref": "#/definitions/NullableString"},
    "model": {"type": "string"},
}

G0_PROTECTED_PATHS = (
    Path(".codex/hooks.json"),
    Path(".agents/skills/readpaper/scripts/paper.py"),
    Path(".agents/skills/readpaper/scripts/_host_probe_parse_invocation.py"),
    Path(".codex/config.toml"),
)


def _run(argv: list[str], *, check: bool = True) -> str:
    completed = subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_state(path: Path) -> dict[str, Any]:
    absolute = ROOT / path
    if not absolute.exists():
        return {
            "path": path.as_posix(),
            "exists": False,
            "mode": None,
            "sha256": None,
        }
    metadata = absolute.stat()
    return {
        "path": path.as_posix(),
        "exists": True,
        "mode": f"{metadata.st_mode & 0o7777:04o}",
        "sha256": _sha256(absolute),
    }


def _extract_json_after_label(
    text: str,
    label: str,
    *,
    expected_hook_event_name: str | None = None,
) -> dict[str, Any]:
    # The shorter label ``stop.command.input`` is a suffix of
    # ``subagent-stop.command.input`` and the output of ``strings`` does not
    # always preserve a left delimiter. Decode every suffix match, then select
    # by the schema's exact hook_event_name const.
    marker = f"{label}{{"
    candidates: list[dict[str, Any]] = []
    search_from = 0
    while True:
        marker_index = text.find(marker, search_from)
        if marker_index < 0:
            break
        object_index = marker_index + len(label)
        try:
            value, _ = json.JSONDecoder().raw_decode(text[object_index:])
        except json.JSONDecodeError:
            search_from = marker_index + 1
            continue
        if isinstance(value, dict):
            candidates.append(value)
        search_from = marker_index + 1

    if not candidates:
        raise ValueError(f"embedded schema marker not found: {label}")
    if expected_hook_event_name is not None:
        candidates = [
            value
            for value in candidates
            if value.get("properties", {})
            .get("hook_event_name", {})
            .get("const")
            == expected_hook_event_name
        ]
    if len(candidates) != 1:
        raise ValueError(
            f"embedded schema marker is not unique: {label} "
            f"({len(candidates)} matching schemas)"
        )
    value = candidates[0]
    if expected_hook_event_name is not None:
        actual_event_name = (
            value.get("properties", {})
            .get("hook_event_name", {})
            .get("const")
        )
        if actual_event_name != expected_hook_event_name:
            raise ValueError(
                "embedded schema event mismatch: "
                f"{label} expected {expected_hook_event_name!r}, "
                f"got {actual_event_name!r}"
            )
    return value


def extract_command_input_schemas(binary: Path = CODEX) -> dict[str, dict[str, Any]]:
    strings_binary = shutil.which("strings") or "/usr/bin/strings"
    embedded = _run([strings_binary, str(binary)])
    return {
        event: _extract_json_after_label(
            embedded,
            f"{schema_name}.command.input",
            expected_hook_event_name=HOOK_EVENT_NAMES[event],
        )
        for event, schema_name in HOOK_SCHEMA_NAMES.items()
    }


def semantic_identity_summary(
    command_schemas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Classify inputs needed for local semantic keys, not raw callback IDs."""

    available_by_event: dict[str, list[str]] = {}
    missing_by_event: dict[str, list[str]] = {}
    missing_present_by_event: dict[str, list[str]] = {}
    mismatched_by_event: dict[str, dict[str, dict[str, Any]]] = {}
    for event, required_fields in SEMANTIC_IDENTITY_REQUIRED_FIELDS.items():
        schema = command_schemas[event]
        properties = schema.get("properties", {})
        schema_fields = set(properties)
        schema_required = set(schema.get("required", []))
        present_fields = SEMANTIC_IDENTITY_PRESENT_FIELDS.get(event, set())
        available_by_event[event] = sorted(required_fields & schema_required)
        missing_by_event[event] = sorted(
            required_fields - (schema_fields & schema_required)
        )
        missing_present_by_event[event] = sorted(present_fields - schema_fields)
        mismatched_by_event[event] = {}
        fields_to_validate = (
            (required_fields & schema_fields & schema_required)
            | (present_fields & schema_fields)
        )
        for field in sorted(fields_to_validate):
            expected = SEMANTIC_IDENTITY_FIELD_CONTRACTS[field]
            actual = properties[field]
            if any(actual.get(key) != value for key, value in expected.items()):
                mismatched_by_event[event][field] = {
                    "expected": expected,
                    "actual": actual,
                }
    return {
        "required_fields_by_event": {
            event: sorted(fields)
            for event, fields in SEMANTIC_IDENTITY_REQUIRED_FIELDS.items()
        },
        "available_required_fields_by_event": available_by_event,
        "missing_required_fields_by_event": missing_by_event,
        "presence_only_fields_by_event": {
            event: sorted(fields)
            for event, fields in SEMANTIC_IDENTITY_PRESENT_FIELDS.items()
        },
        "missing_presence_only_fields_by_event": missing_present_by_event,
        "mismatched_field_contracts_by_event": mismatched_by_event,
        "ready_by_event": {
            event: not missing
            and not missing_present_by_event[event]
            and not mismatched_by_event[event]
            for event, missing in missing_by_event.items()
        },
        "all_events_ready": not any(missing_by_event.values())
        and not any(missing_present_by_event.values())
        and not any(mismatched_by_event.values()),
    }


def _definition_properties(schema: dict[str, Any], name: str) -> list[str]:
    definition = schema.get("definitions", {}).get(name, {})
    return sorted(definition.get("properties", {}).keys())


def _top_level_properties(schema: dict[str, Any]) -> list[str]:
    return sorted(schema.get("properties", {}).keys())


def _find_titled_object(schema: Any, title: str) -> dict[str, Any] | None:
    if isinstance(schema, dict):
        if schema.get("title") == title:
            return schema
        for value in schema.values():
            found = _find_titled_object(value, title)
            if found is not None:
                return found
    elif isinstance(schema, list):
        for value in schema:
            found = _find_titled_object(value, title)
            if found is not None:
                return found
    return None


def _generated_schema_summary(binary: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="readpaper-g0-public-") as directory:
        output = Path(directory)
        _run(
            [
                str(binary),
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(output),
            ]
        )
        hook_started = json.loads(
            (output / "v2/HookStartedNotification.json").read_text(encoding="utf-8")
        )
        hook_completed = json.loads(
            (output / "v2/HookCompletedNotification.json").read_text(
                encoding="utf-8"
            )
        )
        raw_response = json.loads(
            (output / "v2/RawResponseCompletedNotification.json").read_text(
                encoding="utf-8"
            )
        )
        thread_settings = json.loads(
            (output / "v2/ThreadSettingsUpdatedNotification.json").read_text(
                encoding="utf-8"
            )
        )
        item_completed = json.loads(
            (output / "v2/ItemCompletedNotification.json").read_text(
                encoding="utf-8"
            )
        )
        turn_started = json.loads(
            (output / "v2/TurnStartedNotification.json").read_text(encoding="utf-8")
        )
        thread_started = json.loads(
            (output / "v2/ThreadStartedNotification.json").read_text(
                encoding="utf-8"
            )
        )

        collab_tool_call = _find_titled_object(
            item_completed, "CollabAgentToolCallThreadItem"
        )
        collab_properties = (
            collab_tool_call.get("properties", {}) if collab_tool_call else {}
        )

    return {
        "hook_started_notification_fields": _top_level_properties(hook_started),
        "hook_completed_notification_fields": _top_level_properties(hook_completed),
        "hook_run_fields": _definition_properties(hook_started, "HookRunSummary"),
        "hook_completed_run_fields": _definition_properties(
            hook_completed, "HookRunSummary"
        ),
        "raw_response_completed_fields": _top_level_properties(raw_response),
        "thread_settings_fields": _definition_properties(
            thread_settings, "ThreadSettings"
        ),
        "turn_fields": _definition_properties(turn_started, "Turn"),
        "thread_fields": _definition_properties(thread_started, "Thread"),
        "collab_agent_tool_call_fields": sorted(collab_properties),
        "collab_agent_model_description": collab_properties.get("model", {}).get(
            "description"
        ),
        "collab_agent_effort_description": collab_properties.get(
            "reasoningEffort", {}
        ).get("description"),
    }


def _rollout_summary() -> dict[str, Any]:
    thread_id = os.environ.get("CODEX_THREAD_ID")
    host_session_id = os.environ.get("CODEX_SESSION_ID")
    codex_data_dir = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    candidates = (
        list((codex_data_dir / "sessions").rglob(f"*{thread_id}*.jsonl"))
        if thread_id
        else []
    )
    result: dict[str, Any] = {
        "environment_thread_id": thread_id,
        "environment_session_id": host_session_id,
        "rollout_found": len(candidates) == 1,
    }
    if len(candidates) != 1:
        return result

    session_meta: dict[str, Any] | None = None
    turn_contexts: dict[str, tuple[int, dict[str, Any]]] = {}
    task_starts: dict[str, tuple[int, dict[str, Any]]] = {}
    ordinals_present = True
    with candidates[0].open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = json.loads(raw_line)
            ordinals_present = ordinals_present and isinstance(line.get("ordinal"), int)
            if line.get("type") == "session_meta" and session_meta is None:
                session_meta = line.get("payload", {})
            elif line.get("type") == "turn_context":
                payload = line.get("payload", {})
                turn_id = payload.get("turn_id")
                if isinstance(turn_id, str):
                    ordinal = line.get("ordinal")
                    turn_contexts[turn_id] = (
                        ordinal if isinstance(ordinal, int) else -1,
                        payload,
                    )
            elif (
                line.get("type") == "event_msg"
                and line.get("payload", {}).get("type") == "task_started"
            ):
                payload = line.get("payload", {})
                turn_id = payload.get("turn_id")
                if isinstance(turn_id, str):
                    ordinal = line.get("ordinal")
                    task_starts[turn_id] = (
                        ordinal if isinstance(ordinal, int) else -1,
                        payload,
                    )

    matching_turn_ids = set(turn_contexts) & set(task_starts)
    selected_turn_id = (
        max(
            matching_turn_ids,
            key=lambda value: max(
                turn_contexts[value][0], task_starts[value][0]
            ),
        )
        if matching_turn_ids
        else None
    )
    selected_turn_context = (
        turn_contexts[selected_turn_id][1] if selected_turn_id else None
    )
    selected_task_started = (
        task_starts[selected_turn_id][1] if selected_turn_id else None
    )

    result.update(
        {
            "rollout_path_sha256": hashlib.sha256(
                str(candidates[0]).encode("utf-8")
            ).hexdigest(),
            "ordinals_present": ordinals_present,
            "thread_id": session_meta.get("id") if session_meta else None,
            "session_id": session_meta.get("session_id") if session_meta else None,
            "originator": session_meta.get("originator") if session_meta else None,
            "thread_source": session_meta.get("thread_source") if session_meta else None,
            "parent_thread_id": (
                session_meta.get("parent_thread_id") if session_meta else None
            ),
            "matched_turn_context_and_task_started": selected_turn_id is not None,
            "latest_matched_turn_id": selected_turn_id,
            "task_started_context_window": (
                selected_task_started.get("model_context_window")
                if selected_task_started
                else None
            ),
            "turn_context_model": (
                selected_turn_context.get("model")
                if selected_turn_context
                else None
            ),
            "turn_context_effort": (
                selected_turn_context.get("effort")
                if selected_turn_context
                else None
            ),
            "turn_context_is_execution_receipt": False,
        }
    )
    return result


def _desktop_process_summary(binary: Path) -> dict[str, Any]:
    output = _run(["/bin/ps", "-axo", "pid=,ppid=,command="])
    matches: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid_text, parent_text, command = fields
        if str(binary) in command and " app-server" in command:
            matches.append((int(pid_text), int(parent_text), command))
    if not matches:
        return {"running_app_server_found": False}
    pid, parent_pid, command = matches[0]
    return {
        "running_app_server_found": True,
        "pid": pid,
        "parent_pid": parent_pid,
        "transport": "explicit_listen" if " --listen " in command else "stdio_default",
        "control_socket_argument_present": "--sock" in command,
    }


def collect() -> dict[str, Any]:
    if not CODEX.is_file():
        raise FileNotFoundError(f"Codex Desktop binary not found: {CODEX}")

    command_schemas = extract_command_input_schemas(CODEX)
    command_fields = {
        event: sorted(schema.get("properties", {}).keys())
        for event, schema in command_schemas.items()
    }
    command_required = {
        event: sorted(schema.get("required", []))
        for event, schema in command_schemas.items()
    }
    command_additional_properties = {
        event: schema.get("additionalProperties")
        for event, schema in command_schemas.items()
    }
    command_hook_event_names = {
        event: schema.get("properties", {})
        .get("hook_event_name", {})
        .get("const")
        for event, schema in command_schemas.items()
    }
    all_command_fields = set().union(*(set(value) for value in command_fields.values()))
    generated = _generated_schema_summary(CODEX)

    plist = APP / "Contents/Info.plist"
    app_version = _run(
        [
            "/usr/libexec/PlistBuddy",
            "-c",
            "Print :CFBundleShortVersionString",
            str(plist),
        ]
    )
    app_build = _run(
        [
            "/usr/libexec/PlistBuddy",
            "-c",
            "Print :CFBundleVersion",
            str(plist),
        ]
    )

    callback_identity_by_event = {
        event: sorted(HOST_CALLBACK_ID_FIELDS & set(fields))
        for event, fields in command_fields.items()
    }
    callback_identity_available = all(callback_identity_by_event.values())
    semantic_identity = semantic_identity_summary(command_schemas)
    root_execution_identity_available = bool(
        ROOT_EXECUTION_ID_FIELDS & all_command_fields
    )
    context_stream_identity_available = bool(
        CONTEXT_STREAM_ID_FIELDS & all_command_fields
    )
    hook_aggregate_fields = set(generated["hook_started_notification_fields"]) | set(
        generated["hook_run_fields"]
    )
    aggregate_identity_available = bool(
        {"aggregateId", "aggregate_id", "callbackId", "callback_id"}
        & hook_aggregate_fields
    )
    raw_response_fields = set(generated["raw_response_completed_fields"])
    turn_fields = set(generated["turn_fields"])
    upstream_execution_model_effort_available = {
        "model",
        "effort",
    }.issubset(raw_response_fields) or {
        "model",
        "reasoningEffort",
    }.issubset(raw_response_fields) or {
        "model",
        "effort",
    }.issubset(turn_fields) or {
        "model",
        "reasoningEffort",
    }.issubset(turn_fields)

    blockers = []
    if not semantic_identity["all_events_ready"]:
        blockers.append(
            {
                "id": "semantic_identity_inputs_missing",
                "evidence": {
                    "missing_required": semantic_identity[
                        "missing_required_fields_by_event"
                    ],
                    "missing_presence_only": semantic_identity[
                        "missing_presence_only_fields_by_event"
                    ],
                    "schema_mismatches": semantic_identity[
                        "mismatched_field_contracts_by_event"
                    ],
                },
            }
        )
    capability_boundaries = [
        {
            "id": "raw_callback_cardinality_not_observable",
            "present": callback_identity_available,
            "release_blocker": False,
            "handling": "Use event-specific semantic keys and do not claim one ledger row per raw callback.",
        },
        {
            "id": "root_execution_id_not_host_provided",
            "present": root_execution_identity_available,
            "release_blocker": False,
            "handling": "Allocate a local execution ID and bind it to session, turn, actor, and tool events in the live probe.",
        },
        {
            "id": "context_stream_id_not_host_provided",
            "present": context_stream_identity_available,
            "release_blocker": False,
            "handling": "Derive the stream from session_id and the root sentinel or agent_id.",
        },
        {
            "id": "hook_aggregate_receipt_not_exposed",
            "present": aggregate_identity_available,
            "release_blocker": False,
            "handling": "Observe the nonce-matching continuation directly; close missing starts as not_started without guessing why.",
        },
        {
            "id": "executed_effort_receipt_not_exposed",
            "present": upstream_execution_model_effort_available,
            "release_blocker": False,
            "handling": "Separate requested, host-validated, and observed values; never report requested effort as observed.",
        },
    ]
    live_validation_pending = [
        "root_main_and_subagent_actor_binding",
        "pretool_posttool_one_use_capability",
        "compact_phase_and_epoch_pairing",
        "stop_block_creates_same_task_continuation",
        "duplicate_stop_payload_reuses_transaction_and_counter",
        "continuation_nonce_claim_is_one_use",
        "crash_replay_has_at_most_once_authorized_effect",
        "stop_hook_active_prevents_nested_continuation",
        "ordinary_user_prompt_cancels_pending_auto_resume",
    ]

    return {
        "schema_version": 3,
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "gate": "G0",
        "status": "blocked" if blockers else "requires_live_desktop_probe",
        "host": {
            "app_version": app_version,
            "app_build": app_build,
            "codex_version": _run([str(CODEX), "--version"]),
            "codex_binary_sha256": _sha256(CODEX),
            "app_server_process": _desktop_process_summary(CODEX),
            "default_control_socket_exists": (
                Path.home()
                / ".codex/app-server-control/app-server-control.sock"
            ).exists(),
        },
        "current_rollout": _rollout_summary(),
        "command_hook_schemas": {
            "fields": command_fields,
            "required": command_required,
            "additional_properties": command_additional_properties,
            "hook_event_name_consts": command_hook_event_names,
            "host_callback_identity_fields_by_event": callback_identity_by_event,
            "semantic_identity": semantic_identity,
        },
        "app_server_schemas": generated,
        "checks": {
            "host_callback_identity_in_command_input": callback_identity_available,
            "host_root_execution_identity_in_command_input": (
                root_execution_identity_available
            ),
            "host_context_stream_identity_in_command_input": (
                context_stream_identity_available
            ),
            "host_aggregate_identity_in_hook_notifications": aggregate_identity_available,
            "executed_model_and_effort_in_public_response_or_turn": (
                upstream_execution_model_effort_available
            ),
            "semantic_identity_inputs_ready": semantic_identity[
                "all_events_ready"
            ],
            "stop_hook_active": "stop_hook_active" in command_fields["stop"],
            "last_assistant_message_available": (
                "last_assistant_message" in command_fields["stop"]
            ),
            "tool_use_identity": (
                "tool_use_id" in command_fields["pre_tool_use"]
                and "tool_use_id" in command_fields["post_tool_use"]
            ),
            "turn_identity": all(
                "turn_id" in fields
                for event, fields in command_fields.items()
                if event != "session_start"
            ),
            "subagent_identity": all(
                {"agent_id", "agent_type"}.issubset(command_fields[event])
                for event in ("subagent_start", "subagent_stop")
            ),
            "exact_user_prompt_available": (
                "prompt" in command_fields["user_prompt_submit"]
            ),
            "model_available_in_command_input": all(
                "model" in fields for fields in command_fields.values()
            ),
            "effort_available_in_command_input": all(
                "effort" in fields for fields in command_fields.values()
            ),
        },
        "project_path_state": {
            "captured_before_any_temporary_hook_install": True,
            "paths": [_path_state(path) for path in G0_PROTECTED_PATHS],
            "configuration_mutated": False,
        },
        "blockers": blockers,
        "capability_boundaries": capability_boundaries,
        "live_validation_pending": live_validation_pending,
        "live_probe": {
            "temporary_hook_installed": False,
            "reason_not_installed": "This command is the read-only static phase. A reviewed temporary hook and a new Desktop session are still required for the live phase.",
            "same_main_stop_continuation_proven": False,
            "host_prompt_exactly_once_claimed": False,
            "required_guarantee": "At most one nonce claim and authorized repair effect per continuation attempt.",
            "required_scenarios": live_validation_pending,
        },
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-static-ready", action="store_true")
    args = parser.parse_args()

    report = collect()
    if args.output:
        _atomic_write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.require_static_ready:
        return int(bool(report["blockers"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
