"""Live nhentai probe: the v2 API still answers anonymously with what cards use, and the CDN serves page 1.
Gallery 450000 carries none of the blocked tags.

Run with: uv run pytest tests/test_nhentai_live.py -m live
"""

from __future__ import annotations

import aiohttp
import pytest

from bb import nhentai

pytestmark = pytest.mark.live

GALLERY = nhentai.Gallery(450000)


async def test_api_and_cover():
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers={"User-Agent": "BB-bot tests"}, timeout=timeout) as session:
        meta = (await nhentai.fetch_metadata(session, [GALLERY]))[GALLERY.gid]
        assert not nhentai.is_blocked(meta)
        assert meta["num_pages"] > 0
        assert nhentai.card_text(meta, GALLERY).startswith("### [")
        cover = await nhentai.fetch_cover(session, GALLERY, meta, 8 * 1024 * 1024)
        assert cover is not None and len(cover.data) > 10_000
        assert (nhentai.thumbnail_url(meta, GALLERY.gid) or "").startswith("https://")
