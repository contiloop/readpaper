"""Page-preserving extraction, semantic sections, transport frames, and rendering."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Sequence

from PIL import Image, ImageOps
from pypdf import PdfReader

from .canonical import digest_text, sha256_bytes
from .errors import ErrorCode, ReadPaperError
from .ids import sequence_id


MAX_PDF_PAGES = 200
MAX_PAGE_INCHES = 200
MAX_RASTER_AXIS = 20_000
MAX_RASTER_PIXELS = 100_000_000
TRANSPORT_FRAME_TOKEN_LIMIT = 48_000

KNOWN_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "preliminaries",
    "problem formulation",
    "method",
    "methods",
    "methodology",
    "approach",
    "model",
    "architecture",
    "implementation",
    "experimental setup",
    "experiments",
    "evaluation",
    "results",
    "analysis",
    "discussion",
    "limitations",
    "conclusion",
    "conclusions",
    "future work",
    "broader impact",
    "acknowledgments",
    "acknowledgements",
    "references",
    "appendix",
    "supplementary material",
}
DECIMAL_HEADING_RE = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)*)[.)]?\s+(?P<title>\S.{0,160})\s*$"
)
ROMAN_HEADING_RE = re.compile(
    r"^\s*(?P<number>[IVXLC]+)[.)]\s+(?P<title>\S.{0,160})\s*$"
)
LETTER_HEADING_RE = re.compile(
    r"^\s*(?P<number>[A-Z])[.)]\s+(?P<title>\S.{0,160})\s*$"
)
APPENDIX_HEADING_RE = re.compile(
    r"^\s*(?:appendix|supplementary)\s*(?:[A-Z0-9]+)?(?:\s*[:.\-]\s*|\s+)?(.{0,160})$",
    re.IGNORECASE,
)
REJECT_HEADING_PREFIXES = (
    "figure ",
    "fig. ",
    "table ",
    "algorithm ",
    "equation ",
    "lemma ",
    "theorem ",
    "proof.",
)
HEADING_SCORE_THRESHOLD = 5


def _numbered_heading_match(line: str) -> re.Match[str] | None:
    for pattern in (DECIMAL_HEADING_RE, ROMAN_HEADING_RE, LETTER_HEADING_RE):
        match = pattern.fullmatch(line)
        if match is not None:
            return match
    return None


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
class SourceRange:
    artifact_ref_id: str
    artifact_id: str
    pdf_page: int
    char_start: int
    char_end: int
    content_sha256: str


@dataclass(frozen=True)
class DocumentSection:
    section_id: str
    artifact_ref_id: str
    artifact_id: str
    ordinal: int
    title: str
    normalized_title: str
    level: int
    parent_section_id: str | None
    start_page: int
    end_page: int
    source_ranges: tuple[SourceRange, ...]
    content_sha256: str
    estimated_tokens: int
    detection_method: Literal[
        "known_heading",
        "numbered_heading",
        "text_heuristic",
        "front_matter",
        "fallback",
    ]
    detection_confidence: float
    frame_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransportFrame:
    frame_id: str
    section_id: str
    frame_index: int
    frame_count: int
    source_ranges: tuple[SourceRange, ...]
    content_sha256: str
    estimated_tokens: int
    start_marker: str
    middle_marker: str
    end_marker: str


@dataclass(frozen=True)
class ExtractedDocument:
    pages: tuple[PageText, ...]
    sections: tuple[DocumentSection, ...]
    frames: tuple[TransportFrame, ...]


@dataclass(frozen=True)
class HeadingBoundary:
    title: str
    normalized_title: str
    level: int
    pdf_page: int
    char_start: int
    detection_method: Literal["known_heading", "numbered_heading", "text_heuristic"]
    confidence: float


def normalize_repeated_line(text: str) -> str:
    normalized = re.sub(r"\d+", "#", text.casefold())
    return " ".join(normalized.split())


def heading_score(line: str, *, previous_blank: bool, next_blank: bool) -> int:
    stripped = " ".join(line.split())
    normalized = stripped.casefold()
    if not stripped or len(stripped) > 180:
        return -100
    if normalized.startswith(REJECT_HEADING_PREFIXES):
        return -100
    score = 0
    if normalized.rstrip(":") in KNOWN_HEADINGS:
        score += 6
    numbered = _numbered_heading_match(stripped)
    if numbered is not None:
        title = numbered.group("title")
        number = numbered.group("number")
        # Unpunctuated decimal prefixes need independent heading evidence.
        explicit_punctuation = bool(re.match(r"^\s*\d+(?:\.\d+)*[.)]\s+", stripped))
        if number[0].isdigit() and not explicit_punctuation:
            words = title.split()
            title_case = bool(words) and all(word[:1].isupper() or word.casefold() in {"a", "an", "the", "of", "and", "for", "in", "to", "with", "on"} for word in words)
            signals = sum((previous_blank, next_blank, len(words) <= 10, title_case, title.casefold() in KNOWN_HEADINGS))
            if len(number.split(".")[0]) > 2 or not (title_case or title.casefold() in KNOWN_HEADINGS) or signals < 2:
                return -100
        score += 5
    if APPENDIX_HEADING_RE.fullmatch(stripped):
        score += 6
    if previous_blank:
        score += 1
    if next_blank:
        score += 1
    if stripped.isupper() and 2 <= len(stripped.split()) <= 12:
        score += 2
    if stripped.endswith(".") and normalized not in KNOWN_HEADINGS:
        score -= 2
    if len(stripped.split()) > 18:
        score -= 3
    return score


def is_heading_candidate(line: str, *, previous_blank: bool, next_blank: bool) -> bool:
    return heading_score(line, previous_blank=previous_blank, next_blank=next_blank) >= HEADING_SCORE_THRESHOLD


def _line_records(text: str) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        records.append((offset, raw.rstrip("\r\n")))
        offset += len(raw)
    if text and not records:
        records.append((0, text))
    return records


def _repeated_header_footer_lines(pages: Sequence[PageText]) -> set[str]:
    appearances: dict[tuple[str, str], set[int]] = {}
    for page in pages:
        lines = _line_records(page.text)
        if not lines:
            continue
        region_size = max(1, math.ceil(len(lines) * 0.10))
        for region, selected in (("top", lines[:region_size]), ("bottom", lines[-region_size:])):
            for _, line in selected:
                normalized = normalize_repeated_line(line)
                if normalized:
                    appearances.setdefault((region, normalized), set()).add(page.pdf_page)
    return {
        normalized
        for (_, normalized), page_numbers in appearances.items()
        if len(page_numbers) >= 3
    }


def _heading_level(line: str) -> int:
    match = _numbered_heading_match(" ".join(line.split()))
    if match is None:
        return 1
    number = match.group("number")
    if number[:1].isdigit():
        return number.count(".") + 1
    return 1


def _normalized_heading_title(line: str) -> str:
    stripped = " ".join(line.split()).rstrip(":")
    numbered = _numbered_heading_match(stripped)
    if numbered is not None:
        stripped = numbered.group("title")
    return stripped.casefold()


def _heading_method(line: str) -> Literal["known_heading", "numbered_heading", "text_heuristic"]:
    normalized = " ".join(line.split()).casefold().rstrip(":")
    if normalized in KNOWN_HEADINGS or APPENDIX_HEADING_RE.fullmatch(line):
        return "known_heading"
    if _numbered_heading_match(" ".join(line.split())):
        return "numbered_heading"
    return "text_heuristic"


def detect_heading_boundaries(pages: Sequence[PageText]) -> tuple[HeadingBoundary, ...]:
    repeated = _repeated_header_footer_lines(pages)
    boundaries: list[HeadingBoundary] = []
    for page in pages:
        lines = _line_records(page.text)
        for index, (char_start, line) in enumerate(lines):
            if normalize_repeated_line(line) in repeated:
                continue
            previous_blank = index == 0 or not lines[index - 1][1].strip()
            next_blank = index + 1 == len(lines) or not lines[index + 1][1].strip()
            score = heading_score(line, previous_blank=previous_blank, next_blank=next_blank)
            if score < HEADING_SCORE_THRESHOLD:
                continue
            title = " ".join(line.split())
            boundaries.append(
                HeadingBoundary(
                    title=title,
                    normalized_title=_normalized_heading_title(title),
                    level=_heading_level(title),
                    pdf_page=page.pdf_page,
                    char_start=char_start,
                    detection_method=_heading_method(title),
                    confidence=min(1.0, max(0.0, score / 8)),
                )
            )
    return tuple(boundaries)


def _range_for_slice(
    *, artifact_ref_id: str, artifact_id: str, page: PageText, start: int, end: int
) -> SourceRange:
    return SourceRange(
        artifact_ref_id=artifact_ref_id,
        artifact_id=artifact_id,
        pdf_page=page.pdf_page,
        char_start=start,
        char_end=end,
        content_sha256=digest_text(page.text[start:end]),
    )


def _ranges_between(
    pages: Sequence[PageText],
    *,
    artifact_ref_id: str,
    artifact_id: str,
    start: tuple[int, int],
    end: tuple[int, int],
) -> tuple[SourceRange, ...]:
    ranges: list[SourceRange] = []
    start_page, start_char = start
    end_page, end_char = end
    for page in pages:
        if page.pdf_page < start_page or page.pdf_page > end_page or not page.text:
            continue
        char_start = start_char if page.pdf_page == start_page else 0
        char_end = end_char if page.pdf_page == end_page else len(page.text)
        if char_end > char_start:
            ranges.append(
                _range_for_slice(
                    artifact_ref_id=artifact_ref_id,
                    artifact_id=artifact_id,
                    page=page,
                    start=char_start,
                    end=char_end,
                )
            )
    return tuple(ranges)


def _page_value(page: PageText | dict[str, Any], name: str) -> Any:
    return page[name] if isinstance(page, dict) else getattr(page, name)


def _range_value(source_range: SourceRange | dict[str, Any], name: str) -> Any:
    return source_range[name] if isinstance(source_range, dict) else getattr(source_range, name)


def materialize_source_ranges(
    pages: Sequence[PageText | dict[str, Any]],
    source_ranges: Sequence[SourceRange | dict[str, Any]],
    *,
    include_page_markers: bool = True,
) -> str:
    lookup: dict[tuple[str | None, int], str] = {}
    for page in pages:
        artifact_ref = _page_value(page, "artifact_ref_id") if isinstance(page, dict) else None
        lookup[(artifact_ref, int(_page_value(page, "pdf_page")))] = str(_page_value(page, "text"))
    output: list[str] = []
    for source_range in source_ranges:
        artifact_ref = str(_range_value(source_range, "artifact_ref_id"))
        pdf_page = int(_range_value(source_range, "pdf_page"))
        text = lookup.get((artifact_ref, pdf_page), lookup.get((None, pdf_page)))
        if text is None:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "source range references a missing canonical page")
        start = int(_range_value(source_range, "char_start"))
        end = int(_range_value(source_range, "char_end"))
        if start < 0 or end <= start or end > len(text):
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "source range is outside canonical page text")
        content = text[start:end]
        expected = _range_value(source_range, "content_sha256")
        if expected is not None and digest_text(content) != expected:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "source range hash differs from canonical page text")
        if include_page_markers:
            if output:
                output.append("\n\n")
            label = f"PDF PAGE {pdf_page}" if pdf_page > 0 else "TEXT ARTIFACT"
            output.append(f"[{label}]\n")
        output.append(content)
    return "".join(output)


def split_transport_offsets(text: str, *, token_limit: int) -> list[tuple[int, int]]:
    """Split one payload without overlap, preferring paragraph and line boundaries."""

    if token_limit < 1:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "transport frame token limit must be positive")
    if not text:
        return [(0, 0)]
    offsets: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = start
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(text[start:middle]) <= token_limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= start:
            raise ReadPaperError(ErrorCode.STATE_CONFLICT, "transport splitter failed to make progress")
        end = best
        if end < len(text):
            minimum = start + max(1, int((end - start) * 0.70))
            boundary = max(
                text.rfind("\n\n", minimum, end),
                text.rfind("\n", minimum, end),
                text.rfind(" ", minimum, end),
            )
            if boundary > start:
                end = boundary + (2 if text.startswith("\n\n", boundary) else 1)
        offsets.append((start, end))
        start = end
    return offsets


def _make_section(
    *,
    bundle_id: str,
    artifact_ref_id: str,
    artifact_id: str,
    ordinal: int,
    title: str,
    normalized_title: str,
    level: int,
    source_ranges: tuple[SourceRange, ...],
    detection_method: Literal["known_heading", "numbered_heading", "text_heuristic", "front_matter", "fallback"],
    detection_confidence: float,
    pages: Sequence[PageText],
) -> DocumentSection:
    content = materialize_source_ranges(pages, source_ranges, include_page_markers=False)
    section_id = sequence_id(
        "sec",
        2,
        bundle_id,
        artifact_ref_id,
        ordinal,
        title,
        [
            [item.pdf_page, item.char_start, item.char_end, item.content_sha256]
            for item in source_ranges
        ],
    )
    return DocumentSection(
        section_id=section_id,
        artifact_ref_id=artifact_ref_id,
        artifact_id=artifact_id,
        ordinal=ordinal,
        title=title,
        normalized_title=normalized_title,
        level=level,
        parent_section_id=None,
        start_page=source_ranges[0].pdf_page,
        end_page=source_ranges[-1].pdf_page,
        source_ranges=source_ranges,
        content_sha256=digest_text(content),
        estimated_tokens=estimate_tokens(content),
        detection_method=detection_method,
        detection_confidence=detection_confidence,
    )


def build_document_sections(
    pages: Sequence[PageText],
    *,
    bundle_id: str,
    artifact_ref_id: str,
    artifact_id: str,
) -> tuple[DocumentSection, ...]:
    nonempty_pages = [page for page in pages if page.text]
    if not nonempty_pages:
        return ()
    boundaries = list(detect_heading_boundaries(pages))
    document_start = (pages[0].pdf_page, 0)
    document_end = (pages[-1].pdf_page, len(pages[-1].text))
    definitions: list[
        tuple[
            str,
            str,
            int,
            tuple[int, int],
            tuple[int, int],
            Literal["known_heading", "numbered_heading", "text_heuristic", "front_matter", "fallback"],
            float,
        ]
    ] = []
    if not boundaries:
        definitions.append(("Full Document", "full document", 1, document_start, document_end, "fallback", 1.0))
    else:
        first = boundaries[0]
        first_position = (first.pdf_page, first.char_start)
        if first_position != document_start:
            definitions.append(("Front Matter", "front matter", 1, document_start, first_position, "front_matter", 1.0))
        for index, boundary in enumerate(boundaries):
            next_position = (
                (boundaries[index + 1].pdf_page, boundaries[index + 1].char_start)
                if index + 1 < len(boundaries)
                else document_end
            )
            definitions.append(
                (
                    boundary.title,
                    boundary.normalized_title,
                    boundary.level,
                    (boundary.pdf_page, boundary.char_start),
                    next_position,
                    boundary.detection_method,
                    boundary.confidence,
                )
            )
    sections: list[DocumentSection] = []
    for title, normalized, level, start, end, method, confidence in definitions:
        ranges = _ranges_between(
            pages,
            artifact_ref_id=artifact_ref_id,
            artifact_id=artifact_id,
            start=start,
            end=end,
        )
        if not ranges:
            continue
        sections.append(
            _make_section(
                bundle_id=bundle_id,
                artifact_ref_id=artifact_ref_id,
                artifact_id=artifact_id,
                ordinal=len(sections) + 1,
                title=title,
                normalized_title=normalized,
                level=level,
                source_ranges=ranges,
                detection_method=method,
                detection_confidence=confidence,
                pages=pages,
            )
        )
    hierarchy: list[DocumentSection] = []
    stack: list[DocumentSection] = []
    for section in sections:
        while stack and stack[-1].level >= section.level:
            stack.pop()
        parent = stack[-1].section_id if stack else None
        current = replace(section, parent_section_id=parent)
        hierarchy.append(current)
        stack.append(current)
    validate_section_coverage(tuple(pages), tuple(hierarchy))
    return tuple(hierarchy)


def validate_section_coverage(
    pages: tuple[PageText, ...], sections: tuple[DocumentSection, ...]
) -> None:
    ranges_by_page: dict[int, list[SourceRange]] = {}
    for section in sections:
        for source_range in section.source_ranges:
            ranges_by_page.setdefault(source_range.pdf_page, []).append(source_range)
    for page in pages:
        if not page.text:
            continue
        ranges = sorted(ranges_by_page.get(page.pdf_page, []), key=lambda item: item.char_start)
        if not ranges:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, f"page {page.pdf_page} is not covered by any section")
        expected_start = 0
        for source_range in ranges:
            if source_range.char_start != expected_start:
                raise ReadPaperError(
                    ErrorCode.ID_MISMATCH,
                    f"section coverage gap or overlap on page {page.pdf_page}: expected {expected_start}, found {source_range.char_start}",
                )
            if source_range.char_end <= source_range.char_start:
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "empty or reversed section source range")
            if source_range.char_end > len(page.text):
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "section source range exceeds canonical page text")
            if digest_text(page.text[source_range.char_start:source_range.char_end]) != source_range.content_sha256:
                raise ReadPaperError(ErrorCode.ID_MISMATCH, "section source range hash mismatch")
            expected_start = source_range.char_end
        if expected_start != len(page.text):
            raise ReadPaperError(
                ErrorCode.ID_MISMATCH,
                f"section coverage ends at {expected_start}, but page {page.pdf_page} contains {len(page.text)} characters",
            )


def _split_source_range(
    source_range: SourceRange,
    *,
    page: PageText,
    token_limit: int,
) -> list[SourceRange]:
    pieces: list[SourceRange] = []
    start = source_range.char_start
    while start < source_range.char_end:
        low, high = start + 1, source_range.char_end
        best = start
        while low <= high:
            middle = (low + high) // 2
            candidate = _range_for_slice(
                artifact_ref_id=source_range.artifact_ref_id,
                artifact_id=source_range.artifact_id,
                page=page,
                start=start,
                end=middle,
            )
            if estimate_tokens(materialize_source_ranges((page,), (candidate,))) <= token_limit:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= start:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "transport frame metadata exceeds token limit")
        end = best
        if end < source_range.char_end:
            minimum = start + max(1, int((end - start) * 0.70))
            boundary = max(
                page.text.rfind("\n\n", minimum, end),
                page.text.rfind("\n", minimum, end),
                page.text.rfind(" ", minimum, end),
            )
            if boundary > start:
                end = boundary + (2 if page.text.startswith("\n\n", boundary) else 1)
        pieces.append(
            _range_for_slice(
                artifact_ref_id=source_range.artifact_ref_id,
                artifact_id=source_range.artifact_id,
                page=page,
                start=start,
                end=end,
            )
        )
        start = end
    return pieces


def _frame_content(
    *,
    section: DocumentSection | dict[str, Any],
    frame_id: str,
    frame_index: int,
    frame_count: int,
    pages: Sequence[PageText | dict[str, Any]],
    source_ranges: Sequence[SourceRange | dict[str, Any]],
) -> str:
    body = materialize_source_ranges(pages, source_ranges)
    page_numbers = [int(_range_value(item, "pdf_page")) for item in source_ranges]
    if page_numbers and min(page_numbers) > 0:
        source_pages = str(min(page_numbers)) if min(page_numbers) == max(page_numbers) else f"{min(page_numbers)}-{max(page_numbers)}"
    else:
        source_pages = "text"
    source_content_sha256 = digest_text(body)
    title = escape(str(_range_value(section, "title")), quote=True)
    section_id = escape(str(_range_value(section, "section_id")), quote=True)
    safe_frame_id = escape(frame_id, quote=True)
    return (
        f'<readpaper-section section_id="{section_id}" frame_id="{safe_frame_id}" '
        f'title="{title}" frame="{frame_index}/{frame_count}" source_pages="{source_pages}" '
        f'source_content_sha256="{source_content_sha256}">\n'
        f"{body}\n"
        "</readpaper-section>"
    )


def build_transport_frames(
    section: DocumentSection,
    pages: Sequence[PageText],
    *,
    token_limit: int = TRANSPORT_FRAME_TOKEN_LIMIT,
) -> tuple[TransportFrame, ...]:
    if token_limit < 1:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "transport frame token limit must be positive")
    page_lookup = {page.pdf_page: page for page in pages}
    wrapper_probe = _frame_content(
        section=section,
        frame_id="rf_" + "0" * 64,
        frame_index=999_999,
        frame_count=999_999,
        pages=(),
        source_ranges=(),
    )
    variability_reserve = 512 if token_limit >= 2_048 else 64
    payload_limit = token_limit - estimate_tokens(wrapper_probe) - variability_reserve
    if payload_limit < 1:
        raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "transport frame limit cannot hold its metadata wrapper")
    atomic_ranges: list[SourceRange] = []
    for source_range in section.source_ranges:
        page = page_lookup.get(source_range.pdf_page)
        if page is None:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "section references a missing page")
        if estimate_tokens(materialize_source_ranges((page,), (source_range,))) <= payload_limit:
            atomic_ranges.append(source_range)
        else:
            atomic_ranges.extend(_split_source_range(source_range, page=page, token_limit=payload_limit))
    groups: list[tuple[SourceRange, ...]] = []
    current: list[SourceRange] = []
    for source_range in atomic_ranges:
        candidate = (*current, source_range)
        if current and estimate_tokens(materialize_source_ranges(pages, candidate)) > payload_limit:
            groups.append(tuple(current))
            current = [source_range]
        else:
            current.append(source_range)
    if current:
        groups.append(tuple(current))
    frames: list[TransportFrame] = []
    frame_count = len(groups)
    for index, ranges in enumerate(groups, start=1):
        frame_id = sequence_id(
            "rf",
            2,
            section.section_id,
            index,
            [[item.pdf_page, item.char_start, item.char_end] for item in ranges],
        )
        content = _frame_content(
            section=section,
            frame_id=frame_id,
            frame_index=index,
            frame_count=frame_count,
            pages=pages,
            source_ranges=ranges,
        )
        tokens = estimate_tokens(content)
        if tokens > token_limit:
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "transport frame exceeds token limit")
        start_marker, middle_marker, end_marker = _markers(content)
        frames.append(
            TransportFrame(
                frame_id=frame_id,
                section_id=section.section_id,
                frame_index=index,
                frame_count=frame_count,
                source_ranges=ranges,
                content_sha256=digest_text(content),
                estimated_tokens=tokens,
                start_marker=start_marker,
                middle_marker=middle_marker,
                end_marker=end_marker,
            )
        )
    return tuple(frames)


def materialize_frame(
    *,
    pages: Sequence[PageText | dict[str, Any]],
    frame: TransportFrame | dict[str, Any],
    section: DocumentSection | dict[str, Any],
) -> str:
    frame_section_id = _range_value(frame, "section_id")
    section_id = _range_value(section, "section_id")
    if frame_section_id != section_id:
        raise ReadPaperError(ErrorCode.ID_MISMATCH, "frame references another section")
    content = _frame_content(
        section=section,
        frame_id=str(_range_value(frame, "frame_id")),
        frame_index=int(_range_value(frame, "frame_index")),
        frame_count=int(_range_value(frame, "frame_count")),
        pages=pages,
        source_ranges=_range_value(frame, "source_ranges"),
    )
    if digest_text(content) != _range_value(frame, "content_sha256"):
        raise ReadPaperError(ErrorCode.ID_MISMATCH, "materialized frame differs from prepared inventory")
    return content


def _attach_frames(
    sections: tuple[DocumentSection, ...],
    pages: tuple[PageText, ...],
    *,
    frame_token_limit: int,
) -> tuple[tuple[DocumentSection, ...], tuple[TransportFrame, ...]]:
    updated_sections: list[DocumentSection] = []
    frames: list[TransportFrame] = []
    for section in sections:
        section_frames = build_transport_frames(section, pages, token_limit=frame_token_limit)
        section_text = materialize_source_ranges(pages, section.source_ranges, include_page_markers=False)
        frame_text = "".join(
            materialize_source_ranges(pages, frame.source_ranges, include_page_markers=False)
            for frame in section_frames
        )
        if frame_text != section_text:
            raise ReadPaperError(ErrorCode.ID_MISMATCH, "transport frames do not exactly cover their section")
        frames.extend(section_frames)
        updated_sections.append(replace(section, frame_ids=tuple(item.frame_id for item in section_frames)))
    return tuple(updated_sections), tuple(frames)


def extract_text(
    data: bytes,
    *,
    bundle_id: str,
    artifact_ref_id: str,
    artifact_id: str,
    frame_token_limit: int = TRANSPORT_FRAME_TOKEN_LIMIT,
) -> ExtractedDocument:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "text supplementary is not UTF-8") from error
    pages = (
        PageText(
            pdf_page=0,
            pdf_label=None,
            width_points=0.0,
            height_points=0.0,
            text=text,
            text_sha256=digest_text(text),
            warnings=(),
        ),
    )
    sections = build_document_sections(
        pages,
        bundle_id=bundle_id,
        artifact_ref_id=artifact_ref_id,
        artifact_id=artifact_id,
    )
    sections, frames = _attach_frames(sections, pages, frame_token_limit=frame_token_limit)
    return ExtractedDocument(pages=pages, sections=sections, frames=frames)


def extract_pdf(
    data: bytes,
    *,
    bundle_id: str,
    artifact_ref_id: str,
    artifact_id: str,
    frame_token_limit: int = TRANSPORT_FRAME_TOKEN_LIMIT,
) -> ExtractedDocument:
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
    labels = getattr(reader, "page_labels", []) or []
    for page in reader.pages:
        if not (0 < float(page.cropbox.width) <= MAX_PAGE_INCHES * 72 and 0 < float(page.cropbox.height) <= MAX_PAGE_INCHES * 72):
            raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "PDF page dimensions are unsafe")
    with tempfile.TemporaryDirectory(prefix="readpaper-text-") as directory:
        source = Path(directory) / "source.pdf"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [executable, "-layout", "-enc", "UTF-8", str(source), "-"],
                capture_output=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ReadPaperError(ErrorCode.TIMEOUT, "PDF text extraction timed out") from error
        if completed.returncode != 0:
            raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "pdftotext failed")
        try:
            extracted = completed.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as error:
            raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "pdftotext returned invalid UTF-8") from error
    page_texts = extracted.split("\f")
    if page_texts and page_texts[-1] == "":
        page_texts.pop()
    if len(page_texts) != len(reader.pages):
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "pdftotext page separators do not match the PDF page count")
    for index, (page, raw_text) in enumerate(zip(reader.pages, page_texts, strict=True), start=1):
        text = raw_text.removesuffix("\n")
        warnings: list[str] = []
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
                width_points=float(page.cropbox.width),
                height_points=float(page.cropbox.height),
                text=text,
                text_sha256=digest_text(text),
                warnings=tuple(warnings),
            )
        )
    page_tuple = tuple(pages)
    sections = build_document_sections(
        page_tuple,
        bundle_id=bundle_id,
        artifact_ref_id=artifact_ref_id,
        artifact_id=artifact_id,
    )
    sections, frames = _attach_frames(sections, page_tuple, frame_token_limit=frame_token_limit)
    return ExtractedDocument(pages=page_tuple, sections=sections, frames=frames)


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
