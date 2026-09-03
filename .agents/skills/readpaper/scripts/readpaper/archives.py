"""Fail-closed ZIP supplementary inspection."""

from __future__ import annotations

import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass

from .errors import ErrorCode, ReadPaperError
from .sources import MediaKind, classify_media


MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 256
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RATIO = 100


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    data: bytes
    media_kind: str


def _safe_name(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "unsafe ZIP member path")
    normalized = posixpath.normpath(value)
    if value.startswith("/") or normalized in {".", ".."} or normalized.startswith("../"):
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "ZIP path traversal")
    return normalized


def inspect_zip(data: bytes) -> tuple[ArchiveMember, ...]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP exceeds 128 MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "invalid ZIP") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP has too many members")
        names: set[str] = set()
        total = 0
        for info in infos:
            name = _safe_name(info.filename)
            if name in names:
                raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "duplicate normalized ZIP path")
            names.add(name)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "ZIP special files are forbidden")
            if info.flag_bits & 0x1:
                raise ReadPaperError(ErrorCode.ACCESS_DENIED, "encrypted ZIP is unsupported")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP member is too large")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP expansion is too large")
            if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > MAX_RATIO):
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP expansion ratio is too high")
        members: list[ArchiveMember] = []
        observed_total = 0
        for info in infos:
            if info.is_dir():
                continue
            name = _safe_name(info.filename)
            with archive.open(info, "r") as source:
                chunks: list[bytes] = []
                observed = 0
                while True:
                    chunk = source.read(min(1024 * 1024, MAX_MEMBER_BYTES + 1 - observed))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    observed += len(chunk)
                    observed_total += len(chunk)
                    if observed > MAX_MEMBER_BYTES or observed_total > MAX_TOTAL_BYTES:
                        raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "ZIP streaming limit exceeded")
            member_data = b"".join(chunks)
            kind = classify_media(member_data)
            if kind == MediaKind.ZIP:
                raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "nested archives are forbidden")
            if kind not in {MediaKind.PDF, MediaKind.TEXT, MediaKind.IMAGE}:
                raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "unsupported ZIP member")
            members.append(ArchiveMember(path=name, data=member_data, media_kind=kind))
        return tuple(members)
