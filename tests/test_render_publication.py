from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from readpaper.canonical import sha256_bytes
from readpaper.documents import render_image, render_pdf_page
from readpaper.errors import ErrorCode, ReadPaperError
from test_t3_documents import make_pdf
from test_t9_hooks import execute_authorized, prepared_run


@pytest.fixture(params=["pdf", "image"])
def render(tmp_path: Path, request):
    if request.param == "pdf":
        source = tmp_path / "source.pdf"
        make_pdf(source, pages=1)
        return lambda output: render_pdf_page(source, pdf_page=1, output=output)
    source = tmp_path / "source.png"
    Image.new("RGB", (16, 16), "blue").save(source)
    return lambda output: render_image(source, output=output)


@pytest.mark.parametrize("destination", ["symlink", "dangling_symlink", "directory", "fifo"])
def test_render_rejects_nonregular_destination_without_touching_target(tmp_path: Path, render, destination: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output = evidence / "page.png"
    external = tmp_path / "external.txt"
    original = b"External file must remain unchanged."
    if destination == "symlink":
        external.write_bytes(original)
        output.symlink_to(external)
    elif destination == "dangling_symlink":
        output.symlink_to(external)
    elif destination == "directory":
        output.mkdir()
    else:
        os.mkfifo(output)
    before = output.lstat()
    with pytest.raises(ReadPaperError) as error:
        render(output)
    assert error.value.code == ErrorCode.INVALID_ARGUMENT
    assert (output.lstat().st_ino, output.lstat().st_mode) == (before.st_ino, before.st_mode)
    assert list(evidence.iterdir()) == [output]  # Temporary files are cleaned on failure.
    if destination == "symlink":
        assert external.read_bytes() == original
    else:
        assert not external.exists()


def test_render_atomically_replaces_regular_file_with_valid_private_image(tmp_path: Path, render) -> None:
    output = tmp_path / "page.png"
    output.write_bytes(b"Previous render")
    with output.open("rb") as old_file:
        rendered = render(output)
        assert old_file.read() == b"Previous render"  # The original inode was not overwritten.
    assert rendered.path == output
    assert rendered.image_sha256 == sha256_bytes(output.read_bytes())
    assert output.stat().st_mode & 0o777 == 0o600
    with Image.open(output) as image:
        assert image.size == (rendered.pixel_width, rendered.pixel_height)
        image.verify()
    assert not list(tmp_path.glob("readpaper-render-*"))


def test_invalid_render_preserves_previous_output(tmp_path: Path, monkeypatch, render) -> None:
    output = tmp_path / "page.png"
    original = b"Previous render"
    output.write_bytes(original)
    def reject_image(*args, **kwargs):
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "invalid raster output")
    monkeypatch.setattr("readpaper.documents._validate_image", reject_image)
    with pytest.raises(ReadPaperError) as error:
        render(output)
    assert error.value.code == ErrorCode.CORRUPT_ARTIFACT
    assert output.read_bytes() == original
    assert not list(tmp_path.glob("readpaper-render-*"))


def test_protected_render_rejects_symlink_without_creating_evidence(tmp_path: Path) -> None:
    runtime, prepared, _, unit_id = prepared_run(tmp_path)
    output = runtime.state.layout.run_dir(prepared["paper_id"], prepared["run_id"]) / "evidence" / f"{unit_id}-144.png"
    output.parent.mkdir()
    external = tmp_path / "external.txt"
    external.write_bytes(b"Keep this content")
    output.symlink_to(external)
    response = execute_authorized(runtime, ["render", prepared["run_id"], "--unit-id", unit_id,
                                  "--client-request-id", "cr_" + "4" * 32], "render-symlink")
    assert not response["ok"]
    assert response["error"]["code"] == "INVALID_ARGUMENT"
    assert external.read_bytes() == b"Keep this content"
    events = [json.loads(line) for line in runtime.state.layout.run_events(prepared["paper_id"], prepared["run_id"]).read_text().splitlines()]
    assert not any(event["event_kind"] in {"render_created", "visual_open_observed"} for event in events)
