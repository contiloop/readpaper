"""fsync-backed local storage and deterministic lock ordering."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes
from .errors import ErrorCode, ReadPaperError


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"unsafe directory: {path}")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReadPaperError(ErrorCode.NOT_FOUND, f"missing state file: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"invalid state file: {path}") from error
    if not isinstance(value, dict):
        raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"state is not an object: {path}")
    return value


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600, replace: bool = True) -> None:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise FileExistsError(path)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any, *, replace: bool = True) -> None:
    atomic_write(path, canonical_bytes(value) + b"\n", replace=replace)


def append_jsonl_once(path: Path, value: dict[str, Any], *, identity_field: str, identity: str) -> None:
    """Append one complete line, returning harmlessly if the exact ID exists."""

    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.lseek(descriptor, 0, os.SEEK_SET)
        existing = os.read(descriptor, os.fstat(descriptor).st_size)
        for line in existing.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReadPaperError(ErrorCode.STATE_CONFLICT, f"corrupt JSONL: {path}") from error
            if item.get(identity_field) == identity:
                if canonical_bytes(item) != canonical_bytes(value):
                    raise ReadPaperError(ErrorCode.ID_MISMATCH, f"conflicting {identity_field}")
                return
        os.lseek(descriptor, 0, os.SEEK_END)
        os.write(descriptor, canonical_bytes(value) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def assert_regular_private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "input must be one private regular file")


class FileLock(AbstractContextManager["FileLock"]):
    def __init__(self, path: Path):
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> "FileLock":
        ensure_private_directory(self.path.parent)
        self._descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


class Layout:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runtime = self.root / ".readpaper"
        self.papers = self.root / "papers"
        self.locks = self.runtime / "locks"

    def initialize(self) -> None:
        for path in (
            self.papers / "_objects",
            self.runtime / "task-bindings",
            self.runtime / "host-events",
            self.runtime / "prepare-operations",
            self.runtime / "prepare-work",
            self.runtime / "stop-transactions",
            self.runtime / "invocation-capabilities",
            self.runtime / "client-requests",
            self.runtime / "deletion-requests",
            self.runtime / "deletion-staging",
            self.locks,
        ):
            ensure_private_directory(path)

    @property
    def reference_lock(self) -> Path:
        return self.locks / "00-project-reference.lock"

    def task_hash(self, task_id: str) -> str:
        from .canonical import digest_text

        return digest_text(task_id)

    def task_binding(self, task_id: str) -> Path:
        return self.runtime / "task-bindings" / f"{self.task_hash(task_id)}.json"

    def task_lock(self, task_id: str) -> Path:
        return self.locks / f"10-task-{self.task_hash(task_id)}.lock"

    def host_ledger(self, task_id: str) -> Path:
        return self.runtime / "host-events" / f"{self.task_hash(task_id)}.jsonl"

    def host_state(self, task_id: str) -> Path:
        return self.runtime / "host-events" / f"{self.task_hash(task_id)}.state.json"

    def run_dir(self, paper_id: str, run_id: str) -> Path:
        return self.papers / paper_id / "runs" / run_id

    def run_state(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "state.json"

    def run_events(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "events.jsonl"

    def run_records(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "records"

    def run_index(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "run-index.json"

    def run_summary(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "summary.md"

    def run_lock(self, run_id: str) -> Path:
        return self.locks / f"20-run-{run_id}.lock"

    def run_transaction(self, paper_id: str, run_id: str) -> Path:
        return self.run_dir(paper_id, run_id) / "transaction-intent.json"

    def object_source(self, artifact_id: str) -> Path:
        digest_hex = artifact_id.removeprefix("a_")
        return self.papers / "_objects" / digest_hex[:2] / artifact_id / "source"

    def deletion_request(self, request_id: str) -> Path:
        return self.runtime / "deletion-requests" / f"{request_id}.json"

    def deletion_stage(self, request_id: str) -> Path:
        return self.runtime / "deletion-staging" / request_id
