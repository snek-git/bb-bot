"""Offline tests for the upload fallback. All HTTP goes to a local aiohttp server; yt-dlp is faked."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bb import upload
from bb.links.model import LinkMatch, Site
from bb.upload import MediaFile, fetch_media

MB = 1024 * 1024
JPEG = b"\xff\xd8\xff" + b"j" * 1000
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000


def match(site_id: str, key: str, url: str = "") -> LinkMatch:
    return LinkMatch(site=Site(site_id, site_id, site_id, (), ""), url=url, canonical_key=key)


@dataclass
class Http:
    base: str
    session: aiohttp.ClientSession
    routes: dict = field(default_factory=dict)
    hits: list = field(default_factory=list)

    def json(self, path, data, status=200):
        async def handler(request):
            return web.json_response(data, status=status)

        self.routes[path] = handler

    def file(self, path, body, content_type="image/jpeg", *, chunked=False, need=None, delay=0.0):
        async def handler(request):
            if need and any(request.headers.get(k) != v for k, v in need.items()):
                return web.Response(status=403)
            if delay:
                await asyncio.sleep(delay)
            if not chunked:
                return web.Response(body=body, content_type=content_type)
            resp = web.StreamResponse(headers={"Content-Type": content_type})
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            for i in range(0, len(body), 4096):
                await resp.write(body[i : i + 4096])
            await resp.write_eof()
            return resp

        self.routes[path] = handler

    def url(self, path):
        return self.base + path


@pytest.fixture(autouse=True)
def local_hosts_allowed(monkeypatch):
    """The fake servers here live on 127.0.0.1, which the real guard refuses (see the guard tests)."""
    monkeypatch.setattr(upload, "ALLOW_PRIVATE_HOSTS", True)


@pytest.fixture
async def http():
    routes: dict = {}
    hits: list = []

    async def dispatch(request):
        hits.append(request.path_qs)
        handler = routes.get(request.path)
        if handler is None:
            return web.Response(status=404, text="nope")
        return await handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield Http(base=str(server.make_url("")).rstrip("/"), session=session, routes=routes, hits=hits)
    await server.close()


@dataclass
class FakeYtdlp:
    calls: list = field(default_factory=list)
    result: list = field(default_factory=list)


@pytest.fixture
def ytdlp(monkeypatch):
    fake = FakeYtdlp()

    async def fake_ytdlp(ctx, url):
        fake.calls.append(url)
        return fake.result

    monkeypatch.setattr(upload, "_ytdlp", fake_ytdlp)
    return fake


@pytest.fixture
def local(http, monkeypatch, ytdlp):
    """Point every API base at the local server; yt-dlp is faked so nothing leaves the machine."""
    for name, prefix in [
        ("FXTWITTER_API", "/fx"),
        ("VXTWITTER_API", "/vx"),
        ("BSKY_APPVIEW", "/bsky"),
        ("PLC_DIRECTORY", "/plc"),
        ("PIXIV", "/pixiv"),
        ("REDDIT", "/reddit"),
        ("OLD_REDDIT", "/oldreddit"),
        ("I_REDD_IT", "/iredd"),
    ]:
        monkeypatch.setattr(upload, name, http.base + prefix)
    # aiohttp's cookie jar ignores cookies from bare IPs, so the reddit warm-up needs a hostname
    named = http.base.replace("127.0.0.1", "localhost")
    monkeypatch.setattr(upload, "REDDIT", named + "/reddit")
    monkeypatch.setattr(upload, "OLD_REDDIT", named + "/oldreddit")
    return http


async def fetch(http, m, tmp_path, max_bytes=10 * MB):
    return await fetch_media(m, max_bytes=max_bytes, session=http.session, workdir=tmp_path)


def names(files: list[MediaFile]) -> list[str]:
    return [f.filename for f in files]


# --- which post -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("site", "key", "url", "expected"),
    [
        ("twitter", "twitter:1234", "", ("twitter", "1234")),
        ("pixiv", "pixiv:5678", "", ("pixiv", "5678")),
        ("reddit", "reddit:abc12", "", ("reddit", "abc12")),
        ("reddit", "reddit:ABC12", "", ("reddit", "abc12")),
        ("tiktok", "tiktok:7300000000000000000", "", ("tiktok", "7300000000000000000")),
        ("bluesky", "bsky:alice.bsky.social/3l3vgf77uco2g", "", ("bsky", "alice.bsky.social/3l3vgf77uco2g")),
        (
            "bluesky",
            "bsky:did:plc:z72i7hdynmk6r22z27h6tvur/3kabc",
            "",
            ("bsky", "did:plc:z72i7hdynmk6r22z27h6tvur/3kabc"),
        ),
        ("instagram", "instagram:BQ0eAlwhDrw", "", ("instagram", "BQ0eAlwhDrw")),
        # broken keys fall back to the URL, on any host (fixer domains included)
        ("twitter", "twitter:", "https://fxtwitter.com/a/status/99?s=20", ("twitter", "99")),
        ("twitter", "", "https://vxtwitter.com/a/statuses/42", ("twitter", "42")),
        ("pixiv", "pixiv:x", "https://www.phixiv.net/en/artworks/777", ("pixiv", "777")),
        ("pixiv", "", "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=88", ("pixiv", "88")),
        ("reddit", "", "https://rxddit.com/r/pics/comments/Ab12c/title/", ("reddit", "ab12c")),
        ("reddit", "", "https://www.reddit.com/gallery/1qnfjqx", ("reddit", "1qnfjqx")),
        ("tiktok", "", "https://www.vxtiktok.com/@u/video/7123", ("tiktok", "7123")),
        (
            "bluesky",
            "",
            "https://bsky.app/profile/did:plc:abc123/post/3kxyz",
            ("bsky", "did:plc:abc123/3kxyz"),
        ),
        (
            "bluesky",
            "",
            "https://fxbsky.app/profile/bob.example.com/post/3kxyz",
            ("bsky", "bob.example.com/3kxyz"),
        ),
        ("instagram", "", "https://www.ddinstagram.com/reel/C1a-B_c/", ("instagram", "C1a-B_c")),
    ],
)
def test_target(site, key, url, expected):
    assert upload._target(match(site, key, url)) == expected


@pytest.mark.parametrize(
    ("site", "key", "url"),
    [
        ("twitter", "twitter:abc", "https://x.com/someone"),
        ("bluesky", "bsky:alice", "https://bsky.app/profile/alice"),
        ("bluesky", "bsky:/3kabc", ""),
        ("mastodon", "mastodon:1", "https://example.social/@a/1"),
        ("instagram", "instagram:a/b", "https://www.instagram.com/someone/"),
    ],
)
def test_target_rejects_garbage(site, key, url):
    assert upload._target(match(site, key, url)) is None


def test_stem_is_filename_safe():
    assert upload._stem("bsky", "did:plc:abc/3l3vgf77uco2g") == "bsky_3l3vgf77uco2g"
    assert upload._stem("twitter", "1234") == "twitter_1234"


# --- JSON parsing -----------------------------------------------------------------------

FX_VIDEO = {
    "type": "video",
    "url": "https://video.twimg.com/vid/720/best.mp4",
    "formats": [
        {"url": "https://video.twimg.com/pl/x.m3u8", "container": "m3u8"},
        {"url": "https://video.twimg.com/vid/320/low.mp4", "bitrate": 432000, "container": "mp4"},
        {"url": "https://video.twimg.com/vid/540/mid.mp4", "bitrate": 832000, "container": "mp4"},
        {"url": "https://video.twimg.com/vid/720/best.mp4", "bitrate": 1280000, "container": "mp4"},
    ],
    "variants": [
        {"url": "https://video.twimg.com/pl/x.m3u8", "bitrate": 0, "content_type": "application/x-mpegURL"},
        {"url": "https://video.twimg.com/vid/540/mid.mp4", "bitrate": 832000, "content_type": "video/mp4"},
    ],
}


def test_fx_items_photo_video_gif():
    data = {
        "code": 200,
        "tweet": {
            "media": {
                "all": [
                    {"type": "photo", "url": "https://pbs.twimg.com/media/A.jpg?name=orig"},
                    FX_VIDEO,
                    {"type": "gif", "url": "https://video.twimg.com/tweet_video/G.mp4"},
                    {"type": "mystery", "url": "https://example.com/x"},
                ]
            }
        },
    }
    photo, video, gif = upload._fx_items(data)
    assert photo.urls == (
        "https://pbs.twimg.com/media/A.jpg?name=orig",
        "https://pbs.twimg.com/media/A.jpg?name=large",
    )
    assert video.video and video.urls == (
        "https://video.twimg.com/vid/720/best.mp4",
        "https://video.twimg.com/vid/540/mid.mp4",
        "https://video.twimg.com/vid/320/low.mp4",
    )
    assert gif.video and gif.urls == ("https://video.twimg.com/tweet_video/G.mp4",)


def test_fx_items_quote_and_failures():
    quoted = {
        "code": 200,
        "tweet": {
            "text": "hi",
            "quote": {"media": {"photos": [{"type": "photo", "url": "https://x/q.png"}]}},
        },
    }
    assert [i.urls for i in upload._fx_items(quoted)] == [("https://x/q.png",)]
    assert upload._fx_items({"code": 200, "tweet": {"text": "no media"}}) == []
    assert upload._fx_items({"code": 404, "message": "NOT_FOUND"}) is None
    assert upload._fx_items(None) is None
    assert upload._fx_items(["weird"]) is None


def test_vx_items():
    data = {
        "tweetID": "1",
        "media_extended": [
            {"type": "image", "url": "https://pbs.twimg.com/media/B.jpg"},
            {"type": "video", "url": "https://video.twimg.com/v.mp4"},
        ],
    }
    image, video = upload._vx_items(data)
    assert image.urls[0] == "https://pbs.twimg.com/media/B.jpg?name=orig"
    assert video.video and video.urls == ("https://video.twimg.com/v.mp4",)
    legacy = upload._vx_items({"tweetID": "1", "mediaURLs": ["https://video.twimg.com/a.mp4"]})
    assert legacy[0].video
    qrt = upload._vx_items(
        {"tweetID": "1", "media_extended": [], "qrt": {"tweetID": "2", "mediaURLs": ["https://x/q.jpg"]}}
    )
    assert qrt[0].urls == ("https://x/q.jpg",)
    assert upload._vx_items({"error": "rate limited"}) is None


def test_reddit_items_gallery_order_and_fallbacks():
    post = {
        "is_gallery": True,
        "gallery_data": {
            "items": [
                {"media_id": "b"},
                {"media_id": "gone", "is_deleted": True},
                {"media_id": "a"},
                {"media_id": "anim"},
                {"media_id": "bad"},
            ]
        },
        "media_metadata": {
            "a": {
                "status": "valid",
                "e": "Image",
                "m": "image/png",
                "s": {"u": "https://preview.redd.it/a.png?s=1"},
            },
            "b": {
                "status": "valid",
                "e": "Image",
                "m": "image/jpg",
                "s": {"u": "https://preview.redd.it/b.jpg?s=1"},
            },
            "gone": {"status": "valid", "e": "Image", "m": "image/jpg", "s": {}},
            "anim": {
                "status": "valid",
                "e": "AnimatedImage",
                "s": {"mp4": "https://x/anim.mp4", "gif": "https://x/anim.gif"},
            },
            "bad": {"status": "failed"},
        },
    }
    items = upload._reddit_items(post)
    assert [i.urls for i in items] == [
        (f"{upload.I_REDD_IT}/b.jpg", "https://preview.redd.it/b.jpg?s=1"),
        (f"{upload.I_REDD_IT}/a.png", "https://preview.redd.it/a.png?s=1"),
        ("https://x/anim.mp4", "https://x/anim.gif"),
    ]
    assert items[2].video


def test_reddit_items_image_crosspost_and_video():
    image = {
        "post_hint": "image",
        "url_overridden_by_dest": "https://i.redd.it/x.jpeg",
        "preview": {"images": [{"source": {"url": "https://preview.redd.it/x.jpeg?s=2"}}]},
    }
    assert upload._reddit_items(image)[0].urls == (
        "https://i.redd.it/x.jpeg",
        "https://preview.redd.it/x.jpeg?s=2",
    )
    crosspost = {"post_hint": "link", "url": "/r/a/comments/1", "crosspost_parent_list": [image]}
    assert upload._reddit_items(crosspost)[0].urls[0] == "https://i.redd.it/x.jpeg"
    video = {"is_video": True, "post_hint": "hosted:video", "url": "https://v.redd.it/abc"}
    assert upload._reddit_items(video) == []
    assert (
        upload._reddit_items({"post_hint": "self", "url": "https://www.reddit.com/r/a/comments/1/t/"}) == []
    )


def test_bsky_media_shapes():
    images = {
        "$type": "app.bsky.embed.images#view",
        "images": [{"fullsize": "https://cdn.bsky.app/img/feed_fullsize/plain/d/c"}],
    }
    found, video = upload._bsky_media(images, "did:plc:a")
    assert found[0].urls == (
        "https://cdn.bsky.app/img/feed_fullsize/plain/d/c@jpeg",
        "https://cdn.bsky.app/img/feed_fullsize/plain/d/c",
    )
    assert video is None
    vid = {"$type": "app.bsky.embed.video#view", "cid": "bafy", "playlist": "https://video.bsky.app/x.m3u8"}
    assert upload._bsky_media(vid, "did:plc:a") == ([], ("did:plc:a", "bafy"))
    with_media = {"$type": "app.bsky.embed.recordWithMedia#view", "media": vid, "record": {}}
    assert upload._bsky_media(with_media, "did:plc:a")[1] == ("did:plc:a", "bafy")
    quote = {
        "$type": "app.bsky.embed.record#view",
        "record": {
            "$type": "app.bsky.embed.record#viewRecord",
            "author": {"did": "did:plc:q"},
            "embeds": [vid],
        },
    }
    assert upload._bsky_media(quote, "did:plc:a")[1] == ("did:plc:q", "bafy")
    assert upload._bsky_media({"$type": "app.bsky.embed.external#view"}, "did:plc:a") == ([], None)
    assert upload._bsky_media(None, None) == ([], None)


# --- downloading and size limits ----------------------------------------------------------


async def test_download_rejects_oversized_content_length(local, tmp_path):
    local.file("/big.jpg", b"x" * 5000)
    ctx = upload._Ctx(local.session, tmp_path, max_bytes=4000, stem="t_1")
    assert await upload._download(ctx, upload._item([local.url("/big.jpg")]), 1) is None
    assert list(tmp_path.iterdir()) == []


async def test_download_enforces_limit_while_streaming(local, tmp_path):
    local.file("/stream.mp4", b"x" * 50_000, "video/mp4", chunked=True)
    ctx = upload._Ctx(local.session, tmp_path, max_bytes=20_000, stem="t_1")
    assert await upload._download(ctx, upload._item([local.url("/stream.mp4")]), 1) is None
    assert list(tmp_path.iterdir()) == []  # partial file removed


async def test_download_falls_back_to_smaller_candidate(local, tmp_path):
    local.file("/orig.png", b"x" * 9000, "image/png")
    local.file("/large.png", b"x" * 3000, "image/png")
    ctx = upload._Ctx(local.session, tmp_path, max_bytes=5000, stem="t_1")
    got = await upload._download(
        ctx, upload._item([local.url("/missing"), local.url("/orig.png"), local.url("/large.png")]), 2
    )
    assert got == MediaFile(path=tmp_path / "t_1_2.png", filename="t_1_2.png", size=3000)
    assert got.path.read_bytes() == b"x" * 3000


async def test_download_extension_from_content_type_then_url(local, tmp_path):
    local.file("/noext", JPEG, "image/jpeg")
    local.file("/clip.mp4", MP4, "application/octet-stream")
    local.file("/page", b"<html>", "text/html")
    ctx = upload._Ctx(local.session, tmp_path, max_bytes=MB, stem="s")
    assert (await upload._download(ctx, upload._item([local.url("/noext")]), 1)).filename == "s_1.jpg"
    assert (
        await upload._download(ctx, upload._item([local.url("/clip.mp4?tag=1")]), 2)
    ).filename == "s_2.mp4"
    assert await upload._download(ctx, upload._item([local.url("/page")]), 3) is None


# --- Twitter ------------------------------------------------------------------------------


async def test_twitter_fxtwitter(local, tmp_path, ytdlp):
    local.file("/media/a.jpg", JPEG)
    local.file("/vid/best.mp4", MP4, "video/mp4")
    local.json(
        "/fx/status/1234",
        {
            "code": 200,
            "tweet": {
                "media": {
                    "all": [
                        {"type": "photo", "url": local.url("/media/a.jpg")},
                        {"type": "video", "url": local.url("/vid/best.mp4"), "variants": []},
                    ]
                }
            },
        },
    )
    files = await fetch(local, match("twitter", "twitter:1234"), tmp_path)
    assert names(files) == ["twitter_1234_1.jpg", "twitter_1234_2.mp4"]
    assert files[0].path.read_bytes() == JPEG and files[1].size == len(MP4)
    assert ytdlp.calls == []


async def test_twitter_video_variant_that_fits(local, tmp_path):
    local.file("/vid/best.mp4", b"v" * 9000, "video/mp4")
    local.file("/vid/low.mp4", b"v" * 2000, "video/mp4")
    video = {
        "type": "video",
        "url": local.url("/vid/best.mp4"),
        "formats": [{"url": local.url("/vid/low.mp4"), "bitrate": 300000, "container": "mp4"}],
    }
    local.json("/fx/status/5", {"code": 200, "tweet": {"media": {"all": [video]}}})
    files = await fetch(local, match("twitter", "twitter:5"), tmp_path, max_bytes=5000)
    assert [(f.filename, f.size) for f in files] == [("twitter_5_1.mp4", 2000)]


async def test_twitter_falls_back_to_vxtwitter(local, tmp_path, ytdlp):
    local.routes["/fx/status/77"] = _html  # Cloudflare-style HTML page with status 200
    local.file("/media/v.png", b"png", "image/png")
    local.json(
        "/vx/Twitter/status/77",
        {"tweetID": "77", "media_extended": [{"type": "image", "url": local.url("/media/v.png")}]},
    )
    files = await fetch(local, match("twitter", "twitter:77", "https://x.com/a/status/77"), tmp_path)
    assert names(files) == ["twitter_77_1.png"]
    assert ytdlp.calls == []


async def _html(request):
    return web.Response(text="<!DOCTYPE html><title>Attention Required!</title>", content_type="text/html")


async def test_twitter_ytdlp_last_resort_when_apis_fail(local, tmp_path, ytdlp):
    local.json("/fx/status/9", {"code": 500, "message": "API_FAIL"}, status=500)
    files = await fetch(local, match("twitter", "twitter:9"), tmp_path)
    assert files == []
    assert ytdlp.calls == ["https://x.com/i/status/9"]


async def test_twitter_ytdlp_when_no_video_variant_fits(local, tmp_path, ytdlp):
    local.file("/vid/huge.mp4", b"v" * 9000, "video/mp4")
    local.json(
        "/fx/status/3",
        {"code": 200, "tweet": {"media": {"all": [{"type": "video", "url": local.url("/vid/huge.mp4")}]}}},
    )
    ytdlp.result = [MediaFile(tmp_path / "twitter_3_1.mp4", "twitter_3_1.mp4", 10)]
    files = await fetch(local, match("twitter", "twitter:3"), tmp_path, max_bytes=1000)
    assert names(files) == ["twitter_3_1.mp4"]
    assert ytdlp.calls == ["https://x.com/i/status/3"]


async def test_twitter_no_media_skips_ytdlp(local, tmp_path, ytdlp):
    local.json("/fx/status/20", {"code": 200, "tweet": {"text": "just setting up my twttr"}})
    assert await fetch(local, match("twitter", "twitter:20"), tmp_path) == []
    assert ytdlp.calls == []


async def test_at_most_four_files(local, tmp_path):
    photos = []
    for n in range(6):
        local.file(f"/media/{n}.jpg", JPEG)
        photos.append({"type": "photo", "url": local.url(f"/media/{n}.jpg")})
    local.json("/fx/status/6", {"code": 200, "tweet": {"media": {"all": photos}}})
    files = await fetch(local, match("twitter", "twitter:6"), tmp_path)
    assert names(files) == [f"twitter_6_{n}.jpg" for n in range(1, 5)]
    assert sum(1 for hit in local.hits if hit.startswith("/media/")) == 4


# --- Pixiv --------------------------------------------------------------------------------


async def test_pixiv_multi_page_with_referer(local, tmp_path):
    need = {"Referer": "https://www.pixiv.net/"}
    local.file("/pximg/p0.jpg", JPEG, need=need)
    local.file("/pximg/p1.png", b"png", "image/png", need=need)
    local.json(
        "/pixiv/ajax/illust/555",
        {
            "error": False,
            "body": {
                "illustId": "555",
                "xRestrict": 0,
                "pageCount": 2,
                "urls": {"original": local.url("/pximg/p0.jpg")},
            },
        },
    )
    local.json(
        "/pixiv/ajax/illust/555/pages",
        {
            "error": False,
            "body": [
                {"urls": {"original": local.url("/pximg/p0.jpg"), "regular": local.url("/nope")}},
                {"urls": {"original": local.url("/pximg/p1.png")}},
            ],
        },
    )
    files = await fetch(local, match("pixiv", "pixiv:555"), tmp_path)
    assert names(files) == ["pixiv_555_1.jpg", "pixiv_555_2.png"]


async def test_pixiv_original_too_big_uses_regular(local, tmp_path):
    local.file("/pximg/orig.png", b"x" * 9000, "image/png")
    local.file("/pximg/master.jpg", JPEG)
    urls = {"original": local.url("/pximg/orig.png"), "regular": local.url("/pximg/master.jpg")}
    local.json(
        "/pixiv/ajax/illust/8", {"error": False, "body": {"xRestrict": 0, "pageCount": 1, "urls": urls}}
    )
    files = await fetch(local, match("pixiv", "pixiv:8"), tmp_path, max_bytes=5000)
    assert names(files) == ["pixiv_8_1.jpg"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "error": False,
            "body": {"xRestrict": 1, "pageCount": 1, "urls": {"original": "https://i.pximg.net/x.jpg"}},
        },
        {"error": True, "message": "", "body": []},
    ],
)
async def test_pixiv_r18_and_missing_are_skipped(local, tmp_path, payload):
    local.json("/pixiv/ajax/illust/9", payload)
    assert await fetch(local, match("pixiv", "pixiv:9"), tmp_path) == []
    assert not any("pximg" in hit for hit in local.hits)


# --- Reddit -------------------------------------------------------------------------------


def _reddit_routes(http, post, *, need_cookie=True):
    async def warmup(request):
        resp = web.Response(text="old reddit")
        resp.set_cookie("loid", "abc")
        return resp

    async def post_json(request):
        if need_cookie and request.cookies.get("loid") != "abc":
            return web.Response(status=403, text="blocked")
        assert request.query.get("raw_json") == "1"
        return web.json_response([{"data": {"children": [{"data": post}]}}, {}])

    http.routes["/oldreddit/"] = warmup
    http.routes[f"/reddit/comments/{post['id']}/.json"] = post_json


async def test_reddit_gallery(local, tmp_path, ytdlp):
    local.file("/iredd/a.jpg", JPEG)
    local.file("/preview/b.png", b"png", "image/png")
    post = {
        "id": "g1",
        "is_gallery": True,
        "gallery_data": {"items": [{"media_id": "a"}, {"media_id": "b"}]},
        "media_metadata": {
            "a": {"status": "valid", "e": "Image", "m": "image/jpg", "s": {"u": local.url("/preview/a.jpg")}},
            "b": {"status": "valid", "e": "Image", "m": "image/png", "s": {"u": local.url("/preview/b.png")}},
        },
    }
    _reddit_routes(local, post)
    files = await fetch(local, match("reddit", "reddit:g1"), tmp_path)
    assert names(files) == ["reddit_g1_1.jpg", "reddit_g1_2.png"]  # b's original is missing -> preview
    assert ytdlp.calls == []


async def test_reddit_video_goes_to_ytdlp(local, tmp_path, ytdlp):
    _reddit_routes(
        local, {"id": "v1", "is_video": True, "post_hint": "hosted:video", "url": "https://v.redd.it/q"}
    )
    await fetch(local, match("reddit", "reddit:v1"), tmp_path)
    assert ytdlp.calls == ["https://www.reddit.com/comments/v1/"]


async def test_reddit_blocked_json_goes_to_ytdlp(local, tmp_path, ytdlp):
    _reddit_routes(local, {"id": "b1", "post_hint": "image", "url": "https://i.redd.it/x.jpg"})
    local.routes.pop("/oldreddit/")  # no loid cookie -> 403
    assert await fetch(local, match("reddit", "reddit:b1"), tmp_path) == []
    assert ytdlp.calls == ["https://www.reddit.com/comments/b1/"]


# --- Bluesky ------------------------------------------------------------------------------


def _bsky_thread(http, post):
    async def handler(request):
        assert request.query["uri"] == "at://alice.test/app.bsky.feed.post/3kpost"
        return web.json_response({"thread": {"$type": "app.bsky.feed.defs#threadViewPost", "post": post}})

    http.routes["/bsky/xrpc/app.bsky.feed.getPostThread"] = handler


async def test_bluesky_images(local, tmp_path):
    local.file("/cdn/one", JPEG)
    local.file("/cdn/two", b"webp", "image/webp")
    embed = {
        "$type": "app.bsky.embed.recordWithMedia#view",
        "media": {
            "$type": "app.bsky.embed.images#view",
            "images": [{"fullsize": local.url("/cdn/one")}, {"fullsize": local.url("/cdn/two")}],
        },
    }
    _bsky_thread(local, {"author": {"did": "did:plc:alice"}, "embed": embed})
    files = await fetch(local, match("bluesky", "bsky:alice.test/3kpost"), tmp_path)
    assert names(files) == ["bsky_3kpost_1.jpg", "bsky_3kpost_2.webp"]


async def test_bluesky_video_blob_then_ytdlp(local, tmp_path, monkeypatch, ytdlp):
    async def pds(ctx, did):
        assert did == "did:plc:alice"
        return local.url("/pds")

    async def blob(request):
        assert request.query["did"] == "did:plc:alice" and request.query["cid"] == "bafyvid"
        return web.Response(body=MP4, content_type="video/mp4")

    monkeypatch.setattr(upload, "_pds_endpoint", pds)
    local.routes["/pds/xrpc/com.atproto.sync.getBlob"] = blob
    embed = {
        "$type": "app.bsky.embed.video#view",
        "cid": "bafyvid",
        "playlist": "https://video.bsky.app/p.m3u8",
    }
    _bsky_thread(local, {"author": {"did": "did:plc:alice"}, "embed": embed})
    m = match("bluesky", "bsky:alice.test/3kpost")

    files = await fetch(local, m, tmp_path)
    assert names(files) == ["bsky_3kpost_1.mp4"] and ytdlp.calls == []

    files = await fetch(local, m, tmp_path, max_bytes=100)  # blob too big -> HLS via yt-dlp
    assert files == [] and ytdlp.calls == ["https://bsky.app/profile/did:plc:alice/post/3kpost"]


async def test_bluesky_missing_post(local, tmp_path):
    local.json(
        "/bsky/xrpc/app.bsky.feed.getPostThread",
        {"error": "NotFound", "message": "Post not found"},
        status=400,
    )
    assert await fetch(local, match("bluesky", "bsky:alice.test/3kpost"), tmp_path) == []


async def test_pds_endpoint_from_plc(local, tmp_path):
    local.json(
        "/plc/did:plc:alice",
        {
            "service": [
                {
                    "id": "#atproto_pds",
                    "type": "AtprotoPersonalDataServer",
                    "serviceEndpoint": "https://pds.example/",
                }
            ]
        },
    )
    local.json(
        "/plc/did:plc:plain", {"service": [{"id": "#atproto_pds", "serviceEndpoint": "http://10.0.0.1"}]}
    )
    ctx = upload._Ctx(local.session, tmp_path, MB, "s")
    assert await upload._pds_endpoint(ctx, "did:plc:alice") == "https://pds.example"
    assert await upload._pds_endpoint(ctx, "did:plc:plain") is None  # https only
    assert await upload._pds_endpoint(ctx, "did:plc:missing") is None
    assert await upload._pds_endpoint(ctx, "did:web:127.0.0.1") is None


# --- yt-dlp sites -------------------------------------------------------------------------


async def test_ytdlp_sites_use_canonical_urls(local, tmp_path, ytdlp):
    await fetch(local, match("tiktok", "tiktok:7123", "https://vxtiktok.com/@a/video/7123"), tmp_path)
    await fetch(local, match("instagram", "instagram:Cx_1", "https://ddinstagram.com/reel/Cx_1/"), tmp_path)
    assert ytdlp.calls == ["https://www.tiktok.com/@/video/7123", "https://www.instagram.com/p/Cx_1/"]


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: writes files where the options say and reports them."""

    seen_opts: dict = {}
    sizes: list = [100]
    error: Exception | None = None

    def __init__(self, opts):
        FakeYDL.seen_opts = opts
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        if FakeYDL.error:
            raise FakeYDL.error
        home = Path(self.opts["paths"]["home"])
        stem = self.opts["outtmpl"]["default"].split("_%(")[0]
        entries = []
        for n, size in enumerate(FakeYDL.sizes, 1):
            path = home / f"{stem}_{n}.mp4"
            path.write_bytes(b"v" * size)
            entries.append({"requested_downloads": [{"filepath": str(path)}]})
        if len(entries) == 1:
            return {"id": "x", **entries[0]}
        return {"_type": "playlist", "entries": [*entries, None]}


