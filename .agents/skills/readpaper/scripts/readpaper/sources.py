"""Source identity, SSRF policy, landing discovery, and media classification."""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from PIL import Image

from .canonical import digest_text
from .errors import ErrorCode, ReadPaperError


SUPPLEMENTARY_PHRASES = ("supplementary", "supporting information", "additional file")
MAIN_LABELS = {"pdf", "download pdf", "full text pdf"}


def _normalize_percent(value: str) -> str:
    output: list[str] = []
    index = 0
    unreserved = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    while index < len(value):
        if value[index] == "%" and index + 2 < len(value):
            try:
                byte = int(value[index + 1 : index + 3], 16)
            except ValueError:
                output.append(value[index])
                index += 1
                continue
            if byte in unreserved:
                output.append(chr(byte))
            else:
                output.append(f"%{byte:02X}")
            index += 3
        else:
            output.append(value[index])
            index += 1
    return "".join(output)


def normalize_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ReadPaperError(ErrorCode.UNSUPPORTED_SOURCE, "only HTTP(S) sources are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ReadPaperError(ErrorCode.ACCESS_DENIED, "URL credentials are forbidden")
    if parsed.hostname is None:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "URL host is required")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise ReadPaperError(ErrorCode.INVALID_ARGUMENT, "invalid URL host or port") from error
    default_port = 80 if scheme == "http" else 443
    if port not in {None, default_port}:
        raise ReadPaperError(ErrorCode.ACCESS_DENIED, "nonstandard ports are forbidden")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath(_normalize_percent(raw_path))
    if raw_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    return urlunsplit(SplitResult(scheme, netloc, normalized_path, _normalize_percent(parsed.query), ""))


def local_source_token(path: Path) -> str:
    return f"ls_{digest_text(str(path.resolve()))}"


def require_public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value)
    # macOS may return RFC 6052 well-known DNS64 answers alongside ordinary
    # public IPv4 answers.  The prefix itself is classified as reserved by
    # `ipaddress`, but it is safe for this public-only policy when (and only
    # when) the embedded IPv4 destination is itself public.
    nat64_prefix = ipaddress.IPv6Network("64:ff9b::/96")
    if isinstance(address, ipaddress.IPv6Address) and address in nat64_prefix:
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded.is_global and not any(
            (
                embedded.is_private,
                embedded.is_loopback,
                embedded.is_link_local,
                embedded.is_multicast,
                embedded.is_reserved,
                embedded.is_unspecified,
            )
        ):
            return address
    if not address.is_global or any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        raise ReadPaperError(ErrorCode.ACCESS_DENIED, "source resolves to a non-public address")
    return address


def resolve_public_ips(url: str) -> tuple[str, ...]:
    parsed = urlsplit(normalize_url(url))
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ReadPaperError(ErrorCode.FETCH_FAILED, "DNS resolution failed") from error
    values = sorted({item[4][0] for item in answers})
    if not values:
        raise ReadPaperError(ErrorCode.FETCH_FAILED, "DNS returned no addresses")
    for value in values:
        require_public_ip(value)
    return tuple(values)


def normalize_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def is_supplementary_label(value: str) -> bool:
    normalized = normalize_label(value)
    return any(phrase in normalized for phrase in SUPPLEMENTARY_PHRASES)


@dataclass(frozen=True)
class LandingLink:
    url: str
    label: str
    rel_type: str | None = None


class _LandingParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.citation_pdf_urls: list[str] = []
        self.links: list[LandingLink] = []
        self._anchor: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "meta" and (values.get("name") or "").casefold() == "citation_pdf_url":
            if values.get("content"):
                self.citation_pdf_urls.append(urljoin(self.base_url, values["content"] or ""))
        if tag.casefold() == "a" and values.get("href"):
            self._anchor = values
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchor is not None:
            label = "".join(self._text)
            self.links.append(
                LandingLink(
                    url=urljoin(self.base_url, self._anchor["href"] or ""),
                    label=label,
                    rel_type=self._anchor.get("type"),
                )
            )
            self._anchor = None
            self._text = []


@dataclass(frozen=True)
class LandingDiscovery:
    main_candidates: tuple[tuple[int, str], ...]
    supplementary_urls: tuple[str, ...]


def discover_landing(html: bytes, base_url: str) -> LandingDiscovery:
    if len(html) > 5 * 1024 * 1024:
        raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "landing page exceeds 5 MiB")
    try:
        text = html.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "landing page is not UTF-8") from error
    parser = _LandingParser(normalize_url(base_url))
    parser.feed(text)
    main: list[tuple[int, str]] = []
    supplements: list[str] = []
    for url in parser.citation_pdf_urls:
        main.append((1, normalize_url(url)))
    for link in parser.links:
        normalized = normalize_url(link.url)
        if is_supplementary_label(link.label):
            supplements.append(normalized)
            continue
        if (link.rel_type or "").casefold() == "application/pdf":
            main.append((2, normalized))
        elif normalize_label(link.label) in MAIN_LABELS:
            main.append((3, normalized))
    return LandingDiscovery(
        main_candidates=tuple(dict.fromkeys(sorted(main))),
        supplementary_urls=tuple(dict.fromkeys(supplements)),
    )


