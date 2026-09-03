#!/usr/bin/env python3
"""Install, inspect, snapshot, and exactly restore the G0 live probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from live_probe_state import (
    StateStore,
    atomic_write,
    canonical_bytes,
    initial_state,
    now,
    sanitized_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
HOOK_SCRIPT = ROOT / "tests/host_probe/live_hook.py"
HOOKS_PATH = ROOT / ".codex/hooks.json"
PAPER_PATH = ROOT / ".agents/skills/readpaper/scripts/paper.py"
PARSER_PATH = (
    ROOT / ".agents/skills/readpaper/scripts/_host_probe_parse_invocation.py"
)
CONFIG_PATH = ROOT / ".codex/config.toml"
PROTECTED = (HOOKS_PATH, PAPER_PATH, PARSER_PATH)


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": path.relative_to(ROOT).as_posix(),
            "exists": False,
            "mode": None,
            "sha256": None,
        }
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "exists": True,
        "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
        "sha256": sha256_file(path),
    }


def immutable_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable evidence: {path}")
    atomic_write(path, canonical_bytes(value) + b"\n", mode=0o644)


def parser_source(runtime: Path) -> bytes:
    schema = {
        "command": "prepare",
        "flags": ["--run-id", "--attempt-id", "--nonce", "--request-id"],
        "prefix": [str(PYTHON), str(PAPER_PATH), "--g0-live-probe", "prepare"],
    }
    schema_sha = hashlib.sha256(canonical_bytes(schema)).hexdigest()
    source = f'''# Generated only for the reviewed G0 Desktop live probe.
from __future__ import annotations

import shlex
from pathlib import Path

RUNTIME = Path({str(runtime)!r})
PYTHON = {str(PYTHON)!r}
PAPER = {str(PAPER_PATH)!r}
SCHEMA_SHA256 = {schema_sha!r}
PREFIX = [PYTHON, PAPER, "--g0-live-probe", "prepare"]
FLAG_ORDER = ("--run-id", "--attempt-id", "--nonce", "--request-id")


def _parse(words):
    if len(words) != len(PREFIX) + 8 or words[:len(PREFIX)] != PREFIX:
        return None
    result = {{}}
    index = len(PREFIX)
    for flag in FLAG_ORDER:
        if words[index] != flag or not words[index + 1]:
            return None
        result[flag[2:].replace("-", "_")] = words[index + 1]
        index += 2
    return result


def parse_command(command):
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    if shlex.join(words) != command:
        return None
    return _parse(words)


def parse_argv(argv):
    return _parse([PYTHON, PAPER, *argv])


def build_command(*, run_id, attempt_id, nonce, request_id):
    words = PREFIX + [
        "--run-id", run_id,
        "--attempt-id", attempt_id,
        "--nonce", nonce,
        "--request-id", request_id,
    ]
    return shlex.join(words)
'''
    return source.encode("utf-8")


def paper_source() -> bytes:
    source = f'''#!/usr/bin/env python3
"""Fail-closed G0 probe CLI; this is not the ReadPaper product CLI."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path({str(ROOT)!r})
sys.path.insert(0, str(ROOT / "tests/host_probe"))
from live_probe_state import StateStore, canonical_bytes, digest_text, now

