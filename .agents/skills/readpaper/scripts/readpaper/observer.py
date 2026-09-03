"""Codex Desktop hook observer and protected-command authority adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .authority import InvocationAuthority, bound_request_document
from .canonical import canonical_bytes, digest, digest_text, sha256_bytes
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id
from .models import Actor, EventKind, EventResult, HostEventKind, RunState
from .parse_invocation import SCHEMA_SHA256, Invocation, parse_command
from .state import StateService
from .storage import FileLock, assert_regular_private_file, atomic_write_json, read_json


HOOK_DEFINITION = "readpaper-observer/v2"
HOOK_DEFINITION_HASH = digest_text(HOOK_DEFINITION)


def _output(value: dict[str, Any]) -> bytes:
    return canonical_bytes(value) + b"\n"


def _deny(reason: str) -> bytes:
    return _output({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})


def _allow() -> bytes:
    return _output({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})


class DesktopObserver:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.python_path = self.root / ".venv/bin/python"
        self.script_path = self.root / ".agents/skills/readpaper/scripts/paper.py"
        self.state = StateService(self.root)
        self.authority = InvocationAuthority(self.root)

    @property
    def _unbound_host_dir(self) -> Path:
        return self.state.layout.runtime / "unbound-host-events"

    @property
    def _unbound_host_lock(self) -> Path:
        return self.state.layout.locks / "05-unbound-host-events.lock"

    def _store_unbound_host_event(self, *, semantic_key: str, record: dict[str, Any]) -> None:
        path = self._unbound_host_dir / f"{semantic_key}.json"
        with FileLock(self._unbound_host_lock):
            if path.exists():
                if read_json(path) != record:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "unbound host event payload conflict")
                return
            atomic_write_json(path, record, replace=False)

    def _matching_unbound_host_event(
        self, *, event_name: str, session_id: str, turn_id: str | None = None
    ) -> tuple[Path, dict[str, Any]] | None:
        matches: list[tuple[Path, dict[str, Any]]] = []
        with FileLock(self._unbound_host_lock):
            for path in self._unbound_host_dir.glob("*.json"):
                record = read_json(path)
                if record.get("event_name") != event_name or record.get("session_id") != session_id:
                    continue
                if turn_id is not None and record.get("turn_id") != turn_id:
                    continue
                matches.append((path, record))
        if len(matches) > 1:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "unbound host event is ambiguous")
        return matches[0] if matches else None

    def _mark_unbound_host_event_bound(self, path: Path, record: dict[str, Any], task_id: str) -> None:
        task_sha = digest_text(task_id)
        with FileLock(self._unbound_host_lock):
            current = read_json(path)
            existing = current.get("bound_task_sha256")
            if existing not in {None, task_sha}:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "unbound host event belongs to another task")
            if existing is None:
                current["bound_task_sha256"] = task_sha
                atomic_write_json(path, current)

    def _bind_unbound_session_start(self, *, task_id: str, payload: dict[str, Any]) -> None:
        session = str(payload["session_id"])
        match = self._matching_unbound_host_event(event_name="SessionStart", session_id=session)
        if match is None:
            return
        path, record = match
        self._observe_session_start(task_id=task_id, session=session, source=str(record["source"]))
        self._mark_unbound_host_event_bound(path, record, task_id)

    def _bind_unbound_user_prompt(self, *, task_id: str, payload: dict[str, Any]) -> None:
        session = str(payload["session_id"])
        turn = str(payload["turn_id"])
        match = self._matching_unbound_host_event(
            event_name="UserPromptSubmit", session_id=session, turn_id=turn
        )
        if match is None:
            return
        path, record = match
        self._observe_user_prompt(
            task_id=task_id,
            session=session,
            turn=turn,
            prompt_sha256=str(record["prompt_sha256"]),
            byte_length=int(record["byte_length"]),
        )
        self._mark_unbound_host_event_bound(path, record, task_id)

    def handle(self, payload: dict[str, Any]) -> bytes:
        event = payload.get("hook_event_name")
        if event == "PreToolUse":
            return self.pre_tool(payload)
        if event == "PostToolUse":
            return self.post_tool(payload)
        if event == "SessionStart":
            return self.session_start(payload)
        if event == "UserPromptSubmit":
            return self.user_prompt(payload)
        if event in {"PreCompact", "PostCompact"}:
            return self.compact(payload)
        if event in {"SubagentStart", "SubagentStop"}:
            return self.agent_event(payload)
        return b""

    def _validate_common(self, payload: dict[str, Any], *, turn: bool = False) -> tuple[str, str | None]:
        session = payload.get("session_id")
        turn_id = payload.get("turn_id")
        cwd = payload.get("cwd")
        if not isinstance(session, str) or not session:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "hook payload lacks session_id")
        if turn and (not isinstance(turn_id, str) or not turn_id):
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "hook payload lacks turn_id")
        if cwd is not None and Path(str(cwd)).resolve() != self.root:
            raise ReadPaperError(ErrorCode.ACCESS_DENIED, "ReadPaper hook cwd is outside the project root")
        return session, turn_id if isinstance(turn_id, str) else None

    def _run_for_invocation(self, invocation: Invocation) -> tuple[str, str | None, str | None]:
        explicit = invocation.flags.get("--task-id")
        if isinstance(explicit, str):
            return explicit, None, None
        run_id = invocation.positional[0]
        for path in self.state.layout.papers.glob(f"p_*/runs/{run_id}/inventory.json"):
            inventory = read_json(path)
            run = self.state.get_run(inventory["paper_id"], run_id)
            return run.task_id, run.paper_id, run_id
        raise ReadPaperError(ErrorCode.NOT_FOUND, "protected invocation does not resolve to a current run")

    def _task_for_payload(self, payload: dict[str, Any]) -> str | None:
        explicit = payload.get("task_id") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("READPAPER_TASK_ID")
        if isinstance(explicit, str) and explicit:
            return explicit
        session = payload.get("session_id")
        if isinstance(session, str):
            for path in self.state.layout.runtime.joinpath("task-bindings").glob("*.json"):
                binding = read_json(path)
                if binding.get("session_id") == session:
                    return str(binding["task_id"])
        return None

    def _actor_context(self, payload: dict[str, Any], task_id: str, invocation: Invocation) -> tuple[str, str, str, str, str]:
        session, turn = self._validate_common(payload, turn=True)
        agent_id = payload.get("agent_id")
        actor = "root"
        if agent_id is not None:
            if invocation.command != "record" or invocation.flags.get("--kind") not in {"audit_result", "flow_result"}:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "subagents may only submit assigned reviewer results")
            if not isinstance(agent_id, str) or not agent_id:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "subagent identity is invalid")
            actor = agent_id
        execution = sequence_id("ae", task_id, session, turn, actor)
        stream = sequence_id("ctx", task_id, session, actor)
        return session, str(turn), execution, stream, actor

    def _claim_reviewer_execution(
        self,
        *,
        task_id: str,
        paper_id: str,
        run_id: str,
        invocation: Invocation,
        agent_id: str,
        request_digest: str,
    ) -> tuple[str, dict[str, Any]]:
        """Bind a result call to its reservation using composite host evidence.

        Desktop does not repeat assignment metadata in ``SubagentStart``.  The
        trusted binding is therefore the conjunction of (1) the native start
        event and (2) this same native agent's immutable, nonce-bearing
        protected result request.  The CAS claim makes a copied challenge from
        another actor fail closed.
        """
        result_kind = str(invocation.flags.get("--kind"))
        start_kind = {"audit_result": "audit_start", "flow_result": "flow_start"}.get(result_kind)
        if start_kind is None:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer result kind is not bindable")
        payload_path = Path(str(invocation.flags.get("--payload"))).resolve()
        assert_regular_private_file(payload_path)
        if payload_path.stat().st_size > 4 * 1024 * 1024:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "reviewer result payload exceeds 4 MiB")
        try:
            result = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid reviewer result payload") from error
        if not isinstance(result, dict):
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "reviewer result payload must be an object")
        assignment_id = result.get("reviewer_assignment_id")
        if not isinstance(assignment_id, str) or not assignment_id:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer result lacks assignment identity")
        records_dir = self.state.layout.run_records(paper_id, run_id)
        starts = []
        for path in records_dir.glob("rec_*.json"):
            record = read_json(path)
            if (
                record.get("record_kind") == start_kind
                and record.get("payload", {}).get("reviewer_assignment_id") == assignment_id
            ):
                starts.append(record)
        if len(starts) != 1:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "exactly one reviewer reservation is required")
        start_record = starts[0]
        start = start_record["payload"]
        common_fields = (
            "reviewer_assignment_id",
            "assignment_nonce",
            "assignment_input_digest",
            "agent_execution_id",
            "attempt_no",
        )
        if any(result.get(field) != start.get(field) for field in common_fields):
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer challenge does not match reservation")
        if result_kind == "audit_result":
            audit_fields = ("audit_id", "audit_stage_id", "stage", "role")
            if any(result.get(field) != start.get(field) for field in audit_fields):
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "content audit result targets another reservation")
            agent_type = str(start.get("role"))
        else:
            flow_fields = ("flow_audit_id", "answer_id", "input_draft_version_id")
            if any(result.get(field) != start.get(field) for field in flow_fields):
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "flow result targets another reservation")
            agent_type = "explanation_flow"
        if result.get("reviewer_agent_id") != agent_id:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer result actor differs from host actor")
        expected_agent = start.get("expected_reviewer_agent_id")
        if expected_agent is not None and expected_agent != agent_id:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "same-reviewer reservation actor mismatch")

        floor = start.get("reservation_host_event_seq_floor")
        if expected_agent is None:
            if not isinstance(floor, int) or floor < 0:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "new reviewer reservation lacks host sequence floor")
            agent_starts = self.state.find_agent_starts(
                task_id=task_id,
                agent_id=None,
                agent_type=agent_type,
                after_host_event_seq=floor,
            )
        else:
            agent_starts = self.state.find_agent_starts(
                task_id=task_id,
                agent_id=agent_id,
                agent_type=agent_type,
            )
        if len(agent_starts) != 1:
            raise ReadPaperError(
                ErrorCode.OBSERVER_UNAVAILABLE,
                "exactly one matching semantic agent start is required",
            )
        agent_start = agent_starts[0]
        if agent_start.subject_id != agent_id:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer challenge actor did not own the reserved start")
        reserved_execution = start.get("agent_execution_id")
        nonce = start.get("assignment_nonce")
        input_digest = start.get("assignment_input_digest")
        if not all(isinstance(value, str) and value for value in (reserved_execution, nonce, input_digest)):
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer reservation is incomplete")
        binding = {
            "schema_version": 1,
            "evidence_kind": "agent_start_plus_protected_challenge_v1",
            "reviewer_assignment_id": assignment_id,
            "reservation_record_id": start_record["record_id"],
            "agent_started_host_event_id": agent_start.host_event_id,
            "agent_started_host_event_seq": agent_start.host_event_seq,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "reserved_agent_execution_id": reserved_execution,
            "assignment_nonce_sha256": digest_text(nonce),
            "assignment_input_digest": input_digest,
            "protected_request_digest": request_digest,
            "parent_agent_execution_id": start.get("reservation_parent_agent_execution_id"),
        }
        claimed = self.state.claim_reviewer_binding(
            task_id=task_id,
            reviewer_assignment_id=assignment_id,
            binding=binding,
        )
        return str(reserved_execution), claimed

    def pre_tool(self, payload: dict[str, Any]) -> bytes:
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if payload.get("tool_name") != "Bash" or not isinstance(command, str):
            return b""
        invocation = parse_command(command, python_path=self.python_path, script_path=self.script_path)
        if invocation is None:
            # Only commands targeting paper.py are fail-closed; unrelated Bash remains untouched.
            if str(self.script_path.resolve()) in command:
                return _deny("ReadPaper command is not the canonical direct invocation grammar.")
            return b""
        try:
            task_id, paper_id, run_id = self._run_for_invocation(invocation)
            # `check` is the contract's read-only inspection command. It has no
            # client request ID and creates no mutation capability or coverage
            # evidence, so canonical parsing plus run resolution is sufficient.
            if invocation.command == "check":
                return _allow()
            self._bind_unbound_session_start(task_id=task_id, payload=payload)
            self._bind_unbound_user_prompt(task_id=task_id, payload=payload)
            session, turn, execution, stream, actor = self._actor_context(payload, task_id, invocation)
            tool_use = payload.get("tool_use_id")
            if not isinstance(tool_use, str) or not tool_use:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "PreToolUse lacks tool_use_id")
            binding = self.state.bind_session(task_id=task_id, session_id=session, hard_boundary=False)
            request_digest = digest(bound_request_document(invocation))
            reviewer_binding = None
            if actor != "root":
                if paper_id is None or run_id is None:
                    raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "reviewer invocation has no current run")
                execution, reviewer_binding = self._claim_reviewer_execution(
                    task_id=task_id,
                    paper_id=paper_id,
                    run_id=run_id,
                    invocation=invocation,
                    agent_id=actor,
                    request_digest=request_digest,
                )
            command_sha = digest_text(command)
            semantic = digest({"kind": "PreToolUse/v2", "session_id": session, "turn_id": turn, "agent_id": actor, "tool_use_id": tool_use, "tool_name": "Bash"})
            host = self.state.append_host_event(
                task_id=task_id,
                event_kind=HostEventKind.PRETOOL_AUTHORIZED,
                semantic_key=semantic,
                subject_id=tool_use,
                payload={
                    "argv_sha256": command_sha,
                    "request_digest": request_digest,
                    "parser_schema_sha256": SCHEMA_SHA256,
                    "actor": "root_main" if actor == "root" else "subagent",
                    "reviewer_assignment_id": None if reviewer_binding is None else reviewer_binding["reviewer_assignment_id"],
                    "reviewer_binding_evidence_kind": None if reviewer_binding is None else reviewer_binding["evidence_kind"],
                },
            )
            from .stop import StopCoordinator

            StopCoordinator(self.root).claim_pretool_if_expected(
                task_id=task_id, payload=payload, command_sha256=command_sha, invocation=invocation,
                host_event_id=host.host_event_id,
            )
            capability = self.authority.issue(
                pretool_semantic_key=semantic,
                client_request_id=str(invocation.flags["--client-request-id"]),
                request_digest=request_digest,
                argv_sha256=command_sha,
                hook_definition_hash=HOOK_DEFINITION_HASH,
                task_id=task_id,
                session_id=session,
                turn_id=turn,
                tool_use_id=tool_use,
                agent_id=actor,
                agent_execution_id=execution,
                context_stream_id=stream,
                context_epoch=self._context_epoch(task_id, stream),
            )
            if capability["pretool_semantic_key"] != host.semantic_key:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "host event and capability diverged")
            return _allow()
        except ReadPaperError as error:
            return _deny(f"{error.code.value}: {error}")

    def _context_epoch(self, task_id: str, stream: str) -> int:
        path = self.state.layout.host_state(task_id)
        if not path.exists():
            return 0
        return int(read_json(path).get("compact_streams", {}).get(stream, {}).get("context_epoch", 0))

    def _capability_for_tool(self, tool_use_id: str) -> dict[str, Any] | None:
        for path in self.state.layout.runtime.joinpath("invocation-capabilities").glob("cap_*.json"):
            value = read_json(path)
            if value.get("tool_use_id") == tool_use_id:
                return value
        return None

    @staticmethod
    def _response_envelope(tool_response: Any) -> dict[str, Any] | None:
        candidates: list[str] = []
        if isinstance(tool_response, str):
            candidates.append(tool_response)
        elif isinstance(tool_response, dict):
            for key in ("output", "stdout", "text"):
                value = tool_response.get(key)
                if isinstance(value, str):
                    candidates.append(value)
        for candidate in candidates:
            for line in reversed(candidate.splitlines()):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("schema_version") == "1":
                    return value
        return None

    def post_tool(self, payload: dict[str, Any]) -> bytes:
        tool_use = payload.get("tool_use_id")
        if not isinstance(tool_use, str):
            return b""
        if payload.get("tool_name") == "view_image":
            return self._observe_visual_open(payload)
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if payload.get("tool_name") != "Bash" or not isinstance(command, str):
            return b""
        invocation = parse_command(command, python_path=self.python_path, script_path=self.script_path)
        if invocation is None:
            return b""
        capability = self._capability_for_tool(tool_use)
        if capability is None or capability.get("status") != "consumed":
            return b""
        envelope = self._response_envelope(payload.get("tool_response"))
        success = envelope is not None and envelope.get("ok") is True
        task_id = str(capability["task_id"])
        event_kind = HostEventKind.TOOL_COMPLETED if success else HostEventKind.TOOL_FAILED
        host = self.state.append_host_event(
            task_id=task_id,
            event_kind=event_kind,
            semantic_key=digest({"kind": "PostToolUse/v1", "session_id": payload.get("session_id"), "turn_id": payload.get("turn_id"), "tool_use_id": tool_use, "tool_name": "Bash"}),
            subject_id=tool_use,
            payload={"capability_id": capability["capability_id"], "response_sha256": digest(payload.get("tool_response")), "success": success},
        )
        if success and invocation.command == "read":
            data = envelope.get("data")
            if isinstance(data, dict) and isinstance(data.get("units"), list):
                for unit in data["units"]:
                    if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str) or not isinstance(unit.get("content"), str):
                        continue
                    self.state.append_event(
                        paper_id=str(envelope["paper_id"]), run_id=str(envelope["run_id"]),
                        event_kind=EventKind.UNIT_EMITTED, subject_id=unit["unit_id"], result=EventResult.SUCCEEDED,
                        actor=Actor.ROOT_MAIN,
                        payload={"content_sha256": digest_text(unit["content"]), "complete": True, "capability_id": capability["capability_id"]},
                        idempotency_key=f"posttool:{tool_use}:unit:{unit['unit_id']}", source_host_event_id=host.host_event_id,
                        client_request_id=capability["client_request_id"], session_id=capability["session_id"], turn_id=capability["turn_id"],
                        agent_id=None, agent_execution_id=capability["agent_execution_id"], context_stream_id=capability["context_stream_id"],
                        context_epoch=int(capability["context_epoch"]), tool_use_id=tool_use,
                    )
        return b""

    def _observe_visual_open(self, payload: dict[str, Any]) -> bytes:
        tool_input = payload.get("tool_input")
        raw_path = tool_input.get("path") if isinstance(tool_input, dict) else None
        if not isinstance(raw_path, str):
            return b""
        path = Path(raw_path).resolve()
        try:
            relative = path.relative_to(self.state.layout.papers)
        except ValueError:
            return b""
        parts = relative.parts
        if len(parts) < 5 or parts[1] != "runs" or parts[3] != "evidence":
            return b""
        paper_id, run_id = parts[0], parts[2]
        run = self.state.get_run(paper_id, run_id)
        task_id = run.task_id
        session, turn = self._validate_common(payload, turn=True)
        if payload.get("agent_id") is not None:
            return b""
        execution = sequence_id("ae", task_id, session, turn, "root")
        stream = sequence_id("ctx", task_id, session, "root")
        image_sha = sha256_bytes(path.read_bytes()) if path.is_file() else None
        event = self.state.append_host_event(
            task_id=task_id, event_kind=HostEventKind.TOOL_COMPLETED,
            semantic_key=digest({"kind": "PostToolUse/v1", "session_id": session, "turn_id": turn, "tool_use_id": payload["tool_use_id"], "tool_name": "view_image"}),
            subject_id=str(path), payload={"path_sha256": digest_text(str(path)), "image_sha256": image_sha, "actual_open": path.is_file()},
        )
        if path.is_file():
            self.state.append_event(
                paper_id=paper_id, run_id=run_id, event_kind=EventKind.VISUAL_OPEN_OBSERVED,
                subject_id=path.stem.rsplit("-", 1)[0], result=EventResult.SUCCEEDED, actor=Actor.ROOT_MAIN,
                payload={"path_sha256": digest_text(str(path)), "image_sha256": image_sha},
                idempotency_key=f"visual-open:{payload['tool_use_id']}", source_host_event_id=event.host_event_id,
                session_id=session, turn_id=turn, agent_execution_id=execution, context_stream_id=stream,
                context_epoch=self._context_epoch(task_id, stream), tool_use_id=payload["tool_use_id"],
            )
        return b""

    def session_start(self, payload: dict[str, Any]) -> bytes:
        session, _ = self._validate_common(payload)
        source = payload.get("source")
        if source not in {"startup", "resume", "clear", "compact"}:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "unknown SessionStart source")
        task_id = self._task_for_payload(payload)
        if task_id is None:
            semantic = digest({"kind": "UnboundSessionStart/v1", "session_id": session, "source": source})
            self._store_unbound_host_event(
                semantic_key=semantic,
                record={
                    "schema_version": 1,
                    "event_name": "SessionStart",
                    "session_id": session,
                    "source": source,
                    "semantic_key": semantic,
                    "bound_task_sha256": None,
                },
            )
            return b""
        self._observe_session_start(task_id=task_id, session=session, source=str(source))
        return b""

    def _observe_session_start(self, *, task_id: str, session: str, source: str) -> None:
        before = self.state.get_binding(task_id)
        hard_boundary = source != "compact" and before.session_id != session
        binding = self.state.bind_session(task_id=task_id, session_id=session, hard_boundary=source != "compact")
        host = self.state.append_host_event(task_id=task_id, event_kind=HostEventKind.SESSION_STARTED,
            semantic_key=digest({"kind": "SessionStart/v1", "session_id": session, "source": source, "session_epoch": binding.session_epoch}),
            subject_id=session, payload={"source": source, "session_epoch": binding.session_epoch})
        if source in {"startup", "resume", "clear"}:
            from .stop import StopCoordinator
            StopCoordinator(self.root).abandon_on_restart(task_id=task_id, session_id=session)
            if hard_boundary and before.delivery_candidate_answer_id:
                self.state.mark_delivery_unknown(
                    task_id=task_id,
                    reason_code=f"session_{source}_before_stop_observation",
                    authority_host_event_id=host.host_event_id,
                )
            if hard_boundary and before.active_run_id and before.current_paper_id:
                run = self.state.get_run(before.current_paper_id, before.active_run_id)
                if run.state.value in {"prepared", "reading", "reviewing", "needs_work"}:
                    self.state.transition(
                        task_id=task_id, paper_id=run.paper_id, run_id=run.run_id,
                        to_state=RunState.PAUSED,
                        actor=Actor.STATE_SERVICE, reason_code=f"session_{source}",
                    )
            if hard_boundary and before.pending_answer_id and before.current_run_id and before.current_paper_id:
                current = self.state.get_binding(task_id)
                if current.pending_answer_status not in {"interrupted", None}:
                    self.state.interrupt_answer(
                        task_id=task_id, paper_id=before.current_paper_id, run_id=before.current_run_id,
                        answer_id=before.pending_answer_id, reason_code=f"session_{source}",
                        authority_host_event_id=host.host_event_id,
                    )

    def user_prompt(self, payload: dict[str, Any]) -> bytes:
        session, turn = self._validate_common(payload, turn=True)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "UserPromptSubmit lacks exact prompt")
        task_id = self._task_for_payload(payload)
        if task_id is None:
            semantic = digest({"kind": "UserPromptSubmit/v1", "session_id": session, "turn_id": turn, "prompt_sha256": digest_text(prompt)})
            self._store_unbound_host_event(
                semantic_key=semantic,
                record={
                    "schema_version": 1,
                    "event_name": "UserPromptSubmit",
                    "session_id": session,
                    "turn_id": turn,
                    "prompt_sha256": digest_text(prompt),
                    "byte_length": len(prompt.encode()),
                    "semantic_key": semantic,
                    "bound_task_sha256": None,
                },
            )
            return b""
        return self._observe_user_prompt(
            task_id=task_id,
            session=session,
            turn=str(turn),
            prompt_sha256=digest_text(prompt),
            byte_length=len(prompt.encode()),
            payload=payload,
        )

    def _observe_user_prompt(
        self,
        *,
        task_id: str,
        session: str,
        turn: str,
        prompt_sha256: str,
        byte_length: int,
        payload: dict[str, Any] | None = None,
    ) -> bytes:
        event = self.state.append_host_event(task_id=task_id, event_kind=HostEventKind.USER_TURN_STARTED,
            semantic_key=digest({"kind": "UserPromptSubmit/v1", "session_id": session, "turn_id": turn, "prompt_sha256": prompt_sha256}),
            subject_id=str(turn), payload={"prompt_sha256": prompt_sha256, "byte_length": byte_length})
        from .stop import StopCoordinator
        hook_payload = payload or {"session_id": session, "turn_id": turn}
        result = StopCoordinator(self.root).claim_prompt_or_cancel(task_id=task_id, payload=hook_payload, prompt_sha256=prompt_sha256, host_event_id=event.host_event_id)
        if result == "duplicate":
            return _output({"decision": "block", "reason": "ReadPaper continuation was already consumed or is stale."})
        if result in {"ordinary", "cancelled"}:
            self.state.mark_delivery_unknown(
                task_id=task_id,
                reason_code="new_user_turn_before_stop_observation",
                authority_host_event_id=event.host_event_id,
            )
        return b""

    def compact(self, payload: dict[str, Any]) -> bytes:
        session, _ = self._validate_common(payload)
        task_id = self._task_for_payload(payload)
        if task_id is None:
            return b""
        actor = str(payload.get("agent_id") or "root")
        stream = sequence_id("ctx", task_id, session, actor)
        event_name = str(payload["hook_event_name"])
        trigger = payload.get("trigger")
        semantic_box: dict[str, str] = {}
        def transform(state):
            streams = {key: dict(value) for key, value in state.compact_streams.items()}
            current = dict(streams.get(stream, {"context_epoch": 0, "open": None, "completed": []}))
            if event_name == "PreCompact":
                if current["open"] is None:
                    ordinal = len(current["completed"]) + 1
                    semantic = digest({"kind": "PreCompact/v1", "session_id": session, "actor": actor, "trigger": trigger, "ordinal": ordinal})
                    current["open"] = {"semantic_key": semantic, "trigger": trigger, "ordinal": ordinal}
                elif current["open"].get("trigger") != trigger:
                    raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "ambiguous overlapping compact callback")
                semantic_box["value"] = current["open"]["semantic_key"]
            else:
                opened = current.get("open")
                if opened is None or opened.get("trigger") != trigger:
                    raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "PostCompact has no matching PreCompact")
                semantic = digest({"kind": "PostCompact/v1", "pre_semantic_key": opened["semantic_key"]})
                semantic_box["value"] = semantic
                if semantic not in current["completed"]:
                    current["context_epoch"] = int(current["context_epoch"]) + 1
                    current["completed"] = [*current["completed"], semantic]
                current["open"] = None
            streams[stream] = current
            return state.model_copy(update={"compact_streams": streams})
        host_state = self.state.update_host_state(task_id=task_id, transform=transform)
        semantic = semantic_box["value"]
        kind = HostEventKind.COMPACT_STARTED if event_name == "PreCompact" else HostEventKind.COMPACT_FINISHED
        self.state.append_host_event(task_id=task_id, event_kind=kind, semantic_key=semantic, subject_id=stream,
            payload={"trigger": trigger, "actor": actor, "context_epoch": host_state.compact_streams[stream]["context_epoch"]})
        return b""

    def agent_event(self, payload: dict[str, Any]) -> bytes:
        task_id = self._task_for_payload(payload)
        if task_id is None:
            return b""
        session, turn = self._validate_common(payload, turn=True)
        agent_id = payload.get("agent_id")
        agent_type = payload.get("agent_type")
        if not isinstance(agent_id, str) or not isinstance(agent_type, str):
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "subagent hook lacks actor identity")
        kind = HostEventKind.AGENT_STARTED if payload["hook_event_name"] == "SubagentStart" else HostEventKind.AGENT_STOPPED
        self.state.append_host_event(task_id=task_id, event_kind=kind,
            semantic_key=digest({"kind": payload["hook_event_name"] + "/v1", "session_id": session, "turn_id": turn, "agent_id": agent_id, "agent_type": agent_type}),
            subject_id=agent_id, payload={
                "agent_type": agent_type,
                "session_id": session,
                "turn_id": turn,
                "stop_hook_active": payload.get("stop_hook_active"),
            })
        return b""
