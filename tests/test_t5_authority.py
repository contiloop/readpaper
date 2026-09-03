from __future__ import annotations

from pathlib import Path

import pytest

from readpaper.authority import InvocationAuthority
from readpaper.canonical import digest, digest_text
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.parse_invocation import SCHEMA_SHA256, parse_argv, parse_command


def quote_all(words: list[str]) -> str:
    return " ".join("'" + word.replace("'", "'\"'\"'") + "'" for word in words)


def test_all_eight_command_grammars_parse() -> None:
    client = "cr_" + "1" * 32
    valid = [
        ["prepare", "/tmp/p.pdf", "--task-id", "task", "--user-turn-id", "turn", "--client-request-id", client],
        ["read", "run_" + "1" * 32, "--unit-id", "unit", "--client-request-id", client],
        ["render", "run_" + "1" * 32, "--unit-id", "visual", "--client-request-id", client],
        ["record", "run_" + "1" * 32, "--kind", "printed_label", "--payload", "/tmp/p.json", "--client-request-id", client],
        ["check", "run_" + "1" * 32],
        ["answer", "run_" + "1" * 32, "--begin", "--task-id", "task", "--user-turn-id", "turn", "--client-request-id", client],
        ["answer", "run_" + "1" * 32, "--finalize", "--answer-id", "ans_" + "2" * 64, "--task-id", "task", "--user-turn-id", "turn", "--client-request-id", client],
        ["resume", "run_" + "1" * 32, "--task-id", "task", "--user-turn-id", "turn", "--client-request-id", client],
        ["delete", "p_" + "1" * 64, "--preview", "--task-id", "task", "--user-turn-id", "turn", "--client-request-id", client],
    ]
    assert all(parse_argv(words) is not None for words in valid)
    assert len(SCHEMA_SHA256) == 64


@pytest.mark.parametrize(
    "words",
    [
        ["read", "run", "--client-request-id", "cr_" + "1" * 32],
        ["read", "run", "--unit-id", "a", "--unit-id", "b", "--client-request-id", "cr_" + "1" * 32],
        ["read", "run", "--unit-id", "a", "--unknown", "b", "--client-request-id", "cr_" + "1" * 32],
        ["answer", "run", "--begin", "--resume", "--task-id", "t", "--user-turn-id", "u", "--client-request-id", "cr_" + "1" * 32],
        ["delete", "paper", "--execute", "--task-id", "t", "--user-turn-id", "u", "--client-request-id", "cr_" + "1" * 32],
    ],
)
def test_duplicate_unknown_and_incomplete_flags_are_rejected(words: list[str]) -> None:
    assert parse_argv(words) is None


def test_direct_command_requires_exact_absolute_prefix_and_all_token_quoting(tmp_path: Path) -> None:
    python = tmp_path / ".venv/bin/python"
    script = tmp_path / ".agents/skills/readpaper/scripts/paper.py"
    words = [
        str(python), str(script), "check", "run_" + "1" * 32,
    ]
    assert parse_command(quote_all(words), python_path=python, script_path=script) is not None
    assert parse_command(" ".join(words), python_path=python, script_path=script) is None
    assert parse_command(quote_all(["uv", "run", *words]), python_path=python, script_path=script) is None
    assert parse_command(quote_all(words) + " | 'tee' 'x'", python_path=python, script_path=script) is None


def test_direct_command_preserves_project_venv_symlink_prefix(tmp_path: Path) -> None:
    interpreter = tmp_path / "python-real"
    interpreter.write_text("", encoding="utf-8")
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(interpreter)
    script = tmp_path / ".agents/skills/readpaper/scripts/paper.py"
    words = [str(python), str(script), "check", "run_" + "1" * 32]

    assert python.resolve() == interpreter
    assert parse_command(quote_all(words), python_path=python, script_path=script) is not None
    resolved_words = [str(python.resolve()), str(script), "check", "run_" + "1" * 32]
    assert parse_command(quote_all(resolved_words), python_path=python, script_path=script) is None


def issue(authority: InvocationAuthority, *, client: str, request: str, tool: str = "tool-1") -> dict:
    return authority.issue(
        pretool_semantic_key=digest(["pretool", tool]),
        client_request_id=client,
        request_digest=request,
        argv_sha256=digest_text("argv"),
        hook_definition_hash="h" * 64,
        task_id="task",
        session_id="session",
        turn_id="turn",
        tool_use_id=tool,
        agent_id="root",
        agent_execution_id="ae_" + "1" * 64,
        context_stream_id="cs_" + "1" * 64,
        context_epoch=0,
    )


def test_capability_is_one_use_and_completed_response_replays_exactly(tmp_path: Path) -> None:
    authority = InvocationAuthority(tmp_path)
    client = "cr_" + "2" * 32
    request = digest({"command": "prepare"})
    cap = issue(authority, client=client, request=request)
    route, replay = authority.consume_and_reserve(
        scope_key="task:prepare", client_request_id=client, request_digest=request
    )
    assert replay is None
    assert route["capability_id"] == cap["capability_id"]
    completed = authority.complete(
        scope_key="task:prepare", client_request_id=client, request_digest=request, response=b'{"ok":true}\n'
    )
    _, replayed = authority.consume_and_reserve(
        scope_key="task:prepare", client_request_id=client, request_digest=request
    )
    assert replayed == completed
    with pytest.raises(ReadPaperError) as conflict:
        authority.consume_and_reserve(
            scope_key="task:prepare", client_request_id=client, request_digest=digest({"different": True})
        )
    assert conflict.value.code is ErrorCode.STATE_CONFLICT


def test_missing_or_ambiguous_capability_fails_closed(tmp_path: Path) -> None:
    authority = InvocationAuthority(tmp_path)
    with pytest.raises(ReadPaperError) as missing:
        authority.consume_and_reserve(
            scope_key="run:read", client_request_id="cr_" + "3" * 32, request_digest="a" * 64
        )
    assert missing.value.code is ErrorCode.OBSERVER_UNAVAILABLE
    client = "cr_" + "4" * 32
    request = "b" * 64
    issue(authority, client=client, request=request, tool="tool-1")
    issue(authority, client=client, request=request, tool="tool-2")
    with pytest.raises(ReadPaperError) as ambiguous:
        authority.consume_and_reserve(scope_key="run:read", client_request_id=client, request_digest=request)
    assert ambiguous.value.code is ErrorCode.OBSERVER_UNAVAILABLE
