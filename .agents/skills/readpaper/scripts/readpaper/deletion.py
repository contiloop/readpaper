"""Exact-preview, separately confirmed, recoverable paper deletion."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .canonical import digest, digest_text
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id, validate_id
from .models import RunSnapshot, RunState, TaskBinding, utc_now
from .storage import FileLock, Layout, atomic_write_json, fsync_directory, read_json


BLOCKING_RUN_STATES = {
    RunState.PREPARED.value,
    RunState.READING.value,
    RunState.REVIEWING.value,
    RunState.NEEDS_WORK.value,
}


class DeletionService:
    def __init__(self, root: Path):
        self.layout = Layout(root)
        self.layout.initialize()

    def _scope(self, paper_id: str) -> dict[str, Any]:
        paper_path = self.layout.papers / paper_id
        if paper_path.is_symlink():
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "paper path may not be a symlink")
        runs: list[dict[str, str]] = []
        blockers: list[str] = []
        runs_dir = paper_path / "runs"
        if runs_dir.exists():
            for state_path in sorted(runs_dir.glob("run_*/state.json")):
                run = RunSnapshot.model_validate(read_json(state_path))
                runs.append({"run_id": run.run_id, "state": run.state.value})
                if run.state.value in BLOCKING_RUN_STATES:
                    blockers.append(f"run:{run.run_id}:{run.state.value}")
                for answer in run.answers.values():
                    if answer["answer_status"] not in {
                        "content_finalized",
                        "sent_verified",
                        "delivery_unknown",
                        "abandoned",
                    }:
                        blockers.append(f"answer:{answer['answer_id']}:{answer['answer_status']}")
        bindings: list[dict[str, Any]] = []
        for path in sorted((self.layout.runtime / "task-bindings").glob("*.json")):
            binding = TaskBinding.model_validate(read_json(path))
            if binding.current_paper_id == paper_id:
                bindings.append(
                    {
                        "binding_file": path.name,
                        "task_id_sha256": self.layout.task_hash(binding.task_id),
                        "active_run_id": binding.active_run_id,
                        "current_run_id": binding.current_run_id,
                        "pending_answer_id": binding.pending_answer_id,
                        "delivery_candidate_answer_id": binding.delivery_candidate_answer_id,
                    }
                )
        return {
            "paper_exists": paper_path.is_dir(),
            "paper_id": paper_id,
            "runs": runs,
            "bindings": bindings,
            "blockers": sorted(blockers),
        }

    def create_preview(self, *, task_id: str, paper_id: str, client_request_id: str) -> dict[str, Any]:
        validate_id(paper_id, prefix="p", lengths=(64,))
        with FileLock(self.layout.reference_lock):
            scope = self._scope(paper_id)
            if not scope["paper_exists"]:
                raise ReadPaperError(ErrorCode.NOT_FOUND, "paper is not stored")
            scope_digest = digest(scope)
            request_id = sequence_id("del", paper_id, task_id, client_request_id, scope_digest)
            path = self.layout.deletion_request(request_id)
            if path.exists():
                existing = read_json(path)
                if existing["scope_digest"] != scope_digest:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "deletion request replay conflict")
                return existing
            lines = [
                f"# ReadPaper 삭제 미리보기",
                "",
                f"- paper_id: `{paper_id}`",
                f"- deletion_request_id: `{request_id}`",
                f"- 저장 run: {len(scope['runs'])}",
                f"- 연결 task binding: {len(scope['bindings'])}",
                f"- 실행 차단 항목: {len(scope['blockers'])}",
                "",
                "계속하려면 별도 사용자 turn에서 다음 문구를 정확히 입력하세요:",
                "",
                f"`DELETE {paper_id} {request_id}`",
            ]
            preview = "\n".join(lines)
            request = {
                "schema_version": 1,
                "deletion_request_id": request_id,
                "task_id_sha256": self.layout.task_hash(task_id),
                "paper_id": paper_id,
                "client_request_id": client_request_id,
                "state": "created",
                "created_at": utc_now(),
                "scope": scope,
                "scope_digest": scope_digest,
                "preview_text": preview,
                "preview_content_sha256": digest_text(preview),
                "preview_message_host_event_id": None,
                "commit_plan": None,
                "operations": [],
                "response": None,
            }
            atomic_write_json(path, request, replace=False)
            return request

    def mark_presented(self, *, request_id: str, actual_message: str, host_event_id: str) -> dict[str, Any]:
        with FileLock(self.layout.reference_lock):
            path = self.layout.deletion_request(request_id)
            request = read_json(path)
            if request["state"] == "presented":
                if request["preview_message_host_event_id"] != host_event_id:
                    raise ReadPaperError(ErrorCode.STATE_CONFLICT, "preview host event changed")
                return request
            if request["state"] != "created" or digest_text(actual_message) != request["preview_content_sha256"]:
                raise ReadPaperError(ErrorCode.DELETE_CONFIRMATION_REQUIRED, "exact preview was not observed")
            request["state"] = "presented"
            request["preview_message_host_event_id"] = host_event_id
            request["presented_at"] = utc_now()
            atomic_write_json(path, request)
            return request

    def execute(
        self,
        *,
        request_id: str,
        task_id: str,
        approval_text: str,
        approval_turn_event_id: str,
    ) -> dict[str, Any]:
        with FileLock(self.layout.reference_lock):
            path = self.layout.deletion_request(request_id)
            request = read_json(path)
            if request["task_id_sha256"] != self.layout.task_hash(task_id):
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "deletion task identity mismatch")
            if request["state"] == "completed":
                return request["response"]
            if request["state"] == "committing":
                return self._replay(path, request)
            if request["state"] != "presented":
                raise ReadPaperError(ErrorCode.DELETE_CONFIRMATION_REQUIRED, "deletion preview is not presented")
            expected = f"DELETE {request['paper_id']} {request_id}"
            if approval_text != expected:
                raise ReadPaperError(ErrorCode.DELETE_CONFIRMATION_REQUIRED, "exact deletion confirmation required")
            current_scope = self._scope(request["paper_id"])
            if digest(current_scope) != request["scope_digest"]:
                request["state"] = "invalidated"
                request["invalidated_at"] = utc_now()
                atomic_write_json(path, request)
                raise ReadPaperError(ErrorCode.DELETE_SCOPE_CHANGED, "deletion scope changed after preview")
            if current_scope["blockers"]:
                raise ReadPaperError(
                    ErrorCode.DELETE_CONFIRMATION_REQUIRED,
                    "paper still has active run or pending answer blockers",
                    details={"blocking_ids": current_scope["blockers"]},
                )
            paper_path = self.layout.papers / request["paper_id"]
            stage_root = self.layout.deletion_stage(request_id)
            staged_paper = stage_root / request["paper_id"]
            before_bindings: list[dict[str, Any]] = []
            for item in current_scope["bindings"]:
                binding_path = self.layout.runtime / "task-bindings" / item["binding_file"]
                before_bindings.append({"path": item["binding_file"], "before": read_json(binding_path)})
            response = {
                "schema_version": 1,
                "status": "deleted",
                "paper_id": request["paper_id"],
                "deletion_request_id": request_id,
                "cleared_binding_count": len(before_bindings),
            }
            request["state"] = "committing"
            request["approval_turn_event_id"] = approval_turn_event_id
            request["commit_plan"] = {
                "paper_path": str(paper_path),
                "staged_paper": str(staged_paper),
                "bindings": before_bindings,
                "response": response,
            }
            atomic_write_json(path, request)
            return self._replay(path, request)

    def _replay(self, path: Path, request: dict[str, Any]) -> dict[str, Any]:
        plan = request["commit_plan"]
        if not isinstance(plan, dict):
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "committing deletion lacks plan")
        completed = {item["operation"] for item in request["operations"]}
        paper_path = Path(plan["paper_path"])
        staged_paper = Path(plan["staged_paper"])
        if "stage_paper" not in completed:
            staged_paper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if paper_path.exists() and not staged_paper.exists():
                paper_path.rename(staged_paper)
                fsync_directory(paper_path.parent)
                fsync_directory(staged_paper.parent)
            elif not staged_paper.exists():
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "paper disappeared outside deletion plan")
            request["operations"].append({"operation": "stage_paper", "completed_at": utc_now()})
            atomic_write_json(path, request)
        for item in plan["bindings"]:
            operation = f"clear_binding:{item['path']}"
            if operation in completed:
                continue
            binding_path = self.layout.runtime / "task-bindings" / item["path"]
            binding = TaskBinding.model_validate(read_json(binding_path))
            if binding.current_paper_id == request["paper_id"]:
                cleared = binding.model_copy(
                    update={
                        "active_run_id": None,
                        "current_run_id": None,
                        "current_paper_id": None,
                        "current_bundle_id": None,
                        "pending_answer_id": None,
                        "pending_answer_status": None,
                        "current_response_attempt_id": None,
                        "delivery_candidate_answer_id": None,
                        "delivery_candidate_status": None,
                        "delivery_candidate_response_attempt_id": None,
                        "delivery_candidate_run_id": None,
                        "delivery_candidate_paper_id": None,
                    }
                )
                atomic_write_json(binding_path, cleared.model_dump(mode="json"))
            request["operations"].append({"operation": operation, "completed_at": utc_now()})
            atomic_write_json(path, request)
        completed = {item["operation"] for item in request["operations"]}
        if "remove_staged_paper" not in completed:
            if staged_paper.exists():
                shutil.rmtree(staged_paper)
                fsync_directory(staged_paper.parent)
            request["operations"].append({"operation": "remove_staged_paper", "completed_at": utc_now()})
            atomic_write_json(path, request)
        request["state"] = "completed"
        request["completed_at"] = utc_now()
        request["response"] = plan["response"]
        atomic_write_json(path, request)
        return plan["response"]
