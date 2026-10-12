"""Offline tests for bb.links: extraction, site matching, fixer URLs, chains and short-link expansion."""

from __future__ import annotations

import pytest
from aiohttp import ClientSession, test_utils, web

from bb.links import resolve, sites
from bb.links.extract import FoundURL, extract_urls
from bb.links.model import Fixer

# --- extraction ----------------------------------------------------------------------------------

T1 = "https://x.com/a/status/1"
T2 = "https://x.com/b/status/2"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (f"look {T1}", [FoundURL(T1)]),
        (f"{T1}. and {T1}, again", [FoundURL(T1)]),
        (f"{T1}!?", [FoundURL(T1)]),
        (f"wow ({T1})", [FoundURL(T1)]),
        (f"[cool post]({T1})", [FoundURL(T1)]),
        (f"[cool post](<{T1}>)", []),
        (f"<{T1}> but {T2}", [FoundURL(T2)]),
        (f"`{T1}` {T2}", [FoundURL(T2)]),
        (f"``code {T1} code`` {T2}", [FoundURL(T2)]),
        (f"```\n{T1}\n```\n{T2}", [FoundURL(T2)]),
        (f"```py\n{T1}```", []),
        (f"||{T1}|| {T2}", [FoundURL(T1, spoiler=True), FoundURL(T2)]),
        (f"|| look: {T1} ||", [FoundURL(T1, spoiler=True)]),
        (f"**{T1}**", [FoundURL(T1)]),
        (f"_{T1}_", [FoundURL(T1)]),
        (f"__{T1}__ and ~~{T2}~~", [FoundURL(T1), FoundURL(T2)]),
        ("https://www.instagram.com/p/CAbcDef_", [FoundURL("https://www.instagram.com/p/CAbcDef_")]),
        ("see https://www.instagram.com/p/CAbcDef_.", [FoundURL("https://www.instagram.com/p/CAbcDef_")]),
        (f"'{T1}'", [FoundURL(T1)]),
        (f"{T2}\n{T1}\n{T2}", [FoundURL(T2), FoundURL(T1)]),
        ("https://en.wikipedia.org/wiki/Foo_(bar)", [FoundURL("https://en.wikipedia.org/wiki/Foo_(bar)")]),
        ("no links here, just http:// and ftp://x.com", []),
        (f"{T1}?s=20", [FoundURL(f"{T1}?s=20")]),
    ],
)
def test_extract_urls(content: str, expected: list[FoundURL]) -> None:
    assert extract_urls(content) == expected


def test_extract_unclosed_fence_is_plain_text() -> None:
    # Discord renders an unclosed ``` as literal text, so the link still previews.
    assert extract_urls(f"```{T1}") == [FoundURL(T1)]


def test_extract_keeps_order() -> None:
    urls = [f"https://x.com/u/status/{i}" for i in range(6)]
    assert [f.url for f in extract_urls(" ".join(urls))] == urls


# --- matching ------------------------------------------------------------------------------------

