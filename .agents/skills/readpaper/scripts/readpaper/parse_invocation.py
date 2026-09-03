"""Pure strict grammar shared by production PreTool and paper.py."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .canonical import digest


CLIENT_RE = re.compile(r"^cr_[0-9a-f]{32}$")
COMMANDS = ("prepare", "read", "render", "record", "check", "answer", "resume", "delete")
SCHEMA = {
    "prepare": {"positionals": 1, "required": ("--task-id", "--user-turn-id", "--client-request-id"), "optional": ()},
    "read": {"positionals": 1, "required": ("--client-request-id",), "optional": ("--unit-id", "--batch-id")},
    "render": {"positionals": 1, "required": ("--unit-id", "--client-request-id"), "optional": ("--locator-id", "--render-dpi")},
    "record": {"positionals": 1, "required": ("--kind", "--payload", "--client-request-id"), "optional": ()},
    "check": {"positionals": 1, "required": (), "optional": ("--answer-id",)},
    "answer": {"positionals": 1, "required": ("--task-id", "--user-turn-id", "--client-request-id"), "optional": ("--begin", "--resume", "--abandon", "--finalize", "--answer-id")},
    "resume": {"positionals": 1, "required": ("--task-id", "--user-turn-id", "--client-request-id"), "optional": ()},
    "delete": {"positionals": 1, "required": ("--task-id", "--user-turn-id", "--client-request-id"), "optional": ("--preview", "--execute", "--request-id", "--approval-turn-id")},
}
BOOLEAN_FLAGS = {"--begin", "--resume", "--abandon", "--finalize", "--preview", "--execute"}
SCHEMA_SHA256 = digest(SCHEMA)


@dataclass(frozen=True)
class Invocation:
    command: str
    positional: tuple[str, ...]
    flags: dict[str, str | bool]

    def canonical_request(self) -> dict[str, object]:
        return {"schema_version": 1, "command": self.command, "positional": self.positional, "flags": self.flags}


def parse_argv(argv: Iterable[str]) -> Invocation | None:
    words = list(argv)
    if not words or words[0] not in COMMANDS:
        return None
    command = words[0]
    schema = SCHEMA[command]
    positionals: list[str] = []
    flags: dict[str, str | bool] = {}
    allowed = set(schema["required"]) | set(schema["optional"])
    index = 1
    while index < len(words):
        word = words[index]
        if word.startswith("--"):
            if word not in allowed or word in flags:
                return None
            if word in BOOLEAN_FLAGS:
                flags[word] = True
                index += 1
            else:
                if index + 1 >= len(words) or words[index + 1].startswith("--"):
                    return None
                flags[word] = words[index + 1]
                index += 2
        else:
            if flags:
                return None
            positionals.append(word)
            index += 1
    if len(positionals) != schema["positionals"] or any(flag not in flags for flag in schema["required"]):
        return None
    client = flags.get("--client-request-id")
    if client is not None and (not isinstance(client, str) or CLIENT_RE.fullmatch(client) is None):
        return None
    if command == "read" and sum(name in flags for name in ("--unit-id", "--batch-id")) != 1:
        return None
    if command == "answer":
        modes = [name for name in ("--begin", "--resume", "--abandon", "--finalize") if name in flags]
        if len(modes) != 1:
            return None
        if modes[0] == "--begin" and "--answer-id" in flags:
            return None
        if modes[0] != "--begin" and "--answer-id" not in flags:
            return None
    if command == "delete":
        if sum(name in flags for name in ("--preview", "--execute")) != 1:
            return None
        if "--execute" in flags and not all(name in flags for name in ("--request-id", "--approval-turn-id")):
            return None
        if "--preview" in flags and any(name in flags for name in ("--request-id", "--approval-turn-id")):
            return None
    return Invocation(command=command, positional=tuple(positionals), flags=flags)


def parse_command(command: str, *, python_path: Path, script_path: Path) -> Invocation | None:
    if any(character in command for character in ("\n", "\r", "\x00")):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    # The production contract binds the literal project-local command prefix.
    # Resolving the venv interpreter symlink here would replace
    # `<project>/.venv/bin/python` with the base interpreter path and reject the
    # exact command that the hook is required to authorize.
    expected_prefix = [str(python_path.absolute()), str(script_path.absolute())]
    if len(words) < 3 or words[:2] != expected_prefix:
        return None
    canonical = " ".join("'" + word.replace("'", "'\"'\"'") + "'" for word in words)
    if canonical != command:
        return None
    return parse_argv(words[2:])
