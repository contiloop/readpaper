from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from live_hook import handle_main
from live_probe_state import StateStore, atomic_write, canonical_bytes, initial_state
from manage_live_probe import parser_source


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "tests/host_probe/live_hook.py"


def make_runtime(tmp_path: Path) -> tuple[Path, Path, str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, parents=True)
    project = tmp_path / "project"
    parser_path = (
        project
        / ".agents/skills/readpaper/scripts/_host_probe_parse_invocation.py"
    )
    parser_path.parent.mkdir(parents=True)
    atomic_write(parser_path, parser_source(runtime))
    run_id = "g0-test-run"
    state = initial_state(run_id, project, tmp_path / "evidence")
    atomic_write(runtime / "state.json", canonical_bytes(state) + b"\n")
    return runtime, project, run_id


def session_payload(session: str = "session-1", source: str = "startup") -> dict:
    return {
        "session_id": session,
        "transcript_path": None,
        "cwd": "/tmp/project",
        "hook_event_name": "SessionStart",
        "source": source,
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
    }


def prompt_payload(prompt: str, turn: str = "turn-1", session: str = "session-1") -> dict:
    return {
        "session_id": session,
        "transcript_path": None,
        "cwd": "/tmp/project",
        "hook_event_name": "UserPromptSubmit",
        "turn_id": turn,
        "prompt": prompt,
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
    }


def stop_payload(
    *, active: bool = False, turn: str = "turn-1", session: str = "session-1"
) -> dict:
    return {
        "session_id": session,
        "transcript_path": None,
        "cwd": "/tmp/project",
        "hook_event_name": "Stop",
        "turn_id": turn,
        "stop_hook_active": active,
        "last_assistant_message": "G0_INITIAL_STOP",
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
    }


def pre_tool_payload(
    command: str,
    *,
    tool_use_id: str = "tool-1",
    turn: str = "turn-1",
    session: str = "session-1",
) -> dict:
    return {
        "session_id": session,
        "transcript_path": None,
        "cwd": "/tmp/project",
        "hook_event_name": "PreToolUse",
        "turn_id": turn,
        "tool_name": "Bash",
        "tool_use_id": tool_use_id,
        "tool_input": {"command": command},
        "model": "gpt-5.6-sol",
        "permission_mode": "dontAsk",
    }


def command_from_stop(result: subprocess.CompletedProcess[bytes]) -> str:
    return json.loads(result.stdout)["reason"].split("then report its JSON marker and stop: ", 1)[1]


def invoke(runtime: Path, payload: dict, handler: str = "main") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(HOOK),
            "--runtime",
            str(runtime),
            "--handler",
            handler,
            "--readpaper-g0-live",
        ],
        input=canonical_bytes(payload),
        capture_output=True,
        timeout=10,
        check=False,
    )


def arm(runtime: Path, run_id: str) -> None:
    store = StateStore(runtime)
    handle_main(store, session_payload())
    result = handle_main(store, prompt_payload(f"READPAPER_G0_LIVE_START {run_id}"))
    assert b"primary_armed" not in result


def test_identical_stop_replay_returns_exact_bytes_and_one_counter(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    payload = stop_payload()

    first = invoke(runtime, payload)
    second = invoke(runtime, payload)

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["decision"] == "block"
    transaction = StateStore(runtime).read()["stop_transaction"]
    assert transaction["continuation_counter"] == 1
    assert transaction["status"] == "requested"


def test_concurrent_stop_replay_is_one_logical_transaction(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    payload = stop_payload()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: invoke(runtime, payload), range(4)))

    assert {result.returncode for result in results} == {0}
    assert len({result.stdout for result in results}) == 1
    state = StateStore(runtime).read()
    assert state["stop_transaction"]["continuation_counter"] == 1