TWEET = "twitter:266031293945503744"
OB = "BarackObama/status/266031293945503744"
FIXED = {"already_fixed": "1"}
TT = "7437673559146827054"
TTK = f"tiktok:{TT}"
DID = "did:plc:z72i7hdynmk6r22z27h6tvur"
CASES: list[tuple[str, str, dict[str, str], str | None]] = [
    # twitter
    (f"https://x.com/{OB}", TWEET, {"user": "BarackObama"}, "BarackObama"),
    (f"https://twitter.com/{OB}?s=20&t=abc", TWEET, {}, "BarackObama"),
    (f"https://mobile.twitter.com/{OB}", TWEET, {}, "BarackObama"),
    (f"https://www.x.com/{OB}/photo/1", TWEET, {}, "BarackObama"),
    (f"https://x.com/{OB}/video/2", TWEET, {}, "BarackObama"),
    ("https://x.com/i/status/266031293945503744", TWEET, {}, None),
    ("https://twitter.com/i/web/status/266031293945503744", TWEET, {}, None),
    ("https://x.com/BarackObama/statuses/266031293945503744", TWEET, {}, "BarackObama"),
    (f"https://fxtwitter.com/{OB}", TWEET, FIXED, "BarackObama"),
    (f"https://fixupx.com/{OB}/es", TWEET, FIXED, "BarackObama"),
    ("https://vxtwitter.com/i/status/266031293945503744", TWEET, FIXED, None),
    (f"https://d.fxtwitter.com/{OB}", TWEET, FIXED, "BarackObama"),
    (f"https://fixvx.com/{OB}", TWEET, FIXED, "BarackObama"),
    (f"https://twittpr.com/{OB}/pt-br", TWEET, FIXED, "BarackObama"),
    # pixiv
    ("https://www.pixiv.net/artworks/1580459", "pixiv:1580459", {"id": "1580459"}, None),
    ("https://pixiv.net/en/artworks/1580459", "pixiv:1580459", {}, None),
    ("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=1580459", "pixiv:1580459", {}, None),
    ("https://www.pixiv.net/i/1580459", "pixiv:1580459", {}, None),
    ("https://phixiv.net/artworks/1580459/2", "pixiv:1580459", FIXED, None),
    ("https://www.ppxiv.net/en/artworks/1580459", "pixiv:1580459", FIXED, None),
    # reddit
    ("https://www.reddit.com/r/pics/comments/haucpf/funny/", "reddit:haucpf", {"sub": "pics"}, None),
    ("https://old.reddit.com/r/pics/comments/haucpf/", "reddit:haucpf", {"sub": "pics"}, None),
    ("https://new.reddit.com/r/pics/comments/haucpf", "reddit:haucpf", {"id": "haucpf"}, None),
    ("https://reddit.com/r/pics/comments/haucpf/slug/fz1abc2/?context=3", "reddit:haucpf",
     {"comment": "fz1abc2"}, None),
    ("https://www.reddit.com/comments/haucpf", "reddit:haucpf", {}, None),
    ("https://www.reddit.com/gallery/haucpf", "reddit:haucpf", {}, None),
    ("https://www.reddit.com/user/spez/comments/haucpf/hello/", "reddit:haucpf", {"user": "spez"}, "spez"),
    ("https://www.reddit.com/u/spez/comments/haucpf/", "reddit:haucpf", {"user": "spez"}, "spez"),
    ("https://redd.it/haucpf", "reddit:haucpf", {"id": "haucpf"}, None),
    ("https://vxreddit.com/r/pics/comments/haucpf/", "reddit:haucpf", FIXED, None),
    ("https://rxddit.com/haucpf", "reddit:haucpf", FIXED, None),
    # tiktok
    (f"https://www.tiktok.com/@discord/video/{TT}", TTK,
     {"user": "discord", "kind": "video"}, "discord"),
    (f"https://www.tiktok.com/@some.user_1/photo/{TT}?lang=en", TTK,
     {"user": "some.user_1", "kind": "photo"}, "some.user_1"),
    (f"https://m.tiktok.com/v/{TT}.html", TTK, {"kind": "video"}, None),
    (f"https://www.tiktok.com/embed/v2/{TT}", TTK, {}, None),
    (f"https://tnktok.com/@discord/video/{TT}", TTK,
     FIXED, "discord"),
    (f"https://vxtiktok.com/@i/video/{TT}", TTK,
     FIXED, None),
    # bluesky
    ("https://bsky.app/profile/bsky.app/post/3m4j4iy4oek2d", "bsky:bsky.app/3m4j4iy4oek2d",
     {"actor": "bsky.app", "rkey": "3m4j4iy4oek2d"}, "bsky.app"),
    ("https://bsky.app/profile/Someone.Bsky.Social/post/3m4j4iy4oek2d",
     "bsky:someone.bsky.social/3m4j4iy4oek2d", {}, "someone.bsky.social"),
    (f"https://bsky.app/profile/{DID}/post/3m4j4iy4oek2d",
     f"bsky:{DID}/3m4j4iy4oek2d", {"actor": DID}, None),
    ("https://fxbsky.app/profile/bsky.app/post/3m4j4iy4oek2d", "bsky:bsky.app/3m4j4iy4oek2d",
     FIXED, "bsky.app"),
    ("https://bskyx.app/profile/bsky.app/post/3m4j4iy4oek2d/", "bsky:bsky.app/3m4j4iy4oek2d",
     FIXED, "bsky.app"),
    # instagram
    ("https://www.instagram.com/p/BsOGulcndj-/", "instagram:BsOGulcndj-", {"kind": "p"}, None),
    ("https://instagram.com/p/BsOGulcndj-/?img_index=2&igsh=x", "instagram:BsOGulcndj-", {"kind": "p"}, None),
    ("https://www.instagram.com/reel/C9AbCdEfGhI/", "instagram:C9AbCdEfGhI", {"kind": "reel"}, None),
    ("https://www.instagram.com/reels/C9AbCdEfGhI/", "instagram:C9AbCdEfGhI", {"kind": "reel"}, None),
    ("https://www.instagram.com/tv/C9AbCdEfGhI/", "instagram:C9AbCdEfGhI", {"kind": "p"}, None),
    ("https://www.instagram.com/world_record_egg/p/BsOGulcndj-/", "instagram:BsOGulcndj-",
     {"user": "world_record_egg"}, "world_record_egg"),
    ("https://oginstagram.com/p/BsOGulcndj-/", "instagram:BsOGulcndj-", FIXED, None),
    ("https://ddinstagram.com/reel/C9AbCdEfGhI", "instagram:C9AbCdEfGhI", FIXED, None),
    ("https://fxig.seria.moe/p/BsOGulcndj-/", "instagram:BsOGulcndj-", FIXED, None),
]