@pytest.fixture
def fake_ydl(monkeypatch):
    monkeypatch.setattr(upload.yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.sizes, FakeYDL.error = [100], None
    return FakeYDL


def test_ytdlp_sync_options_and_files(tmp_path, fake_ydl):
    files = upload._ytdlp_sync(
        "https://www.tiktok.com/@/video/1", tmp_path, "tiktok_1", 5000, threading.Event()
    )
    assert files == [MediaFile(tmp_path / "tiktok_1_1.mp4", "tiktok_1_1.mp4", 100)]
    opts = fake_ydl.seen_opts
    assert opts["max_filesize"] == 5000
    assert opts["paths"]["home"] == str(tmp_path)
    assert opts["format_sort"][0] == "vcodec:h264"
    assert "[filesize<=?5000]" in opts["format"]
    assert opts["playlist_items"] == "1-4"


def test_ytdlp_sync_drops_oversized_and_reads_playlists(tmp_path, fake_ydl):
    fake_ydl.sizes = [100, 9000, 200]
    files = upload._ytdlp_sync("u", tmp_path, "ig_1", 5000, threading.Event())
    assert [(f.filename, f.size) for f in files] == [("ig_1_1.mp4", 100), ("ig_1_3.mp4", 200)]
    assert not (tmp_path / "ig_1_2.mp4").exists()


def test_ytdlp_sync_errors_give_nothing(tmp_path, fake_ydl):
    fake_ydl.error = upload.yt_dlp.utils.DownloadError("ERROR: [Instagram] login required")
    assert upload._ytdlp_sync("u", tmp_path, "ig_1", 5000, threading.Event()) == []


def test_ytdlp_progress_hook_cancels(tmp_path):
    cancel = threading.Event()
    hook = upload._ytdlp_options(tmp_path, "s", 1, cancel)["progress_hooks"][0]
    hook({"status": "downloading"})
    cancel.set()
    with pytest.raises(upload.yt_dlp.utils.DownloadCancelled):
        hook({"status": "downloading"})


# --- timeouts and never raising -----------------------------------------------------------


async def test_total_timeout_returns_empty(local, tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "TOTAL_TIMEOUT", 0.3)
    local.file("/media/slow.jpg", JPEG, delay=5)
    local.json(
        "/fx/status/1",
        {"code": 200, "tweet": {"media": {"all": [{"type": "photo", "url": local.url("/media/slow.jpg")}]}}},
    )
    started = time.monotonic()
    assert await fetch(local, match("twitter", "twitter:1"), tmp_path) == []
    assert time.monotonic() - started < 2


async def test_timeout_cancels_ytdlp_thread(http, tmp_path, monkeypatch):
    monkeypatch.setattr(upload, "TOTAL_TIMEOUT", 0.3)
    seen = {}

    def slow_sync(url, workdir, stem, max_bytes, cancel):
        seen["cancelled"] = cancel.wait(5)
        return []

    monkeypatch.setattr(upload, "_ytdlp_sync", slow_sync)
    started = time.monotonic()
    assert await fetch(http, match("tiktok", "tiktok:1"), tmp_path) == []
    assert time.monotonic() - started < 2
    for _ in range(50):
        if "cancelled" in seen:
            break
        await asyncio.sleep(0.05)
    assert seen.get("cancelled") is True


async def test_never_raises(http, tmp_path, monkeypatch):
    async def boom(ctx, ident):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(upload._HANDLERS, "twitter", boom)
    assert await fetch(http, match("twitter", "twitter:1"), tmp_path) == []
    broken = LinkMatch(site=Site("twitter", "", "", (), ""), url=None, canonical_key=None)  # type: ignore[arg-type]
    assert await fetch(http, broken, tmp_path) == []
    assert await fetch(http, match("mastodon", "mastodon:1"), tmp_path) == []


async def test_unreachable_apis_return_empty(local, tmp_path, ytdlp):
    # every route 404s: each site degrades to [] (or to the faked yt-dlp) without raising
    for site, key in [
        ("twitter", "twitter:1"),
        ("pixiv", "pixiv:1"),
        ("bluesky", "bsky:a.test/3k"),
        ("reddit", "reddit:r1"),
    ]:
        assert await fetch(local, match(site, key), tmp_path) == []
    assert ytdlp.calls == ["https://x.com/i/status/1", "https://www.reddit.com/comments/r1/"]


# --- the public-address guard ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("http://127.0.0.1:8123/", False),
        ("http://172.17.0.1/", False),  # the Docker host
        ("http://192.168.1.1/admin", False),
        ("http://169.254.169.254/latest/meta-data/", False),  # cloud metadata
        ("http://[::1]/", False),
        ("http://localhost/", False),
        ("ftp://1.1.1.1/x.jpg", False),
        ("https://1.1.1.1/x.jpg", True),
    ],
)
async def test_public_url_guard(monkeypatch, url, public):
    monkeypatch.setattr(upload, "ALLOW_PRIVATE_HOSTS", False)
    assert await upload._public_url(url) is public


