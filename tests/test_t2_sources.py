from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from readpaper.archives import inspect_zip
from readpaper.errors import ErrorCode, ReadPaperError
from readpaper.sources import (
    MediaKind,
    classify_media,
    discover_landing,
    fetch_public_url,
    is_supplementary_label,
    local_source_token,
    normalize_url,
    require_public_ip,
)


def zip_bytes(entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


def test_url_normalization_preserves_query_semantics() -> None:
    assert normalize_url("HTTPS://Exämple.com:443/a/../b/%7e?q=2&q=1#frag") == (
        "https://xn--exmple-cua.com/b/~?q=2&q=1"
    )
    with pytest.raises(ReadPaperError) as credentials:
        normalize_url("https://user:pass@example.com/x")
    assert credentials.value.code is ErrorCode.ACCESS_DENIED
    with pytest.raises(ReadPaperError):
        normalize_url("https://example.com:444/x")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_and_metadata_addresses_are_rejected(address: str) -> None:
    with pytest.raises(ReadPaperError) as error:
        require_public_ip(address)
    assert error.value.code is ErrorCode.ACCESS_DENIED


def test_public_address_and_local_source_identity(tmp_path: Path) -> None:
    assert str(require_public_ip("8.8.8.8")) == "8.8.8.8"
    assert str(require_public_ip("64:ff9b::808:808")) == "64:ff9b::808:808"
    with pytest.raises(ReadPaperError):
        require_public_ip("64:ff9b::7f00:1")
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"x")
    assert local_source_token(path).startswith("ls_")
    assert local_source_token(path) == local_source_token(tmp_path / "." / "paper.pdf")


def test_landing_discovery_uses_strict_labels_and_precedence() -> None:
    html = b"""
    <html><head><meta name="citation_pdf_url" content="/canonical.pdf"></head>
    <body>
      <a href="/typed.pdf" type="application/pdf">article</a>
      <a href="/label.pdf">Download PDF</a>
      <a href="/supp.pdf">Supporting Information</a>
      <a href="/code">source code</a>
    </body></html>
    """
    result = discover_landing(html, "https://example.org/article")
    assert result.main_candidates == (
        (1, "https://example.org/canonical.pdf"),
        (2, "https://example.org/typed.pdf"),
        (3, "https://example.org/label.pdf"),
    )
    assert result.supplementary_urls == ("https://example.org/supp.pdf",)
    assert is_supplementary_label(" Additional   File 1 ")
    assert not is_supplementary_label("source repository")


def test_magic_classification_rejects_declared_mismatch() -> None:
    assert classify_media(b"%PDF-1.7\n") == MediaKind.PDF
    assert classify_media(b"plain utf-8") == MediaKind.TEXT
    png = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(png, format="PNG")
    assert classify_media(png.getvalue()) == MediaKind.IMAGE
    with pytest.raises(ReadPaperError) as error:
        classify_media(b"<html></html>", "application/pdf")
    assert error.value.code is ErrorCode.CORRUPT_ARTIFACT


def test_safe_zip_supports_pdf_text_and_image() -> None:
    png = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(png, format="PNG")
    members = inspect_zip(
        zip_bytes(
            [
                ("supp/paper.pdf", b"%PDF-1.7\n"),
                ("supp/readme.txt", b"hello"),
                ("supp/figure.png", png.getvalue()),
            ]
        )
    )
    assert [(item.path, item.media_kind) for item in members] == [
        ("supp/paper.pdf", MediaKind.PDF),
        ("supp/readme.txt", MediaKind.TEXT),
        ("supp/figure.png", MediaKind.IMAGE),
    ]


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "dir\\evil.txt"])
def test_zip_path_escape_is_rejected(name: str) -> None:
    with pytest.raises(ReadPaperError) as error:
        inspect_zip(zip_bytes([(name, b"text")]))
    assert error.value.code is ErrorCode.CORRUPT_ARTIFACT


def test_zip_symlink_nested_and_duplicate_normalized_paths_are_rejected() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, "target")
    with pytest.raises(ReadPaperError):
        inspect_zip(output.getvalue())

    with pytest.raises(ReadPaperError) as nested:
        inspect_zip(zip_bytes([("nested.zip", zip_bytes([("a.txt", b"a")]))]))
    assert nested.value.code is ErrorCode.UNSUPPORTED_ARTIFACT

    with pytest.raises(ReadPaperError):
        inspect_zip(zip_bytes([("a/../b.txt", b"a"), ("b.txt", b"b")]))


def test_zip_ratio_limit_is_rejected() -> None:
    bomb = zip_bytes([("large.txt", b"0" * (1024 * 1024))], compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(ReadPaperError) as error:
        inspect_zip(bomb)
    assert error.value.code is ErrorCode.OUTPUT_BUDGET_EXCEEDED


def test_fetch_pins_validated_ip_and_follows_redirects_manually(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("readpaper.sources.resolve_public_ips", lambda url: ("93.184.216.34",))

    def run(command, **kwargs):
        calls.append(command)
        header = Path(command[command.index("--dump-header") + 1])
        body = Path(command[command.index("--output") + 1])
        if len(calls) == 1:
            header.write_bytes(b"HTTP/1.1 302 Found\r\nLocation: /paper.pdf\r\n\r\n")
            body.write_bytes(b"")
        else:
            header.write_bytes(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n\r\n")
            body.write_bytes(b"%PDF-fixture")
        return SimpleNamespace(stdout=b"93.184.216.34", stderr=b"", returncode=0)

    monkeypatch.setattr("readpaper.sources.subprocess.run", run)
    result = fetch_public_url("https://example.com/landing")
    assert result.final_url == "https://example.com/paper.pdf"
    assert result.data == b"%PDF-fixture"
    assert all("--location" not in command and "-L" not in command for command in calls)
    assert all("example.com:443:93.184.216.34" in command for command in calls)
