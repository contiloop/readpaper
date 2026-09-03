"""Page-preserving PDF extraction, reading units, batches, and rendering."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps
from pypdf import PdfReader

from .canonical import digest, digest_text, sha256_bytes
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id


MAX_PDF_PAGES = 200
MAX_PAGE_INCHES = 200
MAX_RASTER_AXIS = 20_000
MAX_RASTER_PIXELS = 100_000_000
UNIT_TOKEN_LIMIT = 4_000
BATCH_UNIT_LIMIT = 8
BATCH_TOKEN_LIMIT = 12_000


def estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        count = len(tiktoken.get_encoding("o200k_base").encode(text))
        return math.ceil(count * 1.2)
    except Exception:
        return len(text.encode("utf-8"))


def _markers(text: str) -> tuple[str, str, str]:
    size = min(64, len(text))
    middle = max(0, (len(text) - size) // 2)
    return (
        digest_text(text[:size]),
        digest_text(text[middle : middle + size]),
        digest_text(text[-size:] if size else ""),
    )


@dataclass(frozen=True)
class PageText:
    pdf_page: int
    pdf_label: str | None
    width_points: float
    height_points: float
    text: str
    text_sha256: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReadingUnit:
    artifact_ref_id: str
    artifact_id: str
    unit_id: str
    section_id: str
    media_kind: str
    pdf_page: int
    chunk_index: int
    chunk_count: int
    char_start: int
    char_end: int
    content: str
    content_sha256: str
    char_count: int
    utf8_byte_count: int
    estimated_tokens: int
    start_marker: str
    middle_marker: str
    end_marker: str


@dataclass(frozen=True)
class ReadBatch:
    batch_id: str
    section_id: str
    batch_index: int
    unit_ids: tuple[str, ...]
    estimated_tokens: int


@dataclass(frozen=True)
class ExtractedDocument:
    pages: tuple[PageText, ...]
    units: tuple[ReadingUnit, ...]
    batches: tuple[ReadBatch, ...]


def extract_text(data: bytes, *, bundle_id: str, artifact_ref_id: str, artifact_id: str) -> ExtractedDocument:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "text supplementary is not UTF-8") from error
    offsets = _split_offsets(text)
    first_seed = digest([bundle_id, artifact_ref_id, offsets[0]])
    section = _section_id(artifact_ref_id, 1, first_seed)
    units: list[ReadingUnit] = []
    for index, (start, end) in enumerate(offsets, start=1):
        content = text[start:end]
        start_marker, middle_marker, end_marker = _markers(content)
        units.append(ReadingUnit(
            artifact_ref_id=artifact_ref_id, artifact_id=artifact_id,
            unit_id=f"{artifact_ref_id}:t:c{index:06d}", section_id=section, media_kind="text",
            pdf_page=0, chunk_index=index, chunk_count=len(offsets), char_start=start, char_end=end,
            content=content, content_sha256=digest_text(content), char_count=len(content),
            utf8_byte_count=len(content.encode("utf-8")), estimated_tokens=estimate_tokens(content),
            start_marker=start_marker, middle_marker=middle_marker, end_marker=end_marker,
        ))
    batches: list[ReadBatch] = []
    for offset in range(0, len(units), BATCH_UNIT_LIMIT):
        group = units[offset:offset + BATCH_UNIT_LIMIT]
        ids = tuple(item.unit_id for item in group)
        batches.append(ReadBatch(
            batch_id=sequence_id("rb", 1, ids), section_id=section,
            batch_index=len(batches) + 1, unit_ids=ids,
            estimated_tokens=sum(item.estimated_tokens for item in group),
        ))
    return ExtractedDocument(pages=(), units=tuple(units), batches=tuple(batches))


def _split_offsets(text: str, limit: int = UNIT_TOKEN_LIMIT) -> list[tuple[int, int]]:
    if not text:
        return [(0, 0)]
    offsets: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(text[start:middle]) <= limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        end = best
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start:
                end = boundary + 1
        offsets.append((start, end))
        start = end
    return offsets


def _section_id(artifact_ref_id: str, page: int, first_unit_seed: str) -> str:
    return sequence_id("sec", artifact_ref_id, page, [f"Synthetic {page}"], first_unit_seed)


def extract_pdf(data: bytes, *, bundle_id: str, artifact_ref_id: str, artifact_id: str) -> ExtractedDocument:
    if not data.startswith(b"%PDF-"):
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "PDF magic is missing")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
    except Exception as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "PDF parser rejected source") from error
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "PDF page limit exceeded")
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "pdftotext is unavailable")
    pages: list[PageText] = []
    units: list[ReadingUnit] = []
    labels = getattr(reader, "page_labels", []) or []
    with tempfile.TemporaryDirectory(prefix="readpaper-text-") as directory:
      source = Path(directory) / "source.pdf"
      source.write_bytes(data)
      for index, page in enumerate(reader.pages, start=1):
        box = page.cropbox
        width = float(box.width)
        height = float(box.height)
        if width <= 0 or height <= 0 or width / 72 > MAX_PAGE_INCHES or height / 72 > MAX_PAGE_INCHES:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "PDF page dimensions are unsafe")
        warnings: list[str] = []
        try:
            completed = subprocess.run(
                [executable, "-layout", "-enc", "UTF-8", "-f", str(index), "-l", str(index), str(source), "-"],
                capture_output=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ReadPaperError(ErrorCode.TIMEOUT, f"text extraction timed out on page {index}") from error
        if completed.returncode != 0:
            raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, f"pdftotext failed on page {index}")
        try:
            text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, f"pdftotext returned invalid UTF-8 on page {index}") from error
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if text.endswith("\f"):
            text = text[:-1]
            if text.endswith("\n"):
                text = text[:-1]
        if not text.strip():
            try:
                warnings.append("suspected_scan" if len(page.images) else "empty_text")
            except Exception:
                warnings.append("empty_text")
        if "�" in text:
            warnings.append("text_replacement_character")
        pages.append(
            PageText(
                pdf_page=index,
                pdf_label=labels[index - 1] if index <= len(labels) else None,
                width_points=width,
                height_points=height,
                text=text,
                text_sha256=digest_text(text),
                warnings=tuple(warnings),
            )
        )
        offsets = _split_offsets(text)
        if not text:
            continue
        seed = digest([bundle_id, artifact_ref_id, index, offsets[0]])
        section = _section_id(artifact_ref_id, index, seed)
        for chunk_index, (start, end) in enumerate(offsets, start=1):
            content = text[start:end]
            content_sha = digest_text(content)
            unit_id = f"{artifact_ref_id}:p{index:06d}:c{chunk_index:04d}"
            start_marker, middle_marker, end_marker = _markers(content)
            units.append(
                ReadingUnit(
                    artifact_ref_id=artifact_ref_id,
                    artifact_id=artifact_id,
                    unit_id=unit_id,
                    section_id=section,
                    media_kind="pdf",
                    pdf_page=index,
                    chunk_index=chunk_index,
                    chunk_count=len(offsets),
                    char_start=start,
                    char_end=end,
                    content=content,
                    content_sha256=content_sha,
                    char_count=len(content),
                    utf8_byte_count=len(content.encode("utf-8")),
                    estimated_tokens=estimate_tokens(content),
                    start_marker=start_marker,
                    middle_marker=middle_marker,
                    end_marker=end_marker,
                )
            )
    batches: list[ReadBatch] = []
    grouped: dict[str, list[ReadingUnit]] = {}
    for unit in units:
        grouped.setdefault(unit.section_id, []).append(unit)
    for section, section_units in grouped.items():
        current: list[ReadingUnit] = []
        current_tokens = 0
        section_batches: list[list[ReadingUnit]] = []
        for unit in section_units:
            if current and (len(current) >= BATCH_UNIT_LIMIT or current_tokens + unit.estimated_tokens > BATCH_TOKEN_LIMIT):
                section_batches.append(current)
                current, current_tokens = [], 0
            current.append(unit)
            current_tokens += unit.estimated_tokens
        if current:
            section_batches.append(current)
        for index, group in enumerate(section_batches, start=1):
            ids = tuple(item.unit_id for item in group)
            batches.append(
                ReadBatch(
                    batch_id=sequence_id("rb", 1, ids),
                    section_id=section,
                    batch_index=index,
                    unit_ids=ids,
                    estimated_tokens=sum(item.estimated_tokens for item in group),
                )
            )
    return ExtractedDocument(pages=tuple(pages), units=tuple(units), batches=tuple(batches))


@dataclass(frozen=True)
class RenderedImage:
    path: Path
    pixel_width: int
    pixel_height: int
    image_sha256: str
    pixel_sha256: str
    render_dpi: int | None


def _validate_image(path: Path, *, dpi: int | None) -> RenderedImage:
    try:
        with Image.open(path) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "multi-frame image is unsupported")
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            width, height = image.size
            if width > MAX_RASTER_AXIS or height > MAX_RASTER_AXIS or width * height > MAX_RASTER_PIXELS:
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "raster dimensions are unsafe")
            pixel_sha = sha256_bytes(image.tobytes())
    except ReadPaperError:
        raise
    except Exception as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "invalid raster output") from error
    return RenderedImage(
        path=path,
        pixel_width=width,
        pixel_height=height,
        image_sha256=sha256_bytes(path.read_bytes()),
        pixel_sha256=pixel_sha,
        render_dpi=dpi,
    )


def render_pdf_page(source: Path, *, pdf_page: int, output: Path, dpi: int = 144) -> RenderedImage:
    if not 72 <= dpi <= 600 or pdf_page < 1:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid page or DPI")
    executable = shutil.which("pdftoppm")
    if executable is None:
        raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "pdftoppm is unavailable")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="readpaper-render-", dir=output.parent) as directory:
        prefix = Path(directory) / "page"
        command = [
            executable,
            "-f", str(pdf_page), "-l", str(pdf_page), "-singlefile", "-png", "-cropbox",
            "-r", str(dpi), str(source), str(prefix),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as error:
            raise ReadPaperError(ErrorCode.TIMEOUT, "PDF render timed out") from error
        rendered = prefix.with_suffix(".png")
        if completed.returncode != 0 or not rendered.is_file():
            raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "pdftoppm failed")
        result = _validate_image(rendered, dpi=dpi)
        output.write_bytes(rendered.read_bytes())
    return _validate_image(output, dpi=dpi)


def render_image(source: Path, *, output: Path) -> RenderedImage:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "multi-frame image is unsupported")
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            width, height = image.size
            if width > MAX_RASTER_AXIS or height > MAX_RASTER_AXIS or width * height > MAX_RASTER_PIXELS:
                raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "raster dimensions are unsafe")
            image.save(output, format="PNG", optimize=False)
    except ReadPaperError:
        raise
    except Exception as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "image decode failed") from error
    return _validate_image(output, dpi=None)
