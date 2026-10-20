from __future__ import annotations

import pytest

from bb import nhentai

N1 = "https://nhentai.net/g/450000/"
API = "https://nhentai.net/api/v2"
CDN = {
    "image_servers": ["https://i1.example.net", "https://i2.example.net"],
    "thumb_servers": ["https://t1.example.net/"],
}
PAGE1 = "https://i1.example.net/galleries/2518280/1.webp"  # 450000 % 2 == 0: the first image server


def meta(gid: int = 450000, tags: list[dict] | None = None, **extra) -> dict:
    """Shaped like a real GET /api/v2/galleries/{id} answer (trimmed)."""
    return {
        "id": gid,
        "media_id": "2518280",
        "title": {"english": "(C99) [Circle (Artist)] Title", "japanese": "タイトル", "pretty": "Title"},
        "cover": {"path": "galleries/2518280/cover.webp.webp", "width": 350, "height": 500},
        "thumbnail": {"path": "galleries/2518280/thumb.webp", "width": 250, "height": 357},
        "tags": tags
        if tags is not None
        else [
            {"type": "category", "name": "doujinshi"},
            {"type": "language", "name": "translated"},
            {"type": "language", "name": "english"},
            {"type": "parody", "name": "touhou project"},
            {"type": "character", "name": "reimu hakurei"},
            {"type": "artist", "name": "someone"},
            {"type": "tag", "name": "glasses"},
            {"type": "tag", "name": "sole female"},
        ],
        "num_pages": 13,
        "num_favorites": 3232,
        "pages": [
            {"number": 1, "path": "galleries/2518280/1.webp"},
            {"number": 2, "path": "galleries/2518280/2.webp"},
        ],
        **extra,
    }


class _Content:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i : i + n]


