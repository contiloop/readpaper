"""Command adapter for the eight ReadPaper P0 commands."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .authority import InvocationAuthority, bound_request_document
from .audits import (
    AuditStage,
    ContentRole,
    content_audit_stage_returned,
    flow_finding_id,
    validate_content_findings,
)
from .canonical import canonical_bytes, digest, digest_text
from .archives import inspect_zip
from .deletion import DeletionService
from .documents import (
    TRANSPORT_FRAME_TOKEN_LIMIT,
    extract_pdf,
    extract_text,
    materialize_frame,
    render_image,
    render_pdf_page,
)
from .errors import ErrorCode, ReadPaperError
from .ids import artifact_id, artifact_ref_id, bundle_id, paper_id, sequence_id
from .models import AnswerStatus, Actor, EventKind, EventResult, RecordKind, RunState, ScopeKind, utc_now
from .parse_invocation import Invocation
from .sources import MediaKind, classify_media, discover_landing, fetch_public_url, local_source_token, normalize_url
from .state import StateService
from .storage import atomic_write_json, assert_regular_private_file, read_json


EVENT_FOR_RECORD = {
    "scope_confirmation": EventKind.SCOPE_CONFIRMED,
    "printed_label": EventKind.PRINTED_LABEL_RECORDED,
    "locator_candidate": EventKind.LOCATOR_CANDIDATE_RECORDED,
    "locator_confirmation": EventKind.LOCATOR_CONFIRMED,
    "understanding_note": EventKind.NOTE_VERSIONED,
    "model_request": EventKind.MODEL_REQUESTED,
    "agent_execution": EventKind.AGENT_EXECUTION_STATUSED,
    "model_observation": EventKind.MODEL_OBSERVED,
    "audit_start": EventKind.AUDIT_STARTED,
    "audit_result": EventKind.AUDIT_RESULT_RECORDED,
    "finding_disposition": EventKind.FINDING_DISPOSITIONED,
    "explanation_draft": EventKind.DRAFT_VERSIONED,
    "flow_start": EventKind.FLOW_AUDIT_STARTED,
    "flow_result": EventKind.FLOW_RESULT_RECORDED,
    "flow_finding_disposition": EventKind.FLOW_FINDING_DISPOSITIONED,
    "explanation_finalized": EventKind.EXPLANATION_FINALIZED,
    "user_pause": EventKind.USER_PAUSED,
    "answer_grounding": EventKind.ANSWER_GROUNDED,
}

ROOT_WRITABLE_RECORDS = {
    "scope_confirmation", "printed_label", "locator_candidate", "locator_confirmation",
    "understanding_note", "model_request", "audit_start", "finding_disposition",
    "explanation_draft", "flow_start", "flow_finding_disposition", "explanation_finalized",
    "user_pause", "answer_grounding",
}
REVIEWER_WRITABLE_RECORDS = {"audit_result", "flow_result"}


class CommandRuntime:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = StateService(self.root)
        self.authority = InvocationAuthority(self.root)
        self.deletion = DeletionService(self.root)

    def execute(self, invocation: Invocation) -> bytes:
        request = bound_request_document(invocation)
        request_digest = digest(request)
        client = invocation.flags.get("--client-request-id")
        if invocation.command == "check":
            return self._encode(self._dispatch(invocation, None))
        if not isinstance(client, str):
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "client request ID is required")
        scope_key = self._scope_key(invocation)
        route, replay = self.authority.consume_and_reserve(
            scope_key=scope_key,
            client_request_id=client,
            request_digest=request_digest,
        )
        if replay is not None:
            return replay["response"].encode("utf-8")
        capability = self.authority.get_capability(route["capability_id"])
        requested_task = invocation.flags.get("--task-id")
        if requested_task is not None and requested_task != capability["task_id"]:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "capability is bound to another task")
        try:
            response = self._encode(self._dispatch(invocation, capability))
        except ReadPaperError as error:
            response = error_envelope(invocation.command, error)
        self.authority.complete(
            scope_key=scope_key,
            client_request_id=client,
            request_digest=request_digest,
            response=response,
        )
        return response

    def _scope_key(self, invocation: Invocation) -> str:
        client = str(invocation.flags["--client-request-id"])
        if invocation.command in {"prepare", "answer", "resume", "delete"}:
            return ":".join(
                [str(invocation.flags.get("--task-id")), invocation.command, self._mode(invocation), client]
            )
        return ":".join([invocation.positional[0], invocation.command, client])

    def _mode(self, invocation: Invocation) -> str:
        for value in ("--begin", "--resume", "--abandon", "--finalize", "--preview", "--execute"):
            if value in invocation.flags:
                return value.removeprefix("--")
        return "default"

    def _dispatch(self, invocation: Invocation, capability: dict[str, Any] | None) -> dict[str, Any]:
        handler = getattr(self, f"_{invocation.command}")
        return handler(invocation, capability)

    def _prepare(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        source_value = invocation.positional[0]
        landing_url: str | None = None
        resolved_url: str | None = None
        supplementary_urls: list[str] = []
        if source_value.startswith(("http://", "https://")):
            fetched = fetch_public_url(source_value)
            resolved_url = fetched.final_url
            kind = classify_media(fetched.data, fetched.content_type)
            if kind == MediaKind.HTML:
                landing_url = fetched.final_url
                discovery = discover_landing(fetched.data, landing_url)
                supplementary_urls = list(discovery.supplementary_urls)
                if not discovery.main_candidates:
                    raise ReadPaperError(ErrorCode.UNSUPPORTED_SOURCE, "landing page has no deterministic main PDF")
                fetched = fetch_public_url(discovery.main_candidates[0][1])
                resolved_url = fetched.final_url
                kind = classify_media(fetched.data, fetched.content_type)
            if kind != MediaKind.PDF:
                raise ReadPaperError(ErrorCode.UNSUPPORTED_SOURCE, "resolved main source is not a PDF")
            data = fetched.data
            source_token = normalize_url(source_value)
        else:
            source = Path(source_value).resolve()
            if not source.is_file() or source.is_symlink():
                raise ReadPaperError(ErrorCode.NOT_FOUND, "local source must be a regular file")
            data = source.read_bytes()
            source_token = local_source_token(source)
        if len(data) > 128 * 1024 * 1024:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "source exceeds 128 MiB")
        if classify_media(data) != MediaKind.PDF:
            raise ReadPaperError(ErrorCode.UNSUPPORTED_SOURCE, "P0 prepare currently requires a PDF main source")
        paper = paper_id(data)
        artifact = artifact_id(data)
        ref = artifact_ref_id(role="main", source_token=source_token)
        artifact_record = {
            "artifact_ref_id": ref,
            "artifact_id": artifact,
            "role": "main",
            "media_kind": "pdf",
            "support_state": "supported",
            "discovery_url": source_value if resolved_url else None,
            "resolved_url": resolved_url,
            "parent_artifact_id": None,
            "archive_member_path": None,
            "detected_content_type": "application/pdf",
            "size_bytes": len(data),
            "sha256": artifact.removeprefix("a_"),
            "failure_code": None,
        }
        artifact_records = [artifact_record]
        artifact_bytes: dict[str, bytes] = {artifact: data}
        for supplement_index, supplement_url in enumerate(supplementary_urls, start=1):
            supplement = fetch_public_url(supplement_url)
            parent = artifact_id(supplement.data)
            parent_ref = artifact_ref_id(role="supplementary", source_token=supplement.final_url)
            try:
                supplement_kind = classify_media(supplement.data, supplement.content_type)
            except ReadPaperError as error:
                if error.code not in {ErrorCode.UNSUPPORTED_ARTIFACT, ErrorCode.CORRUPT_ARTIFACT}:
                    raise
                supplement_kind = "unsupported"
            members = ()
            archive_failure: str | None = None
            if supplement_kind == MediaKind.ZIP:
                try:
                    members = inspect_zip(supplement.data)
                except ReadPaperError as error:
                    if error.code is not ErrorCode.UNSUPPORTED_ARTIFACT:
                        raise
                    archive_failure = error.code.value
            if supplement_kind == "unsupported" or archive_failure is not None:
                artifact_records.append({
                    "artifact_ref_id": parent_ref, "artifact_id": parent, "role": "supplementary",
                    "media_kind": supplement_kind, "support_state": "unsupported",
                    "discovery_url": supplement_url, "resolved_url": supplement.final_url,
                    "parent_artifact_id": None, "archive_member_path": None,
                    "detected_content_type": supplement.content_type, "size_bytes": len(supplement.data),
                    "sha256": parent.removeprefix("a_"),
                    "failure_code": archive_failure or ErrorCode.UNSUPPORTED_ARTIFACT.value,
                })
                artifact_bytes[parent] = supplement.data
                continue
            if supplement_kind == MediaKind.ZIP:
                artifact_records.append({
                    "artifact_ref_id": parent_ref, "artifact_id": parent, "role": "supplementary_container",
                    "media_kind": "zip", "support_state": "container", "discovery_url": supplement_url,
                    "resolved_url": supplement.final_url, "parent_artifact_id": None, "archive_member_path": None,
                    "detected_content_type": supplement.content_type, "size_bytes": len(supplement.data),
                    "sha256": parent.removeprefix("a_"), "failure_code": None,
                })
                artifact_bytes[parent] = supplement.data
                expanded = [(item.path, item.data, item.media_kind) for item in members]
            else:
                expanded = [(None, supplement.data, supplement_kind)]
            for member_index, (member_path, member_data, member_kind) in enumerate(expanded, start=1):
                member_artifact = artifact_id(member_data)
                token = f"{supplement.final_url}#{member_path}" if member_path else supplement.final_url
                member_ref = artifact_ref_id(role="supplementary", source_token=token)
                prose_extension = Path(member_path or urlsplit(supplement.final_url).path).suffix.casefold()
                supported = not (member_kind == MediaKind.TEXT and prose_extension not in {".txt", ".md", ".markdown", ".rst"})
                artifact_records.append({
                    "artifact_ref_id": member_ref, "artifact_id": member_artifact, "role": "supplementary",
                    "media_kind": member_kind, "support_state": "supported" if supported else "unsupported",
                    "discovery_url": supplement_url, "resolved_url": supplement.final_url,
                    "parent_artifact_id": parent if member_path else None, "archive_member_path": member_path,
                    "detected_content_type": supplement.content_type if not member_path else None,
                    "size_bytes": len(member_data), "sha256": member_artifact.removeprefix("a_"),
                    "failure_code": None if supported else ErrorCode.UNSUPPORTED_ARTIFACT.value,
                })
                artifact_bytes[member_artifact] = member_data
        bundle = bundle_id(schema_version=2, paper_id=paper, landing_url=landing_url, artifacts=artifact_records)
        object_paths: dict[str, str] = {}
        for artifact_key, artifact_data in artifact_bytes.items():
            stored_artifact, stored_path = self.state.put_object(artifact_data)
            if stored_artifact != artifact_key:
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "object identity changed")
            object_paths[artifact_key] = str(stored_path)
        object_path = Path(object_paths[artifact])
        documents = []
        for record in artifact_records:
            if record["support_state"] != "supported":
                continue
            artifact_data = artifact_bytes[record["artifact_id"]]
            if record["media_kind"] == MediaKind.PDF:
                documents.append((record, extract_pdf(artifact_data, bundle_id=bundle, artifact_ref_id=record["artifact_ref_id"], artifact_id=record["artifact_id"])))
            elif record["media_kind"] == MediaKind.TEXT:
                documents.append((record, extract_text(artifact_data, bundle_id=bundle, artifact_ref_id=record["artifact_ref_id"], artifact_id=record["artifact_id"])))
        task_id = str(invocation.flags["--task-id"])
        run = self.state.create_run(task_id=task_id, paper_id=paper, bundle_id=bundle)
        manifest = {
            "schema_version": 2,
            "paper_id": paper,
            "bundle_id": bundle,
            "prepared_at": utc_now(),
            "landing_url": landing_url,
            "artifacts": artifact_records,
        }
        bundle_path = self.state.layout.papers / paper / "bundles" / bundle / "manifest.json"
        if bundle_path.exists():
            existing_manifest = read_json(bundle_path)
            identity_existing = {key: value for key, value in existing_manifest.items() if key != "prepared_at"}
            identity_new = {key: value for key, value in manifest.items() if key != "prepared_at"}
            if identity_existing != identity_new:
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "immutable bundle manifest changed")
        else:
            atomic_write_json(bundle_path, manifest, replace=False)
        visual_units = []
        for record, extracted in documents:
            if record["media_kind"] == MediaKind.PDF:
                visual_units.extend({"unit_id": f"{record['artifact_ref_id']}:p{page.pdf_page:06d}:visual", "artifact_ref_id": record["artifact_ref_id"], "artifact_id": record["artifact_id"], "media_kind": "pdf", "pdf_page": page.pdf_page} for page in extracted.pages)
        for record in artifact_records:
            if record["support_state"] == "supported" and record["media_kind"] == MediaKind.IMAGE:
                visual_units.append({"unit_id": f"{record['artifact_ref_id']}:image", "artifact_ref_id": record["artifact_ref_id"], "artifact_id": record["artifact_id"], "media_kind": "image", "pdf_page": None})
        all_sections = [asdict(item) for _, extracted in documents for item in extracted.sections]
        for ordinal, section in enumerate(all_sections, start=1):
            section["ordinal"] = ordinal
        all_frames = [asdict(item) for _, extracted in documents for item in extracted.frames]
        all_pages = [asdict(page) | {"artifact_ref_id": record["artifact_ref_id"], "artifact_id": record["artifact_id"]} for record, extracted in documents for page in extracted.pages]
        paper_text_tokens = sum(int(item["estimated_tokens"]) for item in all_sections)
        emitted_text_tokens = sum(int(item["estimated_tokens"]) for item in all_frames)
        transport_overhead_tokens = max(0, emitted_text_tokens - paper_text_tokens)
        estimated_total_source_tokens = paper_text_tokens + transport_overhead_tokens
        inventory = {
            "schema_version": 2,
            "paper_id": paper,
            "bundle_id": bundle,
            "run_id": run.run_id,
            "source_object_path": str(object_path),
            "object_paths": object_paths,
            "pages": all_pages,
            "sections": all_sections,
            "frames": all_frames,
            "visual_units": visual_units,
            "reading_policy": {
                "semantic_unit": "section",
                "transport_unit": "frame",
                "transport_frame_token_limit": TRANSPORT_FRAME_TOKEN_LIMIT,
                "current_epoch_required": True,
            },
        }
        atomic_write_json(self.state.layout.run_dir(paper, run.run_id) / "inventory.json", inventory)
        self.state.append_event(
            paper_id=paper,
            run_id=run.run_id,
            event_kind=EventKind.SOURCE_PREPARED,
            subject_id=bundle,
            result=EventResult.SUCCEEDED,
            actor=Actor.STATE_SERVICE,
            payload={"artifact_ref_ids": [item["artifact_ref_id"] for item in artifact_records], "page_count": len(all_pages)},
            idempotency_key=f"prepare:{invocation.flags['--client-request-id']}:source",
            client_request_id=str(invocation.flags["--client-request-id"]),
        )
        return self._success(
            invocation.command,
            paper,
            bundle,
            run.run_id,
            {
                "prepare_operation_id": f"po_{digest([task_id, invocation.flags['--client-request-id']])}",
                "paper_id": paper,
                "bundle_id": bundle,
                "run_id": run.run_id,
                "task_id": task_id,
                "proposed_scope_kind": "full",
                "scope_locked": False,
                "artifacts": artifact_records,
                "sections": all_sections,
                "transport_frames": [{**item, "content": None} for item in all_frames],
                "visual_units": visual_units,
                "page_counts": {record["artifact_ref_id"]: len(extracted.pages) for record, extracted in documents if record["media_kind"] == MediaKind.PDF},
                "paper_input_estimate": estimated_total_source_tokens,
                "artifact_exclusion_estimates": [],
                "limits_applied": {
                    "pdf_pages": 200,
                    "semantic_unit": "section",
                    "transport_frame_tokens": TRANSPORT_FRAME_TOKEN_LIMIT,
                    "tool_output_tokens": 65_536,
                    "auto_compact_tokens": 850_000,
                },
                "residency_plan": {
                    "strategy": "full_source_section_stream",
                    "paper_text_tokens": paper_text_tokens,
                    "transport_overhead_tokens": transport_overhead_tokens,
                    "estimated_total_source_tokens": estimated_total_source_tokens,
                    "target_auto_compact_limit": 850_000,
                    "context_reserve_tokens": 200_000,
                    "estimated_to_fit": estimated_total_source_tokens <= 650_000,
                },
                "warnings": [warning for _, extracted in documents for page in extracted.pages for warning in page.warnings],
                "scope_limitations": [],
            },
        )

    def _inventory(self, run_id: str) -> tuple[dict[str, Any], Any]:
        for path in self.state.layout.papers.glob(f"p_*/runs/{run_id}/inventory.json"):
            inventory = read_json(path)
            return inventory, self.state.get_run(inventory["paper_id"], run_id)
        raise ReadPaperError(ErrorCode.NOT_FOUND, "run inventory not found")

    def _read(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        inventory, run = self._inventory(invocation.positional[0])
        if int(inventory.get("schema_version", 0)) != 2:
            raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "read requires inventory schema 2")
        if not run.scope_locked:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "reading scope must be locked before source emission")
        if run.state not in {RunState.READING, RunState.REVIEWING, RunState.NEEDS_WORK, RunState.COMPLETE}:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"source cannot be read while run is {run.state.value}")
        binding = self.state.get_binding(run.task_id)
        frame_id = str(invocation.flags["--frame-id"])
        frame = next((item for item in inventory["frames"] if item["frame_id"] == frame_id), None)
        if frame is None:
            raise ReadPaperError(ErrorCode.NOT_FOUND, "transport frame not found")
        section = next((item for item in inventory["sections"] if item["section_id"] == frame["section_id"]), None)
        if section is None:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "frame references a missing section")
        if section["artifact_ref_id"] not in set(run.required_artifact_ref_ids):
            raise ReadPaperError(ErrorCode.ACCESS_DENIED, "transport frame is outside the locked reading scope")
        content = materialize_frame(pages=inventory["pages"], frame=frame, section=section)
        frame_index = next(index for index, item in enumerate(inventory["frames"]) if item["frame_id"] == frame_id)
        next_frame_id = (
            inventory["frames"][frame_index + 1]["frame_id"]
            if frame_index + 1 < len(inventory["frames"])
            else None
        )
        data = {
            "request_mode": "section_transport_frame",
            "section": {
                key: section[key]
                for key in (
                    "section_id", "ordinal", "title", "normalized_title", "level",
                    "parent_section_id", "start_page", "end_page", "estimated_tokens",
                    "detection_method", "detection_confidence",
                )
            },
            "frame": frame,
            "content": content,
            "next_frame_id": next_frame_id,
            "invocation_capability_id": capability["capability_id"],
            "client_request_id": invocation.flags["--client-request-id"],
            "tool_use_id": capability["tool_use_id"],
            "task_id": capability["task_id"],
            "session_id": capability["session_id"],
            "turn_id": capability["turn_id"],
            "agent_id": capability["agent_id"],
            "agent_execution_id": capability["agent_execution_id"],
            "context_stream_id": capability["context_stream_id"],
            "context_epoch": capability["context_epoch"],
            "session_epoch": binding.session_epoch,
        }
        return self._success("read", run.paper_id, run.bundle_id, run.run_id, data)

    def _render(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        inventory, run = self._inventory(invocation.positional[0])
        visual = next((item for item in inventory["visual_units"] if item["unit_id"] == invocation.flags["--unit-id"]), None)
        if visual is None:
            raise ReadPaperError(ErrorCode.NOT_FOUND, "visual unit not found")
        dpi = int(str(invocation.flags.get("--render-dpi", "144")))
        output = self.state.layout.run_dir(run.paper_id, run.run_id) / "evidence" / f"{visual['unit_id']}-{dpi}.png"
        source_path = Path(inventory.get("object_paths", {}).get(visual["artifact_id"], inventory["source_object_path"]))
        if visual.get("media_kind", "pdf") == "image":
            rendered = render_image(source_path, output=output)
            dpi = None
        else:
            rendered = render_pdf_page(source_path, pdf_page=visual["pdf_page"], output=output, dpi=dpi)
        render_id = f"ren_{digest([run.bundle_id, visual['unit_id'], rendered.image_sha256, dpi, None])}"
        data = visual | asdict(rendered) | {
            "path": str(rendered.path),
            "render_id": render_id,
            "bbox": None,
            "invocation_capability_id": capability["capability_id"],
            "client_request_id": invocation.flags["--client-request-id"],
            "tool_use_id": capability["tool_use_id"],
            "task_id": capability["task_id"],
            "session_id": capability["session_id"],
            "turn_id": capability["turn_id"],
            "agent_id": capability["agent_id"],
            "agent_execution_id": capability["agent_execution_id"],
            "context_stream_id": capability["context_stream_id"],
            "context_epoch": capability["context_epoch"],
            "session_epoch": self.state.get_binding(run.task_id).session_epoch,
        }
        data["render_dpi"] = dpi
        self.state.append_event(
            paper_id=run.paper_id,
            run_id=run.run_id,
            event_kind=EventKind.RENDER_CREATED,
            subject_id=render_id,
            result=EventResult.SUCCEEDED,
            actor=Actor.STATE_SERVICE,
            payload={
                "unit_id": visual["unit_id"],
                "image_sha256": rendered.image_sha256,
                "pixel_sha256": rendered.pixel_sha256,
                "pixel_width": rendered.pixel_width,
                "pixel_height": rendered.pixel_height,
                "render_dpi": dpi,
                "bbox": None,
            },
            idempotency_key=f"render:{invocation.flags['--client-request-id']}:{render_id}",
            client_request_id=str(invocation.flags["--client-request-id"]),
            session_id=capability["session_id"],
            turn_id=capability["turn_id"],
            agent_id=capability["agent_id"],
            agent_execution_id=capability["agent_execution_id"],
            context_stream_id=capability["context_stream_id"],
            context_epoch=capability["context_epoch"],
            tool_use_id=capability["tool_use_id"],
        )
        return self._success("render", run.paper_id, run.bundle_id, run.run_id, data)

    def _record(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        inventory, run = self._inventory(invocation.positional[0])
        kind = str(invocation.flags["--kind"])
        try:
            record_kind = RecordKind(kind)
        except ValueError as error:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "unknown record kind") from error
        if record_kind in {RecordKind.AUDIT_FINDING, RecordKind.FLOW_FINDING}:
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "internal child record kind is not caller-writable")
        is_root = capability.get("agent_id") == "root"
        if is_root and kind not in ROOT_WRITABLE_RECORDS:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "record kind is not writable by root Main")
        if not is_root and kind not in REVIEWER_WRITABLE_RECORDS:
            raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "record kind is not writable by a reviewer")
        payload_path = Path(str(invocation.flags["--payload"])).resolve()
        assert_regular_private_file(payload_path)
        if payload_path.stat().st_size > 4 * 1024 * 1024:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "record payload exceeds 4 MiB")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "record payload must be an object")
        if kind == "scope_confirmation":
            try:
                scope_kind = ScopeKind(payload["scope_kind"])
                required = list(payload["required_artifact_ref_ids"])
                excluded = list(payload["excluded_artifacts"])
                authority_turn = self.state.find_user_turn(task_id=run.task_id, turn_or_event_id=str(payload["user_turn_id"]))
                authority_event_id = authority_turn.host_event_id
            except (KeyError, TypeError, ValueError) as error:
                raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid scope confirmation payload") from error
            artifacts = {item["artifact_ref_id"]: item for item in read_json(self.state.layout.papers / run.paper_id / "bundles" / run.bundle_id / "manifest.json")["artifacts"]}
            supported = {key for key, item in artifacts.items() if item["support_state"] == "supported"}
            semantic_artifacts = {key for key, item in artifacts.items() if item["support_state"] != "container"}
            required_set = set(required)
            excluded_refs = {str(item.get("artifact_ref_id")) for item in excluded}
            if not required_set <= supported or required_set & excluded_refs:
                raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "scope contains unsupported, unknown, or both required and excluded refs")
            if scope_kind is ScopeKind.FULL and (excluded or required_set != supported):
                raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "full scope must include every supported artifact")
            if scope_kind is ScopeKind.FULL and any(artifacts[key]["support_state"] != "supported" for key in semantic_artifacts):
                raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "full scope contains unsupported supplementary material")
            if scope_kind is ScopeKind.USER_REDUCED:
                if required_set | excluded_refs != semantic_artifacts:
                    raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "reduced scope must classify every artifact")
                for item in excluded:
                    if item.get("reason_code") not in {"user_excluded", "unsupported", "unavailable", "failed", "over_budget"}:
                        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid scope exclusion reason")
                    if item.get("user_confirmation_event_id") != authority_event_id:
                        raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "scope exclusion is not bound to the observed user turn")
            selected_sections = [item for item in inventory["sections"] if item["artifact_ref_id"] in required_set]
            selected_section_ids = {item["section_id"] for item in selected_sections}
            selected_frames = [item for item in inventory["frames"] if item["section_id"] in selected_section_ids]
            selected_visuals = [item for item in inventory["visual_units"] if item["artifact_ref_id"] in required_set]
            estimate = sum(int(item["estimated_tokens"]) for item in selected_frames) + len(selected_visuals) * 2000 + 4000
            if estimate > 650_000:
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "locked scope exceeds the 650,000-token residency estimate")
            disclosure = ""
            if scope_kind is ScopeKind.USER_REDUCED:
                lines = ["> ReadPaper 범위 제한: 다음 자료는 제외되어 이 답변은 요청 범위만 다룹니다."]
                for item in sorted(excluded, key=lambda value: value["artifact_ref_id"]):
                    artifact_item = artifacts[item["artifact_ref_id"]]
                    lines.append(f"> - ref={item['artifact_ref_id']}; media={artifact_item['media_kind']}; reason={item['reason_code']}; failure={artifact_item.get('failure_code') or 'none'}")
                disclosure = "\n".join(lines)
            disclosure_sha = digest_text(disclosure)
            payload = payload | {"scope_disclosure_markdown": disclosure, "scope_disclosure_sha256": disclosure_sha, "paper_input_estimate": estimate}
            record = self.state.put_versioned_record(
                paper_id=run.paper_id, run_id=run.run_id, record_kind=kind, entity_id=run.run_id, payload=payload,
            )
            event = self.state.lock_scope(
                paper_id=run.paper_id,
                run_id=run.run_id,
                scope_kind=scope_kind,
                required_artifact_ref_ids=required,
                excluded_artifacts=excluded,
                authority_event_id=authority_event_id,
                scope_disclosure_markdown=disclosure,
                scope_disclosure_sha256=disclosure_sha,
            )
            current_after_scope = self.state.get_run(run.paper_id, run.run_id)
            transition_event = None
            if current_after_scope.state is RunState.PREPARED:
                transition_event = self.state.transition(
                    task_id=run.task_id, paper_id=run.paper_id, run_id=run.run_id,
                    to_state=RunState.READING, actor=Actor.ROOT_MAIN, reason_code="scope_locked",
                    authority_event_id=event.event_id,
                )
            return self._success(
                "record", run.paper_id, run.bundle_id, run.run_id,
                {
                    "record_id": record.record_id,
                    "record_kind": kind,
                    "entity_id": run.run_id,
                    "version_id": None,
                    "parent_version_id": None,
                    "payload_sha256": digest(payload),
                    "child_record_ids": [],
                    "related_record_ids": [],
                    "primary_event_id": event.event_id,
                    "appended_events": [
                        {"event_id": event.event_id, "event_seq": event.event_seq, "event_kind": event.event_kind.value, "subject_id": event.subject_id},
                        *([] if transition_event is None else [{"event_id": transition_event.event_id, "event_seq": transition_event.event_seq, "event_kind": transition_event.event_kind.value, "subject_id": transition_event.subject_id}]),
                    ],
                    "run_state": self.state.get_run(run.paper_id, run.run_id).state.value,
                    "context_stream_id": capability["context_stream_id"],
                    "context_epoch": capability["context_epoch"],
                    "session_epoch": self.state.get_binding(run.task_id).session_epoch,
                },
            )
        if kind in {"audit_start", "flow_start"}:
            host_state_path = self.state.layout.host_state(run.task_id)
            host_event_seq_floor = (
                int(read_json(host_state_path).get("host_event_seq", 0))
                if host_state_path.exists()
                else 0
            )
            payload = payload | {
                "reservation_host_event_seq_floor": host_event_seq_floor,
                "reservation_parent_agent_id": capability["agent_id"],
                "reservation_parent_agent_execution_id": capability["agent_execution_id"],
                "reservation_session_id": capability["session_id"],
                "reservation_turn_id": capability["turn_id"],
            }
        entity_id = str(payload.get("entity_id") or f"{kind}_{digest([run.run_id, payload])}")
        version_id = payload.get("version_id")
        parent_version_id = payload.get("parent_version_id")
        if kind in {"understanding_note", "explanation_draft"} and not isinstance(version_id, str):
            raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "versioned note/draft requires version_id")
        child_records = []
        if kind == "audit_result":
            try:
                stage = AuditStage(str(payload["stage"]))
                validated = validate_content_findings(
                    audit_stage_id=str(payload["audit_stage_id"]), attempt_no=int(payload["attempt_no"]),
                    stage=stage, findings=list(payload.get("findings", [])),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid content audit result") from error
            for finding in validated:
                child_records.append(self.state.put_versioned_record(
                    paper_id=run.paper_id, run_id=run.run_id, record_kind="audit_finding",
                    entity_id=str(finding["finding_id"]), payload=finding,
                ))
        if kind == "flow_result":
            try:
                flow_id = str(payload["flow_audit_id"])
                attempt_no = int(payload["attempt_no"])
                findings = list(payload.get("findings", []))
            except (KeyError, TypeError, ValueError) as error:
                raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid flow result") from error
            for ordinal, finding in enumerate(findings, start=1):
                body = {key: value for key, value in dict(finding).items() if key not in {"finding_id", "finding_ordinal"}}
                expected = flow_finding_id(flow_audit_id=flow_id, attempt_no=attempt_no, ordinal=ordinal, body=body)
                if finding.get("finding_ordinal") != ordinal or finding.get("finding_id") != expected:
                    raise ReadPaperError(ErrorCode.ID_MISMATCH, "flow finding identity or ordinal mismatch")
                child_records.append(self.state.put_versioned_record(
                    paper_id=run.paper_id, run_id=run.run_id, record_kind="flow_finding",
                    entity_id=expected, payload=dict(finding),
                ))
        record = self.state.put_versioned_record(
            paper_id=run.paper_id,
            run_id=run.run_id,
            record_kind=kind,
            entity_id=entity_id,
            version_id=version_id,
            parent_version_id=parent_version_id,
            payload=payload,
        )
        child_events = []
        for child in child_records:
            child_kind = EventKind.FINDING_RECORDED if child.record_kind == "audit_finding" else EventKind.FLOW_FINDING_RECORDED
            child_events.append(self.state.append_event(
                paper_id=run.paper_id, run_id=run.run_id, event_kind=child_kind,
                subject_id=child.entity_id, result=EventResult.SUCCEEDED, actor=Actor.SUBAGENT,
                payload={"record_id": child.record_id, "parent_result_record_id": record.record_id},
                idempotency_key=f"record:{invocation.flags['--client-request-id']}:{child.record_id}",
                client_request_id=str(invocation.flags["--client-request-id"]), session_id=capability["session_id"],
                turn_id=capability["turn_id"], agent_id=capability["agent_id"],
                agent_execution_id=capability["agent_execution_id"], context_stream_id=capability["context_stream_id"],
                context_epoch=capability["context_epoch"], tool_use_id=capability["tool_use_id"],
            ))
        event = self.state.append_event(
            paper_id=run.paper_id,
            run_id=run.run_id,
            event_kind=EVENT_FOR_RECORD[kind],
            subject_id=entity_id,
            result=EventResult.SUCCEEDED,
            actor=Actor.ROOT_MAIN if is_root else Actor.SUBAGENT,
            payload={"record_id": record.record_id, "version_id": version_id, "agent_execution_id": capability["agent_execution_id"]},
            idempotency_key=f"record:{invocation.flags['--client-request-id']}:{record.record_id}",
            client_request_id=str(invocation.flags["--client-request-id"]),
            session_id=capability["session_id"],
            turn_id=capability["turn_id"],
            agent_id=capability["agent_id"],
            agent_execution_id=capability["agent_execution_id"],
            context_stream_id=capability["context_stream_id"],
            context_epoch=capability["context_epoch"],
            tool_use_id=capability["tool_use_id"],
        )
        transition_event = None
        if kind == "understanding_note":
            after_note = self.state.get_run(run.paper_id, run.run_id)
            if after_note.state is RunState.READING:
                transition_event = self.state.transition(
                    task_id=run.task_id, paper_id=run.paper_id, run_id=run.run_id,
                    to_state=RunState.REVIEWING, actor=Actor.ROOT_MAIN,
                    reason_code="understanding_note_recorded", authority_event_id=event.event_id,
                )
        return self._success(
            "record", run.paper_id, run.bundle_id, run.run_id,
            {
                "record_id": record.record_id,
                "record_kind": kind,
                "entity_id": entity_id,
                "version_id": version_id,
                "parent_version_id": parent_version_id,
                "payload_sha256": record.payload_sha256,
                "child_record_ids": [item.record_id for item in child_records],
                "related_record_ids": [],
                "primary_event_id": event.event_id,
                "appended_events": [
                    *[{"event_id": item.event_id, "event_seq": item.event_seq, "event_kind": item.event_kind.value, "subject_id": item.subject_id} for item in child_events],
                    {"event_id": event.event_id, "event_seq": event.event_seq, "event_kind": event.event_kind.value, "subject_id": event.subject_id},
                    *([] if transition_event is None else [{"event_id": transition_event.event_id, "event_seq": transition_event.event_seq, "event_kind": transition_event.event_kind.value, "subject_id": transition_event.subject_id}]),
                ],
                "run_state": run.state.value,
                "context_stream_id": capability["context_stream_id"],
                "context_epoch": capability["context_epoch"],
                "session_epoch": self.state.get_binding(run.task_id).session_epoch,
            },
        )

    def _check(self, invocation: Invocation, capability: None) -> dict[str, Any]:
        inventory, run = self._inventory(invocation.positional[0])
        if int(inventory.get("schema_version", 0)) != 2:
            raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "check requires inventory schema 2")
        binding = self.state.get_binding(run.task_id)
        required_refs = (
            set(run.required_artifact_ref_ids)
            if run.scope_locked
            else {item["artifact_ref_id"] for item in inventory["sections"]}
        )
        required_section_ids = {
            item["section_id"]
            for item in inventory["sections"]
            if item["artifact_ref_id"] in required_refs
        }
        frames_by_section: dict[str, set[str]] = {}
        for frame in inventory["frames"]:
            frames_by_section.setdefault(str(frame["section_id"]), set()).add(str(frame["frame_id"]))
        required_frames = {
            item["frame_id"]
            for item in inventory["frames"]
            if item["section_id"] in required_section_ids
        }
        host_state_path = self.state.layout.host_state(run.task_id)
        host_state = read_json(host_state_path) if host_state_path.exists() else {}
        main_context_stream_id = sequence_id("ctx", run.task_id, binding.session_id, "root")
        main_context_epoch = int(
            host_state.get("compact_streams", {})
            .get(main_context_stream_id, {})
            .get("context_epoch", 0)
        )
        historical_frames: set[str] = set()
        resident_frames: set[str] = set()
        historical_visuals: set[str] = set()
        resident_visuals: set[str] = set()
        audit_result_events: dict[str, dict[str, Any]] = {}
        events_path = self.state.layout.run_events(run.paper_id, run.run_id)
        for line in events_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_kind"] == "source_frame_emitted" and event["actor"] == "root_main" and event["result"] == "succeeded":
                frame_id = str(event["subject_id"])
                historical_frames.add(frame_id)
                if event.get("context_stream_id") == main_context_stream_id and int(event.get("context_epoch", -1)) == main_context_epoch:
                    resident_frames.add(frame_id)
            if event["event_kind"] == "visual_open_observed" and event["actor"] == "root_main" and event["result"] == "succeeded":
                visual_id = str(event["subject_id"])
                historical_visuals.add(visual_id)
                if event.get("context_stream_id") == main_context_stream_id and int(event.get("context_epoch", -1)) == main_context_epoch:
                    resident_visuals.add(visual_id)
            if event["event_kind"] == "audit_result_recorded" and event["result"] == "succeeded":
                record_id = event.get("payload", {}).get("record_id")
                if isinstance(record_id, str):
                    audit_result_events[record_id] = event
        missing_historical_frames = sorted(required_frames - historical_frames)
        missing_resident_frames = sorted(required_frames - resident_frames)
        historical_section_ids = {
            section_id
            for section_id, frame_ids in frames_by_section.items()
            if frame_ids and frame_ids <= historical_frames
        }
        resident_section_ids = {
            section_id
            for section_id, frame_ids in frames_by_section.items()
            if frame_ids and frame_ids <= resident_frames
        }
        missing_resident_section_ids = sorted(required_section_ids - resident_section_ids)
        required_visuals = {item["unit_id"] for item in inventory["visual_units"] if item["artifact_ref_id"] in required_refs}
        missing_historical_visuals = sorted(required_visuals - historical_visuals)
        missing_resident_visuals = sorted(required_visuals - resident_visuals)
        blockers = []
        if not run.scope_locked:
            blockers.append("scope_not_locked")
        if run.state is RunState.COMPLETE:
            blockers.extend(missing_historical_frames)
            blockers.extend(missing_historical_visuals)
        else:
            blockers.extend(missing_resident_section_ids)
            blockers.extend(missing_resident_visuals)
        record_kinds: dict[str, list[dict[str, Any]]] = {}
        records_path = self.state.layout.run_records(run.paper_id, run.run_id)
        if records_path.exists():
            for path in records_path.glob("rec_*.json"):
                record = read_json(path)
                record_kinds.setdefault(str(record["record_kind"]), []).append(record)
        invalid_agent_execution_ids: set[str] = set()
        invalid_audit_roles: set[str] = set()
        audit_starts = record_kinds.get("audit_start", [])
        audit_results = record_kinds.get("audit_result", [])
        latest_attempts: dict[tuple[str, str], int] = {}
        for item in [*audit_starts, *audit_results]:
            item_payload = item.get("payload", {})
            key = (str(item_payload.get("role", "")), str(item_payload.get("stage", "")))
            try:
                attempt = int(item_payload["attempt_no"])
            except (KeyError, TypeError, ValueError):
                continue
            latest_attempts[key] = max(latest_attempts.get(key, 0), attempt)
        latest_results = [
            item
            for item in audit_results
            if int(item.get("payload", {}).get("attempt_no", -1))
            == latest_attempts.get(
                (
                    str(item.get("payload", {}).get("role", "")),
                    str(item.get("payload", {}).get("stage", "")),
                ),
                -2,
            )
        ]
        reviewer_bindings = host_state.get("reviewer_bindings", {})
        for result_record in latest_results:
            result_payload = result_record.get("payload", {})
            role = str(result_payload.get("role", ""))
            matching_starts = [
                item for item in audit_starts
                if item.get("payload", {}).get("role") == result_payload.get("role")
                and item.get("payload", {}).get("stage") == result_payload.get("stage")
                and item.get("payload", {}).get("attempt_no") == result_payload.get("attempt_no")
            ]
            creator_event = audit_result_events.get(str(result_record.get("record_id")))
            valid_binding = len(matching_starts) == 1 and creator_event is not None
            if valid_binding:
                start_payload = matching_starts[0].get("payload", {})
                for field in (
                    "reviewer_assignment_id", "assignment_nonce", "assignment_input_digest",
                    "agent_execution_id",
                ):
                    if result_payload.get(field) != start_payload.get(field):
                        valid_binding = False
                expected_agent = start_payload.get("expected_reviewer_agent_id")
                if expected_agent is not None and result_payload.get("reviewer_agent_id") != expected_agent:
                    valid_binding = False
                if creator_event.get("agent_id") != result_payload.get("reviewer_agent_id"):
                    valid_binding = False
                if creator_event.get("agent_execution_id") != result_payload.get("agent_execution_id"):
                    valid_binding = False
                claim = reviewer_bindings.get(str(result_payload.get("reviewer_assignment_id")))
                if not isinstance(claim, dict):
                    valid_binding = False
                else:
                    if claim.get("evidence_kind") != "agent_start_plus_protected_challenge_v1":
                        valid_binding = False
                    if claim.get("reservation_record_id") != matching_starts[0].get("record_id"):
                        valid_binding = False
                    if claim.get("agent_id") != result_payload.get("reviewer_agent_id"):
                        valid_binding = False
                    if claim.get("reserved_agent_execution_id") != result_payload.get("agent_execution_id"):
                        valid_binding = False
                    if claim.get("assignment_input_digest") != result_payload.get("assignment_input_digest"):
                        valid_binding = False
                    nonce = result_payload.get("assignment_nonce")
                    if not isinstance(nonce, str) or claim.get("assignment_nonce_sha256") != digest_text(nonce):
                        valid_binding = False
            if not valid_binding:
                invalid_audit_roles.add(role)
                execution_id = None if creator_event is None else creator_event.get("agent_execution_id")
                if not isinstance(execution_id, str):
                    execution_id = result_payload.get("agent_execution_id")
                if isinstance(execution_id, str):
                    invalid_agent_execution_ids.add(execution_id)

        pending_audits: list[str] = []
        content_audit_records = [
            *audit_starts,
            *record_kinds.get("audit_result", []),
        ]
        for role in ("math_visual", "claim_experiment"):
            source_first_complete = content_audit_stage_returned(
                content_audit_records,
                role=ContentRole(role),
                stage=AuditStage.SOURCE_FIRST,
            )
            comparison_complete = content_audit_stage_returned(
                content_audit_records,
                role=ContentRole(role),
                stage=AuditStage.NOTE_COMPARISON,
            )
            if not source_first_complete or not comparison_complete or role in invalid_audit_roles:
                pending_audits.append(role)
        if not record_kinds.get("understanding_note"):
            blockers.append("understanding_note_missing")
        blockers.extend(f"audit:{role}" for role in pending_audits)
        blockers.extend(f"invalid_agent_execution:{item}" for item in sorted(invalid_agent_execution_ids))
        requested_answer_id = invocation.flags.get("--answer-id")
        if binding.pending_answer_id is not None and requested_answer_id is not None and requested_answer_id != binding.pending_answer_id:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "pending answer cannot be bypassed")
        answer_id = requested_answer_id if isinstance(requested_answer_id, str) else None
        finalized_content_sha256 = None
        answer_status = None
        response_attempt_id = None
        response_attempt_status = None
        answer_delivery_state = None
        if answer_id is not None:
            answer = run.answers.get(answer_id)
            if not isinstance(answer, dict):
                raise ReadPaperError(ErrorCode.NOT_FOUND, "answer not found in run")
            answer_status = str(answer.get("answer_status"))
            response_attempt_id = str(answer.get("current_response_attempt_id"))
            attempt = answer.get("attempts", {}).get(response_attempt_id, {})
            response_attempt_status = attempt.get("status")
            finalizations = [
                item
                for item in record_kinds.get("explanation_finalized", [])
                if item["payload"].get("answer_id") == answer_id
                and item["payload"].get("response_attempt_id") == response_attempt_id
            ]
            finalizations.sort(key=lambda item: str(item.get("created_at", "")))
            finalized = bool(finalizations)
            if finalized:
                finalized_content_sha256 = finalizations[-1]["payload"].get("final_content_sha256")
            groundings = [
                item
                for item in record_kinds.get("answer_grounding", [])
                if item["payload"].get("answer_id") == answer_id
                and item["payload"].get("response_attempt_id") == response_attempt_id
            ]
            matching_grounding = any(
                item["payload"].get("final_content_sha256") == finalized_content_sha256
                for item in groundings
            )
            content_terminal = answer_status in {
                AnswerStatus.CONTENT_FINALIZED.value,
                AnswerStatus.SENT_VERIFIED.value,
                AnswerStatus.DELIVERY_UNKNOWN.value,
            }
            if not content_terminal:
                if not finalized:
                    blockers.append("answer_not_finalized")
                if not groundings:
                    blockers.append("answer_not_grounded")
                elif not matching_grounding:
                    blockers.append("answer_grounding_hash_mismatch")
            stored_content_hash = answer.get("final_content_sha256")
            if content_terminal and stored_content_hash != finalized_content_sha256:
                blockers.append("answer_content_hash_mismatch")
            if answer_status == AnswerStatus.CONTENT_FINALIZED.value:
                answer_delivery_state = "pending_observation"
            elif answer_status == AnswerStatus.SENT_VERIFIED.value:
                answer_delivery_state = "sent_verified"
            elif answer_status == AnswerStatus.DELIVERY_UNKNOWN.value:
                answer_delivery_state = "unknown"
            else:
                answer_delivery_state = "not_finalized" if not finalized else "content_ready"
        if blockers:
            decision = "block"
        elif answer_id is not None and answer_status not in {
            AnswerStatus.CONTENT_FINALIZED.value,
            AnswerStatus.SENT_VERIFIED.value,
            AnswerStatus.DELIVERY_UNKNOWN.value,
        }:
            decision = "ready_to_finalize_content"
        elif answer_id is None:
            decision = "reading_ready"
        else:
            decision = "allow"
        data = {
            "run_state": run.state.value,
            "scope_kind": run.scope_kind.value,
            "interpretation_state": run.interpretation_state.value,
            "decision": decision,
            "blocking_ids": blockers,
            "warning_ids": (
                ["delivery_observation_pending"]
                if answer_delivery_state == "pending_observation"
                else (["delivery_observer_unavailable"] if answer_delivery_state == "unknown" else [])
            ),
            "historical_coverage": {
                "frames": len(historical_frames & required_frames),
                "required_frames": len(required_frames),
                "sections": len(historical_section_ids & required_section_ids),
                "required_sections": len(required_section_ids),
                "visuals": len(historical_visuals & required_visuals),
                "required_visuals": len(required_visuals),
            },
            "resident_coverage": {
                "context_stream_id": main_context_stream_id,
                "context_epoch": main_context_epoch,
                "frames": len(resident_frames & required_frames),
                "required_frames": len(required_frames),
                "sections": len(resident_section_ids & required_section_ids),
                "required_sections": len(required_section_ids),
                "visuals": len(resident_visuals & required_visuals),
                "required_visuals": len(required_visuals),
            },
            "synthesis_coverage": {
                "frames": len(resident_frames & required_frames),
                "required_frames": len(required_frames),
                "sections": len(resident_section_ids & required_section_ids),
                "required_sections": len(required_section_ids),
            },
            "missing_historical_frame_ids": missing_historical_frames,
            "missing_resident_frame_ids": missing_resident_frames,
            "missing_resident_section_ids": missing_resident_section_ids,
            "missing_historical_visual_unit_ids": missing_historical_visuals,
            "missing_resident_visual_unit_ids": missing_resident_visuals,
            "full_source_currently_resident": not missing_resident_frames,
            "pending_audit_ids": pending_audits,
            "pending_finding_ids": [],
            "invalid_agent_execution_ids": sorted(invalid_agent_execution_ids),
            "scope_limitations": [],
            "observer_state": "verified" if host_state_path.exists() else "unavailable",
            "session_epoch": binding.session_epoch,
            "main_context_stream_id": main_context_stream_id,
            "main_context_epoch": main_context_epoch,
            "auto_resume_count": 0,
            "answer_id": answer_id,
            "answer_status": answer_status,
            "response_attempt_id": response_attempt_id,
            "response_attempt_status": response_attempt_status,
            "answer_auto_resume_count": None,
            "answer_delivery_state": answer_delivery_state,
            "finalized_content_sha256": finalized_content_sha256,
            "checked_event_seq": run.event_seq,
            "content_completion_state": (
                "finalized" if answer_status in {
                    AnswerStatus.CONTENT_FINALIZED.value,
                    AnswerStatus.SENT_VERIFIED.value,
                    AnswerStatus.DELIVERY_UNKNOWN.value,
                } else ("ready" if decision == "ready_to_finalize_content" else "incomplete")
            ) if answer_id else None,
            "user_facing_completion_label": None,
        }
        return self._success("check", run.paper_id, run.bundle_id, run.run_id, data)

    def _answer(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        _, run = self._inventory(invocation.positional[0])
        task_id = str(invocation.flags["--task-id"])
        client = str(invocation.flags["--client-request-id"])
        if "--begin" in invocation.flags:
            question_turn = self.state.find_user_turn(task_id=task_id, turn_or_event_id=str(invocation.flags["--user-turn-id"]))
            if question_turn.subject_id != capability["turn_id"] or capability["session_id"] != self.state.get_binding(task_id).session_id:
                raise ReadPaperError(ErrorCode.OBSERVER_UNAVAILABLE, "answer begin is not bound to the observed current user turn")
            answer = self.state.begin_answer(
                task_id=task_id, paper_id=run.paper_id, run_id=run.run_id,
                question_event_id=question_turn.host_event_id,
                question_turn_id=str(invocation.flags["--user-turn-id"]),
                question_hash=str(question_turn.payload["prompt_sha256"]),
                authority_turn_event_id=question_turn.host_event_id,
                root_main_agent_execution_id=capability["agent_execution_id"], client_request_id=client,
            )
            data = answer | {"response_attempt_id": answer["current_response_attempt_id"], "response_attempt_status": "active", "previous_response_attempt_id": None, "current_draft_record_id": None, "current_finalization_id": None, "missing_answer_blockers": [], "pending_binding": True, "client_request_id": client}
        elif "--resume" in invocation.flags:
            answer = self.state.resume_answer(
                task_id=task_id, paper_id=run.paper_id, run_id=run.run_id,
                answer_id=str(invocation.flags["--answer-id"]),
                authority_turn_event_id=f"hev_{digest([capability['session_id'], capability['turn_id']])}",
                root_main_agent_execution_id=capability["agent_execution_id"], client_request_id=client,
            )
            data = answer | {"response_attempt_id": answer["current_response_attempt_id"], "response_attempt_status": "active", "current_draft_record_id": None, "current_finalization_id": None, "missing_answer_blockers": [], "pending_binding": True, "client_request_id": client}
        elif "--finalize" in invocation.flags:
            answer_id = str(invocation.flags["--answer-id"])
            checked = self._check(
                Invocation(
                    command="check",
                    positional=(run.run_id,),
                    flags={"--answer-id": answer_id},
                ),
                None,
            )
            check_data = checked["data"]
            if check_data["decision"] != "ready_to_finalize_content":
                raise ReadPaperError(
                    ErrorCode.STATE_CONFLICT,
                    "answer content still has completion blockers",
                    details={"blocking_ids": check_data["blocking_ids"]},
                )
            final_hash = check_data.get("finalized_content_sha256")
            if not isinstance(final_hash, str):
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, "finalized content hash is missing")
            authority = self.state.find_user_turn(
                task_id=task_id,
                turn_or_event_id=str(invocation.flags["--user-turn-id"]),
            )
            event = self.state.finalize_answer_content(
                task_id=task_id,
                paper_id=run.paper_id,
                run_id=run.run_id,
                answer_id=answer_id,
                final_content_sha256=final_hash,
                expected_event_seq=int(check_data["checked_event_seq"]),
                authority_host_event_id=authority.host_event_id,
                committed_by_agent_execution_id=capability["agent_execution_id"],
                client_request_id=client,
            )
            after = self.state.get_run(run.paper_id, run.run_id)
            data = {
                "answer_id": answer_id,
                "answer_status": AnswerStatus.CONTENT_FINALIZED.value,
                "content_completion_state": "finalized",
                "answer_delivery_state": "pending_observation",
                "response_attempt_id": event.payload["response_attempt_id"],
                "response_attempt_status": "content_finalized",
                "finalized_content_sha256": final_hash,
                "run_state": after.state.value,
                "pending_binding": False,
                "delivery_observation_pending": True,
                "client_request_id": client,
            }
        else:
            answer_id = str(invocation.flags["--answer-id"])
            event = self.state.abandon_answer(
                task_id=task_id, paper_id=run.paper_id, run_id=run.run_id, answer_id=answer_id,
                authority_turn_event_id=f"hev_{digest([capability['session_id'], capability['turn_id']])}",
                root_main_agent_execution_id=capability["agent_execution_id"], client_request_id=client,
            )
            data = {"answer_id": answer_id, "answer_status": "abandoned", "abandoned_response_attempt_id": event.payload["abandoned_response_attempt_id"], "response_attempt_status": "abandoned", "pending_binding": False, "client_request_id": client}
        return self._success("answer", run.paper_id, run.bundle_id, run.run_id, data)

    def _resume(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        _, run = self._inventory(invocation.positional[0])
        if run.state is not RunState.PAUSED or run.resume_phase is None:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "run is not explicitly resumable")
        event = self.state.transition(
            task_id=str(invocation.flags["--task-id"]), paper_id=run.paper_id, run_id=run.run_id,
            to_state=run.resume_phase, actor=Actor.ROOT_MAIN, reason_code="explicit_user_resume",
        )
        binding = self.state.get_binding(run.task_id)
        return self._success("resume", run.paper_id, run.bundle_id, run.run_id, {"resume_phase": run.resume_phase.value, "pending_answer_id": binding.pending_answer_id, "answer_resume_required": binding.pending_answer_status == "interrupted", "pending_reading_frame_ids": [], "pending_visual_unit_ids": [], "pending_audit_ids": [], "pending_finding_ids": [], "scope_limitations": [], "session_epoch": binding.session_epoch, "main_context_stream_id": capability["context_stream_id"], "main_context_epoch": capability["context_epoch"], "event_id": event.event_id})

    def _delete(self, invocation: Invocation, capability: dict[str, Any]) -> dict[str, Any]:
        paper = invocation.positional[0]
        task = str(invocation.flags["--task-id"])
        if "--preview" in invocation.flags:
            request = self.deletion.create_preview(task_id=task, paper_id=paper, client_request_id=str(invocation.flags["--client-request-id"]))
            data = request | {"request_state": request["state"], "exact_confirmation_text": f"DELETE {paper} {request['deletion_request_id']}"}
        else:
            request_id = str(invocation.flags["--request-id"])
            approval = self.state.find_user_turn(task_id=task, turn_or_event_id=str(invocation.flags["--approval-turn-id"]))
            expected = f"DELETE {paper} {request_id}"
            if approval.payload.get("prompt_sha256") != digest_text(expected):
                raise ReadPaperError(ErrorCode.DELETE_CONFIRMATION_REQUIRED, "approval turn did not contain exact confirmation")
            data = self.deletion.execute(request_id=request_id, task_id=task, approval_text=expected, approval_turn_event_id=approval.host_event_id)
        return self._success("delete", paper, None, None, data)

    @staticmethod
    def _success(command: str, paper: str | None, bundle: str | None, run: str | None, data: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": "1", "command": command, "ok": True, "paper_id": paper, "bundle_id": bundle, "run_id": run, "data": data, "error": None}

    @staticmethod
    def _encode(value: dict[str, Any]) -> bytes:
        return canonical_bytes(value) + b"\n"


def error_envelope(command: str, error: ReadPaperError) -> bytes:
    return canonical_bytes(
        {
            "schema_version": "1",
            "command": command,
            "ok": False,
            "paper_id": None,
            "bundle_id": None,
            "run_id": None,
            "data": None,
            "error": {
                "code": error.code.value,
                "message": str(error),
                "retryable": error.code in {ErrorCode.TIMEOUT, ErrorCode.FETCH_FAILED},
                "details": error.details,
            },
        }
    ) + b"\n"
