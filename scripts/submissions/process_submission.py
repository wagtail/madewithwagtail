#!/usr/bin/env python3
"""Triage showcase site submissions: validate, render, publish.

Subcommands:
    validate --issue-body <file> [--content-dir <dir>]     -> proposal JSON on stdout
    render   --proposal <file> --out-dir <dir> [--url URL] -> detection.json, screenshot.webp, logo.webp
    publish prepare --proposal <file> --detection <file>
              --screenshot <file> [--logo <file>] --repo-root <dir> [--dry-run]
    publish pr      --proposal <file> --detection <file> --repo-root <dir>
                    [--co-author <login> --co-author-id <id>] [--dry-run]

Exit codes: 0 success, 2 rejection (reasons JSON printed to stdout by
`validate`), 1 unexpected error.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.28",
#   "playwright==1.62.0",
#   "pillow>=11",
#   "pydantic>=2.10",
#   "python-slugify>=8",
#   "pyyaml>=6",
#   "wappalyzer>=2,<3",
# ]
# ///

from __future__ import annotations

import argparse
import difflib
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slugify import slugify

SLUG_RE = r"^[a-z0-9][a-z0-9-]{0,49}$"

# Controlled vocabularies for the site classification facets. Must match the
# option lists in .github/ISSUE_TEMPLATE/site-submission.yml and the values
# migrated into src/content/developers/**/index.md.
SECTOR_VALUES = frozenset(
    {
        "agriculture",
        "arts",
        "climate",
        "consumer",
        "culture",
        "education",
        "energy",
        "engineering",
        "entertainment",
        "environment",
        "finance",
        "food",
        "forestry",
        "games",
        "government",
        "healthcare",
        "hospitality",
        "industry",
        "non-profit",
        "professional services",
        "research",
        "retail",
        "science",
        "sport",
        "sustainability",
        "technology",
        "telecom",
        "travel",
    }
)
SITE_TYPE_VALUES = frozenset(
    {
        "blog",
        "documentation",
        "e-commerce",
        "events",
        "news",
        "portfolio",
        "product",
        "reports",
    }
)
CAPABILITY_VALUES = frozenset(
    {
        "3D",
        "booking",
        "chatbot",
        "headless",
        "maps",
        "multilingual",
        "multisite",
    }
)

RESERVED_SLUGS = frozenset(
    {
        "index",
        "public",
        "src",
        "dist",
        "images",
        "page",
        "developers",
        "sites",
        "api",
        "admin",
        "assets",
        "static",
    }
)


class Proposal(BaseModel):
    """The single contract between the validate, render, and publish jobs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1]
    issue_number: int = Field(ge=1)
    site_url: str = Field(min_length=1, max_length=2000)
    site_title: str = Field(min_length=1, max_length=80)
    site_description: str = Field(min_length=1, max_length=800)
    sector: list[str] = Field(default_factory=list)
    site_type: list[str] = Field(default_factory=list)
    capability: list[str] = Field(default_factory=list)
    developer_name: str = Field(min_length=1, max_length=80)
    developer_slug: str = Field(pattern=SLUG_RE)
    site_slug: str = Field(pattern=SLUG_RE)
    developer_exists: bool
    # Slugs of existing profiles with a similar name, when the submission
    # creates a new profile: a reviewer-facing hint against duplicates.
    similar_developers: list[str] = Field(default_factory=list)
    developer_url: str | None = Field(default=None, max_length=2000)
    developer_location: str | None = Field(default=None, max_length=100)
    lat: str | None = Field(default=None)
    lon: str | None = Field(default=None)
    github_user: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"
    )
    logo_url: str | None = Field(default=None, max_length=2000)
    other_notes: str | None = Field(default=None, max_length=2000)
    submitted_at: datetime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FormParseError(ValueError):
    """The issue body is not one of our form submissions."""


CHECKBOX_RE = re.compile(r"^- \[([xX]| )\] (.*)$", re.MULTILINE)

# Only the form's own labels are section boundaries: user-typed Markdown
# containing `### Something` must stay inside the previous field's content.
SECTION_RE = re.compile(
    r"^### (?P<heading>Site URL|Site title|Short description|Sector|"
    r"Site type|Capabilities|"
    r"Developer name|Developer URL|Developer location|Latitude|Longitude|GitHub username|"
    r"Logo URL|Other notes|Confirmations)[ \t]*$",
    re.MULTILINE,
)

# The form renders sections in this exact order; it is the yardstick for
# telling real sections from headings forged inside free-text fields.
FORM_HEADINGS = (
    "Site URL",
    "Site title",
    "Short description",
    "Sector",
    "Site type",
    "Capabilities",
    "Developer name",
    "Developer URL",
    "Developer location",
    "Latitude",
    "Longitude",
    "GitHub username",
    "Logo URL",
    "Other notes",
    "Confirmations",
)
FORM_ORDER = {heading: index for index, heading in enumerate(FORM_HEADINGS)}

# GitHub substitutes this literal for optional fields the submitter left
# blank; it must be treated as unset, not as submitted data.
NO_RESPONSE_PLACEHOLDER = "_No response_"


def parse_issue_form_body(
    body: str,
) -> dict[str, str | list[str] | list[tuple[str, bool]]]:
    """Parse a GitHub issue form body into {heading: content}.

    GitHub renders form issues as `### <label>` sections. Multiselect
    values arrive comma-separated; confirmations as a checkbox list.
    Blank optional fields arrive as the `_No response_` placeholder and
    are reported as unset (empty list).
    """
    matches = list(SECTION_RE.finditer(body))
    if not any(match.group("heading") == "Site URL" for match in matches):
        raise FormParseError("Issue body does not look like a site submission form.")

    def parse_section(heading: str, content: str):
        if content == NO_RESPONSE_PLACEHOLDER:
            return []
        if heading == "Confirmations":
            return [
                (label.strip(), mark.casefold() == "x")
                for mark, label in CHECKBOX_RE.findall(content)
            ]
        if heading in ("Sector", "Site type", "Capabilities"):
            return [value.strip() for value in content.split(",") if value.strip()]
        return content

    result: dict[str, str | list[str] | list[tuple[str, bool]]] = {}
    # Real sections appear in the form's canonical order; a heading that is
    # out of order or repeats an already-seen section was forged inside a
    # free-text field, so its text stays with the field it was typed in.
    prev_index = -1
    for index, match in enumerate(matches):
        heading = match.group("heading")
        if heading == "Confirmations":
            continue  # handled after the loop: last occurrence always wins
        section_index = FORM_ORDER[heading]
        if section_index <= prev_index or heading in result:
            continue
        prev_index = section_index
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result[heading] = parse_section(heading, body[match.end() : end].strip())

    # Confirmations is the form's final section, so the last match is the
    # real one even when earlier free text contains a forged heading.
    confirmations = [
        match for match in matches if match.group("heading") == "Confirmations"
    ]
    if confirmations:
        match = confirmations[-1]
        result["Confirmations"] = parse_section(
            "Confirmations", body[match.end() :].strip()
        )
    return result


# Submissions must not point at our own hosting (self-DoS guard, spec §Security 7).
HOST_BLOCKLIST_SUFFIXES = (
    "github.com",
    "github.io",
    "githubusercontent.com",
)


def _is_browsable_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    return ip.is_global


def check_public_url(raw: str, resolver=socket.getaddrinfo) -> str:
    """Validate a user-supplied URL for browsing; return the normalized URL.

    Enforces the spec's SSRF rules: http(s) only, default port only, no
    userinfo, public DNS resolution, not on the host blocklist.
    Raises pydantic ValidationError with a descriptive message otherwise.
    """
    from pydantic import ValidationError as _VE

    def bad(reason: str) -> Exception:
        # pydantic 2 requires a title; single-dict construction (as the plan
        # sketched) raises TypeError. Build via from_exception_data instead.
        return _VE.from_exception_data(
            "URL validation",
            [
                {
                    "type": "value_error",
                    "loc": ("url",),
                    "input": raw,
                    "ctx": {"error": ValueError(reason)},
                }
            ],
        )

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise bad(f"URL does not parse: {exc}") from exc

    if parts.scheme not in ("http", "https"):
        raise bad(f"Scheme must be http or https, got {parts.scheme!r}")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise bad("URL must not contain credentials (userinfo)")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise bad("URL has an invalid port") from exc
    if explicit_port is not None:
        raise bad("URL must use the default port")

    hostname = parts.hostname or ""
    if not hostname:
        raise bad("URL has no hostname")

    host_lower = hostname.rstrip(".").lower()
    if any(
        host_lower == s or host_lower.endswith("." + s) for s in HOST_BLOCKLIST_SUFFIXES
    ):
        raise bad(f"Host {host_lower} is on the submission blocklist")

    # IP literals: validate directly. Hostnames: resolve.
    try:
        ips = [ipaddress.ip_address(host_lower)]
    except ValueError:
        try:
            infos = resolver(
                host_lower, parts.port or (443 if parts.scheme == "https" else 80)
            )
        except Exception as exc:
            raise bad(f"Hostname {host_lower} does not resolve") from exc
        try:
            ips = [ipaddress.ip_address(info[4][0]) for info in infos]
        except ValueError as exc:
            raise bad(f"Resolver returned a malformed address: {exc}") from exc

    # An empty resolution list must not pass vacuously through all().
    if not ips or not all(_is_browsable_ip(ip) for ip in ips):
        raise bad(f"Host {host_lower} does not resolve to a public-only address")

    # str(SplitResult) returns the repr on Python 3.14+; geturl() returns the
    # URL string on every supported version.
    return parts.geturl()


