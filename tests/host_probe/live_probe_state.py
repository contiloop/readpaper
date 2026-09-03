"""Durable state machine used by the reviewed G0 Desktop live probe.

The live probe deliberately writes only to its private temporary directory.  A
sanitized, immutable evidence snapshot is produced by ``manage_live_probe``
after the live scenarios finish.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
START_PREFIX = "READPAPER_G0_LIVE_START"
SUPPRESS_PREFIX = "READPAPER_G0_LIVE_SUPPRESSION"
CANCEL_PREFIX = "READPAPER_G0_LIVE_CANCEL"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def digest_text(value: str | None) -> str | None:
    if value is None:
        return None
    return digest_bytes(value.encode("utf-8"))


def token(size: int = 18) -> str:
    return secrets.token_hex(size)


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{token(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


class ProbeConflict(RuntimeError):
    """A semantic slot was replayed with different immutable input."""


class StateStore:
    def __init__(self, runtime: Path):
        self.runtime = runtime.resolve()
        self.state_path = self.runtime / "state.json"
        self.lock_path = self.runtime / "state.lock"
        self.raw_dir = self.runtime / "raw-inputs"

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            before = canonical_bytes(state)
            yield state
            after = canonical_bytes(state)
            if after != before:
                atomic_write(self.state_path, after + b"\n")
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        with self.locked() as state:
            return json.loads(json.dumps(state))

    def save_raw(self, name: str, payload: dict[str, Any]) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.raw_dir, 0o700)
        path = self.raw_dir / f"{name}.json"
        if not path.exists():
            atomic_write(path, canonical_bytes(payload) + b"\n")

    def load_raw(self, name: str) -> dict[str, Any]:
        return json.loads((self.raw_dir / f"{name}.json").read_text(encoding="utf-8"))


def initial_state(run_id: str, project_root: Path, evidence_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project_root": str(project_root.resolve()),
        "evidence_dir": str(evidence_dir.resolve()),
        "created_at": now(),
        "phase": "installed",
        "session": None,
        "armed": None,
        "stop_transaction": None,
        "prompt_claim": None,
        "capabilities": {},
        "authorized_effect": None,
        "authorized_effect_count": 0,
        "post_tool_observation": None,
        "subagents": {},
        "compact_streams": {},
        "events": [],
        "failures": [],
        "scenario_results": {},
    }


def event_metadata(payload: dict[str, Any], *, outcome: str) -> dict[str, Any]:
    event_name = str(payload.get("hook_event_name", "unknown"))
    metadata: dict[str, Any] = {
        "observed_at": now(),
        "event": event_name,
        "input_sha256": digest_json(payload),
        "session_id_sha256": digest_text(payload.get("session_id")),
        "turn_id_sha256": digest_text(payload.get("turn_id")),
        "model": payload.get("model"),
        "outcome": outcome,
    }
    if "agent_id" in payload:
        metadata["agent_id_sha256"] = digest_text(payload.get("agent_id"))
        metadata["agent_type"] = payload.get("agent_type")
    if "tool_use_id" in payload:
        metadata["tool_use_id_sha256"] = digest_text(payload.get("tool_use_id"))
        metadata["tool_name"] = payload.get("tool_name")
    if event_name in {"PreCompact", "PostCompact"}:
        metadata["trigger"] = payload.get("trigger")
    if event_name == "SessionStart":
        metadata["source"] = payload.get("source")
    if "stop_hook_active" in payload:
        metadata["stop_hook_active"] = payload.get("stop_hook_active")
    return metadata


def encode_output(output: dict[str, Any]) -> dict[str, str]:
    raw = canonical_bytes(output) + b"\n"
    return {
        "base64": base64.b64encode(raw).decode("ascii"),
        "sha256": digest_bytes(raw),
    }


def decode_output(encoded: dict[str, str]) -> bytes:
    raw = base64.b64decode(encoded["base64"], validate=True)
    if digest_bytes(raw) != encoded["sha256"]:
        raise ProbeConflict("stored Stop output hash mismatch")
    return raw


def append_event(state: dict[str, Any], payload: dict[str, Any], outcome: str) -> None:
    state["events"].append(event_metadata(payload, outcome=outcome))


def continuation_reason(
    *, run_id: str, attempt_id: str, nonce: str, request_id: str, command: str
) -> str:
    return (
        f"READPAPER_G0_CONTINUE run={run_id} attempt={attempt_id} "
        f"nonce={nonce} request={request_id}. "
        "This is the reviewed G0 continuation. In the same Main task, invoke "
        "exactly this one Bash command, then report its JSON marker and stop: "
        f"{command}"
    )


def sanitized_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Return evidence without raw prompts, messages, commands, or backup paths."""

    stop = state.get("stop_transaction")
    prompt_claim = state.get("prompt_claim")
    effect = state.get("authorized_effect")
    session = state.get("session")
    return {
        "schema_version": state["schema_version"],
        "run_id": state["run_id"],
        "captured_at": now(),
        "phase": state.get("phase"),
        "session": None
        if session is None
        else {
            "session_id_sha256": session["session_id_sha256"],
            "source": session["source"],
            "observed_at": session["observed_at"],
        },
        "stop_transaction": None
        if stop is None
        else {
            "slot_sha256": digest_text(stop["slot"]),
            "input_sha256": stop["input_sha256"],
            "output_sha256": stop["output"]["sha256"],
            "continuation_counter": stop["continuation_counter"],
            "status": stop["status"],
            "attempt_id_sha256": digest_text(stop["attempt_id"]),
            "nonce_sha256": digest_text(stop["nonce"]),
        },
        "prompt_claim": None
        if prompt_claim is None
        else {
            "claim_sha256": prompt_claim["claim_sha256"],
            "prompt_sha256": prompt_claim["prompt_sha256"],
            "status": prompt_claim["status"],
            "claim_source": prompt_claim.get("claim_source", "user_prompt"),
            "tool_use_id_sha256": digest_text(prompt_claim.get("tool_use_id")),
            "command_sha256": prompt_claim.get("command_sha256"),
            "successful_claims": prompt_claim["successful_claims"],
            "duplicate_claims_blocked": prompt_claim["duplicate_claims_blocked"],
        },
        "authorized_effect": None
        if effect is None
        else {
            "effect_id_sha256": digest_text(effect["effect_id"]),
            "client_request_sha256": digest_text(effect["client_request_id"]),
            "tool_use_id_sha256": digest_text(effect["tool_use_id"]),
            "output_sha256": effect["output_sha256"],
        },
        "authorized_effect_count": state.get("authorized_effect_count", 0),
        "post_tool_observation": state.get("post_tool_observation"),
        "subagents": state.get("subagents", {}),
        "compact_streams": state.get("compact_streams", {}),
        "scenario_results": state.get("scenario_results", {}),
        "events": state.get("events", []),
        "failures": state.get("failures", []),
    }
