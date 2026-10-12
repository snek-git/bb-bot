"""Short-link expansion (vm.tiktok.com, reddit /s/ share links, ...). Owned by track A."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)

# Share links redirect browsers to the real post; bot-looking user agents get blocked or a login page.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"}
_TIMEOUT = aiohttp.ClientTimeout(total=10)

_REDDIT_SHARE = re.compile(r"/r/[^/]+/s/[^/]+/?", re.I)
_TIKTOK_SHORT = re.compile(r"/t/[^/]+/?", re.I)
_INSTAGRAM_SHARE = re.compile(r"/share/(?:(?:p|reels?)/)?[^/]+/?", re.I)


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def needs_expansion(url: str) -> bool:
    """True for short/share links that must be followed before sites.match can use them."""
    try:
        split = urlsplit(url)
    except ValueError:
        return False
    host = (split.hostname or "").rstrip(".").lower()
    path = split.path
    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        return path.strip("/") != ""
    if _host_is(host, "tiktok.com"):
        return bool(_TIKTOK_SHORT.fullmatch(path))
    if _host_is(host, "reddit.com"):
        return bool(_REDDIT_SHARE.fullmatch(path))
    if _host_is(host, "instagram.com"):
        return bool(_INSTAGRAM_SHARE.fullmatch(path))
    return False


async def _follow(session: aiohttp.ClientSession, method: str, url: str) -> tuple[int, str]:
    async with session.request(
        method, url, allow_redirects=True, headers=_HEADERS, timeout=_TIMEOUT
    ) as resp:
        return resp.status, str(resp.url)


async def expand(url: str, session: aiohttp.ClientSession) -> str:
    """Follow redirects and return the final URL. Returns the input unchanged on any failure."""
    try:
        status, final = await _follow(session, "HEAD", url)
        if status < 400 and final != url:
            return final
    except Exception as e:  # any network/HTTP failure -> fall back to GET
        log.debug("HEAD %s failed: %r", url, e)
    try:
        # The final page may well be a 403/login wall; its URL is still the post we want.
        _, final = await _follow(session, "GET", url)
        return final
    except Exception as e:
        log.debug("GET %s failed: %r", url, e)
        return url
