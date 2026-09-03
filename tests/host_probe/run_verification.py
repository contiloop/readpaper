#!/usr/bin/env python3
"""Run the non-mutating G0 checks and record compact command evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / "evidence/g0"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(
    command_id: str, argv: list[str], expected_exit_code: int
) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return {
        "command_id": command_id,
        "expected_exit_code": expected_exit_code,
        "exit_code": completed.returncode,
        "matched_expectation": completed.returncode == expected_exit_code,
        "stdout_sha256": _sha256(completed.stdout),
        "stderr_sha256": _sha256(completed.stderr),
        "stdout_line_count": len(completed.stdout.splitlines()),
        "stderr_line_count": len(completed.stderr.splitlines()),
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable evidence: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    captured_at = datetime.now().astimezone()
    run_id = f"{captured_at.strftime('%Y%m%dT%H%M%S%z')}-{os.getpid()}"
    evidence_dir = EVIDENCE_ROOT / f"{captured_at.date().isoformat()}-reassessment"
    run_dir = evidence_dir / "runs" / run_id
    host_contract_output = run_dir / "host_contract.json"
    command_results_output = run_dir / "command-results.json"
    commands = [
        ("uv_sync_frozen", ["uv", "sync", "--frozen"], 0),
        ("pytest", ["uv", "run", "pytest"], 0),
        (
            "compileall",
            [
                "uv",
                "run",
                "python",
                "-m",
                "compileall",
                "-q",
                "tests",
                ".agents/skills/readpaper/scripts",
            ],
            0,
        ),
        (
            "host_contract_snapshot",
            [
                "uv",
                "run",
                "python",
                "tests/host_probe/probe_current_desktop.py",
                "--output",
                host_contract_output.relative_to(ROOT).as_posix(),
            ],
            0,
        ),
        (
            "host_contract_static_ready",
            [
                "uv",
                "run",
                "python",
                "tests/host_probe/probe_current_desktop.py",
                "--require-static-ready",
            ],
            0,
        ),
        (
            "git_diff_check",
            ["git", "diff", "--check"],
            0,
        ),
        (
            "project_goal_unchanged",
            ["git", "diff", "--quiet", "--", "PROJECT_GOAL.md"],
            0,
        ),
    ]
    results = [
        _run(command_id, argv, expected)
        for command_id, argv, expected in commands
    ]
    payload = {
        "schema_version": 3,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "verification_run_id": run_id,
        "metadata_only": True,
        "all_expectations_matched": all(
            result["matched_expectation"] for result in results
        ),
        "commands": results,
    }
    _atomic_write(command_results_output, payload)
    print(
        json.dumps(
            {
                "host_contract": host_contract_output.relative_to(ROOT).as_posix(),
                "command_results": command_results_output.relative_to(ROOT).as_posix(),
                "all_expectations_matched": payload["all_expectations_matched"],
            },
            sort_keys=True,
        )
    )
    return int(not payload["all_expectations_matched"])


if __name__ == "__main__":
    raise SystemExit(main())
