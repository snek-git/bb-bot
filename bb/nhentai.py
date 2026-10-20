"""nhentai gallery cards. nhentai's gallery pages sit behind a Cloudflare challenge, so Discord can't
preview them, but the official API (nhentai.net/api/v2) and the image CDN aren't challenged: BB builds
the card from those, like the e-hentai ones.

House rules from https://nhentai.net/api/v2/docs:

* Authenticate with an API key (NHENTAI_API_KEY, header ``Authorization: Key <key>``). Anonymous callers
  get 20 gallery reads a minute per IP and "may get stricter still"; a key gets 45.
* Take the CDN hosts from /api/v2/cdn, never hardcode them, and use media paths exactly as the API
  returns them: the CDN bans clients that request URLs it doesn't recognise.
* Treat 429 as a backoff signal. It comes with Retry-After (60 seconds when seen in 2026-10).

Galleries tagged lolicon, shotacon or toddlercon are skipped entirely: no card, no record. That rule has
no setting.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from bb import cards
from bb.cards import Cover
from bb.links import extract
from bb.links.model import LinkMatch, Site

log = logging.getLogger(__name__)

SITE_ID = "nhentai"
SITE = Site(id=SITE_ID, name="nhentai", label="Gallery", fixers=(), test_url="")
API_URL = "https://nhentai.net/api/v2"
MAX_GALLERIES = 4
API_TIMEOUT = aiohttp.ClientTimeout(total=10)
OPEN_LABEL = "Open on nhentai"

# Gallery pages and reader pages (/g/<id>/<page>/) alike.
_GALLERY = re.compile(r"https?://(?:www\.)?nhentai\.net/g/(?P<id>\d{1,9})(?=/|$|[?#])", re.IGNORECASE)
BLOCKED_TAGS = frozenset({"lolicon", "shotacon", "toddlercon"})
SHOWN_TYPES = (
    ("parody", "Parody"),
    ("character", "Characters"),
    ("artist", "Artist"),
    ("group", "Group"),
    ("tag", "Tags"),
)

META_TTL = 3600.0
CDN_TTL = 6 * 3600.0
DEFAULT_BACKOFF = 60.0
_META_CACHE_SIZE = 64
_COVER_CACHE_SIZE = 16

_api_key: str | None = None
_backoff_until = 0.0  # monotonic time; no API or CDN requests before it
_meta_cache: dict[int, tuple[float, dict[str, Any]]] = {}  # gid -> (fetched at, metadata)
_cdn: tuple[float, list[str], list[str]] | None = None  # (fetched at, image servers, thumbnail servers)
_cover_cache: dict[int, tuple[bytes, str]] = {}


def set_api_key(key: str | None) -> None:
    global _api_key
    _api_key = (key or "").strip() or None


def _now() -> float:
    return time.monotonic()


@dataclass(frozen=True)
class Gallery:
    gid: int
    spoiler: bool = False

    @property
    def link(self) -> str:
        """The gallery page, whatever form was posted (reader pages, query strings, www.)."""
        return f"https://nhentai.net/g/{self.gid}/"

    @property
    def canonical_key(self) -> str:
        return f"{SITE_ID}:{self.gid}"

    def as_match(self) -> LinkMatch:
        """A LinkMatch, so reruns and quips can treat galleries like any other link."""
        return LinkMatch(site=SITE, url=self.link, canonical_key=self.canonical_key, spoiler=self.spoiler)


def find_galleries(content: str) -> list[Gallery]:
    """Gallery links Discord would preview (code blocks and <url> skipped), deduped, at most 4."""
    found: list[Gallery] = []
    seen: set[int] = set()
    for url in extract.extract_urls(content):
        m = _GALLERY.match(url.url)
        if not m:
            continue
        gid = int(m["id"])
        if gid in seen:
            continue
        seen.add(gid)
        found.append(Gallery(gid, url.spoiler))
        if len(found) >= MAX_GALLERIES:
            break
    return found


def is_blocked(meta: dict[str, Any]) -> bool:
    """Any blocked tag, whatever its type."""
    tags = meta.get("tags")
    if not isinstance(tags, list):
        return False
    return any(isinstance(t, dict) and str(t.get("name", "")).strip().lower() in BLOCKED_TAGS for t in tags)


# --- API --------------------------------------------------------------------------------------


def _remember(cache: dict[Any, Any], key: Any, value: Any, size: int) -> None:
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > size:
        cache.pop(next(iter(cache)))


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Key {_api_key}"} if _api_key else {}


def backing_off() -> bool:
    return _now() < _backoff_until


def _back_off(resp: aiohttp.ClientResponse, what: str) -> None:
    global _backoff_until
    try:
        seconds = max(1.0, float(resp.headers.get("Retry-After", "")))
    except ValueError:
        seconds = DEFAULT_BACKOFF
    _backoff_until = max(_backoff_until, _now() + seconds)
    log.warning("nhentai %s answered 429: no requests for %.0fs", what, seconds)


async def _get_json(session: aiohttp.ClientSession, path: str) -> Any:
    """GET an API path and decode it. None on errors, non-200s and 429s (which start the backoff).
    A rejected API key is dropped for good and the request retried without it: a typo in the key
    costs the higher rate limit, not every card."""
    global _api_key
    async with session.get(f"{API_URL}{path}", headers=_api_headers(), timeout=API_TIMEOUT) as resp:
        status = resp.status
        if status == 200:
            return await resp.json(content_type=None)
        if status == 429:
            _back_off(resp, "API")
            return None
    if status == 401 and _api_key:
        log.warning("nhentai rejected the API key (check NHENTAI_API_KEY): carrying on without it")
        _api_key = None
        return await _get_json(session, path)
    level = logging.INFO if status == 404 else logging.WARNING  # 404: deleted or hidden gallery
    log.log(level, "nhentai API answered %s for %s", status, path)
    return None


async def _fetch_gallery(session: aiohttp.ClientSession, gid: int) -> dict[str, Any] | None:
    try:
        data = await _get_json(session, f"/galleries/{gid}")
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("nhentai API failed for gallery %s: %r", gid, exc)
        return None
    if not isinstance(data, dict) or data.get("id") != gid:
        return None
    return data


async def fetch_metadata(
    session: aiohttp.ClientSession, galleries: list[Gallery]
) -> dict[int, dict[str, Any]]:
    """gid -> metadata for every gallery the API answered. There's no batch call, so it's one request
    per gallery, cached for an hour; none at all while backing off from a 429. Never raises."""
    out: dict[int, dict[str, Any]] = {}
    missing: list[Gallery] = []
    started = _now()
    for g in galleries:
        hit = _meta_cache.get(g.gid)
        if hit is not None and started - hit[0] < META_TTL:
            out[g.gid] = hit[1]
        else:
            missing.append(g)
    if not missing:
        return out
    if backing_off():
        log.info("nhentai: backing off, no card for %d gallery link(s)", len(missing))
        return out
    fetched = await asyncio.gather(*(_fetch_gallery(session, g.gid) for g in missing))
    for g, meta in zip(missing, fetched, strict=True):
        if meta is not None:
            out[g.gid] = meta
            _remember(_meta_cache, g.gid, (started, meta), _META_CACHE_SIZE)
    return out


def _servers(data: Any, key: str) -> list[str]:
    found = data.get(key) if isinstance(data, dict) else None
    if not isinstance(found, list):
        return []
    return [s.rstrip("/") for s in found if isinstance(s, str) and s.startswith("https://")]


async def cdn_servers(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """(image servers, thumbnail servers) from /api/v2/cdn, cached for hours. Only fetched once a cover
    is actually wanted. A failed refresh keeps the old list; with none at all, both are empty."""
    global _cdn
    if _cdn is not None and _now() - _cdn[0] < CDN_TTL:
        return _cdn[1], _cdn[2]
    data = None
    if not backing_off():
        try:
            data = await _get_json(session, "/cdn")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("nhentai CDN list failed: %r", exc)
    images, thumbs = _servers(data, "image_servers"), _servers(data, "thumb_servers")
    if images or thumbs:
        _cdn = (_now(), images, thumbs)
    return (_cdn[1], _cdn[2]) if _cdn is not None else ([], [])


def _media_url(servers: list[str], gid: int, info: Any) -> str | None:
    """A CDN URL for an API media entry ({"path": ...}). The path is used exactly as returned."""
    path = info.get("path") if isinstance(info, dict) else None
    if not servers or not isinstance(path, str) or not path or "://" in path:
        return None
    return f"{servers[gid % len(servers)]}/{path.lstrip('/')}"


def thumbnail_url(meta: dict[str, Any], gid: int) -> str | None:
    """The API's small thumbnail on a thumbnail server, from the cached CDN list (None before the first
    answer). The card's fallback when the full-size cover couldn't be uploaded."""
    return _media_url(_cdn[2], gid, meta.get("thumbnail")) if _cdn is not None else None


# --- full-size cover ---------------------------------------------------------------------

COVER_TIMEOUT = 20.0
COVER_MAX_BYTES = 8 * 1024 * 1024


async def _get_image(session: aiohttp.ClientSession, url: str, max_bytes: int) -> tuple[bytes, str] | None:
    async with session.get(url) as resp:
        if resp.status == 429:
            _back_off(resp, "CDN")
            return None
        return await cards.read_image(resp, max_bytes)


async def fetch_cover(
    session: aiohttp.ClientSession, gallery: Gallery, meta: dict[str, Any], max_bytes: int
) -> Cover | None:
    """The gallery's first page at full size, uploaded with the card like the e-hentai ones (the image
    servers would also take a direct link, but whether Discord's media proxy gets through to them is
    unverified). None on any failure."""
    limit = min(max_bytes, COVER_MAX_BYTES)
    cached = _cover_cache.get(gallery.gid)
    if cached is not None and len(cached[0]) <= limit:
        return Cover(cached[0], f"cover.{cached[1]}")
    pages = meta.get("pages")
    if not isinstance(pages, list) or not pages or backing_off():
        return None
    try:
        async with asyncio.timeout(COVER_TIMEOUT):
            images, _ = await cdn_servers(session)
            url = _media_url(images, gallery.gid, pages[0])
            got = await _get_image(session, url, limit) if url else None
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("cover for nhentai gallery %s failed: %r", gallery.gid, exc)
        return None
    if got is None:
        log.info("no full-size cover for nhentai gallery %s", gallery.gid)
        return None
    _remember(_cover_cache, gallery.gid, got, _COVER_CACHE_SIZE)
    return Cover(got[0], f"cover.{got[1]}")


# --- the card --------------------------------------------------------------------------------


def _names(meta: dict[str, Any], tag_type: str) -> list[str]:
    tags = meta.get("tags")
    if not isinstance(tags, list):
        return []
    return [
        str(t["name"]) for t in tags if isinstance(t, dict) and t.get("type") == tag_type and t.get("name")
    ]


def _title(meta: dict[str, Any], gallery: Gallery) -> str:
    titles = meta.get("title")
    if isinstance(titles, dict):
        for key in ("english", "pretty", "japanese"):
            title = titles.get(key)
            if isinstance(title, str) and title.strip():
                return title.strip()
    return f"Gallery {gallery.gid}"


def tag_lines(meta: dict[str, Any]) -> list[str]:
    lines = []
    for tag_type, label in SHOWN_TYPES:
        names = _names(meta, tag_type)
        if names:
            lines.append(f"**{label}:** {cards.clip(', '.join(names))}")
    return lines


def card_text(meta: dict[str, Any], gallery: Gallery) -> str:
    category = ", ".join(n.title() for n in _names(meta, "category"))
    languages = ", ".join(n.title() for n in _names(meta, "language") if n.lower() != "translated")
    stats = [category, languages, f"{meta.get('num_pages', '?')} pages"]
    favorites = meta.get("num_favorites")
    if isinstance(favorites, int) and favorites > 0:
        stats.append(f"♥ {favorites:,}")
    return cards.card_body(_title(meta, gallery), gallery.link, stats, tag_lines(meta))


def build_card(
    meta: dict[str, Any], gallery: Gallery, *, cover: Cover | None, quip: str | None = None
) -> discord.ui.LayoutView:
    """The card, with the uploaded full-size first page on top, else the API's small thumbnail."""
    media = f"attachment://{cover.filename}" if cover else thumbnail_url(meta, gallery.gid)
    return cards.card_view(
        card_text(meta, gallery),
        link=gallery.link,
        label=OPEN_LABEL,
        media=media,
        spoiler=gallery.spoiler,
        quip=quip,
    )