class MediaKind(str):
    PDF = "pdf"
    TEXT = "text"
    IMAGE = "image"
    ZIP = "zip"
    HTML = "html"


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    redirects: tuple[str, ...]
    content_type: str | None
    data: bytes
    remote_ip: str


def _headers(raw: bytes) -> tuple[int, dict[str, str]]:
    blocks = raw.replace(b"\r\n", b"\n").split(b"\n\n")
    block = next((item for item in reversed(blocks) if item.startswith(b"HTTP/")), b"")
    lines = block.splitlines()
    if not lines:
        raise ReadPaperError(ErrorCode.FETCH_FAILED, "HTTP response headers are missing")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as error:
        raise ReadPaperError(ErrorCode.FETCH_FAILED, "invalid HTTP status line") from error
    values: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            values[key.decode("ascii", "ignore").casefold()] = value.decode("latin-1").strip()
    return status, values


def fetch_public_url(url: str, *, max_bytes: int = 128 * 1024 * 1024) -> FetchResult:
    """Fetch HTTP(S) one hop at a time with public-DNS pinning and bounded retries."""
    requested = normalize_url(url)
    current = requested
    redirects: list[str] = []
    for hop in range(6):
        parsed = urlsplit(current)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ips = resolve_public_ips(current)
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(3):
            ip = ips[attempt % len(ips)]
            with tempfile.TemporaryDirectory(prefix="readpaper-fetch-") as directory:
                header_path = Path(directory) / "headers"
                body_path = Path(directory) / "body"
                command = [
                    "curl", "--silent", "--show-error", "--fail-with-body", "--proto", "=http,https",
                    "--connect-timeout", "10", "--speed-time", "30", "--speed-limit", "1", "--max-time", "120",
                    "--max-filesize", str(max_bytes), "--resolve", f"{parsed.hostname}:{port}:{ip}",
                    "--dump-header", str(header_path), "--output", str(body_path), "--write-out", "%{remote_ip}", current,
                ]
                try:
                    result = subprocess.run(command, check=True, capture_output=True, timeout=125)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                    if isinstance(error, subprocess.CalledProcessError):
                        last_error = error
                    if attempt == 2:
                        code = ErrorCode.TIMEOUT if isinstance(error, subprocess.TimeoutExpired) else ErrorCode.FETCH_FAILED
                        raise ReadPaperError(code, "bounded HTTP fetch failed after retries") from error
                    continue
                remote_ip = result.stdout.decode("ascii", "strict").strip()
                require_public_ip(remote_ip)
                if remote_ip not in ips:
                    raise ReadPaperError(ErrorCode.ACCESS_DENIED, "curl connected to an unvalidated address")
                raw_headers = header_path.read_bytes()
                status, headers = _headers(raw_headers)
                data = body_path.read_bytes()
                if len(data) > max_bytes:
                    raise ReadPaperError(ErrorCode.OUTPUT_BUDGET_EXCEEDED, "download exceeds byte limit")
                if 300 <= status < 400:
                    location = headers.get("location")
                    if not location or hop == 5:
                        raise ReadPaperError(ErrorCode.FETCH_FAILED, "redirect limit or missing location")
                    current = normalize_url(urljoin(current, location))
                    redirects.append(current)
                    break
                if status < 200 or status >= 300:
                    raise ReadPaperError(ErrorCode.FETCH_FAILED, f"HTTP status {status}")
                return FetchResult(requested, current, tuple(redirects), headers.get("content-type"), data, remote_ip)
        else:
            if last_error is not None:
                raise ReadPaperError(ErrorCode.FETCH_FAILED, "HTTP fetch failed") from last_error
        continue
    raise ReadPaperError(ErrorCode.FETCH_FAILED, "redirect limit exceeded")


def classify_media(data: bytes, declared_content_type: str | None = None) -> str:
    declared = (declared_content_type or "").split(";", 1)[0].strip().casefold()
    if data.startswith(b"%PDF-"):
        actual = MediaKind.PDF
    elif data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        actual = MediaKind.ZIP
    else:
        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
            actual = MediaKind.IMAGE
        except Exception:
            stripped = data.lstrip().lower()
            if stripped.startswith((b"<!doctype html", b"<html")):
                actual = MediaKind.HTML
            else:
                try:
                    data.decode("utf-8")
                    actual = MediaKind.TEXT
                except UnicodeDecodeError as error:
                    raise ReadPaperError(ErrorCode.UNSUPPORTED_ARTIFACT, "unrecognized artifact bytes") from error
    declared_map = {
        "application/pdf": MediaKind.PDF,
        "application/zip": MediaKind.ZIP,
        "text/html": MediaKind.HTML,
    }
    expected = declared_map.get(declared)
    if expected is not None and expected != actual:
        raise ReadPaperError(ErrorCode.CORRUPT_ARTIFACT, "declared content type does not match magic")
    return actual
