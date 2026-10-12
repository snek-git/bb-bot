"""Live upload-fallback checks (one real post per site): uv run pytest -m live tests/test_upload_live.py"""

from __future__ import annotations

import aiohttp
import pytest

from bb.links.model import LinkMatch, Site
from bb.upload import fetch_media

pytestmark = pytest.mark.live

LIMIT = 10 * 1024 * 1024  # Discord's default upload limit

# (site id, canonical key, url, minimum files expected). Old, stable posts where possible.
CASES = [
    ("twitter", "twitter:440322224407314432", "https://x.com/TheEllenShow/status/440322224407314432", 1),
    ("twitter", "twitter:1578353380363501568", "https://x.com/DavidToons_/status/1578353380363501568", 1),
    ("pixiv", "pixiv:78127132", "https://www.pixiv.net/en/artworks/78127132", 4),  # 5 pages, capped at 4
    ("reddit", "reddit:haucpf", "https://www.reddit.com/r/pics/comments/haucpf/", 1),
    ("reddit", "reddit:1qnfjqx", "https://www.reddit.com/r/comics/comments/1qnfjqx/", 2),  # gallery
    ("reddit", "reddit:6rrwyj", "https://www.reddit.com/r/videos/comments/6rrwyj/", 1),  # v.redd.it
    ("tiktok", "tiktok:7047596209028074758", "https://tiktok.com/@hankgreen1/video/7047596209028074758", 1),
    ("bluesky", "bsky:bsky.app/3l3vgf77uco2g", "https://bsky.app/profile/bsky.app/post/3l3vgf77uco2g", 1),
    (
        "bluesky",
        "bsky:did:plc:z72i7hdynmk6r22z27h6tvur/3mpok7nkjtc2o",
        "https://bsky.app/profile/did:plc:z72i7hdynmk6r22z27h6tvur/post/3mpok7nkjtc2o",
        1,
    ),
]


def _match(site_id: str, key: str, url: str) -> LinkMatch:
    return LinkMatch(site=Site(site_id, site_id, site_id, (), ""), url=url, canonical_key=key)


@pytest.mark.parametrize(("site_id", "key", "url", "at_least"), CASES, ids=[c[1] for c in CASES])
async def test_fetch_real_post(tmp_path, site_id, key, url, at_least):
    async with aiohttp.ClientSession() as session:
        m = _match(site_id, key, url)
        files = await fetch_media(m, max_bytes=LIMIT, session=session, workdir=tmp_path)
    assert len(files) >= at_least, f"{key}: got {files}"
    assert len(files) <= 4
    for f in files:
        assert f.path.parent == tmp_path and f.path.is_file()
        assert 0 < f.size <= LIMIT and f.path.stat().st_size == f.size
        assert f.filename.rsplit(".", 1)[-1] in {"jpg", "png", "gif", "webp", "mp4", "mov", "webm"}


async def test_instagram_fails_cleanly_or_works(tmp_path):
    # Usually needs cookies; either outcome is fine as long as it doesn't raise.
    async with aiohttp.ClientSession() as session:
        m = _match("instagram", "instagram:BQ0eAlwhDrw", "https://www.instagram.com/p/BQ0eAlwhDrw/")
        files = await fetch_media(m, max_bytes=LIMIT, session=session, workdir=tmp_path)
    assert isinstance(files, list) and len(files) <= 4
