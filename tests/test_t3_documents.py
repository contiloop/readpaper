from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from readpaper.documents import (
    extract_pdf,
    extract_text,
    is_heading_candidate,
    materialize_source_ranges,
    render_pdf_page,
)
from readpaper.ids import artifact_id, artifact_ref_id, bundle_id, paper_id
from readpaper.locators import PdfObjectLocator, PdfPageLocator, TextSpanLocator, bbox_to_ppm


def make_pdf(path: Path, pages: int = 10) -> bytes:
    document = canvas.Canvas(str(path), pagesize=letter)
    for page in range(1, pages + 1):
        document.drawString(72, 720, f"PAGE-{page}-START")
        document.drawString(72, 680, f"This is the unique body for page {page}.")
        document.drawString(72, 640, f"PAGE-{page}-END")
        document.showPage()
    document.save()
    return path.read_bytes()


def identities(data: bytes) -> tuple[str, str, str, str]:
    paper = paper_id(data)
    artifact = artifact_id(data)
    ref = artifact_ref_id(role="main", source_token="local")
    bundle = bundle_id(
        schema_version=2,
        paper_id=paper,
        landing_url=None,
        artifacts=[{"artifact_ref_id": ref, "artifact_id": artifact}],
    )
    return paper, artifact, ref, bundle


def test_ten_page_extraction_preserves_every_page_and_marker(tmp_path: Path) -> None:
    data = make_pdf(tmp_path / "ten.pdf")
    _, artifact, ref, bundle = identities(data)
    import subprocess
    with patch("readpaper.documents.subprocess.run", wraps=subprocess.run) as process:
        result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    assert process.call_count == 1
    assert Path(process.call_args.args[0][0]).name == "pdftotext"
    assert len(result.pages) == 10
    assert result.sections
    assert result.frames
    for page in result.pages:
        ranges = sorted(
            (
                source_range
                for section in result.sections
                for source_range in section.source_ranges
                if source_range.pdf_page == page.pdf_page
            ),
            key=lambda item: item.char_start,
        )
        joined = "".join(page.text[item.char_start:item.char_end] for item in ranges)
        assert joined == page.text
        assert f"PAGE-{page.pdf_page}-START" in joined
        assert f"PAGE-{page.pdf_page}-END" in joined
    assert all(frame.estimated_tokens <= 48_000 for frame in result.frames)