async def test_redirect_into_the_lan_is_refused(http, tmp_path, monkeypatch):
    # A post's image link that redirects to an internal address: never fetched, never uploaded.
    async def bounce(request):
        raise web.HTTPFound("http://192.168.1.1/secret.jpg")

    http.routes["/a.jpg"] = bounce
    monkeypatch.setattr(upload, "ALLOW_PRIVATE_HOSTS", False)

    async def first_hop_only(url):  # the test server itself is on 127.0.0.1: let only it through
        return url.startswith(http.base) or await upload_public(url)

    upload_public = upload._public_url
    monkeypatch.setattr(upload, "_public_url", first_hop_only)
    ctx = upload._Ctx(http.session, tmp_path, max_bytes=MB, stem="r_1")
    assert await upload._download(ctx, upload._item([http.url("/a.jpg")]), 1) is None
    assert list(tmp_path.iterdir()) == []


async def test_web_page_with_an_image_name_is_not_media(http, tmp_path):
    async def page(request):
        return web.Response(text="<html>router admin</html>", content_type="text/html")

    http.routes["/cat.jpg"] = page
    ctx = upload._Ctx(http.session, tmp_path, max_bytes=MB, stem="p_1")
    assert await upload._download(ctx, upload._item([http.url("/cat.jpg")]), 1) is None


def test_ytdlp_never_uses_the_generic_extractor(tmp_path):
    opts = upload._ytdlp_options(tmp_path, "s", MB, threading.Event())
    assert opts["allowed_extractors"] == ["default", "-generic"]
