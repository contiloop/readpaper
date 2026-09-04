"""Strict source locator union with render-independent identities."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, TypeAdapter, model_validator

from .canonical import digest, digest_text
from .models import StrictModel


class LocatorBase(StrictModel):
    schema_version: int = 1
    bundle_id: str
    artifact_ref_id: str
    artifact_id: str
    locator_kind: str

    def identity(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"locator_id"})

    @property
    def locator_id(self) -> str:
        return f"loc_{digest(self.identity())}"


class PdfPageLocator(LocatorBase):
    locator_kind: Literal["pdf_page"] = "pdf_page"
    pdf_page: int = Field(ge=1)


class TextSpanLocator(LocatorBase):
    locator_kind: Literal["text_span"] = "text_span"
    pdf_page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    content_sha256: str

    @model_validator(mode="after")
    def ordered(self) -> "TextSpanLocator":
        if self.char_end <= self.char_start:
            raise ValueError("text span must be non-empty")
        return self


class PdfObjectLocator(LocatorBase):
    locator_kind: Literal["pdf_object"] = "pdf_object"
    pdf_page: int = Field(ge=1)
    object_kind: Literal["figure", "table", "equation", "region"]
    bbox_ppm: tuple[int, int, int, int]

    @model_validator(mode="after")
    def valid_bbox(self) -> "PdfObjectLocator":
        left, top, right, bottom = self.bbox_ppm
        if not (0 <= left < right <= 1_000_000 and 0 <= top < bottom <= 1_000_000):
            raise ValueError("bbox_ppm must be ordered within the page")
        return self


class ImageRegionLocator(LocatorBase):
    locator_kind: Literal["image_region"] = "image_region"
    image_sha256: str
    bbox_ppm: tuple[int, int, int, int]

    @model_validator(mode="after")
    def valid_bbox(self) -> "ImageRegionLocator":
        left, top, right, bottom = self.bbox_ppm
        if not (0 <= left < right <= 1_000_000 and 0 <= top < bottom <= 1_000_000):
            raise ValueError("bbox_ppm must be ordered within the image")
        return self


Locator = Annotated[
    Union[PdfPageLocator, TextSpanLocator, PdfObjectLocator, ImageRegionLocator],
    Field(discriminator="locator_kind"),
]

LOCATOR_ADAPTER = TypeAdapter(Locator)


def validate_locator_confirmation(payload: dict[str, Any], inventory: dict[str, Any]) -> LocatorBase:
    """Resolve the canonical locator against this immutable source inventory."""
    locator = LOCATOR_ADAPTER.validate_python(payload.get("locator"))
    required = inventory.get("required_artifact_ref_ids")
    if required is not None and locator.artifact_ref_id not in required:
        raise ValueError("locator is outside the locked reading scope")
    if locator.locator_id != payload.get("locator_id") or locator.bundle_id != inventory["bundle_id"]:
        raise ValueError("locator identity or bundle does not match")
    if isinstance(locator, ImageRegionLocator):
        if not any(item["artifact_ref_id"] == locator.artifact_ref_id and item["artifact_id"] == locator.artifact_id
                   and item.get("media_kind") == "image" for item in inventory["visual_units"]):
            raise ValueError("image is not in the source inventory")
        if locator.artifact_id != f"a_{locator.image_sha256}":
            raise ValueError("image hash does not match the source artifact")
        return locator
    page = next((item for item in inventory["pages"] if (
        item["artifact_ref_id"] == locator.artifact_ref_id and item["artifact_id"] == locator.artifact_id
        and item["pdf_page"] == locator.pdf_page
    )), None)
    if page is None:
        raise ValueError("page is not in the source inventory")
    if isinstance(locator, TextSpanLocator):
        text = page["text"]
        if locator.char_end > len(text) or digest_text(text[locator.char_start:locator.char_end]) != locator.content_sha256:
            raise ValueError("text span bounds or hash do not match canonical page text")
    elif not any(item["artifact_ref_id"] == locator.artifact_ref_id and item["artifact_id"] == locator.artifact_id
                 and item.get("pdf_page") == locator.pdf_page for item in inventory["visual_units"]):
        raise ValueError("PDF locator does not point to a PDF page")
    return locator


def reopened_sources_cover(locator: LocatorBase, events: list[dict[str, Any]], inventory: dict[str, Any]) -> bool:
    """Require a full page/image open or gapless text coverage, possibly across frames."""
    if not isinstance(locator, TextSpanLocator):
        for event in events:
            if event.get("event_kind") != "visual_open_observed":
                continue
            if any(item["unit_id"] == event.get("subject_id") and item["artifact_ref_id"] == locator.artifact_ref_id
                   and item["artifact_id"] == locator.artifact_id
                   and item.get("pdf_page") == getattr(locator, "pdf_page", None)
                   for item in inventory["visual_units"]):
                return True
    if not isinstance(locator, (TextSpanLocator, PdfPageLocator)):
        return False
    ranges = []
    frame_map = {item["frame_id"]: item for item in inventory["frames"]}
    for event in events:
        frame = frame_map.get(event.get("subject_id"))
        if event.get("event_kind") != "source_frame_emitted" or frame is None:
            continue
        if event.get("payload", {}).get("content_sha256") != frame["content_sha256"]:
            continue
        ranges.extend((item["char_start"], item["char_end"]) for item in frame["source_ranges"] if (
            item["artifact_ref_id"] == locator.artifact_ref_id and item["artifact_id"] == locator.artifact_id
            and item["pdf_page"] == locator.pdf_page
        ))
    if isinstance(locator, TextSpanLocator):
        start, end = locator.char_start, locator.char_end
    else:
        page = next(item for item in inventory["pages"] if item["artifact_ref_id"] == locator.artifact_ref_id
                    and item["artifact_id"] == locator.artifact_id and item["pdf_page"] == locator.pdf_page)
        start, end = 0, len(page["text"])
    if end <= start:
        return False  # Empty/scanned pages require a visual open.
    cursor = start
    for left, right in sorted(ranges):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def bbox_to_ppm(*, left: float, top: float, right: float, bottom: float, width: float, height: float) -> tuple[int, int, int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("canvas dimensions must be positive")
    return tuple(
        round(value)
        for value in (
            left / width * 1_000_000,
            top / height * 1_000_000,
            right / width * 1_000_000,
            bottom / height * 1_000_000,
        )
    )  # type: ignore[return-value]
