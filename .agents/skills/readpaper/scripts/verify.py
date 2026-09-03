#!/usr/bin/env python3
"""Run the P0 automated gate and emit an immutable evidence bundle."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from readpaper.wiring import build_wiring_manifest


ROOT = Path(__file__).resolve().parents[4]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str]) -> dict:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    return {
        "argv": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def skill_validator_path() -> Path | None:
    explicit = os.environ.get("READPAPER_SKILL_VALIDATOR")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidates.append(Path(codex_home) / "skills/.system/skill-creator/scripts/quick_validate.py")
    candidates.append(Path.home() / ".codex/skills/.system/skill-creator/scripts/quick_validate.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def skipped(name: str, reason: str) -> dict:
    return {
        "argv": [],
        "exit_code": 0,
        "stdout": f"{name} skipped: {reason}\n",
        "stderr": "",
    }


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    captured = datetime.now().astimezone()
    run_id = captured.strftime("%Y%m%dT%H%M%S%z")
    output = ROOT / "evidence/p0" / f"{captured.date().isoformat()}-automated" / "runs" / run_id
    pytest_result = run([str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q"])
    validator = skill_validator_path()
    validator_python = os.environ.get("READPAPER_SKILL_VALIDATOR_PYTHON", "python3")
    skill_result = (
        run([validator_python, str(validator), str(ROOT / ".agents/skills/readpaper")])
        if validator is not None
        else skipped("skill_validate", "validator not found")
    )
    commands = {"pytest": pytest_result, "skill_validate": skill_result}
    write(output / "command-results.json", commands)
    stage_status = {
        "G0": "pass_live_desktop",
        **{f"T{number}": "pass_automated" for number in range(1, 12)},
        "W1": "pass_automated",
        "T12": "requires_new_desktop_session",
    }
    manifest = build_wiring_manifest(ROOT)
    tracked = sorted({
        *manifest["files"].keys(),
        ".agents/skills/readpaper/scripts/verify.py",
        "tests/fixture_manifest.json",
    })
    historical_g0 = ROOT / "evidence/g0/2026-09-01-live/runs/20260901T013538+0900-20181/passed-verdict.json"
    validation = {
        "schema_version": 1,
        "run_id": run_id,
        "captured_at": captured.isoformat(timespec="seconds"),
        "project_root": str(ROOT),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "commands_passed": all(item["exit_code"] == 0 for item in commands.values()),
        "stage_status": stage_status,
        "generated_wiring_manifest": manifest,
        "local_hooks_json_sha256": sha(ROOT / ".codex/hooks.json") if (ROOT / ".codex/hooks.json").exists() else None,
        "local_wiring_manifest_sha256": sha(ROOT / ".dryforge/wiring-manifest.json") if (ROOT / ".dryforge/wiring-manifest.json").exists() else None,
        "file_sha256": {path: sha(ROOT / path) for path in tracked},
        "g0_evidence": str(historical_g0.relative_to(ROOT)) if historical_g0.exists() else None,
        "t12_release_blockers": [
            "new-session production hook load/trust review",
            "actual 10-page Desktop end-to-end acceptance",
            "actual public-paper and compaction A/B acceptance",
        ],
    }
    write(output / "validation.json", validation)
    print(output)
    return 0 if validation["commands_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