@pytest.mark.parametrize(("url", "key", "parts", "author"), CASES, ids=[c[0] for c in CASES])
def test_match(url: str, key: str, parts: dict[str, str], author: str | None) -> None:
    m = sites.match(url)
    assert m is not None
    assert m.canonical_key == key
    assert m.url == url
    for k, v in parts.items():
        assert m.parts.get(k) == v, k
    if "already_fixed" not in parts:
        assert "already_fixed" not in m.parts
    assert m.author == author
    assert (m.author_url is None) == (author is None)


def test_match_author_urls() -> None:
    def author_url(u: str) -> str | None:
        m = sites.match(u)
        assert m is not None
        return m.author_url

    assert author_url("https://fxtwitter.com/jack/status/20") == "https://x.com/jack"
    assert author_url("https://www.tiktok.com/@discord/video/7437673559146827054") == "https://www.tiktok.com/@discord"
    assert author_url("https://bsky.app/profile/bsky.app/post/3m4j4iy4oek2d") == "https://bsky.app/profile/bsky.app"
    assert author_url("https://www.instagram.com/egg/p/BsOGulcndj-/") == "https://www.instagram.com/egg/"
    assert author_url("https://www.reddit.com/user/spez/comments/haucpf/") == "https://www.reddit.com/user/spez/"


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/BarackObama",
        "https://x.com/BarackObama/status/",
        "https://x.com/BarackObama/status/123/analytics",
        "https://twitter.com/search?q=cats",
        "https://notx.com/a/status/1",
        "https://fixupx.co/a/status/1",
        "ftp://x.com/a/status/1",
        "https://www.pixiv.net/users/11",
        "https://i.pximg.net/img-master/img/2008/09/10/17/47/18/1580459_p0_master1200.jpg",
        "https://www.reddit.com/r/pics/",
        "https://www.reddit.com/r/pics/s/AbCdEf123",
        "https://i.redd.it/f58v4g8mwh551.jpg",
        "https://v.redd.it/abcdef",
        "https://www.tiktok.com/@discord",
        "https://vm.tiktok.com/ZMabcdef/",
        "https://www.tiktok.com/t/ZTabcdef/",
        "https://bsky.app/profile/bsky.app",
        "https://www.instagram.com/world_record_egg/",
        "https://www.instagram.com/stories/someone/1234567890/",
        "https://www.instagram.com/share/p/BAbCdEf123/",
        "https://example.com/p/BsOGulcndj-/",
        "not a url",
        "",
    ],
)
def test_no_match(url: str) -> None:
    assert sites.match(url) is None