def test_crash_after_reservation_replays_same_counter(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    with StateStore(runtime).locked() as state:
        state["crash_injection"] = "after_reserved"

    crashed = invoke(runtime, stop_payload())
    recovered = invoke(runtime, stop_payload())

    assert crashed.returncode == 91
    assert recovered.returncode == 0
    state = StateStore(runtime).read()
    assert state["stop_transaction"]["continuation_counter"] == 1
    assert state["stop_transaction"]["status"] == "requested"


def test_crash_after_output_storage_reuses_exact_output(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    with StateStore(runtime).locked() as state:
        state["crash_injection"] = "after_output_stored"

    crashed = invoke(runtime, stop_payload())
    stored = StateStore(runtime).read()["stop_transaction"]["output"]
    recovered = invoke(runtime, stop_payload())

    assert crashed.returncode == 92
    assert recovered.returncode == 0
    assert StateStore(runtime).read()["stop_transaction"]["output"] == stored
    assert recovered.stdout == __import__("base64").b64decode(stored["base64"])


def test_continuation_prompt_claim_is_one_use_under_concurrency(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    stop_result = invoke(runtime, stop_payload())
    reason = json.loads(stop_result.stdout)["reason"]
    payload = prompt_payload(reason, turn="turn-2")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(runtime, payload), range(2)))

    decoded = [json.loads(result.stdout) for result in results]
    assert sum(item.get("decision") == "block" for item in decoded) == 1
    claim = StateStore(runtime).read()["prompt_claim"]
    assert claim["successful_claims"] == 1
    assert claim["duplicate_claims_blocked"] == 1


def test_stop_continuation_can_claim_atomically_at_pre_tool(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    stop_result = invoke(runtime, stop_payload())
    command = command_from_stop(stop_result)

    result = invoke(runtime, pre_tool_payload(command))

    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    state = StateStore(runtime).read()
    assert state["prompt_claim"]["claim_source"] == "pre_tool"
    assert state["prompt_claim"]["tool_use_id"] == "tool-1"
    assert state["prompt_claim"]["successful_claims"] == 1
    assert state["stop_transaction"]["status"] == "started"
    assert len(state["capabilities"]) == 1
    assert state["scenario_results"]["continuation_claimed_by_pre_tool"] is True


def test_pre_tool_claim_blocks_second_tool_use_id(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    command = command_from_stop(invoke(runtime, stop_payload()))

    payloads = [
        pre_tool_payload(command, tool_use_id="tool-1"),
        pre_tool_payload(command, tool_use_id="tool-2"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda payload: invoke(runtime, payload), payloads))

    decisions = {
        json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
        for result in results
    }
    assert decisions == {"allow", "deny"}
    state = StateStore(runtime).read()
    assert state["prompt_claim"]["successful_claims"] == 1
    assert state["prompt_claim"]["duplicate_claims_blocked"] == 1
    assert len(state["capabilities"]) == 1


def test_pre_tool_replays_same_tool_use_without_second_capability(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    command = command_from_stop(invoke(runtime, stop_payload()))
    payload = pre_tool_payload(command, tool_use_id="tool-1")

    first = invoke(runtime, payload)
    replay = invoke(runtime, payload)

    assert first.stdout == replay.stdout
    state = StateStore(runtime).read()
    assert state["prompt_claim"]["successful_claims"] == 1
    assert state["prompt_claim"]["duplicate_claims_blocked"] == 0
    assert len(state["capabilities"]) == 1


def test_pre_tool_claim_requires_stop_session_and_turn(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    command = command_from_stop(invoke(runtime, stop_payload()))

    result = invoke(runtime, pre_tool_payload(command, turn="turn-other"))

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    state = StateStore(runtime).read()
    assert state["prompt_claim"] is None
    assert state["capabilities"] == {}


def test_stop_hook_active_never_creates_nested_continuation(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)

    result = invoke(runtime, stop_payload(active=True))

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    state = StateStore(runtime).read()
    assert state["stop_transaction"] is None
    assert state["scenario_results"]["stop_hook_active_prevents_nested"] is True


def test_desktop_escaped_underscores_still_arm_non_authorizing_selector(
    tmp_path: Path,
) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    store = StateStore(runtime)
    handle_main(store, session_payload())

    result = handle_main(
        store,
        prompt_payload(f"READPAPER\\_G0\\_LIVE\\_START {run_id}"),
    )

    assert b"G0_INITIAL_STOP" in result
    assert store.read()["phase"] == "primary_armed"


def test_ordinary_prompt_and_restart_abandon_pending_attempt(tmp_path: Path) -> None:
    runtime, _, run_id = make_runtime(tmp_path)
    arm(runtime, run_id)
    invoke(runtime, stop_payload())

    invoke(runtime, prompt_payload("ordinary user intervention", turn="turn-2"))

    state = StateStore(runtime).read()
    assert state["stop_transaction"]["status"] == "cancelled"
    assert state["scenario_results"]["ordinary_user_prompt_cancels_pending"] is True

    # A fresh pending transaction in a cloned scenario is abandoned at a new session.
    runtime2, _, run_id2 = make_runtime(tmp_path / "restart")
    arm(runtime2, run_id2)
    invoke(runtime2, stop_payload())
    invoke(runtime2, session_payload(session="session-2", source="resume"))
    state2 = StateStore(runtime2).read()
    assert state2["stop_transaction"]["status"] == "abandoned_restart"
    assert state2["scenario_results"]["restart_does_not_resume"] is True


def test_compact_phase_pairs_once_and_increments_epoch(tmp_path: Path) -> None:
    runtime, _, _ = make_runtime(tmp_path)
    store = StateStore(runtime)
    handle_main(store, session_payload())
    common = {
        "session_id": "session-1",
        "transcript_path": None,
        "cwd": "/tmp/project",
        "turn_id": "turn-compact",
        "trigger": "manual",
        "model": "gpt-5.6-sol",
    }
    handle_main(store, {**common, "hook_event_name": "PreCompact"})
    handle_main(store, {**common, "hook_event_name": "PostCompact"})

    state = store.read()
    stream = next(iter(state["compact_streams"].values()))
    assert stream["context_epoch"] == 1
    assert len(stream["completed"]) == 1
    assert state["scenario_results"]["compact_phase_epoch_pairing"] is True
