"""One-use PreTool capabilities and exact client-request replay routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .canonical import digest, sha256_bytes
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id
from .storage import FileLock, Layout, atomic_write_json, read_json


def bound_request_document(invocation: Any) -> dict[str, Any]:
    """Canonical request plus immutable payload-file bytes identity when present."""
    value = dict(invocation.canonical_request())
    payload = invocation.flags.get("--payload")
    if isinstance(payload, str):
        path = Path(payload).resolve()
        if not path.is_file() or path.is_symlink():
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "record payload must be a regular file")
        value["payload_sha256"] = sha256_bytes(path.read_bytes())
    return value


class InvocationAuthority:
    def __init__(self, root: Path):
        self.layout = Layout(root)
        self.layout.initialize()
        self.lock = self.layout.locks / "90-invocation-index.lock"

    def _cap_path(self, capability_id: str) -> Path:
        return self.layout.runtime / "invocation-capabilities" / f"{capability_id}.json"

    def _route_path(self, scope_key: str, client_request_id: str) -> Path:
        return self.layout.runtime / "client-requests" / digest(scope_key) / f"{client_request_id}.json"

    def get_capability(self, capability_id: str) -> dict[str, Any]:
        return read_json(self._cap_path(capability_id))

    def issue(
        self,
        *,
        pretool_semantic_key: str,
        client_request_id: str,
        request_digest: str,
        argv_sha256: str,
        hook_definition_hash: str,
        task_id: str,
        session_id: str,
        turn_id: str,
        tool_use_id: str,
        agent_id: str,
        agent_execution_id: str,
        context_stream_id: str,
        context_epoch: int,
    ) -> dict[str, Any]:
        capability_id = sequence_id(
            "cap", pretool_semantic_key, client_request_id, request_digest, hook_definition_hash
        )
        path = self._cap_path(capability_id)
        with FileLock(self.lock):
            if path.exists():
                existing = read_json(path)
                if existing["request_digest"] != request_digest or existing["tool_use_id"] != tool_use_id:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "PreTool semantic replay conflict")
                return existing
            now = datetime.now(timezone.utc)
            capability = {
                "schema_version": 1,
                "capability_id": capability_id,
                "status": "issued",
                "pretool_semantic_key": pretool_semantic_key,
                "client_request_id": client_request_id,
                "request_digest": request_digest,
                "argv_sha256": argv_sha256,
                "hook_definition_hash": hook_definition_hash,
                "task_id": task_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_use_id": tool_use_id,
                "agent_id": agent_id,
                "agent_execution_id": agent_execution_id,
                "context_stream_id": context_stream_id,
                "context_epoch": context_epoch,
                "issued_at": now.isoformat(timespec="milliseconds"),
                "expires_at": (now + timedelta(seconds=30)).isoformat(timespec="milliseconds"),
            }
            atomic_write_json(path, capability, replace=False)
            return capability

    def consume_and_reserve(
        self,
        *,
        scope_key: str,
        client_request_id: str,
        request_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        route_path = self._route_path(scope_key, client_request_id)
        with FileLock(self.lock):
            if route_path.exists():
                route = read_json(route_path)
                if route["request_digest"] != request_digest:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "client request reused with different request")
                return route, route if route["status"] == "completed" else None
            candidates: list[tuple[Path, dict[str, Any]]] = []
            for path in (self.layout.runtime / "invocation-capabilities").glob("cap_*.json"):
                capability = read_json(path)
                if (
                    capability["client_request_id"] == client_request_id
                    and capability["request_digest"] == request_digest
                    and capability["status"] == "issued"
                ):
                    candidates.append((path, capability))
            if len(candidates) != 1:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "exactly one fresh capability is required")
            cap_path, capability = candidates[0]
            if datetime.fromisoformat(capability["expires_at"]) < datetime.now(timezone.utc):
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "capability expired")
            capability["status"] = "consumed"
            capability["consumed_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            route = {
                "schema_version": 1,
                "scope_key": scope_key,
                "client_request_id": client_request_id,
                "request_digest": request_digest,
                "status": "in_progress",
                "capability_id": capability["capability_id"],
                "response": None,
                "response_sha256": None,
            }
            atomic_write_json(cap_path, capability)
            atomic_write_json(route_path, route, replace=False)
            return route, None

    def complete(self, *, scope_key: str, client_request_id: str, request_digest: str, response: bytes) -> dict[str, Any]:
        path = self._route_path(scope_key, client_request_id)
        with FileLock(self.lock):
            route = read_json(path)
            if route["request_digest"] != request_digest:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "route digest changed")
            response_sha = sha256_bytes(response)
            if route["status"] == "completed":
                if route["response_sha256"] != response_sha:
                    raise ReadPaperError(ErrorCode.ID_MISMATCH, "completed response changed")
                return route
            route.update(
                {
                    "status": "completed",
                    "response": response.decode("utf-8"),
                    "response_sha256": response_sha,
                }
            )
            atomic_write_json(path, route)
            return route