def test_match_spoiler_passthrough() -> None:
    m = sites.match("https://x.com/a/status/1", spoiler=True)
    assert m is not None and m.spoiler


def test_same_post_same_key_across_domains() -> None:
    urls = [
        "https://x.com/BarackObama/status/266031293945503744",
        "https://twitter.com/BarackObama/status/266031293945503744?s=46",
        "https://fxtwitter.com/BarackObama/status/266031293945503744/es",
        "https://vxtwitter.com/i/status/266031293945503744",
        "https://fixupx.com/BarackObama/status/266031293945503744/photo/1",
    ]
    keys = set()
    for u in urls:
        m = sites.match(u)
        assert m is not None
        keys.add(m.canonical_key)
    assert keys == {TWEET}


# --- site table ----------------------------------------------------------------------------------


def test_site_table_shape() -> None:
    assert list(sites.SITES) == ["twitter", "pixiv", "reddit", "tiktok", "bluesky", "instagram"]
    ids = [f.id for s in sites.SITES.values() for f in s.fixers]
    assert len(ids) == len(set(ids)), "fixer ids must be unique across sites"
    for sid, site in sites.SITES.items():
        assert site.id == sid
        assert site.fixers
        m = sites.match(site.test_url)
        assert m is not None and m.site is site, site.test_url
        assert "already_fixed" not in m.parts


def test_only_twitter_is_translatable() -> None:
    for site in sites.SITES.values():
        for f in site.fixers:
            assert f.translatable == (site.id == "twitter"), f.id


def test_get_fixer_and_site_of_fixer() -> None:
    for site in sites.SITES.values():
        for f in site.fixers:
            assert sites.get_fixer(f.id) is f
            assert sites.site_of_fixer(f.id) is site
    assert sites.get_fixer("nope") is None
    assert sites.site_of_fixer("nope") is None


# --- fixed URLs ----------------------------------------------------------------------------------


def _fixed(url: str, fixer_id: str, lang: str | None = None) -> str:
    m = sites.match(url)
    fixer = sites.get_fixer(fixer_id)
    assert m is not None and fixer is not None
    return sites.fixed_url(m, fixer, lang)


