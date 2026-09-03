"""Strict source locator union with render-independent identities."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .canonical import digest
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