PARSER_PATH = Path({str(PARSER_PATH)!r})
spec = importlib.util.spec_from_file_location("g0_probe_parser", PARSER_PATH)
if spec is None or spec.loader is None:
    raise SystemExit("G0 parser unavailable")
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def main():
    invocation = parser.parse_argv(sys.argv[1:])
    if invocation is None:
        raise SystemExit("G0 probe grammar rejected")
    store = StateStore(parser.RUNTIME)
    with store.locked() as state:
        stop = state.get("stop_transaction")
        claim = state.get("prompt_claim")
        if (
            stop is None
            or claim is None
            or claim.get("status") != "started"
            or invocation["run_id"] != state["run_id"]
            or invocation["attempt_id"] != stop["attempt_id"]
            or invocation["nonce"] != stop["nonce"]
            or invocation["request_id"] != stop["request_id"]
        ):
            raise SystemExit("G0 nonce/request binding rejected")
        matching = [
            capability
            for capability in state["capabilities"].values()
            if capability["client_request_id"] == invocation["request_id"]
        ]
        if len(matching) != 1:
            raise SystemExit("G0 one-use capability unavailable")
        capability = matching[0]
        if (
            capability.get("claim_sha256") != claim.get("claim_sha256")
            or (
                claim.get("tool_use_id") is not None
                and capability.get("tool_use_id") != claim.get("tool_use_id")
            )
        ):
            raise SystemExit("G0 claim/capability binding rejected")
        existing = state.get("authorized_effect")
        if existing is None:
            if capability["status"] != "issued":
                raise SystemExit("G0 capability already consumed")
            effect_id = hashlib.sha256(
                (state["run_id"] + invocation["request_id"]).encode("utf-8")
            ).hexdigest()
            output = {{
                "schema_version": 1,
                "kind": "g0_live_probe",
                "status": "authorized_effect_committed",
                "effect_id": effect_id,
                "request_id": invocation["request_id"],
            }}
            output_bytes = canonical_bytes(output) + b"\\n"
            capability["status"] = "consumed"
            capability["consumed_at"] = now()
            state["authorized_effect"] = {{
                "effect_id": effect_id,
                "client_request_id": invocation["request_id"],
                "tool_use_id": capability["tool_use_id"],
                "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "output": output,
                "committed_at": now(),
            }}
            state["authorized_effect_count"] += 1
        else:
            if existing["client_request_id"] != invocation["request_id"]:
                raise SystemExit("G0 effect/request conflict")
            output = existing["output"]
        if state["authorized_effect_count"] != 1:
            raise SystemExit("G0 at-most-once invariant failed")
    sys.stdout.buffer.write(canonical_bytes(output) + b"\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return source.encode("utf-8")


def hook_command(runtime: Path, handler: str) -> str:
    return shlex.join(
        [
            str(PYTHON),
            str(HOOK_SCRIPT),
            "--runtime",
            str(runtime),
            "--handler",
            handler,
            "--readpaper-g0-live",
        ]
    )


def handler(runtime: Path, name: str, *, timeout: int = 10) -> dict[str, Any]:
    return {
        "type": "command",
        "command": hook_command(runtime, name),
        "timeout": timeout,
        "statusMessage": "Running reviewed ReadPaper G0 live probe",
    }


def probe_groups(runtime: Path) -> dict[str, list[dict[str, Any]]]:
    main = handler(runtime, "main")
    return {
        "SessionStart": [
            {"matcher": "^(startup|resume|clear|compact)$", "hooks": [main]}
        ],
        "UserPromptSubmit": [{"hooks": [main]}],
        "SubagentStart": [{"matcher": ".*", "hooks": [main]}],
        "SubagentStop": [{"matcher": ".*", "hooks": [main]}],
        "PreToolUse": [{"matcher": "^Bash$", "hooks": [main]}],
        "PostToolUse": [{"matcher": "^Bash$", "hooks": [main]}],
        "PreCompact": [{"matcher": "^(manual|auto)$", "hooks": [main]}],
        "PostCompact": [{"matcher": "^(manual|auto)$", "hooks": [main]}],
        "Stop": [
            {
                "hooks": [
                    main,
                    handler(runtime, "suppression"),
                ]
            }
        ],
    }


def is_probe_handler(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and "--readpaper-g0-live" in str(value.get("command", ""))
    )


def merge_hooks(original: dict[str, Any], runtime: Path) -> dict[str, Any]:
    merged = json.loads(json.dumps(original))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("existing hooks.json has non-object hooks field")
    for event, groups in probe_groups(runtime).items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"existing hooks.{event} is not an array")
        for group in existing:
            if isinstance(group, dict) and any(
                is_probe_handler(item) for item in group.get("hooks", [])
            ):
                raise RuntimeError("a G0 live probe is already installed")
        existing.extend(groups)
    return merged


def backup_file(runtime: Path, path: Path) -> dict[str, Any]:
    state = path_state(path)
    if state["exists"]:
        destination = runtime / "backup" / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(destination, path.read_bytes(), mode=int(state["mode"], 8))
    return state


