"""e-hentai gallery cards. e-hentai pages have no preview tags and no fixer service exists, so BB builds
the card herself from the official API (api.e-hentai.org, method "gdata").
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from bb import cards
from bb.cards import Cover
from bb.links import extract
from bb.links.model import LinkMatch, Site

log = logging.getLogger(__name__)

SITE_ID = "ehentai"
SITE = Site(id=SITE_ID, name="e-hentai", label="Gallery", fixers=(), test_url="")
API_URL = "https://api.e-hentai.org/api.php"
MAX_GALLERIES = 4
API_TIMEOUT = aiohttp.ClientTimeout(total=10)

_GALLERY = re.compile(
    r"https?://(?:www\.)?(?P<host>e-hentai\.org|exhentai\.org)/g/(?P<gid>\d{1,10})/(?P<token>[0-9a-f]{10})\b",
    re.IGNORECASE,
)
SHOWN_NAMESPACES = (
    ("parody", "Parody"),
    ("character", "Characters"),
    ("artist", "Artist"),
    ("group", "Group"),
)
OTHER_NAMESPACES = ("female", "male", "mixed", "other")


@dataclass(frozen=True)
class Gallery:
    gid: int
    token: str
    url: str  # e-hentai URL: what BB fetches (the API and cover pages live there)
    spoiler: bool = False
    exhentai: bool = False  # posted as an ExHentai link; some galleries exist only there

    @property
    def canonical_key(self) -> str:
        return f"{SITE_ID}:{self.gid}"

    @property
    def link(self) -> str:
        """Where the card's title and button point: ExHentai links stay on ExHentai."""
        return f"https://exhentai.org/g/{self.gid}/{self.token}/" if self.exhentai else self.url

    @property
    def open_label(self) -> str:
        return "Open on ExHentai" if self.exhentai else "Open on e-hentai"

    def as_match(self) -> LinkMatch:
        """A LinkMatch, so reruns and quips can treat galleries like any other link."""
        return LinkMatch(site=SITE, url=self.url, canonical_key=self.canonical_key, spoiler=self.spoiler)


def find_galleries(content: str) -> list[Gallery]:
    """Gallery links Discord would preview (code blocks and <url> skipped), deduped, at most 4."""
    found: list[Gallery] = []
    seen: set[int] = set()
    for url in extract.extract_urls(content):
        m = _GALLERY.match(url.url)
        if not m:
            continue
        gid = int(m["gid"])
        if gid in seen:
            continue
        seen.add(gid)
        token = m["token"].lower()
        ex = m["host"].lower() == "exhentai.org"
        found.append(Gallery(gid, token, f"https://e-hentai.org/g/{gid}/{token}/", url.spoiler, ex))
        if len(found) >= MAX_GALLERIES:
            break
    return found