@pytest.mark.parametrize(
    ("url", "fixer_id", "expected"),
    [
        ("https://x.com/BarackObama/status/266031293945503744?s=20", "fxtwitter",
         "https://fxtwitter.com/BarackObama/status/266031293945503744"),
        ("https://twitter.com/BarackObama/status/266031293945503744/photo/1", "vxtwitter",
         "https://vxtwitter.com/BarackObama/status/266031293945503744"),
        ("https://x.com/i/status/266031293945503744", "fixupx", "https://fixupx.com/i/status/266031293945503744"),
        ("https://fxtwitter.com/BarackObama/status/266031293945503744/es", "vxtwitter",
         "https://vxtwitter.com/BarackObama/status/266031293945503744"),
        ("https://www.pixiv.net/en/artworks/1580459", "phixiv", "https://phixiv.net/artworks/1580459"),
        ("https://www.pixiv.net/member_illust.php?illust_id=1580459&mode=medium", "ppxiv",
         "https://ppxiv.net/artworks/1580459"),
        ("https://old.reddit.com/r/pics/comments/haucpf/slug/", "vxreddit", "https://vxreddit.com/r/pics/comments/haucpf/"),
        ("https://redd.it/haucpf", "redditez", "https://redditez.com/comments/haucpf/"),
        ("https://www.reddit.com/user/spez/comments/haucpf/x/", "vxreddit", "https://vxreddit.com/comments/haucpf/"),
        ("https://www.tiktok.com/@discord/video/7437673559146827054?is_from_webapp=1", "fxtiktok",
         "https://tnktok.com/@discord/video/7437673559146827054"),
        ("https://www.tiktok.com/@discord/photo/7437673559146827054", "tfxktok",
         "https://tfxktok.com/@discord/photo/7437673559146827054"),
        ("https://m.tiktok.com/v/7437673559146827054.html", "tiktokez", "https://tiktokez.com/@i/video/7437673559146827054"),
        ("https://bsky.app/profile/bsky.app/post/3m4j4iy4oek2d", "fxbsky",
         "https://fxbsky.app/profile/bsky.app/post/3m4j4iy4oek2d"),
        ("https://bsky.app/profile/did:plc:abc/post/3m4j4iy4oek2d", "bskx",
         "https://bskx.app/profile/did:plc:abc/post/3m4j4iy4oek2d"),
        ("https://www.instagram.com/egg/p/BsOGulcndj-/?igsh=x", "oginstagram", "https://oginstagram.com/p/BsOGulcndj-/"),
        ("https://www.instagram.com/reels/C9AbCdEfGhI/", "fxig", "https://fxig.seria.moe/reel/C9AbCdEfGhI/"),
        ("https://www.instagram.com/p/BsOGulcndj-/", "inso", "https://ins.so/p/BsOGulcndj-/"),
    ],
)
def test_fixed_url(url: str, fixer_id: str, expected: str) -> None:
    assert _fixed(url, fixer_id) == expected


def test_fixed_url_translation() -> None:
    url = "https://x.com/BarackObama/status/266031293945503744"
    assert _fixed(url, "fxtwitter", "es") == "https://fxtwitter.com/BarackObama/status/266031293945503744/es"
    assert _fixed(url, "fixupx", "EN") == "https://fixupx.com/BarackObama/status/266031293945503744/en"
    assert _fixed(url, "vxtwitter", "pt_BR") == "https://vxtwitter.com/BarackObama/status/266031293945503744/pt-br"
    # an existing suffix on an already-fixed link is replaced, not stacked
    fixed = "https://fxtwitter.com/BarackObama/status/266031293945503744/ja"
    assert _fixed(fixed, "fxtwitter", "es") == "https://fxtwitter.com/BarackObama/status/266031293945503744/es"
    assert _fixed(fixed, "fxtwitter") == "https://fxtwitter.com/BarackObama/status/266031293945503744"
    # junk language codes are ignored rather than pasted into the URL
    assert _fixed(url, "fxtwitter", "../evil") == "https://fxtwitter.com/BarackObama/status/266031293945503744"
    assert _fixed(url, "fxtwitter", "") == "https://fxtwitter.com/BarackObama/status/266031293945503744"


def test_fixed_url_ignores_lang_when_not_translatable() -> None:
    assert _fixed("https://www.pixiv.net/artworks/1", "phixiv", "es") == "https://phixiv.net/artworks/1"
    custom = Fixer("custom", "Custom", "example.com", translatable=False)
    m = sites.match("https://x.com/a/status/1")
    assert m is not None
    assert sites.fixed_url(m, custom, "es") == "https://example.com/a/status/1"


# --- chains --------------------------------------------------------------------------------------


def test_chain_configured_order() -> None:
    assert [f.id for f in sites.chain("twitter")] == ["fxtwitter", "vxtwitter", "fixupx"]
    assert [f.id for f in sites.chain("pixiv")] == ["phixiv", "ppxiv"]
    assert [f.id for f in sites.chain("reddit")] == ["vxreddit", "redditez"]
    assert [f.id for f in sites.chain("tiktok")] == ["fxtiktok", "tfxktok", "tiktokez"]
    assert [f.id for f in sites.chain("bluesky")] == ["fxbsky", "xbsky", "bskx"]
    assert [f.id for f in sites.chain("instagram")] == ["oginstagram", "fxig", "inso", "uuinstagram"]


