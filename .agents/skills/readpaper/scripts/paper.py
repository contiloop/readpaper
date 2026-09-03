#!/usr/bin/env python3
"""ReadPaper P0 product CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from readpaper.commands import CommandRuntime, error_envelope
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.parse_invocation import parse_argv


ROOT = Path(__file__).resolve().parents[4]


def main() -> int:
    invocation = parse_argv(sys.argv[1:])
    command = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    if invocation is None:
        sys.stdout.buffer.write(
            error_envelope(command, ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid ReadPaper invocation"))
        )
        return 2
    try:
        response = CommandRuntime(ROOT).execute(invocation)
    except ReadPaperError as error:
        sys.stdout.buffer.write(error_envelope(invocation.command, error))
        return 1
    except Exception:
        sys.stdout.buffer.write(
            error_envelope(invocation.command, ReadPaperError(ErrorCode.INTERNAL_ERROR, "internal ReadPaper failure"))
        )
        return 1
    sys.stdout.buffer.write(response)
    try:
        return 0 if json.loads(response).get("ok") is True else 1
    except (json.JSONDecodeError, AttributeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