def read_frontmatter(path: Path) -> dict:
    """Minimal frontmatter reader: YAML between the first two --- lines."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    _, fm, _ = text.split("---", 2)
    import yaml

    data = yaml.safe_load(fm)
    return data if isinstance(data, dict) else {}


def make_slug(name: str) -> str:
    slug = slugify(name, max_length=50)
    if not re.fullmatch(SLUG_RE, slug):
        raise ValueError(f"Cannot derive a valid slug from {name!r} (got {slug!r})")
    if slug in RESERVED_SLUGS:
        raise ValueError(f"Slug {slug!r} is reserved")
    return slug


def existing_developers(content_dir: Path) -> dict[str, str]:
    devs = {}
    for index in sorted(content_dir.glob("*/index.md")):
        data = read_frontmatter(index)
        title = data.get("title")
        if isinstance(title, str):
            devs[index.parent.name] = title
    return devs


def match_developer(name: str, devs: dict[str, str]) -> tuple[str, bool] | list[str]:
    by_title = {title.casefold(): slug for slug, title in devs.items()}
    hit = by_title.get(name.casefold())
    if hit:
        return hit, True
    close = difflib.get_close_matches(name.casefold(), list(by_title), n=3, cutoff=0.6)
    return sorted(by_title[c] for c in close)


def existing_site_origins(content_dir: Path) -> set[str]:
    origins = set()
    for index in sorted(content_dir.glob("*/*/index.md")):
        url = read_frontmatter(index).get("site_url")
        if isinstance(url, str) and url:
            try:
                parts = urlsplit(url)
                if parts.scheme and parts.hostname:
                    origins.add(f"{parts.scheme}://{parts.hostname.lower()}")
            except ValueError:
                continue
    return origins


def check_slug_free(kind: str, slug: str, content_dir: Path) -> None:
    if slug in RESERVED_SLUGS:
        raise ValueError(f"Slug {slug!r} is reserved")
    if kind == "developer" and (content_dir / slug).exists():
        raise ValueError(f"Developer directory {slug!r} already exists")
    if kind == "site" and any(p.is_dir() for p in content_dir.glob(f"*/{slug}")):
        raise ValueError(f"Site directory {slug!r} already exists for a developer")


SCREENSHOT_MAX_BYTES = 250_000
LOGO_MAX_BYTES = 50_000


def cover_crop(img: Image.Image, target: tuple[int, int]) -> Image.Image:
    """Resize to cover `target`, then center-crop to exactly `target`."""
    tw, th = target
    scale = max(tw / img.width, th / img.height)
    resized = img.resize(
        (round(img.width * scale), round(img.height * scale)), Image.LANCZOS
    )
    left = (resized.width - tw) // 2
    top = (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def encode_screenshot(
    data: bytes,
    *,
    target: tuple[int, int] = (1200, 996),
    quality: int = 70,
    max_bytes: int = SCREENSHOT_MAX_BYTES,
) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img = cover_crop(img, target)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality, method=6)
    out = buf.getvalue()
    if len(out) > max_bytes:
        raise ValueError(
            f"Screenshot encodes to {len(out)} bytes, too large (over the {max_bytes} byte cap)"
        )
    return out


def encode_logo(
    data: bytes, *, max_size: int = 120, max_bytes: int = LOGO_MAX_BYTES
) -> bytes:
    img = Image.open(io.BytesIO(data))
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=80, method=6)
    out = buf.getvalue()
    if len(out) > max_bytes:
        raise ValueError(
            f"Logo encodes to {len(out)} bytes, too large (over the {max_bytes} byte cap)"
        )
    return out


def assert_webp(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    if img.format != "WEBP":
        raise ValueError(f"Expected WEBP image, got {img.format}")
    return img


RESPONSIVE_EMBED_RE = re.compile(
    r'<div[^>]*\bclass=["\'][^"\']*\bresponsive-object\b', re.IGNORECASE
)
STREAMFIELD_BLOCK_RE = re.compile(
    r'<div[^>]*\bclass=["\'][^"\']*\bw-block-', re.IGNORECASE
)
RICH_TEXT_RE = re.compile(r'\bdata-block-key=["\'][a-z0-9]{5}["\']')

# Rendition URL tiers, most strict first; detect_wagtail reports only the
# most confident matching tier. Adapted from JS regexes proven against
# real-world Wagtail sites. Character classes use [\w.-] (dash not last):
# Python re rejects a trailing dash inside a range.
WAGTAIL_RENDITION_TIERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "strictest",
            r"\/media\/(?:original_images\/[\w-]+\.|images\/[\w.-]+\.((?:fill|max|min)-\d+x\d+(?:-c\d+)?|(?:width|height|scale)-\d+|original)\.)",
        ),
        (
            "strict",
            r"(?:\.[a-z]+|\/media)(?:\/[\w-]+)?\/(?:original_images\/[\w-]+\.|images\/[\w.-]+\.((?:fill|max|min|width|height|scale)-\d|original))",
        ),
        (
            "less_strict_but_long",
            r"(?:\.[a-z]+|\/media)(?:\/[\w-]+)?\/(?:images\/[\w.-]+\.original|original_images\/[\w-]+\.)|\/images\/[\w.-]+\.(?:fill|max|min|width|height|scale)-\d",
        ),
        (
            "lax",
            r"\/(?:original_images\/[\w-]+\.|images\/[\w.-]+\.((?:fill|max|min|width|height|scale)-\d|original))",
        ),
        (
            "laxest",
            r"\/original_images\/|\/[\w.-]+\.((?:fill|max|min|width|height|scale)-\d|original)",
        ),
    )
)


ADMIN_PATHS = ("/admin/", "/cms/", "/cms-admin/")


def rendition_tier(html: str) -> str | None:
    """Most strict rendition tier matching anywhere in the page, if any."""
    for name, pattern in WAGTAIL_RENDITION_TIERS:
        if pattern.search(html):
            return name
    return None


def detect_wagtail(html: str) -> list[str]:
    """Best-effort Wagtail fingerprints from page HTML (spec: evidence, never a gate)."""
    signals = []
    tier = rendition_tier(html)
    if tier is not None:
        signals.append(f"Wagtail rendition URL in image sources ({tier} tier)")
    if RICH_TEXT_RE.search(html):
        signals.append("Rich text data-block-key attribute")
    if RESPONSIVE_EMBED_RE.search(html):
        signals.append("Responsive embed container (responsive-object)")
    if STREAMFIELD_BLOCK_RE.search(html):
        signals.append("StreamField block classes (w-block-*)")
    return signals


# Wappalyzer-next (https://github.com/s0md3v/wappalyzer-next) fingerprints
# the page in headless Chromium, complementing the Wagtail HTML heuristics
# above (which stay authoritative for Wagtail itself).

# Sites built on these are not Wagtail sites; the Wappalyzer report gates
# the submission (spec: the showcase only lists Wagtail sites). PHP-hosted
# frameworks mostly imply PHP, so PHP itself covers most of them.
INCOMPATIBLE_TECHNOLOGIES = frozenset(
    {
        "PHP",
        "Microsoft ASP.NET",
        "Java",
        "Wix",
        "Webflow",
        "Squarespace",
    }
)

# Front-end stacks worth surfacing on the site page: Wagtail sites routinely
# pair with one of these, and the showcase's tags/series benefit from knowing.
COMPLEMENTARY_TECHNOLOGIES = frozenset(
    {
        "React",
        "Vue.js",
        "Next.js",
        "Nuxt.js",
        "Astro",
        "Svelte",
        "Alpine.js",
        "Htmx",
        "Bootstrap",
        "Tailwind CSS",
    }
)

# Technologies never reported in the "Detected technologies" section:
# Wagtail detection stays with our own HTML heuristics above, and
# Django/Python are implied for every Wagtail site.
UNREPORTED_TECHNOLOGIES = frozenset({"wagtail", "django", "python"})

# Wappalyzer categories whose detections are relevant at all; anything else
# (analytics, CDNs, widgets, ...) is noise for a technology report. The
# library reports category *names* in each technology's "categories" list.
WAPPALYZER_CATEGORIES = frozenset(
    {
        "CMS",
        "Blogs",
        "JavaScript frameworks",
        "Web frameworks",
        "Programming languages",
        "Page builders",
        "Static site generator",
        "UI frameworks",
        "JavaScript libraries",
    }
)

WAPPALYZER_SCAN_TIMEOUT_S = 45


def wappalyzer_technologies(url: str) -> dict[str, dict]:
    """Detect technologies with the Wappalyzer extension in Chromium.

    Returns {name: {"version": str, "categories": [str]}} — filtered to the
    categories we report on — or {} when the scan fails for any reason: a
    failed technology scan must never fail the render stage (the Wagtail
    detection above remains the gate for showcase-worthiness).
    """
    try:
        from wappalyzer import Wappalyzer as _Wappalyzer

        with _Wappalyzer(timeout=WAPPALYZER_SCAN_TIMEOUT_S) as scanner:
            results = scanner.analyze(url)
        # analyze() keys the result by the scanner's own final URL, which
        # may differ from the input (redirect, trailing slash) — take the
        # single value regardless of key.
        return next(iter(results.values()), {})
    except Exception as exc:
        print(f"wappalyzer scan failed for {url}: {exc}", file=sys.stderr)
        return {}


def classify_technologies(technologies: dict[str, dict]) -> dict[str, list[str]]:
    """Split detected technologies into incompatible / complementary / other.

    Only technologies in a reportable Wappalyzer category are considered;
    classification is by exact fingerprint name (Wappalyzer DB names, e.g.
    "Vue.js" not "Vue").
    """
    reportable = {
        name: data
        for name, data in technologies.items()
        if any(cat in WAPPALYZER_CATEGORIES for cat in data.get("categories", []))
    }
    return {
        "incompatible": sorted(INCOMPATIBLE_TECHNOLOGIES & reportable.keys()),
        "complementary": sorted(COMPLEMENTARY_TECHNOLOGIES & reportable.keys()),
        "other": sorted(
            name
            for name in reportable
            if name not in INCOMPATIBLE_TECHNOLOGIES
            and name not in COMPLEMENTARY_TECHNOLOGIES
            and name.casefold() not in UNREPORTED_TECHNOLOGIES
        ),
    }


def _response_too_large(response) -> bool:
    """Best-effort size gate: honor content-length when present."""
    headers = getattr(response, "headers", None) or {}
    content_length = headers.get("content-length")
    return bool(
        content_length
        and content_length.isdigit()
        and int(content_length) > MAX_RESPONSE_BYTES
    )


def _capped_text(response) -> str:
    """Decode at most MAX_RESPONSE_BYTES of the response body."""
    if _response_too_large(response):
        return ""
    content = getattr(response, "content", None)
    if content is None:
        return response.text
    return content[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")


def probe_admin_pages(client: "httpx.Client", origin: str) -> list[str]:
    """Best-effort admin probe; every failure mode is silently skipped.

    No redirect following: fetch_page's per-hop SSRF checks are the only
    validated network path, so a redirecting admin simply yields no signal.
    """
    import httpx

    signals = []
    for path in ADMIN_PATHS:
        try:
            response = client.get(origin + path, timeout=5)
        except httpx.HTTPError:
            continue
        body = _capped_text(response)
        if response.status_code == 200 and "wagtail" in body.casefold():
            signals.append(f"Wagtail admin page at {path}")
    return signals


def detection_result(
    signals: list[str], url: str, technologies: dict[str, list[str]] | None = None
) -> dict:
    return {
        "url": url,
        "is_wagtail": bool(signals),
        "signals": signals,
        "technologies": technologies or {},
        "checked_at": utcnow().isoformat(),
    }


MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 3_000_000
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def fetch_page(client: "httpx.Client", url: str) -> tuple[str, str]:
    """GET a page following redirects manually, SSRF-checking every hop.

    Status-code redirect detection and getattr fallbacks keep this testable
    against minimal fake responses (no httpx.Response required).
    """
    import httpx

    current = check_public_url(url)
    for _ in range(MAX_REDIRECTS):
        response = client.get(
            current,
            timeout=15,
            headers={"user-agent": "madewithwagtail-submission-bot"},
        )
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location", "")
            if not location:
                raise ValueError("Redirect without a location header")
            current = check_public_url(str(httpx.URL(str(response.url)).join(location)))
            continue
        # Cap the body at MAX_RESPONSE_BYTES before decoding.
        content = getattr(response, "content", None)
        if content is None:
            content = response.text.encode("utf-8", errors="replace")
        encoding = getattr(response, "encoding", None) or "utf-8"
        text = content[:MAX_RESPONSE_BYTES].decode(encoding, errors="replace")
        return str(response.url), text
    raise ValueError(f"More than {MAX_REDIRECTS} redirects")


ICON_REL_RE = re.compile(
    r"<link[^>]+rel=[\"'][^\"']*(?:apple-touch-icon|icon)[^\"']*[\"'][^>]*>",
    re.IGNORECASE,
)
ICON_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
MANIFEST_HREF_RE = re.compile(
    r"<link[^>]+rel=[\"'][^\"']*manifest[^\"']*[\"'][^>]*>", re.IGNORECASE
)


def gather_logo_candidates(
    client: "httpx.Client",
    html: str,
    developer_url: str | None,
    logo_url: str | None,
    resolver=socket.getaddrinfo,
) -> list[str]:
    """Candidate developer-logo URLs, most authoritative first: the explicit
    Logo URL submission, then icons declared by the Developer URL's site
    (<link> icons, web app manifest entries, conventional paths).

    The submitted site is deliberately NOT a logo source: the developer's
    brand lives on their own site, and the submitter controls both the
    Logo URL and Developer URL fields. All normalized URLs run through the
    same SSRF checks as page fetches — candidates from attacker-controlled
    HTML must never bypass check_public_url."""
    import httpx

    candidates: list[str] = []

    def add(raw: str, origin: str) -> None:
        try:
            absolute = str(httpx.URL(origin).join(raw))
        except ValueError:
            return
        try:
            validated = check_public_url(absolute, resolver=resolver)
        except Exception:
            return  # best-effort: invalid/private candidates are simply skipped
        if validated not in candidates:
            candidates.append(validated)

    if logo_url:
        add(logo_url, logo_url)
    if not developer_url:
        return candidates

    developer_parts = urlsplit(developer_url)
    developer_origin = f"{developer_parts.scheme}://{developer_parts.netloc}"
    for tag in ICON_REL_RE.findall(html):
        match = ICON_HREF_RE.search(tag)
        if match:
            add(match.group(1), developer_origin)
    # Web app manifest: many sites declare only small favicon links but
    # list large icons (commonly 512x512) in their manifest. Best-effort:
    # an unreadable or non-JSON manifest is skipped.
    for tag in MANIFEST_HREF_RE.findall(html):
        match = ICON_HREF_RE.search(tag)
        if not match:
            continue
        try:
            manifest_url = check_public_url(
                str(httpx.URL(developer_origin).join(match.group(1))), resolver=resolver
            )
            manifest = json.loads(client.get(manifest_url, timeout=5).text)
            icons = manifest.get("icons")
            if not isinstance(icons, list):
                continue
        except Exception:
            continue  # best-effort: unreachable or malformed manifest

        def manifest_entry_size(entry: object) -> int:
            """Largest dimension of a manifest icon's declared sizes.

            W3C format is "sizes": "512x512"; also accepts an object with
            width/height fields. Multiple sizes rank by the largest.
            """
            if not isinstance(entry, dict):
                return 0
            sizes = entry.get("sizes")
            declared: list[int] = []
            if isinstance(sizes, str):
                for token in sizes.split():
                    dims = token.lower().split("x")
                    if len(dims) == 2 and dims[0].isdigit() and dims[1].isdigit():
                        declared.extend((int(dims[0]), int(dims[1])))
            elif isinstance(sizes, (list, dict)):
                values = sizes if isinstance(sizes, list) else sizes.values()
                for value in values:
                    if isinstance(value, (int, float)):
                        declared.append(int(value))
            return max(declared, default=0)

        sized = sorted(
            enumerate(icons),
            key=lambda pair: manifest_entry_size(pair[1]),
            reverse=True,
        )
        for _, entry in sized:
            if isinstance(entry, dict) and isinstance(entry.get("src"), str):
                add(entry["src"], developer_origin)
    add("/apple-touch-icon.png", developer_origin)
    add("/favicon.ico", developer_origin)
    return candidates


def select_largest_logo(client: "httpx.Client", candidates: list[str]) -> bytes | None:
    """Download logo candidates and return the largest decodable image.

    Pages declare icons in arbitrary order (a 16px <link rel="icon">
    commonly precedes the 180px apple-touch-icon), and sizes attributes are
    unreliable across implementations, so every candidate is measured after
    download. Ties keep the earlier candidate — the docstring ordering of
    gather_logo_candidates is most-authoritative-first. Any candidate that
    errors, is oversized, or fails to decode is skipped.
    """
    import httpx

    measured: list[tuple[int, int, bytes]] = []
    for index, candidate in enumerate(candidates):
        try:
            response = client.get(candidate, timeout=10)
            if response.status_code != 200 or _response_too_large(response):
                continue
            # No magic-byte sniffing (it misses ICO favicons): try to decode
            # + encode with Pillow; any failure means "not a usable logo".
            # UnidentifiedImageError is an OSError.
            data = response.content[:MAX_RESPONSE_BYTES]
            width, height = Image.open(io.BytesIO(data)).size
            measured.append((width * height, -index, encode_logo(data)))
        except (httpx.HTTPError, ValueError, OSError):
            continue
    if not measured:
        return None
    return max(measured)[2]


def is_private_browser_host(url: str) -> bool:
    """True if the URL points at a private/loopback host literal.

    Used by the Playwright route guard. Hostnames cannot be resolved
    here, so only IP literals and 'localhost' are classified;
    hostname-based SSRF is backstopped by the credential-free container.
    """
    host = (urlsplit(url).hostname or "").rstrip(".")
    if not host:
        return False
    try:
        return not _is_browsable_ip(ipaddress.ip_address(host))
    except ValueError:
        return host.casefold() == "localhost"


# ---------------------------------------------------------------------------
# JS-based overlay dismissal (dialog / cookie-banner / popup agnostic).
#
# Runs inside the page after load, in tiers — each tier only if the previous
# left an overlay in place — so we disturb pages as little as possible:
#   1. Escape key (the standard, human way to dismiss dialogs)
#   2. Close affordances inside dialog/[role=dialog] containers
#   3. CMP accept/reject buttons (selectors from mozilla/cookie-banner-rules-list)
#   4. Text-matched accept/dismiss/close buttons in visible banners
#   5. Native <dialog>.close() as a last resort
#
# The style-based hiding (BANNER_HIDE_CSS) only runs afterwards, as a
# fallback for banners our interaction heuristics cannot dismiss — measured
# by OVERLAY_PROBE_JS, which counts remaining overlays by category.

BANNER_PROBE_SELECTOR = (
    "#onetrust-consent-sdk, #onetrust-banner-sdk, #optanon-popup-bg, "
    "#CybotCookiebotDialog, #usercentrics-root, #fc-consent-root, "
    "#cookiebanner, #cookie-banner, .cookie-banner, #cookie_consent, .cookie_consent, "
    "#qc-cmp2-container, #qc-cmp2-ui, #truste-consent-track, #truste-consent-content, "
    "#Osano-CookieDialog, .osano-cm-window, #termly-code-snippet-support, "
    ".termly-consent-banner, #iubenda-cs-banner, .iubenda-cs-banner, "
    "#cmplz-cookiebanner-container, .cmplz-cookiebanner, #cookie-script, "
    "#cookiescript_injected, #klaro, .klaro, #tarteaucitronRoot, "
    "#tarteaucitronAlertBig, #BorlabsCookieBox, #cky-consent-bar, .cky-consent-bar, "
    "#cky-overlay, .cky-overlay, #cc-banner, .cc-window, .cc-banner, "
    "#cookiebanner, #cookie-banner, #cookie_consent, .cookie_consent, "
    "#cookie-law-info-bar, #cliSettingsPopup, .cli-modal-backdrop, "
    "#moove_gdpr_cookie_info_bar, #moove_gdpr_cookie_modal, "
    "#gdpr-cookie-message, .gdpr-cookie-notice, #gdpr-banner, "
    ".eu-cookie-compliance-banner, .js-cookie-banner, .cookie-notice, "
    ".cookie-consent-banner, .cookie-popup, #cookie-box, #cookie-bar, "
    "#cookie-hint, #cookiehint, #cookies-banner, .cookies-banner, "
    "#cookie-message, .cookies-eu-banner, #cookie-law-banner, #cookiesck, "
    ".sqs-cookie-banner-v2, .wpgdprc-consent-bar, .avia-cookie-consent-wrap, "
    ".fusion-privacy-bar, .woodmart-cookies-popup, .thb-cookie-bar, "
    ".pum-overlay, .elementor-popup-modal"
)
OVERLAY_PROBE_JS = """\
(() => {
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  // Filled, non-transparent box painted by the page: a scrim/spinner/backdrop.
  const painted = (el) => {
    if (!(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (style.backgroundColor === "rgba(0, 0, 0, 0)" || style.backgroundImage === "none") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const inViewport = (el) => {
    const rect = el.getBoundingClientRect();
    return (
      rect.bottom > 0 && rect.right > 0 &&
      rect.top < window.innerHeight && rect.left < window.innerWidth
    );
  };
  const isOpenDialog = (el) => {
    if (el instanceof HTMLDialogElement) return el.open;
    if (el.getAttribute("role") === "dialog" || el.getAttribute("aria-modal") === "true") {
      return visible(el);
    }
    return false;
  };
  const Dialog = "[dialog],[role='dialog'],[aria-modal='true'],div[class*='modal' i],div[class*='overlay' i],div[class*='popup' i]";
  const spinner = "[class*='spinner' i],[class*='loading' i],[class*='preloader' i],[aria-busy='true'],img[alt*='loading' i],img[src*='loading' i],img[src*='spinner' i]";
  window.__mww_overlay_probe = () => ({
    dialog: [...document.querySelectorAll(Dialog)].filter((el) => isOpenDialog(el) && inViewport(el)).length,
    banner: [...document.querySelectorAll("%BANNER_PROBE_SELECTOR%")].filter((el) => visible(el) && inViewport(el)).length,
    spinner: [...document.querySelectorAll(spinner)].filter((el) => visible(el)).length,
    scrim: [...document.querySelectorAll("body *")].filter((el) => {
      const rect = el.getBoundingClientRect();
      const covers = rect.width >= window.innerWidth * 0.95 && rect.height >= window.innerHeight * 0.95;
      return covers && painted(el) && !el.closest(Dialog) && !el.matches(spinner);
    }).length,
  });
  return window.__mww_overlay_probe();
})()
"""

OVERLAY_PROBE_JS = OVERLAY_PROBE_JS.replace(
    "%BANNER_PROBE_SELECTOR%", BANNER_PROBE_SELECTOR
)

DISMISS_CLICK_SELECTORS = (
    "[role='dialog'] [aria-label*='close' i], [aria-modal='true'] [aria-label*='close' i], "
    "dialog[open] [aria-label*='close' i], "
    "[role='dialog'] [data-dismiss], [aria-modal='true'] [data-dismiss], dialog[open] [data-dismiss], "
    "[role='dialog'] [data-close], [aria-modal='true'] [data-close], dialog[open] [data-close], "
    "[role='dialog'] button.close, [aria-modal='true'] button.close, dialog[open] button.close, "
    "[role='dialog'] .modal-close, [aria-modal='true'] .modal-close, dialog[open] .modal-close, "
    "#onetrust-accept-btn-handler, .ot-pc-refuse-all-handler, #onetrust-reject-all-handler, "
    "#onetrust-banner-sdk .onetrust-close-btn-ui, #onetrust-pc-sdk .onetrust-close-btn-ui, "
    "button#didomi-notice-agree-button, button#didomi-notice-disagree-button, "
    ".didomi-continue-without-agreeing, #didomi-host .didomi-button, "
    "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll, "
    "button#CybotCookiebotDialogBodyButtonDecline, #CybotCookiebotDialogBodyButtons, "
    "button#truste-consent-button, button#truste-consent-required, #truste-consent-track button, "
    "button.fc-cta-consent, button.fc-cta-do-not-consent, .fc-cta-consent, "
    "button#almacmp-modalConfirmBtn, button.iubenda-cs-accept-btn, .iubenda-cs-accept-btn, "
    "a.cc-dismiss, .cc-button, .sp_choice_type_11, .sp_choice_type_ACCEPT, "
    "button#_evidon-accept-button, button#_evidon-decline-button, "
    "button.osano-cm-accept-all, .osano-cm-accept-all, .osano-cm-denyAll, "
    "#cmpwelcomebtnyes a, #cmpwelcomebtnno a, a.cmptxt_btn_yes, "
    "#popin_tc_privacy_button_2, #popin_tc_privacy_button_3, "
    "#gdpr-banner-accept, #gdpr-banner-decline, .js-accept, .js-decline, "
    "button.rodo-popup-agree, button#declineButton, .orejime-Notice-declineButton, "
    ".qc-cmp2-summary-buttons button, .qc-cmp2-buttons-desktop button, "
    "#termly-code-snippet-support button[aria-label*='accept' i], "
    ".cky-btn-accept, .cky-btn-reject, .cky-btn-close, "
    "button[data-cky-tag='accept-button'], button[data-cky-tag='reject-button'], "
    ".cookie-consent button, .cookie-banner button, .cookie-notice button, "
    ".cookie-alert button, .cookie-bar button, .cookie-box button, .cookie-hint button, "
    ".cookies-banner button, .cookie-policy button, .gdpr-banner button, "
    "#cookie-banner button, #cookie_consent button, #cookie-law-info-bar button, "
    "#moove_gdpr_cookie_info_bar button, .eu-cookie-compliance-banner button, "
    ".sqs-cookie-banner-v2 button, .pum-close, .elementor-popup-modal .elementor-button, "
    "[aria-label*='close' i], [title*='close' i], [data-dismiss], [data-close], "
    "[data-action='close'], [data-action='dismiss'], [data-testid*='close' i], "
    "button.close, .modal-close, .popup-close, .dialog-close, .banner-close, "
    "button[class*='close' i]:not([class*='closed' i]), "
    "a[class*='close' i]:not([class*='closed' i])"
)

# Text content that marks a button as the one that dismisses a cookie banner
# or popup. Multi-language: en, de, fr, es, it, nl, pt, sv, da, fi, ar, zh.
# Full-string match only: a bare "order" alternative must not match
# "My order history" links, so every alternative is boundary-anchored.
DISMISS_BUTTON_TEXT_RE = re.compile(
    r"""(?ix)^\s*(?:
      (?:accept|allow|agree|ok(?:ay)?|yes|ja|si|sì|oui|sim)\b[\s\w',.()-]{0,30} |
      (?:reject|decline|refuse|deny|dismiss|close|continue|got\s+it|no\b[\s,]*(?:thanks)?|not\s+now|nein|non|não)\b[\s\w',.()-]{0,20} |
      (?:only\s+)?(?:necessary|essential|required)(?:\s+(?:cookies?|only))?\b |
      (?:manage|customize)\s+(?:cookies?|preferences?|options?|settings?)\b |
      preferences?\b | settings?\b
      | akzeptieren\w* | alle\s+akzeptieren | zulassen\w* | ablehnen\w*
      | nur\s+(?:notwendige|erforderliche)\w* | schließen | weiter\s+ohne\w*
      | tout\s+accepter | tout\s+refuser | accepter\w* | refuser\w* | fermer
      | continuer\s+sans\w* | aceptar\w* | rechazar\w* | cerrar
      | continuar\s+sin\w* | accetta\w* | rifiuta\w* | chiudi
      | continua\s+senza\w* | accepteer\w* | weiger\w* | sluiten
      | verder\s+zonder\w* | aceitar\w* | rejeitar\w* | fechar | aceito\w*
      | acceptera\w* | avvisa\w* | stäng | godkänn\w* | afvis\w* | luk
      | hyväksy\w* | sulje
      | قبول | إغلاق | رفض | 接受 | 拒绝 | 关闭 | 同意 | 继续
    )\s*$"""
)

# Buttons whose click would navigate / commit forms / act on data.
DISMISS_BUTTON_TEXT_BLOCK_RE = re.compile(
    r"(?ix)\b(?:subscrib|newsletter|sign\s*up|register|buy|purchase|checkout|"
    r"download|submit|pay|donate|log\s*in|sign\s*in|share|order)\b"
)
DISMISS_OVERLAYS_JS = """\
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const inViewport = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  };
  const safeClick = (el) => {
    try {
      const opts = { bubbles: true, cancelable: true, view: window };
      el.dispatchEvent(new PointerEvent("pointerdown", opts));
      el.dispatchEvent(new MouseEvent("mousedown", opts));
      el.dispatchEvent(new PointerEvent("pointerup", opts));
      el.dispatchEvent(new MouseEvent("mouseup", opts));
      el.click();
    } catch (e) {}
  };
  const pressEscape = () => {
    const target = document.activeElement instanceof HTMLElement ? document.activeElement : document.body;
    const opts = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true, cancelable: true };
    try {
      target.dispatchEvent(new KeyboardEvent("keydown", opts));
      target.dispatchEvent(new KeyboardEvent("keyup", opts));
    } catch (e) {}
  };
  const Dialog = "[dialog],[role='dialog'],[aria-modal='true'],div[class*='modal' i],div[class*='overlay' i],div[class*='popup' i]";
  const dialogCloseAffordances = () => {
    const closeSel = '[aria-label*="close" i], [title*="close" i], [data-dismiss], [data-close], [data-action="close"], [data-action="dismiss"], button.close, .modal-close, .popup-close, .dialog-close, [role="button"]';
    const out = [];
    for (const box of document.querySelectorAll(Dialog)) {
      for (const btn of box.querySelectorAll(closeSel)) {
        if (visible(btn) && inViewport(btn)) out.push(btn);
      }
    }
    return out;
  };
  const bannerButtons = () => {
    const sel = "%BANNER_PROBE_SELECTOR%, dialog[open]";
    const buttons = [];
    for (const box of document.querySelectorAll(sel)) {
      if (!visible(box) || !inViewport(box)) continue;
      for (const btn of box.querySelectorAll("button, a, [role='button'], input[type='button']")) {
        if (visible(btn) && inViewport(btn)) buttons.push(btn);
      }
    }
    return buttons;
  };
  const textLike = (el) => {
    const parts = [el.textContent || "", el.getAttribute("aria-label") || "", el.getAttribute("title") || ""];
    for (const name of el.getAttributeNames()) {
      if (/^(data-|aria-)/.test(name)) parts.push(el.getAttribute(name) || "");
    }
    return parts.join(" ").trim().replace(/\\s+/g, " ");
  };
  const cmpButtons = () => {
    const sel = "%DISMISS_CLICK_SELECTORS%";
    return [...document.querySelectorAll(sel)].filter((el) => visible(el) && inViewport(el));
  };
  const textButtons = (roots) => {
    const out = [];
    for (const btn of roots) {
      const text = textLike(btn);
      if (!text || text.length > 120) continue;
      if ((%DISMISS_BUTTON_TEXT_BLOCK_RE%).test(text)) continue;
      if ((%DISMISS_BUTTON_TEXT_RE%).test(text)) out.push(btn);
    }
    return out;
  };
  const probe = () => {
    try { return window.__mww_overlay_probe() || {}; } catch (e) { return {}; }
  };
  const overlayLeft = () => {
    const p = probe();
    return (p.dialog || 0) > 0 || (p.banner || 0) > 0 || (p.scrim || 0) > 0;
  };
  // 1. Escape — the standard way to dismiss dialogs and popups.
  pressEscape();
  await sleep(250);
  if (overlayLeft()) {
    // 2. Close affordances inside dialog-like containers (never other
    // buttons: clicking a dialog's primary CTA would subscribe/buy).
    for (const btn of dialogCloseAffordances()) safeClick(btn);
    await sleep(400);
  }
  if (overlayLeft()) {
    // 3. CMP accept / reject buttons (mozilla/cookie-banner-rules-list).
    // One click only: accept + decline together cancel the consent, and
    // clicking multiple banners' buttons would consent to the wrong CMP.
    safeClick(cmpButtons()[0]);
    await sleep(500);
  }
  if (overlayLeft()) {
    // 4. Text-matched accept/dismiss/close buttons in visible banners and
    // open native dialogs (a home-grown <dialog class=cookie-banner>).
    // Same single-click rule: the list prefers accept-like labels, and
    // clicking accept then decline would undo the choice.
    safeClick(textButtons(bannerButtons())[0]);
    await sleep(500);
  }
  if (overlayLeft()) {
    // 5. Native <dialog>.close() as a last resort.
    for (const el of document.querySelectorAll("dialog[open]")) {
      try { el.close(); } catch (e) {}
    }
    await sleep(200);
  }
  return probe();
})();
"""


def _js_regex(pattern: re.Pattern) -> str:
    """JS regex literal for a Python pattern. JS has no (?ix) inline flags
    and no VERBOSE mode: strip the flag prefix, drop newlines, and collapse
    the free-spacing indentation (in VERBOSE mode it is ignored; left in
    place it would become mandatory literal spaces). Real spaces in the
    patterns are always written as \\s+ — verified per alternative."""
    source = pattern.pattern
    prefix = "(?ix)" if source.startswith("(?ix)") else "(?i)"
    assert source.startswith(prefix), source  # keep flag support honest
    stripped = re.sub(r"\s+", " ", source[len(prefix) :]).replace("  ", " ")
    # Whitespace that VERBOSE ignored is gone; a single space between
    # alternatives is still ignored by the JS engine only outside classes —
    # but JS does NOT ignore it, so remove it entirely. Literals needing
    # spaces use \s or ' ' explicitly in the source (none do).
    return "/" + stripped.replace(" ", "") + "/i"


DISMISS_OVERLAYS_JS = (
    DISMISS_OVERLAYS_JS.replace("%BANNER_PROBE_SELECTOR%", BANNER_PROBE_SELECTOR)
    .replace("%DISMISS_CLICK_SELECTORS%", DISMISS_CLICK_SELECTORS)
    .replace("%DISMISS_BUTTON_TEXT_RE%", _js_regex(DISMISS_BUTTON_TEXT_RE))
    .replace("%DISMISS_BUTTON_TEXT_BLOCK_RE%", _js_regex(DISMISS_BUTTON_TEXT_BLOCK_RE))
)
BANNER_HIDE_CSS = """\
/* Major CMP SDK containers (grounded in AdGuard's Cookie Notices filter) */
#onetrust-consent-sdk, #onetrust-banner-sdk, #onetrust-pc-sdk,
#optanon-popup-bg, .optanon-show-settings,
#CybotCookiebotDialog, #CybotCookiebotDialogBodyUnderlay, .CybotCookiebotDialogBodyOverlay,
#usercentrics-root, #usercentrics-cmp-ui, #fc-consent-root,
#didomi-host, #didomi-popup, .didomi-host,
#sp_message_container, .sp-message-container, #sp_privacy_manager_container,
#qc-cmp2-container, #qc-cmp2-ui, .qc-cmp2-summary-buttons,
#truste-consent-track, .truste-consent-track, #truste-consent-content,
#Osano-CookieDialog, .osano-cm-window, .osano-cm-info,
#termly-code-snippet-support, .termly-consent-banner,
#iubenda-cs-banner, .iubenda-cs-banner,
#cmplz-cookiebanner-container, .cmplz-cookiebanner,
#cookie-script, .cookiescript_injected_wrapper, #cookiescript_injected,
#klaro, .klaro, .cookie-consent:not(body):not(html),
#klaro0, .klaro-manager-overlay,
#tarteaucitronRoot, .tarteaucitron-root, #tarteaucitronAlertBig, .tarteaucitron-banner,
#BorlabsCookieBox, .borlabs-hide,
#ccm-widget, #ccm-block,
#cky-consent-bar, .cky-consent-bar, .cky-consent-container, #cky-overlay, .cky-overlay,
#cm, #cc-banner, .cc-window, .cc-banner, .cc-revoke,
#cookiebanner, .cookie-banner, #cookie-banner, #cookie_consent, .cookie_consent,
#cookie-law-info-bar, #cookie-law-info-again, .cli-bar-container, #cliSettingsPopup,
.cli-popupbar-overlay, .cli-modal-backdrop,
#moove_gdpr_cookie_info_bar, #moove_gdpr_cookie_modal,
#gdpr-cookie-message, .gdpr-cookie-notice, .gdpr-banner, #gdpr-banner, .gdpr_cookie_bar,
.eu-cookie-compliance-banner:not(body):not(html), .eu-cookie-compliance-overlay,
.js-cookie-banner, .js-cookie-consent, .cookie-notice:not(body):not(html),
.cookie-consent-banner, .cookie-alert:not(body):not(html), .cookie-bar:not(body):not(html),
.cookie-warning, .cookie-policy:not(body):not(html), #cookie-policy,
.cookie-popup, .cookie-popup-wrapper, .cookies-wrapper,
#cookie-box, .cookie-box:not(body):not(html), #cookie-bar, .cookie-bar-overlay,
#cookie-hint, #cookiehint, #cookie-hinweis, .cookie-hint,
#cookies-banner, #cookies-banner-container, .cookies-banner,
#cookie-msg, #cookie-message, .cookie-message:not(body):not(html),
.cookies-eu-banner, #cookie-law-banner, .cookie-law-banner,
#cookiesck, .sqs-cookie-banner-v2, .wpgdprc-consent-bar,
.avia-cookie-consent-wrap, .fusion-privacy-bar, .woodmart-cookies-popup,
.thb-cookie-bar, .pum-open .pum-overlay, .elementor-popup-modal:not(:empty),
/* Generic fallback: any container whose class mentions "cookie". Scoped to
   banner-capable container elements — an unscoped [class*="cookie"] would
   hide recipe content on food blogs (e.g. .cookie-recipes-grid) and blank
   the screenshot. The i flag covers CamelCase classes. */
dialog, [role="dialog"], div[class*="cookie" i], section[class*="cookie" i],
aside[class*="cookie" i], footer[class*="cookie" i], header[class*="cookie" i],
/* Same scoping for id-based banners: id attribute mentioning "cookie". */
div[id*="cookie" i], section[id*="cookie" i], aside[id*="cookie" i],
footer[id*="cookie" i], header[id*="cookie" i]
"""
CONSENT_INIT_JS = (
    """\
(() => {
  const CSS = `%s { display: none !important; }`;
  const ID = "__mww_banner_hide";
  // Deferred installation: the overlay-dismissal script (DISMISS_OVERLAYS_JS)
  // runs first, and this CSS hide-style is only its fallback. The driver
  // calls `window.__mww_install_banner_css()` after the dismissal attempt.
  window.__mww_install_banner_css = () => {
    if (document.getElementById(ID)) return;
    const root = document.head || document.documentElement;
    if (!root) return;
    const style = document.createElement("style");
    style.id = ID;
    style.textContent = CSS;
    root.appendChild(style);
  };
})();"""
    % BANNER_HIDE_CSS
)


def wait_for_page_settled(page, timeout_s: float = 15.0) -> None:
    """Wait until the page stops mutating the DOM and fetching, so spinners,
    lazy-loaded heroes, and CMP mounts have had their chance to appear."""
    mutations = {"count": 0}
    inflight = {"open": 0}
    observer_js = """
    window.__mww_mutations = 0;
    new MutationObserver((records) => { window.__mww_mutations += records.length; })
      .observe(document, { childList: true, subtree: true, attributes: true });
    """
    try:
        page.evaluate(observer_js)
    except Exception:
        pass  # CSP-locked page: settle on network activity alone.

    def _request(_request):
        inflight["open"] += 1

    def _settled(_request):
        inflight["open"] -= 1

    page.on("request", _request)
    page.on("requestfinished", _settled)
    page.on("requestfailed", _settled)

    deadline = time.monotonic() + timeout_s
    last_activity = time.monotonic()
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        now = time.monotonic()
        try:
            count = page.evaluate("window.__mww_mutations || 0")
            if count:
                mutations["count"] += count
                last_activity = now
                page.evaluate("window.__mww_mutations = 0")
        except Exception:
            pass
        if inflight["open"] > 0:
            last_activity = now
        if now - last_activity >= 2.0:
            return
    # Deadline reached; proceed with whatever the page shows now.


def capture_screenshot(url: str, out_path: Path) -> None:
    """Load the URL in headless Chromium and save an encoded WebP screenshot.

    Overlay suppression is JS-first (DISMISS_OVERLAYS_JS): wait for the page
    to settle, then dismiss dialogs / cookie banners / popups by pressing
    Escape and clicking close/accept affordances, before falling back to the
    BANNER_HIDE_CSS style. Navigation is blocked during interaction so a
    misclicked control cannot take the screenshot to a different page.
    """
    from playwright.sync_api import sync_playwright

    def route_guard(route):
        if is_private_browser_host(route.request.url):
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1200, "height": 996}, device_scale_factor=1
        )
        context.route("**/*", route_guard)
        context.add_init_script(CONSENT_INIT_JS)
        page = context.new_page()
        target_origin = urlsplit(url)
        origin = f"{target_origin.scheme}://{target_origin.netloc}"
        try:
            page.goto(url, timeout=30_000, wait_until="load")
            wait_for_page_settled(page)
            # Block navigation during interaction: a misclick on a link
            # inside a banner must not screenshot a different page.
            # Cross-origin navigation is always blocked; same-origin is
            # allowed (banner accept often reloads the same URL).
            page.evaluate(
                """(origin) => {
                  window.__mww_nav_blocked = 0;
                  document.addEventListener('click', (event) => {
                    const link = event.target && event.target.closest && event.target.closest('a[href]');
                    if (!link) return;
                    try {
                      const target = new URL(link.href || link.getAttribute('href') || '', location.href);
                      if (target.origin !== origin) {
                        event.preventDefault();
                        event.stopPropagation();
                        window.__mww_nav_blocked += 1;
                      }
                    } catch (e) {}
                  }, true);
                }""",
                origin,
            )
            page.evaluate(OVERLAY_PROBE_JS)  # installs __mww_overlay_probe
            # Dismissing a banner often reloads the page (the consent cookie
            # only takes effect after navigation), destroying the execution
            # context. Re-run until the probe reports no overlay left.
            probe: dict | None = None
            for _ in range(3):
                try:
                    probe = page.evaluate(DISMISS_OVERLAYS_JS)
                except Exception:
                    # Same-origin reload from the click; re-arm and retry.
                    page.wait_for_load_state("load", timeout=20_000)
                    page.wait_for_timeout(500)
                    page.evaluate(OVERLAY_PROBE_JS)
                    continue
                if not any(probe.get(k) for k in ("dialog", "banner", "scrim")):
                    break
                page.wait_for_timeout(400)
            # Always install the CSS hide-style afterwards as the final
            # fallback: banners re-mounted by CMP timers after the JS pass.
            try:
                page.evaluate(
                    "window.__mww_install_banner_css && window.__mww_install_banner_css()"
                )
            except Exception:
                page.wait_for_load_state("load", timeout=20_000)
                page.evaluate(
                    "window.__mww_install_banner_css && window.__mww_install_banner_css()"
                )
            png = page.screenshot(type="png", animations="disabled")
        finally:
            context.close()
            browser.close()
    leftover = ", ".join(f"{k}={v}" for k, v in (probe or {}).items() if v)
    if leftover:
        print(f"capture_screenshot: overlay remained after dismissal ({leftover})")
    out_path.write_bytes(encode_screenshot(png))


def cmd_render(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="render")
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    # --url smoke mode (local browser testing) skips the proposal entirely.
    proposal: Proposal | None = None
    if args.proposal:
        proposal = Proposal.model_validate_json(
            args.proposal.read_text(encoding="utf-8")
        )
    elif not args.url:
        print("render: either --proposal or --url is required", file=sys.stderr)
        return ERROR_EXIT
    target_url = args.url or proposal.site_url

    import httpx

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # follow_redirects=True for image/manifest GETs: candidate URLs 301
    # between www/apex origins routinely. fetch_page and probe_admin_pages
    # manage redirects manually with per-hop SSRF checks and are unaffected.
    with httpx.Client(follow_redirects=True) as client:
        final_url, html = fetch_page(client, target_url)
        signals = detect_wagtail(html)
        final_parts = urlsplit(final_url)
        origin = f"{final_parts.scheme}://{final_parts.hostname}"
        signals += probe_admin_pages(client, origin)

        logo_bytes: bytes | None = None
        if proposal is not None and not proposal.developer_exists:
            # Logo candidates come from the developer's own site (Developer
            # URL), never the submitted site. Best-effort: an unreachable
            # developer page simply yields no icon candidates.
            developer_html = ""
            if proposal.developer_url:
                try:
                    _, developer_html = fetch_page(client, proposal.developer_url)
                except Exception:
                    pass
            logo_bytes = select_largest_logo(
                client,
                gather_logo_candidates(
                    client, developer_html, proposal.developer_url, proposal.logo_url
                ),
            )
    # Wappalyzer scan (headless Chromium): technology fingerprints that
    # complement the Wagtail HTML detection above, which stays authoritative
    # for Wagtail itself. Best-effort: a failed scan yields no technologies.
    # Incompatible technologies are reported in "technologies" and gated by
    # a dedicated workflow job — never merged into "signals": is_wagtail
    # must stay a pure Wagtail-detection verdict.
    classified = classify_technologies(wappalyzer_technologies(final_url))
    detection = detection_result(signals, final_url, classified)
    (args.out_dir / "detection.json").write_text(
        json.dumps(detection, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    capture_screenshot(final_url, args.out_dir / "screenshot.webp")
    if logo_bytes:
        (args.out_dir / "logo.webp").write_bytes(logo_bytes)
    return 0


REJECTION_EXIT = 2
ERROR_EXIT = 1

CONFIRMATION_LABELS = (
    "I am affiliated with this site or have permission to submit it",
    "This is a production website built with Wagtail",
)

LAT_RE = re.compile(r"^-?(?:[0-8]?\d|90)(?:\.\d+)?$")
LON_RE = re.compile(r"^-?(?:\d{1,2}|1[0-7]\d|180)(?:\.\d+)?$")


class Rejection(Exception):
    def __init__(self, *reasons: str):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


def build_proposal(
    body: str,
    issue_number: int,
    content_dir: Path,
    resolver=socket.getaddrinfo,
    now: datetime | None = None,
) -> Proposal:
    reasons: list[str] = []
    try:
        fields = parse_issue_form_body(body)
    except FormParseError:
        raise Rejection("This issue was not created with the site submission form.")

    def field(label: str) -> str:
        value = fields.get(label, "")
        return value.strip() if isinstance(value, str) else ""

    # Confirmations.
    confirmations = fields.get("Confirmations", [])
    for label in CONFIRMATION_LABELS:
        checked = any(label in item_label and ok for item_label, ok in confirmations)
        if not checked:
            reasons.append(f"Tick the confirmation: “{label}.”")

    # URL.
    site_url = ""
    raw_url = field("Site URL")
    if not raw_url:
        reasons.append("Fill in the site URL.")
    else:
        try:
            site_url = check_public_url(raw_url, resolver=resolver)
        except Exception as exc:
            reasons.append(f"The site URL was rejected: {_validation_message(exc)}")

    # Required text fields.
    site_title = field("Site title")
    if not site_title:
        reasons.append("Fill in the site title.")
    site_description = field("Short description")
    if not site_description:
        reasons.append("Fill in the short description.")
    developer_name = field("Developer name")
    if not developer_name:
        reasons.append("Fill in the developer name.")

    # Facets (already list-valued from the parser), validated against the
    # controlled vocabularies so typos can never reach the content files.
    def facet_values(heading: str, allowed: frozenset[str], label: str) -> list[str]:
        values = [
            v for v in (fields.get(heading) or []) if isinstance(v, str) and v.strip()
        ]
        for value in values:
            if value not in allowed:
                reasons.append(f"{value!r} is not a valid {label} option.")
        return values

    sector = facet_values("Sector", SECTOR_VALUES, "sector")
    site_type = facet_values("Site type", SITE_TYPE_VALUES, "site type")
    capability = facet_values("Capabilities", CAPABILITY_VALUES, "capability")

    # Developer profile, inferred from the name: an exact (case-insensitive)
    # match against an existing profile title adds the site to that profile,
    # and any other name starts a new one. Near-misses surface as a
    # reviewer-facing hint so duplicate profiles are caught at review time.
    developer_exists = False
    developer_slug = ""
    similar_developers: list[str] = []
    if developer_name:
        result = match_developer(developer_name, existing_developers(content_dir))
        if isinstance(result, list):
            similar_developers = result
            try:
                developer_slug = make_slug(developer_name)
                check_slug_free("developer", developer_slug, content_dir)
            except ValueError as exc:
                reasons.append(f"The developer name is not usable: {exc}")
        else:
            developer_exists = True
            developer_slug = result[0]

    # Site slug + dedup.
    site_slug = ""
    if site_title:
        try:
            site_slug = make_slug(site_title)
            check_slug_free("site", site_slug, content_dir)
        except ValueError as exc:
            reasons.append(f"The site title is not usable as a page name: {exc}")

    if site_url:
        origin = urlsplit(site_url)
        origin_key = (
            f"{origin.scheme}://{origin.hostname.lower()}" if origin.hostname else ""
        )
        if origin_key and origin_key in existing_site_origins(content_dir):
            reasons.append(f"{origin_key} is already in the showcase.")

    # Optional fields.
    developer_url = ""
    # Submitters often enter a bare domain ("madewithwagtail.org");
    # assume https rather than rejecting the URL outright.
    if raw := field("Developer URL"):
        if not raw.lower().startswith("http"):
            raw = f"https://{raw}"
        try:
            developer_url = check_public_url(raw, resolver=resolver)
        except Exception as exc:
            reasons.append(
                f"The developer URL was rejected: {_validation_message(exc)}"
            )

    logo_url = ""
    if field("Logo URL"):
        try:
            logo_url = check_public_url(field("Logo URL"), resolver=resolver)
        except Exception as exc:
            reasons.append(f"The logo URL was rejected: {_validation_message(exc)}")

    location = field("Developer location") or None
    lat = field("Latitude") or None
    lon = field("Longitude") or None
    if lat and not LAT_RE.fullmatch(lat):
        reasons.append("Latitude must be a decimal degrees value between -90 and 90.")
        lat = None
    if lon and not LON_RE.fullmatch(lon):
        reasons.append(
            "Longitude must be a decimal degrees value between -180 and 180."
        )
        lon = None

    github_user = field("GitHub username") or None
    other_notes = field("Other notes") or None

    if reasons:
        raise Rejection(*reasons)

    try:
        return Proposal(
            schema_version=1,
            issue_number=issue_number,
            site_url=site_url,
            site_title=site_title,
            site_description=site_description,
            sector=sector,
            site_type=site_type,
            capability=capability,
            developer_name=developer_name,
            developer_slug=developer_slug,
            site_slug=site_slug,
            developer_exists=developer_exists,
            similar_developers=similar_developers,
            developer_url=developer_url or None,
            developer_location=location,
            lat=lat,
            lon=lon,
            github_user=github_user,
            logo_url=logo_url or None,
            other_notes=other_notes,
            submitted_at=now or utcnow(),
        )
    except ValidationError as exc:
        # Model-level caps (title > 80, description > 800, ...) are reachable
        # through the real form — they must surface as a structured rejection,
        # not an exit-1 traceback that silently drops the submission.
        raise Rejection(
            *(_proposal_error_reason(error) for error in exc.errors())
        ) from exc


# Model-level constraints on user-editable fields, mapped to rejection copy.
PROPOSAL_ERROR_REASONS = {
    "site_title": "The site title must be at most 80 characters.",
    "site_description": "The short description must be at most 800 characters.",
    "developer_name": "The developer name must be at most 80 characters.",
    "developer_location": "The developer location must be at most 100 characters.",
}


def _proposal_error_reason(error: dict) -> str:
    loc = error.get("loc") or ()
    field = str(loc[-1]) if loc else ""
    return PROPOSAL_ERROR_REASONS.get(
        field,
        f"An entry in the form was rejected: {error.get('msg', 'invalid value')}.",
    )


def _validation_message(exc: Exception) -> str:
    """Human-readable first message from a pydantic ValidationError."""
    if hasattr(exc, "errors") and callable(exc.errors):
        errors = exc.errors()
        if errors:
            return str(errors[0].get("msg", exc))
    return str(exc)


def cmd_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="validate")
    parser.add_argument("--issue-body", required=True, type=Path)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument(
        "--content-dir", type=Path, default=Path("src/content/developers")
    )
    args = parser.parse_args(argv)
    body = args.issue_body.read_text(encoding="utf-8")
    try:
        proposal = build_proposal(body, args.issue_number, args.content_dir)
    except Rejection as rejection:
        print(json.dumps({"reasons": rejection.reasons}, indent=2, ensure_ascii=False))
        return REJECTION_EXIT
    print(proposal.model_dump_json(indent=2))
    return 0


def _frontmatter_block(data: dict) -> str:
    import yaml

    # Optional fields left unset are omitted rather than written as null:
    # the Astro schemas default them, and `key: null` in committed content
    # is noise reviewers shouldn't see.
    data = {key: value for key, value in data.items() if value is not None}
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True) + "---\n"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def site_markdown(p: Proposal, technologies: dict[str, list[str]] | None = None) -> str:
    complementary = (technologies or {}).get("complementary", [])
    frontmatter = {
        "title": p.site_title,
        "first_published_at": _iso(p.submitted_at),
        "latest_revision_created_at": _iso(p.submitted_at),
        "site_url": p.site_url,
        "in_cooperation_with_slug": None,
        # Facet blocks are omitted entirely when empty: the Astro schemas
        # default them to [], matching how migrated entries are written.
        **({"sector": p.sector} if p.sector else {}),
        **({"site_type": p.site_type} if p.site_type else {}),
        **({"capability": p.capability} if p.capability else {}),
        # Complementary technologies from the Wappalyzer scan, e.g.
        # ["React", "Tailwind CSS"]. Omitted when nothing was detected:
        # the Astro schema defaults to [].
        **({"technologies": complementary} if complementary else {}),
    }
    return _frontmatter_block(frontmatter) + f"\n{p.site_description}\n"


def developer_markdown(p: Proposal) -> str:
    frontmatter = {
        "title": p.developer_name,
        "first_published_at": _iso(p.submitted_at),
        "latest_revision_created_at": _iso(p.submitted_at),
        "location": p.developer_location,
        "lat": p.lat,
        "lon": p.lon,
        "company_url": p.developer_url,
        "twitter_handler": None,
        "github_user": p.github_user,
        "online_profiles": [],
    }
    return _frontmatter_block(frontmatter)


# Proposal fields merged into an existing developer profile when provided,
# mapped to the developer frontmatter keys they update.
PROFILE_UPDATE_FIELDS = {
    "developer_url": "company_url",
    "developer_location": "location",
    "lat": "lat",
    "lon": "lon",
    "github_user": "github_user",
}


def profile_updates(p: Proposal) -> dict[str, str]:
    """Provided developer details for an existing profile, as
    {frontmatter key: value}.

    Empty when the submission provides none of these, so a Developer name
    alone still adds a site without touching the profile.
    """
    return {
        key: value
        for field, key in PROFILE_UPDATE_FIELDS.items()
        if (value := getattr(p, field)) is not None
    }


# Frontmatter occupies the file's first "---\n...\n---\n" block; the profile
# text follows it. Values containing '---' cannot mis-split the extraction.
FRONTMATTER_SPLIT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def update_developer_profile(path: Path, p: Proposal) -> bool:
    """Merge the provided developer details into an existing profile.

    Only the fields the submitter provided are written; every other
    frontmatter value and the profile text are left untouched. The revision
    timestamp is bumped so listing pages reflect the update. Returns False —
    leaving the file alone — when there is nothing to change.
    """
    updates = profile_updates(p)
    if not updates or not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_SPLIT_RE.match(text)
    if not match:
        return False
    import yaml

    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        return False
    changed = {key: value for key, value in updates.items() if data.get(key) != value}
    if not changed:
        return False
    data.update(changed)
    data["latest_revision_created_at"] = _iso(p.submitted_at)
    body = text[match.end() :].strip()
    path.write_text(
        _frontmatter_block(data) + (f"\n{body}\n" if body else ""), encoding="utf-8"
    )
    return True


def output_paths(p: Proposal) -> dict[str, Path]:
    paths = {
        "site_md": Path(
            f"src/content/developers/{p.developer_slug}/{p.site_slug}/index.md"
        ),
        "screenshot": Path(
            f"src/content/developers/{p.developer_slug}/{p.site_slug}/{p.site_slug}.fill-1200x996.webp"
        ),
    }
    if not p.developer_exists:
        paths["developer_md"] = Path(
            f"src/content/developers/{p.developer_slug}/index.md"
        )
        paths["logo"] = Path(
            f"src/content/developers/{p.developer_slug}/{p.developer_slug}.max-120x120.webp"
        )
    elif profile_updates(p):
        # Existing profile with provided details: the profile is updated in
        # place as part of the submission.
        paths["developer_md"] = Path(
            f"src/content/developers/{p.developer_slug}/index.md"
        )
    return paths


def _file_line_count(path: Path) -> int:
    """Number of lines in a committed file, for deep-link ranges."""
    return len(path.read_text(encoding="utf-8").splitlines())


PR_LABEL = "🤖 new site submission"
NEEDS_TRIAGE_LABEL = "needs-triage"
PR_CREATED_LABEL = "submission → PR created"
LIVE_SITE_URL = "https://madewithwagtail.org"


def _profile_line(p: Proposal) -> str:
    """Developer table cell: name linked to their website, profile link after.

    The name links to the developer's own site so the row works for both
    new and existing profiles; the profile-page link only exists once the
    profile is live.
    """
    if p.developer_url:
        name = f"[{p.developer_name}]({p.developer_url})"
    elif p.developer_exists:
        name = f"[{p.developer_name}]({LIVE_SITE_URL}/developers/{p.developer_slug}/)"
    else:
        name = p.developer_name
    if p.developer_exists:
        suffix = (
            f" - [see profile page]({LIVE_SITE_URL}/developers/{p.developer_slug}/)"
        )
    else:
        suffix = " - new 🎉"
    return name + suffix


def _facet_links(values: list[str], facet: str) -> str:
    """Facet values linking to the live site's facet pages, as the site renders them."""
    if not values:
        return "_(none)_"
    return ", ".join(
        f"[{value}]({LIVE_SITE_URL}/sites/{facet}/{slugify(value)}/)"
        for value in values
    )


def _table_cell_text(value: str) -> str:
    """User text for a Markdown table cell."""
    return "<br>".join(line.strip() for line in value.splitlines()).replace("|", r"\|")


def _run_footer(run_url: str) -> str:
    """Small-print footer shared by the PR body and issue comments."""
    return f"<sub>View the [site submission workflow logs]({run_url}).</sub>"


def _detection_value(detection: dict) -> str:
    """Concise table-cell status: verdict plus the signals behind it."""
    if detection["is_wagtail"]:
        signals = "; ".join(detection["signals"]) or "Wagtail detected"
        return f"✅ {signals}"
    return "⚠️ No Wagtail signals detected"


def _committed_file_url(
    path: Path,
    repo_full_name: str,
    head_sha: str | None,
    line_count: int | None,
) -> str:
    """Raw deep link to a committed file, rendered as an inline file viewer.

    The L1-L<last> range makes GitHub render the file directly in the PR
    description; it needs the branch's HEAD SHA and the file's line count.
    The URL is emitted bare — reviewers asked for raw links, not markdown
    links — so GitHub auto-links the visible URL itself.
    """
    if head_sha is None:
        return "(SHA unavailable in dry-run)"
    last = line_count if line_count is not None else 1
    return (
        f"https://github.com/{repo_full_name}/blob/{head_sha}/{path}?plain=1#L1-L{last}"
    )


def build_pr_body(
    p: Proposal,
    detection: dict,
    repo_full_name: str,
    branch: str,
    run_url: str,
    logo_committed: bool | None = None,
    head_sha: str | None = None,
    entry_line_count: int | None = None,
    profile_line_count: int | None = None,
) -> str:
    paths = output_paths(p)
    # Whether the logo image was actually written to the branch. None keeps
    # the default "the proposal expects one" for callers that don't know.
    logo_expected = logo_committed if logo_committed is not None else "logo" in paths
    # 300x249 is the 1200x996 capture scaled down; GitHub renders raw
    # width/height img attributes inside PR descriptions.
    screenshot_cell = (
        f'<img src="https://raw.githubusercontent.com/{repo_full_name}/{branch}/{paths["screenshot"]}"'
        ' width="300" height="249" alt="Screenshot of the new site">'
    )
    lines = [
        f"Closes #{p.issue_number}. Auto-generated PR via the [site submission workflow]"
        "(https://github.com/wagtail/madewithwagtail/blob/main/CONTRIBUTING.md#site-submissions)"
        f" ([view logs]({run_url})).",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Site | <{p.site_url}> |",
        f"| Developer | {_profile_line(p)} |",
        f"| Description | {_table_cell_text(p.site_description)} |",
    ]
    if p.similar_developers:
        links = ", ".join(
            f"[{slug}]({LIVE_SITE_URL}/developers/{slug}/)"
            for slug in p.similar_developers
        )
        lines.append(
            f"| Similar profiles | {links} — check this is not a duplicate |"
        )
    lines += [
        f"| Sector | {_facet_links(p.sector, 'sector')} |",
        f"| Site type | {_facet_links(p.site_type, 'type')} |",
        f"| Capabilities | {_facet_links(p.capability, 'capability')} |",
        f"| Detection | {_detection_value(detection)} |",
        f"| Screenshot | {screenshot_cell} |",
    ]
    if not p.developer_exists and logo_expected:
        logo_cell = (
            f'<img src="https://raw.githubusercontent.com/{repo_full_name}/{branch}/{paths["logo"]}"'
            ' width="120" alt="Logo of the developer">'
        )
        lines.append(f"| Logo | {logo_cell} |")
    lines += [
        f"| Local preview | `/developers/{p.developer_slug}/{p.site_slug}` |",
        "",
        "### Site page",
        "",
        _committed_file_url(
            paths["site_md"], repo_full_name, head_sha, entry_line_count
        ),
        "",
    ]
    if "developer_md" in paths:
        lines += [
            (
                "### Developer profile page"
                if not p.developer_exists
                else "### Developer profile update"
            ),
            "",
            _committed_file_url(
                paths["developer_md"], repo_full_name, head_sha, profile_line_count
            ),
            "",
        ]
    if p.other_notes:
        lines += [
            "### Submitter notes",
            "",
            p.other_notes,
            "",
        ]
    lines += [*_detected_technologies_section(detection)]
    lines += [
        "### Reviewer checklist",
        "",
        "- [ ] Site is live and built with Wagtail",
        "- [ ] Screenshot shows the site (not a cookie banner or login page)",
        "- [ ] Sector, site type, and capabilities are sensible",
        "- [ ] Description reads well",
        "- [ ] Developer details are correct"
        + (" (new profile: check the logo)" if not p.developer_exists else ""),
    ]
    return "\n".join(lines)


def _detected_technologies_section(detection: dict) -> list[str]:
    """'Detected technologies' PR section listing the Wappalyzer findings.

    Incompatible technologies never reach a PR: the workflow's
    reject-technologies job closes those submissions first, so only
    complementary and unclassified findings are reportable here.
    """
    technologies = detection.get("technologies") or {}
    complementary = technologies.get("complementary") or []
    other = technologies.get("other") or []
    if not (complementary or other):
        return [
            "### Detected technologies",
            "",
            "_None detected — the Wappalyzer scan found no reportable technologies._",
            "",
        ]
    lines = ["### Detected technologies", ""]
    if complementary:
        lines.append("- ✅ Complementary: " + ", ".join(complementary))
    if other:
        lines.append("- Other: " + ", ".join(other))
    lines.append("")
    return lines


def git_add_paths(p: Proposal, repo_root: Path) -> list[Path]:
    """Content paths that exist on disk — a missing logo is legitimate, and
    `git add` on a pathspec that matches nothing fails the publish stage."""
    return [
        path
        for rel in output_paths(p).values()
        if (repo_root / rel).exists()
        for path in [repo_root / rel]
    ]


def build_pr_comment(p: Proposal, pr_url: str, run_url: str) -> str:
    return (
        f"Opened pull request {pr_url} with this submission. "
        f"The issue auto-closes when the PR is merged.\n\n"
        f"Workflow run (artifacts): {run_url}\n" + _run_footer(run_url)
    )


def build_rejection_comment(reasons: list[str], run_url: str) -> str:
    bullets = "\n".join(f"- {reason}" for reason in reasons)
    return (
        "Thanks for your submission! Unfortunately it could not be processed:\n\n"
        f"{bullets}\n\n"
        "A maintainer will follow up (the issue is labelled needs-triage). "
        "Feel free to update the submission details here in the meantime.\n"
        + _run_footer(run_url)
    )


def build_failure_comment(stage: str, error: str, run_url: str) -> str:
    return (
        f"The submission pipeline failed at the **{stage}** stage: {error}\n\n"
        f"Check the [workflow run]({run_url}) for details — a maintainer will follow up "
        f"(labelled {NEEDS_TRIAGE_LABEL}).\n" + _run_footer(run_url)
    )


BRANCH_PREFIX = "submission/issue-"


def write_content_files(
    p: Proposal,
    repo_root: Path,
    screenshot: bytes,
    logo: bytes | None,
    technologies: dict[str, list[str]] | None = None,
) -> list[Path]:
    """Write validated content into the repo. Images are re-encoded through
    Pillow and dimension-checked — artifact bytes are never trusted as-is."""
    paths = output_paths(p)

    screenshot_img = assert_webp(screenshot)
    if screenshot_img.size != (1200, 996):
        raise ValueError(f"Screenshot must be 1200x996, got {screenshot_img.size}")
    reencoded_screenshot = encode_screenshot(screenshot)

    logo_out: bytes | None = None
    if not p.developer_exists:
        if logo is None:
            raise ValueError("New developer submission requires a logo (may be empty)")
        if logo:
            assert_webp(logo)  # format check; encode_logo normalizes the size
            logo_out = encode_logo(logo)
            if max(Image.open(io.BytesIO(logo_out)).size) > 120:
                raise ValueError("Encoded logo exceeds the 120x120 limit")

    written: list[Path] = []
    for key in ("site_md", "developer_md"):
        if key not in paths:
            continue
        target = repo_root / paths[key]
        if key == "developer_md" and p.developer_exists:
            # Existing profile: merge the provided details into it instead
            # of overwriting; a no-op update leaves the file alone.
            if update_developer_profile(target, p):
                written.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            site_markdown(p, technologies)
            if key == "site_md"
            else developer_markdown(p),
            encoding="utf-8",
        )
        written.append(target)

    screenshot_target = repo_root / paths["screenshot"]
    screenshot_target.parent.mkdir(parents=True, exist_ok=True)
    screenshot_target.write_bytes(reencoded_screenshot)
    written.append(screenshot_target)

    if logo_out is not None:
        logo_target = repo_root / paths["logo"]
        logo_target.parent.mkdir(parents=True, exist_ok=True)
        logo_target.write_bytes(logo_out)
        written.append(logo_target)

    return written


