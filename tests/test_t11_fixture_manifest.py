from __future__ import annotations

import json
from pathlib import Path


def test_required_fixture_matrix_is_closed_and_unique() -> None:
    path = Path(__file__).with_name("fixture_manifest.json")
    value = json.loads(path.read_text())
    fixtures = value["fixtures"]
    assert value["schema_version"] == 1
    assert len(fixtures) == len(set(fixtures)) == 28
    assert {"single-column", "scan-like", "answer-hash-mismatch", "delete-crash-phases"} <= set(fixtures)