class Response:
    def __init__(self, status=200, body=None, *, headers=None, content_type="application/json", data=b""):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.content_type = content_type
        self.content_length = len(data) if data else None
        self.content = _Content(data)

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Answers GETs from a URL table and records (url, headers) for each one."""

    def __init__(self, routes: dict[str, Response]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        return self.routes.get(url) or Response(404, {"error": "Not found"})

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(nhentai, "_api_key", None)
    monkeypatch.setattr(nhentai, "_backoff_until", 0.0)
    monkeypatch.setattr(nhentai, "_meta_cache", {})
    monkeypatch.setattr(nhentai, "_cover_cache", {})
    monkeypatch.setattr(nhentai, "_cdn", None)


# --- links --------------------------------------------------------------------------------


def test_find_galleries_forms_and_dedupe():
    text = (
        f"look {N1} and the reader https://nhentai.net/g/450000/3/, "
        "https://www.nhentai.net/g/123?x=1 and ||http://nhentai.net/g/7||"
    )
    found = nhentai.find_galleries(text)
    assert [(g.gid, g.spoiler) for g in found] == [(450000, False), (123, False), (7, True)]
    assert found[0].canonical_key == "nhentai:450000"
    assert found[1].link == "https://nhentai.net/g/123/"  # always the plain gallery page


def test_find_galleries_skips_other_pages_code_and_no_preview_links():
    assert nhentai.find_galleries(f"`{N1}` and <{N1}>") == []
    assert nhentai.find_galleries("https://nhentai.net/tag/glasses/ https://nhentai.net/g/abc/") == []
    assert nhentai.find_galleries("https://nhentai.net/g/1234567890123/") == []  # not a gallery id
    assert nhentai.find_galleries("https://nhentai.net.evil.example/g/1/") == []


def test_find_galleries_caps_at_four():
    text = " ".join(f"https://nhentai.net/g/{n}/" for n in range(1, 9))
    assert len(nhentai.find_galleries(text)) == nhentai.MAX_GALLERIES


def test_gallery_as_match():
    m = nhentai.find_galleries(f"||{N1}||")[0].as_match()
    assert (m.site.id, m.url, m.canonical_key, m.spoiler) == ("nhentai", N1, "nhentai:450000", True)


def test_blocked_tags_by_name_whatever_the_type():
    blocked = (
        {"type": "tag", "name": "lolicon"},
        {"type": "tag", "name": "Shotacon"},
        {"name": "toddlercon"},  # whatever the type, even none
    )
    for tag in blocked:
        assert nhentai.is_blocked(meta(tags=[{"type": "parody", "name": "x"}, tag])), tag
    assert not nhentai.is_blocked(meta())
    assert not nhentai.is_blocked(meta(tags=[]))
    assert not nhentai.is_blocked({"tags": "lolicon"})  # malformed: not a list


# --- the card -------------------------------------------------------------------------------


def _parts(view):
    texts = [c for c in view.children if type(c).__name__ == "TextDisplay"]
    (container,) = [c for c in view.children if type(c).__name__ == "Container"]
    return texts, container.children


def test_card_text():
    text = nhentai.card_text(meta(), nhentai.Gallery(450000))
    title, stats = text.split("\n")[:2]
    assert title == f"### [(C99) \\[Circle (Artist)\\] Title]({N1})"
    assert stats == "Doujinshi · English · 13 pages · ♥ 3,232"
    assert "Translated" not in text
    assert "**Parody:** touhou project" in text
    assert "**Characters:** reimu hakurei" in text
    assert "**Artist:** someone" in text
    assert "**Tags:** glasses, sole female" in text
    assert "Group" not in text  # no group tags, no line


def test_card_title_falls_back():
    gallery = nhentai.Gallery(450000)
    assert "### [Title](" in nhentai.card_text(meta(title={"english": "", "pretty": "Title"}), gallery)
    assert "### [Gallery 450000](" in nhentai.card_text(meta(title=None), gallery)


def test_card_with_full_size_cover():
    view = nhentai.build_card(meta(), nhentai.Gallery(450000), cover=nhentai.Cover(b"img", "cover.webp"))
    texts, (gallery_item, _, row) = _parts(view)
    assert texts == []  # no quip line
    assert gallery_item.items[0].media.url == "attachment://cover.webp"
    assert gallery_item.items[0].spoiler is False
    assert row.children[0].url == N1 and row.children[0].label == "Open on nhentai"
    assert view.timeout is None


def test_card_falls_back_to_the_thumbnail_and_shows_the_quip(monkeypatch):
    monkeypatch.setattr(nhentai, "_cdn", (nhentai._now(), [], ["https://t1.example.net"]))
    view = nhentai.build_card(meta(), nhentai.Gallery(450000, spoiler=True), cover=None, quip="hi")
    (quip,), (gallery_item, _, _) = _parts(view)
    assert quip.content == "-# hi"
    assert gallery_item.items[0].media.url == "https://t1.example.net/galleries/2518280/thumb.webp"
    assert gallery_item.items[0].spoiler is True


def test_card_without_any_image_is_still_a_card():
    view = nhentai.build_card(meta(), nhentai.Gallery(450000), cover=None)  # no CDN list yet
    _, children = _parts(view)
    assert [type(c).__name__ for c in children] == ["TextDisplay", "ActionRow"]


# --- API ------------------------------------------------------------------------------------


async def test_fetch_metadata_caches():
    session = Session({f"{API}/galleries/450000": Response(body=meta())})
    galleries = nhentai.find_galleries(f"{N1} https://nhentai.net/g/9/")
    got = await nhentai.fetch_metadata(session, galleries)
    assert list(got) == [450000]  # 9 is a 404: no card
    assert sorted(session.urls) == [f"{API}/galleries/450000", f"{API}/galleries/9"]
    assert session.calls[0][1] == {}  # no key, no Authorization header
    again = await nhentai.fetch_metadata(session, galleries[:1])
    assert again == got and len(session.calls) == 2  # from the cache


async def test_fetch_metadata_sends_the_api_key():
    nhentai.set_api_key("  secret  ")
    session = Session({f"{API}/galleries/450000": Response(body=meta())})
    await nhentai.fetch_metadata(session, nhentai.find_galleries(N1))
    assert session.calls[0][1] == {"Authorization": "Key secret"}
    nhentai.set_api_key("")
    assert nhentai._api_key is None


async def test_fetch_metadata_drops_odd_answers():
    session = Session({f"{API}/galleries/450000": Response(body=meta(gid=1))})  # someone else's gallery
    assert await nhentai.fetch_metadata(session, nhentai.find_galleries(N1)) == {}
    session = Session({f"{API}/galleries/450000": Response(body=["not", "a", "gallery"])})
    assert await nhentai.fetch_metadata(session, nhentai.find_galleries(N1)) == {}


async def test_429_backs_off_for_retry_after(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(nhentai, "_now", lambda: clock[0])
    limited = Response(429, {"error": "Rate limit exceeded"}, headers={"Retry-After": "60"})
    session = Session({f"{API}/galleries/450000": limited})
    galleries = nhentai.find_galleries(N1)
    assert await nhentai.fetch_metadata(session, galleries) == {}
    assert nhentai.backing_off()
    clock[0] += 59
    assert await nhentai.fetch_metadata(session, galleries) == {}
    assert len(session.calls) == 1  # no request at all while backing off
    clock[0] += 2
    session.routes[f"{API}/galleries/450000"] = Response(body=meta())
    assert list(await nhentai.fetch_metadata(session, galleries)) == [450000]


async def test_429_without_retry_after_backs_off_a_minute(monkeypatch):
    monkeypatch.setattr(nhentai, "_now", lambda: 1000.0)
    session = Session({f"{API}/galleries/450000": Response(429, headers={"Retry-After": "soon"})})
    await nhentai.fetch_metadata(session, nhentai.find_galleries(N1))
    assert nhentai._backoff_until == 1000.0 + nhentai.DEFAULT_BACKOFF


# --- cover ----------------------------------------------------------------------------------


def cdn_session(page1: Response) -> Session:
    return Session({f"{API}/cdn": Response(body=CDN), PAGE1: page1})


async def test_fetch_cover_uses_the_cdn_list_and_page_one_as_given():
    session = cdn_session(Response(content_type="image/webp", data=b"webpdata"))
    gallery = nhentai.Gallery(450000)
    cover = await nhentai.fetch_cover(session, gallery, meta(), 10_000_000)
    assert cover == nhentai.Cover(b"webpdata", "cover.webp")
    assert session.urls == [f"{API}/cdn", PAGE1]
    assert await nhentai.fetch_cover(session, gallery, meta(), 10_000_000) == cover
    assert len(session.calls) == 2  # second time from the cache
    # the CDN list is kept, so the thumbnail fallback can use it (trailing slash trimmed)
    assert nhentai.thumbnail_url(meta(), 450000) == "https://t1.example.net/galleries/2518280/thumb.webp"


async def test_fetch_cover_gives_up_quietly():
    gallery = nhentai.Gallery(450000)
    session = Session({})  # /cdn is a 404: no servers, so no image request either
    assert await nhentai.fetch_cover(session, gallery, meta(), 10_000_000) is None
    assert session.urls == [f"{API}/cdn"]
    no_pages = Session({f"{API}/cdn": Response(body=CDN)})
    assert await nhentai.fetch_cover(no_pages, gallery, meta(pages=[]), 10_000_000) is None
    assert no_pages.calls == []


async def test_fetch_cover_respects_the_size_limit():
    session = cdn_session(Response(content_type="image/jpeg", data=b"x" * 2000))
    assert await nhentai.fetch_cover(session, nhentai.Gallery(450000), meta(), 1000) is None


async def test_cdn_429_backs_off_too():
    session = cdn_session(Response(429, headers={"Retry-After": "30"}, content_type="text/plain"))
    assert await nhentai.fetch_cover(session, nhentai.Gallery(450000), meta(), 10_000_000) is None
    assert nhentai.backing_off()