def parse_gdata(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """gid -> metadata for every gallery the API answered without an error."""
    out: dict[int, dict[str, Any]] = {}
    for meta in payload.get("gmetadata") or ():
        if not isinstance(meta, dict) or meta.get("error") or "gid" not in meta:
            continue
        out[int(meta["gid"])] = meta
    return out


async def fetch_metadata(
    session: aiohttp.ClientSession, galleries: list[Gallery]
) -> dict[int, dict[str, Any]]:
    """One API call for up to 25 galleries. Returns {} on any failure; never raises."""
    if not galleries:
        return {}
    body = {"method": "gdata", "gidlist": [[g.gid, g.token] for g in galleries], "namespace": 1}
    try:
        async with session.post(API_URL, json=body, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("e-hentai API answered %s", resp.status)
                return {}
            return parse_gdata(await resp.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("e-hentai API failed: %r", exc)
        return {}


def tag_lines(tags: list[str]) -> list[str]:
    by_ns: dict[str, list[str]] = {}
    for tag in tags:
        ns, _, name = str(tag).rpartition(":")
        by_ns.setdefault(ns or "other", []).append(name)
    lines = [
        f"**{label}:** {cards.clip(', '.join(by_ns[ns]))}" for ns, label in SHOWN_NAMESPACES if by_ns.get(ns)
    ]
    rest = [name for ns in OTHER_NAMESPACES for name in by_ns.get(ns, [])]
    if rest:
        lines.append(f"**Tags:** {cards.clip(', '.join(rest))}")
    return lines


# --- full-size cover ---------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Cookie": "nw=1",  # skips the "this gallery may contain offensive content" interstitial
}
COVER_TIMEOUT = 20.0
COVER_MAX_BYTES = 8 * 1024 * 1024
_IMG = re.compile(r'<img id="img" src="([^"]+)"')
_cover_cache: dict[int, tuple[bytes, str]] = {}
_CACHE_SIZE = 16


def first_page_url(gallery_html: str, gid: int) -> str | None:
    m = re.search(rf"https://e-hentai\.org/s/[0-9a-f]{{10}}/{gid}-1(?![0-9])", gallery_html)
    return m.group(0) if m else None


def page_image_url(page_html: str) -> str | None:
    m = _IMG.search(page_html)
    return html.unescape(m.group(1)) if m else None


async def _get_text(session: aiohttp.ClientSession, url: str) -> str | None:
    async with session.get(url, headers=BROWSER_HEADERS) as resp:
        return await resp.text(errors="replace") if resp.status == 200 else None


async def _get_image(session: aiohttp.ClientSession, url: str, max_bytes: int) -> tuple[bytes, str] | None:
    async with session.get(url, headers={"User-Agent": BROWSER_HEADERS["User-Agent"]}) as resp:
        return await cards.read_image(resp, max_bytes)


async def fetch_cover(
    session: aiohttp.ClientSession, gallery: Gallery, meta: dict[str, Any], max_bytes: int
) -> Cover | None:
    """The gallery's first page at full size: gallery page -> page 1 -> image. None on any failure.

    The image is uploaded with the card rather than linked, because e-hentai's image links expire.
    meta is unused: the API doesn't list pages, so they're scraped."""
    limit = min(max_bytes, COVER_MAX_BYTES)
    cached = _cover_cache.get(gallery.gid)
    if cached is not None and len(cached[0]) <= limit:
        return Cover(cached[0], f"cover.{cached[1]}")
    try:
        async with asyncio.timeout(COVER_TIMEOUT):
            gallery_html = await _get_text(session, gallery.url)
            page = first_page_url(gallery_html or "", gallery.gid)
            page_html = await _get_text(session, page) if page else None
            image = page_image_url(page_html or "")
            got = await _get_image(session, image, limit) if image else None
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("cover for gallery %s failed: %r", gallery.gid, exc)
        return None
    if got is None:
        log.info("no full-size cover for gallery %s", gallery.gid)
        return None
    _cover_cache[gallery.gid] = got
    while len(_cover_cache) > _CACHE_SIZE:
        _cover_cache.pop(next(iter(_cover_cache)))
    return Cover(got[0], f"cover.{got[1]}")


# --- the card --------------------------------------------------------------------------------


def card_text(meta: dict[str, Any], gallery: Gallery) -> str:
    title = html.unescape(str(meta.get("title") or meta.get("title_jpn") or f"Gallery {gallery.gid}"))
    stats = [str(meta.get("category") or "").strip(), f"{meta.get('filecount', '?')} pages"]
    if meta.get("rating"):
        stats.append(f"★ {meta['rating']}")
    return cards.card_body(title, gallery.link, stats, tag_lines(list(meta.get("tags") or ())))


def build_card(
    meta: dict[str, Any], gallery: Gallery, *, cover: Cover | None, quip: str | None = None
) -> discord.ui.LayoutView:
    """The card, with the uploaded full-size first page on top, else the API's small thumbnail."""
    thumb = meta.get("thumb")
    if cover:
        media: str | None = f"attachment://{cover.filename}"
    else:
        media = thumb if isinstance(thumb, str) and thumb.startswith("https://") else None
    return cards.card_view(
        card_text(meta, gallery),
        link=gallery.link,
        label=gallery.open_label,
        media=media,
        spoiler=gallery.spoiler,
        quip=quip,
    )
