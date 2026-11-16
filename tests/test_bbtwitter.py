from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bb import bbtwitter
from bb.links import sites

# FxTwitter's preview page for a real tweet (2026-11), as Discord gets it, trimmed of its ASCII-art banner.
PAGE = (
    '<!DOCTYPE html><html lang="en"><head>'
    '<link rel="canonical" href="https://x.com/linluyun_rin/status/2122140096984215569"/>'
    '<meta property="theme-color" content="#6363ff"/>'
    '<meta property="og:title" content="鹿隱於雲 (@linluyun_rin)"/>'
    '<meta property="og:site_name" content="FxTwitter"/>'
    "<link href='https://assets.fxembed.com/logos/fxtwitter.svg' rel='icon' sizes='svgxsvg' "
    "type='image/svg+xml'>"
    "<link href='https://assets.fxembed.com/logos/fxtwitter64.png' rel='icon' sizes='64x64' type='image/png'>"
    '<link rel="alternate" href="https://fxtwitter.com/owoembed?text=%F0%9F%92%AC%205&status=2122140096984215569'
    '&author=linluyun_rin" type="application/json+oembed" title="鹿隱於雲">'
    "<link href='https://fxtwitter.com/users/linluyun_rin/statuses/6608666766545354' rel='alternate' "
    "type='application/activity+json'></head><body></body></html>"
)
OEMBED = {
    "author_name": "💬 5   🔁 2.4K", "author_url": "https://x.com/linluyun_rin/status/2122140096984215569",
    "provider_name": "FxTwitter", "provider_url": "https://github.com/FxEmbed/FxEmbed", "type": "rich",
}
STATUS = {"id": "2122140096984215569", "media_attachments": [
    {"type": "image", "url": "https://gif.fxtwitter.com/tweet_video/HXNbE-kaYAEP6_o.webp"}]}


def test_page_gets_bbs_name_icon_and_links():
    page = bbtwitter.rebrand_page(PAGE)
    assert '<meta property="og:site_name" content="BBTwitter"/>' in page
    assert "assets.fxembed.com" not in page and "FxTwitter" not in page
    assert page.count("rel='icon'") == 1 and "https://bbtwitter.snek.page/icon.png" in page
    assert 'href="https://bbtwitter.snek.page/owoembed?text=' in page
    assert "href='https://bbtwitter.snek.page/users/linluyun_rin/statuses/6608666766545354'" in page
    assert '<meta property="og:title" content="鹿隱於雲 (@linluyun_rin)"/>' in page  # the rest is untouched


def test_oembed_names_bb_as_provider():
    data = json.loads(bbtwitter.rebrand_oembed(json.dumps(OEMBED)))
    assert (data["provider_name"], data["provider_url"]) == ("BBTwitter", "https://bbtwitter.snek.page")
    assert data["author_name"] == "💬 5   🔁 2.4K"


def test_status_keeps_fxtwitters_media_links():
    body = json.dumps(STATUS)
    assert bbtwitter.rebrand("/api/v1/statuses/6608", "application/json", body) == body  # gif.fxtwitter stays


def test_bbtwitter_leads_the_tweet_chain_with_fxtwitters_media_links():
    fixer = sites.chain("twitter")[0]
    assert (fixer.id, fixer.domain) == ("bbtwitter", bbtwitter.DOMAIN)
    m = sites.match("https://x.com/a/status/1")
    assert m is not None
    assert sites.fixed_url(m, fixer) == "https://bbtwitter.snek.page/a/status/1"
    assert sites.fixed_url(m, fixer, lang="en") == "https://bbtwitter.snek.page/a/status/1/en"
    second = sites.media_links(m, fixer, ["photo", "video"])[1]
    assert second == ("video", "https://d.fxtwitter.com/a/status/1/video/2")
    fixed = sites.match("https://bbtwitter.snek.page/a/status/1")  # someone posting BB's link themselves
    assert fixed is not None and fixed.canonical_key == "twitter:1"
    assert fixed.parts.get("already_fixed") == "1"


class FakeUpstream:
    """FxTwitter, answering from a table: path -> (status, content type, body, location)."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.app = web.Application()
        self.app.router.add_get("/{path:.*}", self.answer)
        self.table: dict[str, tuple[int, str, str, str | None]] = {}

    async def answer(self, request: web.Request) -> web.StreamResponse:
        self.asked.append((request.path_qs, dict(request.headers)))
        status, content_type, body, location = self.table.get(request.path, (404, "text/plain", "nope", None))
        headers = {"Location": location} if location else {}
        return web.Response(status=status, text=body, content_type=content_type, headers=headers)


@pytest.fixture
async def bbx(monkeypatch):
    upstream = FakeUpstream()
    up = TestServer(upstream.app)
    await up.start_server()
    monkeypatch.setattr(bbtwitter, "UPSTREAM", str(up.make_url("")).rstrip("/"))
    async with TestClient(TestServer(web.Application())) as plain:  # just for its session
        server = bbtwitter.Server(plain.session)
        async with TestClient(TestServer(server.app)) as client:
            yield upstream, client
    await up.close()


async def test_relays_the_page_rebranded_and_passes_the_bots_user_agent(bbx):
    upstream, client = bbx
    upstream.table["/linluyun_rin/status/2122140096984215569"] = (200, "text/html", PAGE, None)
    path = "/linluyun_rin/status/2122140096984215569"
    resp = await client.get(path, headers={"User-Agent": "Discordbot/2.0"})
    assert resp.status == 200 and resp.content_type == "text/html"
    assert 'content="BBTwitter"' in await resp.text()
    assert upstream.asked[0][1]["User-Agent"] == "Discordbot/2.0"


async def test_browsers_get_fxtwitters_redirect_to_x(bbx):
    upstream, client = bbx
    upstream.table["/a/status/1"] = (302, "text/plain", "", "https://x.com/a/status/1")
    resp = await client.get("/a/status/1", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "https://x.com/a/status/1"


async def test_relays_oembed_with_its_query(bbx):
    upstream, client = bbx
    upstream.table["/owoembed"] = (200, "application/json", json.dumps(OEMBED), None)
    resp = await client.get("/owoembed?text=hi&status=1&author=a")
    assert (await resp.json())["provider_name"] == "BBTwitter"
    assert upstream.asked[0][0] == "/owoembed?text=hi&status=1&author=a"


async def test_serves_the_icon_and_refuses_anything_not_tweet_shaped(bbx):
    upstream, client = bbx
    resp = await client.get("/icon.png")
    assert resp.status == 200 and resp.content_type == "image/png"
    for path in ("/", "/robots.txt", "/a/status/1/../../admin", "/http://evil.example/"):
        assert (await client.get(path)).status == 404
    assert upstream.asked == []  # never passed on