def install() -> dict[str, Any]:
    if not PYTHON.is_file():
        raise RuntimeError("run `uv sync --frozen` before installing the probe")
    if PAPER_PATH.exists() or PARSER_PATH.exists():
        raise RuntimeError(
            "protected product script path already exists; use a clean throwaway checkout"
        )
    captured = datetime.now().astimezone()
    run_id = f"{captured.strftime('%Y%m%dT%H%M%S%z')}-{os.getpid()}"
    evidence_dir = (
        ROOT
        / "evidence/g0"
        / f"{captured.date().isoformat()}-live"
        / "runs"
        / run_id
    )
    if evidence_dir.exists():
        raise FileExistsError(evidence_dir)
    # The live probe may wait for human review. macOS can purge its ordinary
    # temporary directory during that pause, so keep the mkdtemp directory
    # under .git: it remains private/uncommittable and survives review latency.
    runtime = Path(
        tempfile.mkdtemp(prefix=f"readpaper-g0-{run_id}-", dir=ROOT / ".git")
    )
    os.chmod(runtime, 0o700)
    prestate = [backup_file(runtime, path) for path in PROTECTED]
    config_prestate = path_state(CONFIG_PATH)
    install_manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "project_root": str(ROOT),
        "runtime": str(runtime),
        "evidence_dir": str(evidence_dir),
        "prestate": prestate,
        "config_prestate": config_prestate,
        "installed_hashes": None,
    }
    atomic_write(
        runtime / "install-manifest.json",
        canonical_bytes(install_manifest) + b"\n",
    )
    atomic_write(
        runtime / "state.json",
        canonical_bytes(initial_state(run_id, ROOT, evidence_dir)) + b"\n",
    )

    original_hooks = (
        json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        if HOOKS_PATH.exists()
        else {}
    )
    atomic_write(PARSER_PATH, parser_source(runtime), mode=0o600)
    atomic_write(PAPER_PATH, paper_source(), mode=0o700)
    atomic_write(
        HOOKS_PATH,
        json.dumps(
            merge_hooks(original_hooks, runtime),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
        mode=0o600,
    )
    installed_hashes = [path_state(path) for path in PROTECTED]
    install_manifest["installed_hashes"] = installed_hashes
    atomic_write(
        runtime / "install-manifest.json",
        canonical_bytes(install_manifest) + b"\n",
    )
    immutable_json(
        evidence_dir / "installation.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "captured_at": now(),
            "prestate": prestate,
            "config_prestate": config_prestate,
            "installed_probe_hashes": installed_hashes,
            "backup": {
                "created": True,
                "mode": "0700",
                "location_recorded_only_in_private_runtime": True,
            },
            "config_toml_modified": False,
        },
    )
    return {
        "status": "installed_requires_user_review",
        "run_id": run_id,
        "evidence": str((evidence_dir / "installation.json").relative_to(ROOT)),
        "review_paths": [str(path) for path in (HOOKS_PATH, HOOK_SCRIPT, PAPER_PATH, PARSER_PATH)],
        "start_prompt": f"READPAPER_G0_LIVE_START {run_id}",
    }


def discover_runtime() -> Path:
    if not HOOKS_PATH.is_file():
        raise RuntimeError("G0 probe hooks are not installed")
    value = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
    for groups in value.get("hooks", {}).values():
        for group in groups:
            for item in group.get("hooks", []):
                if is_probe_handler(item):
                    words = shlex.split(item["command"])
                    return Path(words[words.index("--runtime") + 1])
    raise RuntimeError("G0 runtime not found in installed hook definitions")


def status() -> dict[str, Any]:
    runtime = discover_runtime()
    manifest = json.loads(
        (runtime / "install-manifest.json").read_text(encoding="utf-8")
    )
    state = StateStore(runtime).read()
    return {
        "run_id": manifest["run_id"],
        "installed_hashes_match": [path_state(path) for path in PROTECTED]
        == manifest["installed_hashes"],
        "config_toml_unchanged": path_state(CONFIG_PATH) == manifest["config_prestate"],
        "snapshot": sanitized_snapshot(state),
        "next_prompts": {
            "suppression": f"READPAPER_G0_LIVE_SUPPRESSION {manifest['run_id']}",
            "cancel": f"READPAPER_G0_LIVE_CANCEL {manifest['run_id']}",
        },
    }


def snapshot() -> dict[str, Any]:
    runtime = discover_runtime()
    manifest = json.loads(
        (runtime / "install-manifest.json").read_text(encoding="utf-8")
    )
    path = Path(manifest["evidence_dir"]) / "live-snapshot.json"
    immutable_json(path, sanitized_snapshot(StateStore(runtime).read()))
    return {"snapshot": str(path.relative_to(ROOT))}