def test_chain_moves_down_fixers_last() -> None:
    assert [f.id for f in sites.chain("twitter", {"fxtwitter"})] == ["vxtwitter", "fixupx", "fxtwitter"]
    got = sites.chain("twitter", ["fixupx", "fxtwitter"])
    assert [f.id for f in got] == ["vxtwitter", "fxtwitter", "fixupx"]
    everything = {"fxtwitter", "vxtwitter", "fixupx"}
    assert [f.id for f in sites.chain("twitter", everything)] == ["fxtwitter", "vxtwitter", "fixupx"]
    # down fixers from other sites don't matter
    assert [f.id for f in sites.chain("pixiv", {"fxtwitter", "ppxiv"})] == ["phixiv", "ppxiv"]


def test_chain_unknown_site() -> None:
    assert sites.chain("myspace") == []


# --- short links ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://vm.tiktok.com/ZMabcdef/", True),
        ("https://vt.tiktok.com/ZSabcdef/", True),
        ("https://www.tiktok.com/t/ZTabcdef/", True),
        ("https://tiktok.com/t/ZTabcdef", True),
        ("https://www.reddit.com/r/pics/s/AbCdEf123", True),
        ("https://reddit.com/r/pics/s/AbCdEf123/", True),
        ("https://www.instagram.com/share/p/BAbCdEf123/", True),
        ("https://www.instagram.com/share/BAbCdEf123", True),
        ("https://vm.tiktok.com/", False),
        ("https://www.tiktok.com/@discord/video/7437673559146827054", False),
        ("https://www.reddit.com/r/pics/comments/haucpf/", False),
        ("https://redd.it/haucpf", False),
        ("https://x.com/a/status/1", False),
        ("https://example.com/t/abc", False),
        ("not a url", False),
    ],
)
def test_needs_expansion(url: str, expected: bool) -> None:
    assert resolve.needs_expansion(url) is expected


