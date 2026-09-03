from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from readpaper.documents import extract_pdf, render_pdf_page
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
        schema_version=1,
        paper_id=paper,
        landing_url=None,
        artifacts=[{"artifact_ref_id": ref, "artifact_id": artifact}],
    )
    return paper, artifact, ref, bundle


def test_ten_page_extraction_preserves_every_page_and_marker(tmp_path: Path) -> None:
    data = make_pdf(tmp_path / "ten.pdf")
    _, artifact, ref, bundle = identities(data)
    result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    assert len(result.pages) == 10
    assert {unit.pdf_page for unit in result.units} == set(range(1, 11))
    for page in result.pages:
        joined = "".join(unit.content for unit in result.units if unit.pdf_page == page.pdf_page)
        assert joined == page.text
        assert f"PAGE-{page.pdf_page}-START" in joined
        assert f"PAGE-{page.pdf_page}-END" in joined
    assert all(unit.estimated_tokens <= 4_000 for unit in result.units)
    assert all(len(batch.unit_ids) <= 8 and batch.estimated_tokens <= 12_000 for batch in result.batches)


def test_empty_page_is_not_silently_treated_as_text_coverage(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    document = canvas.Canvas(str(path), pagesize=letter)
    document.showPage()
    document.save()
    data = path.read_bytes()
    _, artifact, ref, bundle = identities(data)
    result = extract_pdf(data, bundle_id=bundle, artifact_ref_id=ref, artifact_id=artifact)
    assert result.pages[0].warnings == ("empty_text",)
    assert result.units == ()


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