def remove_and_sync(path: Path) -> None:
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore() -> dict[str, Any]:
    runtime = discover_runtime()
    manifest = json.loads(
        (runtime / "install-manifest.json").read_text(encoding="utf-8")
    )
    current = [path_state(path) for path in PROTECTED]
    if current != manifest["installed_hashes"]:
        interruption = Path(manifest["evidence_dir"]) / "RESTORE_BLOCKED.json"
        if not interruption.exists():
            immutable_json(
                interruption,
                {
                    "schema_version": 1,
                    "captured_at": now(),
                    "reason": "installed probe hashes changed after review",
                    "private_backup_path": str(runtime),
                    "expected": manifest["installed_hashes"],
                    "current": current,
                },
            )
        raise RuntimeError(f"restore blocked; evidence: {interruption}")
    if path_state(CONFIG_PATH) != manifest["config_prestate"]:
        raise RuntimeError(".codex/config.toml changed during G0; refusing cleanup")
    for before, path in zip(manifest["prestate"], PROTECTED, strict=True):
        if before["exists"]:
            backup = runtime / "backup" / path.relative_to(ROOT)
            atomic_write(path, backup.read_bytes(), mode=int(before["mode"], 8))
        else:
            remove_and_sync(path)
    poststate = [path_state(path) for path in PROTECTED]
    if poststate != manifest["prestate"]:
        raise RuntimeError("post-restore state does not match installation prestate")
    evidence_path = Path(manifest["evidence_dir"]) / "restoration.json"
    immutable_json(
        evidence_path,
        {
            "schema_version": 1,
            "captured_at": now(),
            "prestate": manifest["prestate"],
            "poststate": poststate,
            "config_prestate": manifest["config_prestate"],
            "config_poststate": path_state(CONFIG_PATH),
            "exact_restore": True,
        },
    )
    shutil.rmtree(runtime)
    for directory in (PAPER_PATH.parent, ROOT / ".agents/skills/readpaper", ROOT / ".agents/skills", ROOT / ".codex"):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "status": "restored",
        "evidence": str(evidence_path.relative_to(ROOT)),
        "exact_restore": True,
    }


def recover_expired_runtime() -> dict[str, Any]:
    """Restore an all-absent prestate when the private runtime was purged.

    This path is deliberately narrow: it refuses recovery if any protected
    file existed before installation, if installed hashes changed, or if the
    config prestate changed. No missing backup is guessed.
    """

    runtime = discover_runtime()
    if runtime.exists():
        raise RuntimeError("private runtime still exists; use normal restore")
    installations = sorted(
        (ROOT / "evidence/g0").glob("*-live/runs/*/installation.json"),
        reverse=True,
    )
    current = [path_state(path) for path in PROTECTED]
    matching: list[tuple[Path, dict[str, Any]]] = []
    for installation in installations:
        value = json.loads(installation.read_text(encoding="utf-8"))
        if value.get("installed_probe_hashes") == current:
            matching.append((installation, value))
    if len(matching) != 1:
        raise RuntimeError("could not uniquely bind expired runtime to installation evidence")
    installation_path, evidence = matching[0]
    if any(item["exists"] for item in evidence["prestate"]):
        raise RuntimeError("expired-runtime recovery refuses non-empty prestates")
    if path_state(CONFIG_PATH) != evidence["config_prestate"]:
        raise RuntimeError(".codex/config.toml changed; refusing recovery")
    for path in PROTECTED:
        remove_and_sync(path)
    poststate = [path_state(path) for path in PROTECTED]
    if poststate != evidence["prestate"]:
        raise RuntimeError("expired-runtime recovery did not restore exact prestate")
    recovery_path = installation_path.parent / "expired-runtime-recovery.json"
    immutable_json(
        recovery_path,
        {
            "schema_version": 1,
            "captured_at": now(),
            "status": "invalid_live_attempt_restored",
            "reason": "private mkdtemp runtime was absent before Stop evidence inspection",
            "expired_private_runtime_path": str(runtime),
            "installed_hashes_verified_before_removal": current,
            "prestate": evidence["prestate"],
            "poststate": poststate,
            "config_unchanged": True,
            "g0_passed": False,
        },
    )
    for directory in (
        PAPER_PATH.parent,
        ROOT / ".agents/skills/readpaper",
        ROOT / ".agents/skills",
        ROOT / ".codex",
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "status": "invalid_attempt_restored",
        "evidence": str(recovery_path.relative_to(ROOT)),
        "exact_restore": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("install", "status", "snapshot", "restore", "recover-expired"),
    )
    args = parser.parse_args()
    actions = {
        "install": install,
        "status": status,
        "snapshot": snapshot,
        "restore": restore,
        "recover-expired": recover_expired_runtime,
    }
    print(json.dumps(actions[args.action](), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