@pytest.fixture
async def redirect_server():
    async def short(request: web.Request) -> web.Response:
        raise web.HTTPFound("/@someone/video/123456789")

    async def head_refused(request: web.Request) -> web.Response:
        if request.method == "HEAD":
            raise web.HTTPMethodNotAllowed("HEAD", ["GET"])
        raise web.HTTPFound("/@someone/video/987654321")

    async def walled(request: web.Request) -> web.Response:
        raise web.HTTPFound("/blocked")

    async def blocked(request: web.Request) -> web.Response:
        raise web.HTTPForbidden()

    async def final(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_route("*", "/short", short)
    app.router.add_route("*", "/head-refused", head_refused)
    app.router.add_route("*", "/walled", walled)
    app.router.add_route("*", "/blocked", blocked)
    app.router.add_route("*", "/@someone/video/{id}", final)
    server = test_utils.TestServer(app)
    await server.start_server()
    yield server
    await server.close()


async def test_expand_follows_redirects(redirect_server: test_utils.TestServer) -> None:
    async with ClientSession() as session:
        got = await resolve.expand(str(redirect_server.make_url("/short")), session)
    assert got == str(redirect_server.make_url("/@someone/video/123456789"))


async def test_expand_falls_back_to_get(redirect_server: test_utils.TestServer) -> None:
    async with ClientSession() as session:
        got = await resolve.expand(str(redirect_server.make_url("/head-refused")), session)
    assert got == str(redirect_server.make_url("/@someone/video/987654321"))


async def test_expand_keeps_final_url_even_if_blocked(redirect_server: test_utils.TestServer) -> None:
    async with ClientSession() as session:
        got = await resolve.expand(str(redirect_server.make_url("/walled")), session)
    assert got == str(redirect_server.make_url("/blocked"))


async def test_expand_returns_input_on_failure() -> None:
    url = "http://127.0.0.1:9/nothing-listens-here"
    async with ClientSession() as session:
        assert await resolve.expand(url, session) == url
        assert await resolve.expand("not a url", session) == "not a url"


# --- unknown mirror domains (shape-based fallback) -----------------------------------------------

MIRRORED = [
    (f"https://girlcockx.com/{OB}", TWEET, "BarackObama"),
    (f"https://cunnyx.com/{OB}/photo/1", TWEET, "BarackObama"),
    ("https://cunnyx.com/i/status/266031293945503744", TWEET, None),
    (f"https://some-brand-new-embedder.lol/{OB}/es", TWEET, "BarackObama"),
    (f"https://nitter.net/{OB}#m", TWEET, "BarackObama"),
    (f"https://nitter.poast.org/{OB}", TWEET, "BarackObama"),
    (f"https://xcancel.com/{OB}/video/1", TWEET, "BarackObama"),
    ("https://bsky.mirror.example/profile/bsky.app/post/3m4j4iy4oek2d", "bsky:bsky.app/3m4j4iy4oek2d",
     "bsky.app"),
    (f"https://bsky-mirror.example/profile/{DID}/post/3m4j4iy4oek2d", f"bsky:{DID}/3m4j4iy4oek2d", None),
    (f"https://tiktok-mirror.example/@discord/video/{TT}", TTK, "discord"),
    ("https://safereddit.com/r/pics/comments/haucpf/ive_found/", "reddit:haucpf", None),
]


@pytest.mark.parametrize(("url", "key", "author"), MIRRORED, ids=[u for u, _, _ in MIRRORED])
def test_unknown_mirror_matches_by_shape(url: str, key: str, author: str | None) -> None:
    m = sites.match(url)
    assert m is not None
    assert m.canonical_key == key
    assert m.parts.get("already_fixed") == "1"
    assert m.parts.get("mirror") == "1"
    assert m.author == author
    # fixer URLs are rebuilt from parts, so the mirror domain never leaks into them
    assert sites.fixed_url(m, m.site.fixers[0]).startswith(f"https://{m.site.fixers[0].domain}/")


def test_known_fixer_domain_is_not_a_mirror() -> None:
    for url in (f"https://fixvx.com/{OB}", f"https://fxtwitter.com/{OB}", f"https://x.com/{OB}"):
        m = sites.match(url)
        assert m is not None and m.canonical_key == TWEET
        assert "mirror" not in m.parts


@pytest.mark.parametrize(
    "url",
    [
        "https://mastodon.social/@someone/109876543210987654",
        "https://mastodon.social/@someone@example.com/109876543210987654",
        "https://mastodon.social/users/someone/statuses/109876543210987654",
        "https://mastodon.social/web/statuses/109876543210987654",
        "https://mastodon.social/web/@someone/109876543210987654",
        "https://lemmy.world/post/12345678901",
        "https://misskey.io/notes/9abcdefghijk",
        "https://example.com/someone/status/123",
        "https://example.com/status/12345678901234",
        "https://example.com/someone/status/12345678901234/edit",
        "https://example.com/a-very-long-username-here/status/12345678901234",
        "https://example.com/artworks/1580459",
        "https://example.com/en/artworks/1580459",
        "https://example.com/p/BsOGulcndj-/",
        "https://example.com/reel/C9AbCdEfGhI/",
        "https://example.com/profile/bsky.app/post/123",
        "https://example.com/profile/someone/post/3m4j4iy4oek2d",
        "https://example.com/@someone/video/123",
        "https://example.com/r/pics/",
        "https://example.com/comments/haucpf",
        "https://example.com/haucpf",
    ],
)
def test_unknown_domain_generic_paths_do_not_match(url: str) -> None:
    assert sites.match(url) is None
