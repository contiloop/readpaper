#!/usr/bin/env python3
"""Reviewed temporary command hook for the G0 live Desktop probe."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from live_probe_state import (
    CANCEL_PREFIX,
    START_PREFIX,
    SUPPRESS_PREFIX,
    ProbeConflict,
    StateStore,
    append_event,
    canonical_bytes,
    continuation_reason,
    decode_output,
    digest_json,
    digest_text,
    encode_output,
    now,
    token,
)


def _parser_module(project_root: Path) -> Any:
    path = (
        project_root
        / ".agents/skills/readpaper/scripts/_host_probe_parse_invocation.py"
    )
    specification = importlib.util.spec_from_file_location("g0_probe_parser", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("temporary G0 parser is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _json_output(value: dict[str, Any]) -> bytes:
    return canonical_bytes(value) + b"\n"


def _context(event: str, text: str) -> bytes:
    return _json_output(
        {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": text,
            }
        }
    )


def _block(reason: str) -> bytes:
    return _json_output({"decision": "block", "reason": reason})


def _session_start(store: StateStore, payload: dict[str, Any]) -> bytes:
    session_id = payload["session_id"]
    source = payload["source"]
    with store.locked() as state:
        previous = state.get("session")
        pending = state.get("stop_transaction")
        if (
            previous is not None
            and previous["session_id"] != session_id
            and pending is not None
            and pending.get("status") in {"reserved", "output_stored", "requested"}
        ):
            pending["status"] = "abandoned_restart"
            state["scenario_results"]["restart_does_not_resume"] = True
        state["session"] = {
            "session_id": session_id,
            "session_id_sha256": digest_text(session_id),
            "source": source,
            "observed_at": now(),
        }
        append_event(state, payload, "session_observed")
    return b""


def _user_prompt(store: StateStore, payload: dict[str, Any]) -> bytes:
    prompt = payload["prompt"]
    # Some Desktop/UI copy paths escape Markdown underscores before delivering
    # the user-authored arming marker. This normalization is intentionally
    # limited to the non-authorizing scenario selector; continuation prompts
    # still require the exact unmodified prompt hash below.
    scenario_prompt = re.sub(r"\\+_", "_", prompt)
    session_id = payload["session_id"]
    turn_id = payload["turn_id"]
    output = b""
    with store.locked() as state:
        stop = state.get("stop_transaction")
        if stop is not None and digest_text(prompt) == stop.get("prompt_sha256"):
            claim_sha = digest_json(
                {
                    "attempt_id": stop["attempt_id"],
                    "nonce": stop["nonce"],
                    "prompt_sha256": stop["prompt_sha256"],
                }
            )
            claim = state.get("prompt_claim")
            valid = (
                stop.get("status") == "requested"
                and stop["session_id"] == session_id
            )
            if claim is None and valid:
                state["prompt_claim"] = {
                    "claim_sha256": claim_sha,
                    "prompt_sha256": stop["prompt_sha256"],
                    "status": "started",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "successful_claims": 1,
                    "duplicate_claims_blocked": 0,
                    "claimed_at": now(),
                }
                stop["status"] = "started"
                state["phase"] = "continuation_started"
                append_event(state, payload, "continuation_nonce_claimed")
                output = _context(
                    "UserPromptSubmit",
                    "G0 nonce claim succeeded. The same Main must run only the exact "
                    "probe command present in this prompt. Do not alter, wrap, or repeat it.",
                )
            else:
                if claim is None:
                    state["prompt_claim"] = {
                        "claim_sha256": claim_sha,
                        "prompt_sha256": stop["prompt_sha256"],
                        "status": "rejected",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "successful_claims": 0,
                        "duplicate_claims_blocked": 1,
                        "claimed_at": now(),
                    }
                else:
                    claim["duplicate_claims_blocked"] += 1
                append_event(state, payload, "duplicate_continuation_prompt_blocked")
                output = _block("G0 continuation nonce was already consumed or is stale.")
        else:
            if stop is not None and stop.get("status") == "requested":
                stop["status"] = "cancelled"
                state["scenario_results"]["ordinary_user_prompt_cancels_pending"] = True
            words = scenario_prompt.split()
            if len(words) == 2 and words[1] == state["run_id"]:
                if words[0] == START_PREFIX:
                    state["armed"] = {
                        "mode": "primary",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "prompt_sha256": digest_text(prompt),
                    }
                    state["phase"] = "primary_armed"
                    output = _context(
                        "UserPromptSubmit",
                        "This user-authorized G0 probe must exercise actor binding. Start "
                        "exactly one small probe subagent, wait for it, make no Bash call, "
                        "then answer only G0_INITIAL_STOP so the root Stop hook can run.",
                    )
                    append_event(state, payload, "primary_probe_armed")
                elif words[0] == SUPPRESS_PREFIX:
                    state["armed"] = {
                        "mode": "suppression",
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "prompt_sha256": digest_text(prompt),
                    }
                    state["stop_transaction"] = None
                    state["prompt_claim"] = None
                    state["phase"] = "suppression_armed"
                    output = _context(
                        "UserPromptSubmit",
                        "Answer only G0_SUPPRESSION_STOP and make no tool call. Two reviewed "
                        "Stop hooks will run; the suppression hook must win.",
                    )
                    append_event(state, payload, "suppression_probe_armed")
                elif words[0] == CANCEL_PREFIX:
                    append_event(state, payload, "ordinary_cancel_prompt_observed")
                    output = _context(
                        "UserPromptSubmit",
                        "The pending G0 continuation has been cancelled. Do not run the "
                        "probe CLI; answer only G0_CANCEL_CONFIRMED.",
                    )
            else:
                append_event(state, payload, "ordinary_user_prompt_observed")
    return output


def _stop_stage(store: StateStore, payload: dict[str, Any], parser: Any) -> bytes:
    input_sha = digest_json(payload)
    session_id = payload["session_id"]
    turn_id = payload["turn_id"]
    if payload["stop_hook_active"]:
        with store.locked() as state:
            state["scenario_results"]["stop_hook_active_prevents_nested"] = True
            append_event(state, payload, "nested_continuation_prevented")
        return _json_output({})

    # Phase 1: reserve the immutable logical slot and its one continuation.
    with store.locked() as state:
        armed = state.get("armed")
        if (
            armed is None
            or armed["session_id"] != session_id
            or armed["turn_id"] != turn_id
        ):
            append_event(state, payload, "stop_not_armed")
            return _json_output({})
        slot = digest_json(
            {
                "run_id": state["run_id"],
                "session_id": session_id,
                "turn_id": turn_id,
                "actor": "root",
                "hook_definition": parser.SCHEMA_SHA256,
            }
        )
        transaction = state.get("stop_transaction")
        if transaction is None:
            transaction = {
                "slot": slot,
                "input_sha256": input_sha,
                "session_id": session_id,
                "turn_id": turn_id,
                "attempt_id": token(12),
                "nonce": token(16),
                "request_id": token(12),
                "continuation_counter": 1,
                "status": "reserved",
                "reserved_at": now(),
            }
            state["stop_transaction"] = transaction
            append_event(state, payload, "stop_transaction_reserved")
        elif transaction["slot"] != slot or transaction["input_sha256"] != input_sha:
            state["failures"].append("STATE_CONFLICT: Stop logical slot/input mismatch")
            raise ProbeConflict("Stop logical slot replay changed immutable input")
        crash = state.get("crash_injection")
        if crash == "after_reserved":
            state.pop("crash_injection", None)
    if crash == "after_reserved":
        os._exit(91)

    # Phase 2: construct and persist the exact bytes once.
    with store.locked() as state:
        transaction = state["stop_transaction"]
        if transaction["status"] == "reserved":
            command = parser.build_command(
                run_id=state["run_id"],
                attempt_id=transaction["attempt_id"],
                nonce=transaction["nonce"],
                request_id=transaction["request_id"],
            )
            reason = continuation_reason(
                run_id=state["run_id"],
                attempt_id=transaction["attempt_id"],
                nonce=transaction["nonce"],
                request_id=transaction["request_id"],
                command=command,
            )
            output = {"decision": "block", "reason": reason}
            transaction["prompt_sha256"] = digest_text(reason)
            transaction["output"] = encode_output(output)
            transaction["status"] = "output_stored"
            transaction["output_stored_at"] = now()
        crash = state.get("crash_injection")
        if crash == "after_output_stored":
            state.pop("crash_injection", None)
    if crash == "after_output_stored":
        os._exit(92)

    # Phase 3: completion is durable before returning bytes to Desktop.
    with store.locked() as state:
        transaction = state["stop_transaction"]
        if transaction["status"] == "output_stored":
            transaction["status"] = "requested"
            transaction["completed_at"] = now()
            state["phase"] = "continuation_requested"
            append_event(state, payload, "stop_exact_output_completed")
        raw = decode_output(transaction["output"])
    store.save_raw("desktop-stop", payload)
    return raw


def _pre_tool(store: StateStore, payload: dict[str, Any], parser: Any) -> bytes:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        with store.locked() as state:
            append_event(state, payload, "non_probe_tool_observed")
        return b""
    invocation = parser.parse_command(command)
    if invocation is None:
        with store.locked() as state:
            append_event(state, payload, "non_probe_bash_observed")
        return b""
    tool_use_id = payload["tool_use_id"]
    command_sha = digest_text(command)
    key = digest_text(tool_use_id)
    semantic = digest_json(
        {
            "session_id": payload["session_id"],
            "turn_id": payload["turn_id"],
            "tool_use_id": tool_use_id,
            "command_sha256": command_sha,
        }
    )
    with store.locked() as state:
        claim = state.get("prompt_claim")
        stop = state.get("stop_transaction")
        invocation_matches = (
            stop is not None
            and invocation["run_id"] == state["run_id"]
            and invocation["attempt_id"] == stop["attempt_id"]
            and invocation["nonce"] == stop["nonce"]
            and invocation["request_id"] == stop["request_id"]
        )
        existing = state["capabilities"].get(key)
        if existing is not None:
            if existing["semantic_sha256"] != semantic:
                raise ProbeConflict("PreTool semantic replay changed")
            append_event(state, payload, "one_use_capability_replayed")
        else:
            claimed_by_prompt = (
                claim is not None
                and claim["status"] == "started"
                and claim["session_id"] == payload["session_id"]
                and claim["turn_id"] == payload["turn_id"]
                and claim.get("tool_use_id") in {None, tool_use_id}
            )
            claimable_by_tool = (
                claim is None
                and invocation_matches
                and stop.get("status") == "requested"
                and stop["session_id"] == payload["session_id"]
                and stop["turn_id"] == payload["turn_id"]
            )
            if claimed_by_prompt and invocation_matches:
                claim["tool_use_id"] = tool_use_id
                claim["command_sha256"] = command_sha
                claim["claim_source"] = claim.get("claim_source", "user_prompt")
            elif claimable_by_tool:
                claim_sha = digest_json(
                    {
                        "attempt_id": stop["attempt_id"],
                        "nonce": stop["nonce"],
                        "request_id": stop["request_id"],
                        "session_id": payload["session_id"],
                        "turn_id": payload["turn_id"],
                        "tool_use_id": tool_use_id,
                        "command_sha256": command_sha,
                    }
                )
                state["prompt_claim"] = {
                    "claim_sha256": claim_sha,
                    "prompt_sha256": stop["prompt_sha256"],
                    "status": "started",
                    "session_id": payload["session_id"],
                    "turn_id": payload["turn_id"],
                    "tool_use_id": tool_use_id,
                    "command_sha256": command_sha,
                    "claim_source": "pre_tool",
                    "successful_claims": 1,
                    "duplicate_claims_blocked": 0,
                    "claimed_at": now(),
                }
                claim = state["prompt_claim"]
                stop["status"] = "started"
                state["phase"] = "continuation_started"
                state["scenario_results"]["continuation_claimed_by_pre_tool"] = True
                append_event(state, payload, "continuation_nonce_claimed_pre_tool")
            else:
                if claim is not None:
                    claim["duplicate_claims_blocked"] += 1
                    outcome = "duplicate_continuation_tool_blocked"
                else:
                    outcome = "probe_tool_denied_without_claim"
                append_event(state, payload, outcome)
                return _json_output(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "G0 capability binding failed.",
                        }
                    }
                )
            state["capabilities"][key] = {
                "semantic_sha256": semantic,
                "tool_use_id": tool_use_id,
                "client_request_id": invocation["request_id"],
                "claim_sha256": claim["claim_sha256"],
                "status": "issued",
                "issued_at": now(),
            }
            append_event(state, payload, "one_use_capability_issued")
    store.save_raw("desktop-pre-tool", payload)
    return _json_output(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
    )


def _post_tool(store: StateStore, payload: dict[str, Any], parser: Any) -> bytes:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or parser.parse_command(command) is None:
        with store.locked() as state:
            append_event(state, payload, "non_probe_post_tool_observed")
        return b""
    with store.locked() as state:
        capability = state["capabilities"].get(digest_text(payload["tool_use_id"]))
        effect = state.get("authorized_effect")
        valid = (
            capability is not None
            and capability["status"] == "consumed"
            and effect is not None
            and effect["tool_use_id"] == payload["tool_use_id"]
        )
        observation = {
            "observed_at": now(),
            "tool_use_id_sha256": digest_text(payload["tool_use_id"]),
            "tool_response_sha256": digest_json(payload.get("tool_response")),
            "valid": valid,
        }
        state["post_tool_observation"] = observation
        if valid:
            capability["status"] = "observed"
            state["phase"] = "primary_live_complete"
            state["scenario_results"]["pre_post_tool_capability"] = True
            state["scenario_results"]["same_main_additional_tool_call"] = True
            append_event(state, payload, "authorized_effect_observed")
        else:
            state["failures"].append("PostTool could not correlate authorized effect")
            append_event(state, payload, "post_tool_correlation_failed")
    store.save_raw("desktop-post-tool", payload)
    return b""


def _subagent(store: StateStore, payload: dict[str, Any]) -> bytes:
    agent_key = digest_text(payload["agent_id"])
    with store.locked() as state:
        record = state["subagents"].setdefault(
            agent_key,
            {
                "agent_id_sha256": agent_key,
                "agent_type": payload["agent_type"],
                "start_observed": False,
                "stop_observed": False,
            },
        )
        if payload["hook_event_name"] == "SubagentStart":
            record["start_observed"] = True
            outcome = "subagent_start_bound"
        else:
            record["stop_observed"] = True
            record["stop_hook_active"] = payload["stop_hook_active"]
            outcome = "subagent_stop_bound"
        state["scenario_results"]["root_subagent_actor_binding"] = any(
            item["start_observed"] and item["stop_observed"]
            for item in state["subagents"].values()
        )
        append_event(state, payload, outcome)
    return _json_output({}) if payload["hook_event_name"] == "SubagentStop" else b""


def _compact(store: StateStore, payload: dict[str, Any]) -> bytes:
    actor = digest_text(payload.get("agent_id")) or "root"
    stream_key = digest_json({"session_id": payload["session_id"], "actor": actor})
    input_sha = digest_json(payload)
    with store.locked() as state:
        stream = state["compact_streams"].setdefault(
            stream_key,
            {
                "actor": actor,
                "context_epoch": 0,
                "open": None,
                "completed": [],
            },
        )
        if payload["hook_event_name"] == "PreCompact":
            if stream["open"] is None:
                stream["open"] = {
                    "ordinal": len(stream["completed"]) + 1,
                    "trigger": payload["trigger"],
                    "pre_input_sha256": input_sha,
                }
            elif stream["open"]["pre_input_sha256"] != input_sha:
                state["failures"].append("OBSERVER_UNAVAILABLE: ambiguous PreCompact")
                raise ProbeConflict("ambiguous compact open phase")
            outcome = "compact_pre_opened"
        else:
            opened = stream["open"]
            if opened is None or opened["trigger"] != payload["trigger"]:
                state["failures"].append("OBSERVER_UNAVAILABLE: unmatched PostCompact")
                raise ProbeConflict("unmatched compact close phase")
            opened["post_input_sha256"] = input_sha
            stream["completed"].append(opened)
            stream["open"] = None
            stream["context_epoch"] += 1
            state["scenario_results"]["compact_phase_epoch_pairing"] = True
            outcome = "compact_post_paired"
        append_event(state, payload, outcome)
    return _json_output({})


def handle_main(store: StateStore, payload: dict[str, Any]) -> bytes:
    project_root = Path(store.read()["project_root"])
    parser = _parser_module(project_root)
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return _session_start(store, payload)
    if event == "UserPromptSubmit":
        return _user_prompt(store, payload)
    if event == "Stop":
        return _stop_stage(store, payload, parser)
    if event == "PreToolUse":
        return _pre_tool(store, payload, parser)
    if event == "PostToolUse":
        return _post_tool(store, payload, parser)
    if event in {"SubagentStart", "SubagentStop"}:
        return _subagent(store, payload)
    if event in {"PreCompact", "PostCompact"}:
        return _compact(store, payload)
    raise ValueError(f"unsupported G0 hook event: {event!r}")


def handle_suppression(store: StateStore, payload: dict[str, Any]) -> bytes:
    if payload.get("hook_event_name") != "Stop" or payload.get("stop_hook_active"):
        return _json_output({})
    with store.locked() as state:
        armed = state.get("armed")
        if armed is not None and armed.get("mode") == "suppression":
            state["scenario_results"]["matching_stop_continue_false_wins"] = True
            append_event(state, payload, "continuation_suppressed_by_second_hook")
            return _json_output(
                {
                    "continue": False,
                    "stopReason": "Reviewed G0 suppression scenario.",
                }
            )
    return _json_output({})


def main() -> int:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--runtime", type=Path, required=True)
    argument_parser.add_argument(
        "--handler", choices=("main", "suppression"), required=True
    )
    argument_parser.add_argument("--readpaper-g0-live", action="store_true")
    arguments = argument_parser.parse_args()
    payload = json.load(sys.stdin)
    store = StateStore(arguments.runtime)
    try:
        output = (
            handle_main(store, payload)
            if arguments.handler == "main"
            else handle_suppression(store, payload)
        )
    except Exception as error:
        with store.locked() as state:
            state["failures"].append(f"{type(error).__name__}: {error}")
        raise
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
