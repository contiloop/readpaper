"""Immutable ID constructors."""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable

from .canonical import digest, sha256_bytes
from .errors import ErrorCode, ReadPaperError


ID_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9_]*)_(?P<body>[0-9a-f]{32}|[0-9a-f]{64})$")


def _hash_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{digest(value)}"


def paper_id(data: bytes) -> str:
    return f"p_{sha256_bytes(data)}"


def artifact_id(data: bytes) -> str:
    return f"a_{sha256_bytes(data)}"


def artifact_ref_id(
    *, role: str, source_token: str, parent_artifact_id: str | None = None,
    archive_member_path: str | None = None,
) -> str:
    return _hash_id(
        "r",
        {
            "archive_member_path": archive_member_path,
            "parent_artifact_id": parent_artifact_id,
            "role": role,
            "source_token": source_token,
        },
    )


def bundle_id(*, schema_version: int, paper_id: str, landing_url: str | None, artifacts: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(artifacts, key=lambda item: item["artifact_ref_id"])
    return _hash_id(
        "b",
        {
            "artifacts": ordered,
            "landing_url": landing_url,
            "paper_id": paper_id,
            "schema_version": schema_version,
        },
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sequence_id(prefix: str, *parts: Any) -> str:
    return _hash_id(prefix, list(parts))


def context_stream_id(session_id: str, actor_key: str) -> str:
    return sequence_id("cs", session_id, actor_key)


def validate_id(value: str, *, prefix: str, lengths: tuple[int, ...] = (32, 64)) -> str:
    match = ID_RE.fullmatch(value)
    if match is None or match.group("prefix") != prefix or len(match.group("body")) not in lengths:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, f"invalid {prefix} identifier")
    return value