def test_empty_page_is_not_silently_treated_as_text_coverage(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    document = canvas.Canvas(str(path), pagesize=letter)
    document.showPage()
    document.save()
    data = path.read_bytes()
    _, artifact, ref, bundle = identities(data)
    result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    assert result.pages[0].warnings == ("empty_text",)
    assert result.sections == ()
    assert result.frames == ()


def test_blank_middle_page_keeps_original_page_numbers(tmp_path: Path) -> None:
    path = tmp_path / "blank-middle.pdf"
    document = canvas.Canvas(str(path))
    for text in ("first page", "", "third page"):
        if text:
            document.drawString(72, 720, text)
        document.showPage()
    document.save()
    data = path.read_bytes()
    _, artifact, ref, bundle = identities(data)
    result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    assert [page.pdf_page for page in result.pages] == [1, 2, 3]
    assert result.pages[1].warnings == ("empty_text",)
    assert "third page" in result.pages[2].text


def test_numbered_headings_create_nonoverlapping_logical_sections(tmp_path: Path) -> None:
    path = tmp_path / "headings.pdf"
    document = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for heading in (
        "Abstract",
        "1 Introduction",
        "2 Related Work",
        "3 Method",
        "3.1 Architecture",
        "4 Experiments",
        "5 Conclusion",
        "References",
    ):
        document.drawString(72, y, heading)
        y -= 24
        document.drawString(72, y, f"Body for {heading}")
        y -= 36
    document.showPage()
    document.save()
    data = path.read_bytes()
    _, artifact, ref, bundle = identities(data)
    result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    titles = [section.title for section in result.sections]
    assert "1 Introduction" in titles
    assert "3 Method" in titles
    assert "3.1 Architecture" in titles
    assert "References" in titles
    method = next(section for section in result.sections if section.title == "3 Method")
    architecture = next(section for section in result.sections if section.title == "3.1 Architecture")
    assert architecture.parent_section_id == method.section_id
    reconstructed = "".join(
        result.pages[0].text[item.char_start:item.char_end]
        for section in result.sections
        for item in section.source_ranges
    )
    assert reconstructed == result.pages[0].text


def test_figure_caption_is_not_a_section_heading() -> None:
    assert not is_heading_candidate(
        "Figure 3. Accuracy on the validation set.",
        previous_blank=True,
        next_blank=True,
    )


@pytest.mark.parametrize(
    "line",
    [
        "A simple baseline is used for comparison",
        "I propose a simple alternative",
        "V denotes the vocabulary size",
        "1 We evaluate the baseline on two datasets",
        "2024 was a significant year for this method",
    ],
)
def test_unpunctuated_letter_or_roman_prose_is_not_a_heading(line: str) -> None:
    assert not is_heading_candidate(line, previous_blank=False, next_blank=False)
    assert not is_heading_candidate(line, previous_blank=True, next_blank=True)


def test_decimal_and_punctuated_appendix_headings_are_recognized() -> None:
    assert is_heading_candidate(
        "3.2 Training Objective",
        previous_blank=True,
        next_blank=True,
    )
    assert is_heading_candidate(
        "A. Additional Results",
        previous_blank=True,
        next_blank=False,
    )


def test_large_section_uses_multiple_transport_frames_without_overlap() -> None:
    text = "A long methodological paragraph with evidence.\n\n" * 2_000
    artifact = artifact_id(text.encode())
    ref = artifact_ref_id(role="supplementary", source_token="large.txt")
    bundle = bundle_id(
        schema_version=2,
        paper_id="p_" + "1" * 64,
        landing_url=None,
        artifacts=[{"artifact_ref_id": ref, "artifact_id": artifact}],
    )
    result = extract_text(
        text.encode(),
        bundle_id=bundle,
        artifact_ref_id=ref,
        artifact_id=artifact,
        frame_token_limit=400,
    )
    assert len(result.sections) == 1
    assert len(result.frames) > 1
    assert all(frame.estimated_tokens <= 400 for frame in result.frames)
    reconstructed = "".join(
        materialize_source_ranges(result.pages, frame.source_ranges, include_page_markers=False)
        for frame in result.frames
    )
    assert reconstructed == text


def test_pdf_render_uses_cropbox_and_records_pixel_identity(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    make_pdf(source, pages=1)
    rendered = render_pdf_page(source, pdf_page=1, output=tmp_path / "rendered.png", dpi=144)
    assert rendered.path.is_file()
    assert rendered.pixel_width == 1224
    assert rendered.pixel_height == 1584
    assert len(rendered.image_sha256) == len(rendered.pixel_sha256) == 64


def test_locator_union_is_strict_and_render_scale_independent(tmp_path: Path) -> None:
    data = make_pdf(tmp_path / "source.pdf", pages=1)
    _, artifact, ref, bundle = identities(data)
    page = PdfPageLocator(bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact, pdf_page=1)
    assert page.locator_id.startswith("loc_")
    ppm_72 = bbox_to_ppm(left=10, top=20, right=100, bottom=120, width=612, height=792)
    ppm_144 = bbox_to_ppm(left=20, top=40, right=200, bottom=240, width=1224, height=1584)
    assert ppm_72 == ppm_144
    first = PdfObjectLocator(
        bundle_id=bundle,
        artifact_ref_id=ref,
        artifact_id=artifact,
        pdf_page=1,
        object_kind="figure",
        bbox_ppm=ppm_72,
    )
    second = PdfObjectLocator(
        bundle_id=bundle,
        artifact_ref_id=ref,
        artifact_id=artifact,
        pdf_page=1,
        object_kind="figure",
        bbox_ppm=ppm_144,
    )
    assert first.locator_id == second.locator_id
    with pytest.raises(ValidationError):
        PdfPageLocator(
            bundle_id=bundle,
            artifact_ref_id=ref,
            artifact_id=artifact,
            pdf_page=1,
            printed_label="1",
        )


def test_text_span_rejects_empty_or_reversed_bounds(tmp_path: Path) -> None:
    data = make_pdf(tmp_path / "source.pdf", pages=1)
    _, artifact, ref, bundle = identities(data)
    with pytest.raises(ValidationError):
        TextSpanLocator(
            bundle_id=bundle,
            artifact_ref_id=ref,
            artifact_id=artifact,
            pdf_page=1,
            char_start=2,
            char_end=2,
            content_sha256="0" * 64,
        )