def commit_message(
    proposal: Proposal, co_author: str | None, co_author_id: str | None
) -> str:
    """Commit subject plus a Co-authored-by trailer crediting the issue author."""
    message = f"Add site submission from issue #{proposal.issue_number}"
    if co_author and co_author_id:
        message += (
            f"\n\nCo-authored-by: {co_author} "
            f"<{co_author_id}+{co_author}@users.noreply.github.com>"
        )
    return message


def run(args: list[str]) -> None:
    """Run a subprocess with list args (never a shell) and fail loudly."""
    result = subprocess.run(args, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}")


def cmd_publish(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="publish")
    parser.add_argument("stage", choices=["prepare", "pr"])
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--detection", type=Path)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--logo", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--co-author")
    parser.add_argument("--co-author-id")
    args = parser.parse_args(argv)
    proposal = Proposal.model_validate_json(args.proposal.read_text(encoding="utf-8"))

    if args.stage == "prepare":
        screenshot = args.screenshot.read_bytes()
        # A missing logo artifact is a legitimate no-logo outcome, not an
        # error: write_content_files treats empty bytes as "no logo".
        logo = args.logo.read_bytes() if args.logo and args.logo.exists() else b""
        technologies = {}
        if args.detection and args.detection.exists():
            technologies = (
                json.loads(args.detection.read_text(encoding="utf-8")).get(
                    "technologies"
                )
                or {}
            )
        if args.dry_run:
            for key, rel in output_paths(proposal).items():
                print(f"would write {rel}")
            print(site_markdown(proposal, technologies))
            return 0
        written = write_content_files(
            proposal, args.repo_root, screenshot, logo, technologies
        )
        for path in written:
            print(path)
        return 0

    # Stage "pr": branch, commit, push, PR, issue comment.
    # The PR body links the committed site entry at the pushed HEAD SHA, so
    # the commit must exist (and be pushed) before the body is built.
    if args.detection is None:
        parser.error("publish pr requires --detection")
    detection = json.loads(args.detection.read_text(encoding="utf-8"))
    branch = f"{BRANCH_PREFIX}{proposal.issue_number}"
    paths = output_paths(proposal)
    logo_committed = "logo" in paths and (args.repo_root / paths["logo"]).exists()

    body_file = args.repo_root / ".git" / "PR_BODY.md"

    if args.dry_run:
        # Placeholders keep the advertised local dry-run working without CI
        # env, git writes, or a real push.
        repo = os.environ.get("GITHUB_REPOSITORY", "<GITHUB_REPOSITORY>")
        run_url = os.environ.get("GITHUB_RUN_URL", "<GITHUB_RUN_URL>")
        head_sha = os.environ.get("GITHUB_HEAD_SHA")
        body = build_pr_body(
            proposal,
            detection,
            repo,
            branch,
            run_url,
            logo_committed=logo_committed,
            head_sha=head_sha,
        )
        print(f"would create branch {branch} and open a PR on {repo}")
        print(body)
        return 0

    run(["git", "checkout", "-B", branch])
    run(
        ["git", "add", *(str(path) for path in git_add_paths(proposal, args.repo_root))]
    )
    run(
        [
            "git",
            "commit",
            "-m",
            commit_message(proposal, args.co_author, args.co_author_id),
        ]
    )
    # The branch is fully regenerated from validated artifacts each run, so
    # force pushing keeps retries idempotent when the branch (and its PR)
    # already exist from a previous pipeline run. The lease expectation must
    # be explicit: the checkout only fetched the default branch, so no
    # remote-tracking ref exists for --force-with-lease to verify against.
    listing = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_sha = listing.stdout.split()[0] if listing.stdout.strip() else ""
    run(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
            "origin",
            branch,
        ]
    )

    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = os.environ["GITHUB_RUN_URL"]
    # The commit just pushed is HEAD of the current branch.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    head_sha = head.stdout.strip()
    entry_line_count = _file_line_count(args.repo_root / paths["site_md"])
    # Submissions that create or update a developer profile deep-link it
    # with the same file-viewer range.
    profile_line_count = (
        _file_line_count(args.repo_root / paths["developer_md"])
        if "developer_md" in paths
        else None
    )
    body = build_pr_body(
        proposal,
        detection,
        repo,
        branch,
        run_url,
        logo_committed=logo_committed,
        head_sha=head_sha,
        entry_line_count=entry_line_count,
        profile_line_count=profile_line_count,
    )
    body_file.write_text(body, encoding="utf-8")

    # The PR label may not exist yet in the repository.
    run(["gh", "label", "create", PR_LABEL, "--color", "1d76db", "--force"])
    # A retried submission (issue reopened) may already have an open PR for
    # the branch; update it in place instead of failing.
    result = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
        check=True,
        capture_output=True,
        text=True,
    )
    existing = json.loads(result.stdout or "[]")
    if existing:
        pr_url = existing[0]["url"]
        run(["gh", "pr", "edit", pr_url, "--body-file", str(body_file)])
    else:
        # gh pr create prints the PR URL on stdout — capture it for the issue comment.
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"New site submission: {proposal.site_title}",
                "--body-file",
                str(body_file),
                "--head",
                branch,
                "--label",
                PR_LABEL,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pr_url = result.stdout.strip().splitlines()[-1]
    # Retries must not stack duplicate comments on the issue: edit the
    # bot's most recent comment, creating one only if none exists yet.
    run(
        [
            "gh",
            "issue",
            "comment",
            str(proposal.issue_number),
            "--body",
            build_pr_comment(proposal, pr_url, run_url),
            "--edit-last",
            "--create-if-none",
        ]
    )
    run(["gh", "label", "create", PR_CREATED_LABEL, "--color", "0e8a16", "--force"])
    run(
        [
            "gh",
            "issue",
            "edit",
            str(proposal.issue_number),
            "--add-label",
            PR_CREATED_LABEL,
        ]
    )
    body_file.unlink(missing_ok=True)
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, *argv = sys.argv[1:]
    try:
        if command == "validate":
            return cmd_validate(argv)
        if command == "render":
            return cmd_render(argv)
        if command == "publish":
            return cmd_publish(argv)
    except Rejection as rejection:  # defensive: cmd_validate already handles it
        print(json.dumps({"reasons": rejection.reasons}), file=sys.stderr)
        return REJECTION_EXIT
    except Exception:
        # Unexpected errors must stay distinguishable from a form rejection:
        # exit 1 with the traceback on stderr (never 2, never partial JSON on
        # stdout) so callers can triage crashes instead of closing on them.
        traceback.print_exc()
        return ERROR_EXIT
    print(f"Unknown command: {command}", file=sys.stderr)
    return ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
