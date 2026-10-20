from __future__ import annotations

from bb import ehentai

G1 = "https://e-hentai.org/g/618395/0439fa3666/"


def meta(gid: int = 618395, tags: list[str] | None = None, **extra) -> dict:
    return {
        "gid": gid,
        "token": "0439fa3666",
        "title": "Touhou &amp; Friends",
        "category": "Doujinshi",
        "filecount": "20",
        "rating": "4.55",
        "thumb": "https://ehgt.org/w/00/310/49862-ddt1sawg.webp",
        "tags": tags
        if tags is not None
        else ["parody:touhou project", "character:reimu hakurei", "female:glasses"],
        **extra,
    }


def test_find_galleries_forms_and_dedupe():
    text = (
        f"look {G1} and again {G1}, plus https://exhentai.org/g/123/abcdef0123/?p=2 "
        "and ||https://e-hentai.org/g/7/1111111111/||"
    )
    found = ehentai.find_galleries(text)
    assert [(g.gid, g.url, g.spoiler) for g in found] == [
        (618395, G1, False),
        (123, "https://e-hentai.org/g/123/abcdef0123/", False),  # exhentai rewritten to e-hentai
        (7, "https://e-hentai.org/g/7/1111111111/", True),
    ]
    assert found[0].canonical_key == "ehentai:618395"


def test_find_galleries_skips_code_and_no_preview_links():
    assert ehentai.find_galleries(f"`{G1}` and <{G1}>") == []
    assert ehentai.find_galleries("https://e-hentai.org/s/abc/618395-1") == []  # single pages: not yet


def test_find_galleries_caps_at_four():
    text = " ".join(f"https://e-hentai.org/g/{n}/0123456789/" for n in range(1, 9))
    assert len(ehentai.find_galleries(text)) == ehentai.MAX_GALLERIES


def test_parse_gdata_drops_errors():
    payload = {"gmetadata": [meta(), {"gid": 5, "error": "Key missing, or incorrect key provided."}]}
    assert list(ehentai.parse_gdata(payload)) == [618395]
    assert ehentai.parse_gdata({}) == {}


def _parts(view):
    texts = [c for c in view.children if type(c).__name__ == "TextDisplay"]
    (container,) = [c for c in view.children if type(c).__name__ == "Container"]
    return texts, container.children


def test_card_with_full_size_cover():
    gallery = ehentai.find_galleries(G1)[0]
    view = ehentai.build_card(meta(), gallery, cover=ehentai.Cover(b"img", "cover.webp"))
    texts, (gallery_item, text, row) = _parts(view)
    assert texts == []  # no quip line
    assert gallery_item.items[0].media.url == "attachment://cover.webp"  # big, on top
    assert gallery_item.items[0].spoiler is False
    assert text.content.startswith(f"### [Touhou & Friends]({G1})\nDoujinshi · 20 pages · ★ 4.55")
    assert "**Parody:** touhou project" in text.content
    assert "**Characters:** reimu hakurei" in text.content
    assert "**Tags:** glasses" in text.content
    assert row.children[0].url == G1 and row.children[0].label == "Open on e-hentai"
    assert view.timeout is None


def test_card_falls_back_to_the_api_thumbnail_and_shows_the_quip():
    gallery = ehentai.find_galleries(G1)[0]
    view = ehentai.build_card(meta(), gallery, cover=None, quip="hi")
    (quip,), (gallery_item, _, _) = _parts(view)
    assert quip.content == "-# hi"
    assert gallery_item.items[0].media.url == "https://ehgt.org/w/00/310/49862-ddt1sawg.webp"


def test_spoilered_link_blurs_the_cover():
    gallery = ehentai.find_galleries(f"||{G1}||")[0]
    view = ehentai.build_card(meta(), gallery, cover=ehentai.Cover(b"img", "cover.jpg"))
    _, (gallery_item, _, _) = _parts(view)
    assert gallery_item.items[0].spoiler is True


def test_brackets_in_titles_dont_break_the_link():
    gallery = ehentai.find_galleries(G1)[0]
    text = ehentai.card_text(meta(title="(C96) [Group (Artist)] Title"), gallery)
    assert text.startswith(f"### [(C96) \\[Group (Artist)\\] Title]({G1})")


def test_page_parsing():
    html = '<a href="https://e-hentai.org/s/ef945ea8e7/2123135-1"><a href="https://e-hentai.org/s/aa/2123135-10">'
    assert ehentai.first_page_url(html, 2123135) == "https://e-hentai.org/s/ef945ea8e7/2123135-1"
    assert ehentai.first_page_url(html, 999) is None
    page = '<img id="img" src="https://a.hath.network/h/x/y.webp?a=1&amp;b=2" style="">'
    assert ehentai.page_image_url(page) == "https://a.hath.network/h/x/y.webp?a=1&b=2"


async def test_fetch_cover_follows_the_pages_and_caches(monkeypatch):
    calls = []

    async def get_text(session, url):
        calls.append(url)
        if "/g/" in url:
            return '<a href="https://e-hentai.org/s/ef945ea8e7/618395-1">'
        return '<img id="img" src="https://n.hath.network/i.webp">'

    async def get_image(session, url, max_bytes):
        calls.append(url)
        return b"webpdata", "webp"

    monkeypatch.setattr(ehentai, "_get_text", get_text)
    monkeypatch.setattr(ehentai, "_get_image", get_image)
    monkeypatch.setattr(ehentai, "_cover_cache", {})
    gallery = ehentai.find_galleries(G1)[0]
    cover = await ehentai.fetch_cover(None, gallery, meta(), 10_000_000)
    assert cover == ehentai.Cover(b"webpdata", "cover.webp")
    assert calls == [G1, "https://e-hentai.org/s/ef945ea8e7/618395-1", "https://n.hath.network/i.webp"]
    assert await ehentai.fetch_cover(None, gallery, meta(), 10_000_000) == cover
    assert len(calls) == 3  # second time from the cache


async def test_fetch_cover_gives_up_quietly(monkeypatch):
    async def get_text(session, url):
        return "<html>no pages here</html>"

    monkeypatch.setattr(ehentai, "_get_text", get_text)
    monkeypatch.setattr(ehentai, "_cover_cache", {})
    assert await ehentai.fetch_cover(None, ehentai.find_galleries(G1)[0], meta(), 10_000_000) is None


def test_exhentai_links_stay_on_exhentai():
    (gallery,) = ehentai.find_galleries("https://exhentai.org/g/123/abcdef0123/")
    assert gallery.url == "https://e-hentai.org/g/123/abcdef0123/"  # fetched from e-hentai
    assert gallery.link == "https://exhentai.org/g/123/abcdef0123/"  # but the card points to ExHentai
    view = ehentai.build_card(meta(gid=123), gallery, cover=None)
    _, (_, text, row) = _parts(view)
    assert "(https://exhentai.org/g/123/abcdef0123/)" in text.content
    assert (row.children[0].label, row.children[0].url) == ("Open on ExHentai", gallery.link)
    (plain,) = ehentai.find_galleries(G1)
    assert plain.link == G1 and plain.open_label == "Open on e-hentai"
