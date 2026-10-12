"""Live fixer probes: every fixer must serve a preview for its site's test post.

Same pass criterion as the health checker (PLAN, Health checks): Discord's crawler user agent,
HTTP 200, and at least one og:/twitter: meta tag. FxEmbed fixers (fxtwitter, fixupx, fxbsky) put
media behind an activity+json link rather than og:image, so og:image is deliberately not required.

Run with: uv run pytest tests/test_links_live.py -m live
"""

from __future__ import annotations

import re

import aiohttp
import pytest

from bb.links import sites
from bb.links.model import Fixer, Site

pytestmark = pytest.mark.live

DISCORD_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
META = re.compile(r"<meta\b[^>]*\b(?:property|name)\s*=\s*[\"'](?:og|twitter):", re.I)
TIMEOUT = aiohttp.ClientTimeout(total=30)

FIXERS = [(site, fixer) for site in sites.SITES.values() for fixer in site.fixers]
TRANSLATABLE = [(site, fixer) for site, fixer in FIXERS if fixer.translatable]


async def _probe(url: str) -> tuple[int, str]:
    async with (
        aiohttp.ClientSession(headers={"User-Agent": DISCORD_UA}, timeout=TIMEOUT) as session,
        session.get(url, allow_redirects=True) as resp,
    ):
        return resp.status, await resp.text(errors="replace")


def _fixed(site: Site, fixer: Fixer, lang: str | None = None) -> str:
    m = sites.match(site.test_url)
    assert m is not None, site.test_url
    return sites.fixed_url(m, fixer, lang)


@pytest.mark.parametrize(("site", "fixer"), FIXERS, ids=[f.id for _, f in FIXERS])
async def test_fixer_serves_preview(site: Site, fixer: Fixer) -> None:
    url = _fixed(site, fixer)
    status, body = await _probe(url)
    assert status == 200, f"{url} -> HTTP {status}"
    assert META.search(body), f"{url} -> no og:/twitter: meta tags"


@pytest.mark.parametrize(("site", "fixer"), TRANSLATABLE, ids=[f.id for _, f in TRANSLATABLE])
async def test_translated_fixer_serves_preview(site: Site, fixer: Fixer) -> None:
    url = _fixed(site, fixer, "es")
    assert url.endswith("/es")
    status, body = await _probe(url)
    assert status == 200, f"{url} -> HTTP {status}"
    assert META.search(body), f"{url} -> no og:/twitter: meta tags"
