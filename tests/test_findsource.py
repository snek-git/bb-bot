"""Track C: Find source (SauceNAO parsing, engine links, image picking, the cog flow)."""

from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import pytest

from bb import strings
from bb.cogs import findsource as fs

IMG = "https://pbs.twimg.com/media/abc.jpg?name=orig&format=jpg"


def sauce(similarity, url, index_name="Index #5: Pixiv Images - 1234_p0.jpg", **data):
    return {
        "header": {"similarity": str(similarity), "index_name": index_name, "index_id": 5, "thumbnail": "t"},
        "data": {"ext_urls": [url] if url else [], **data},
    }


RESPONSE = {
    "header": {"status": 0, "results_requested": 8, "short_remaining": 3, "long_remaining": 99},
    "results": [
        sauce(93.21, "https://www.pixiv.net/artworks/1234"),
        sauce(91.0, "https://danbooru.donmai.us/post/show/5", "Index #9: Danbooru - danbooru_abc.jpg"),
        sauce(90.5, "https://www.pixiv.net/artworks/1234"),  # duplicate link
        sauce(88.0, None, "Index #18: H-Misc - x.jpg"),  # no link
        sauce(75.0, "https://gelbooru.com/index.php?id=7", ""),
        sauce(70.0, "https://yande.re/post/show/9", "Index #12: Yande.re - y.jpg"),
        sauce(42.0, "https://example.com/low"),
    ],
}


# --- SauceNAO parsing -------------------------------------------------------------------


def test_parse_saucenao_filters_dedupes_and_limits():
    results = fs.parse_saucenao(RESPONSE)
    assert [(r.source, r.similarity, r.url) for r in results] == [
        ("Pixiv Images", 93.21, "https://www.pixiv.net/artworks/1234"),
        ("Danbooru", 91.0, "https://danbooru.donmai.us/post/show/5"),
        ("gelbooru.com", 75.0, "https://gelbooru.com/index.php?id=7"),
    ]


def test_results_name_the_character_artist_and_original_post():
    data = {
        "header": {"status": 0},
        "results": [
            {
                "header": {"similarity": "93.8", "index_name": "Index #9: Danbooru - x.jpg"},
                "data": {
                    "ext_urls": ["https://danbooru.donmai.us/post/show/5"],
                    "creator": "nanka0616",
                    "characters": "okita souji (fate), okita souji alter (fate), saber, nero claudius (fate)",
                    "source": "https://x.com/nanka0616/status/2108844411237077357",
                },
            },
            {
                "header": {"similarity": "88", "index_name": "Index #5: Pixiv Images - 1.jpg"},
                "data": {
                    "ext_urls": ["https://www.pixiv.net/artworks/1"],
                    "member_name": "<@123456789012345678> *x*",
                },
            },
            {
                "header": {"similarity": "80", "index_name": "Index #12: Yande.re - y.jpg"},
                "data": {"ext_urls": ["https://yande.re/post/show/9"]},
            },
        ],
    }
    lines = fs.format_results(fs.parse_saucenao(data)).splitlines()
    assert lines[0] == (
        "- [Danbooru](<https://danbooru.donmai.us/post/show/5>) 93.8% · "
        "Okita Souji (Fate), Okita Souji Alter (Fate), Saber +1 by nanka0616 · "
        "[original](<https://x.com/nanka0616/status/2108844411237077357>)"
    )
    assert "<@123" not in lines[1] and "\\*x\\*" in lines[1]  # names from other sites can't ping or format
    assert lines[2] == "- [Yande.re](<https://yande.re/post/show/9>) 80%"  # nothing known: just the link


def test_parse_saucenao_threshold_and_limit_are_parameters():
    results = fs.parse_saucenao(RESPONSE, min_similarity=40, limit=10)
    assert [r.similarity for r in results] == [93.21, 91.0, 75.0, 70.0, 42.0]


@pytest.mark.parametrize(
    "data",
    [
        {"header": {"status": -2, "message": "bad image"}, "results": [sauce(99, "https://a.b/c")]},
        {"header": {"status": 1}},
        {"header": {"status": 0}},  # no results key
        {"header": {"status": 0}, "results": [{"header": {"similarity": "n/a"}, "data": {}}]},
        None,
        [],
    ],
)
def test_parse_saucenao_handles_errors_and_junk(data):
    assert fs.parse_saucenao(data) == []


def test_format_results():
    results = fs.parse_saucenao(RESPONSE)
    text = fs.format_results(results)
    lines = text.splitlines()  # plain results, no BB line on top
    assert lines[0] == "- [Pixiv Images](<https://www.pixiv.net/artworks/1234>) 93.2%"
    assert lines[2] == "- [gelbooru.com](<https://gelbooru.com/index.php?id=7>) 75%"
    assert fs.format_results([]) in strings.variants("findsource.none")


# --- engine links -----------------------------------------------------------------------


def test_engine_links_encode_the_image_url():
    links = dict(fs.engine_links(IMG))
    assert list(links) == ["SauceNAO", "ascii2d", "IQDB", "Yandex", "Google Lens"]
    assert links["ascii2d"].startswith("https://ascii2d.net/search/url/https%3A%2F%2Fpbs.twimg.com")
    assert links["IQDB"].startswith("https://iqdb.org/?url=")
    assert links["Yandex"].startswith("https://yandex.com/images/search?rpt=imageview&url=")
    assert links["Google Lens"].startswith("https://lens.google.com/uploadbyurl?url=")
    for url in links.values():
        assert "&format=jpg" not in url  # the image's own query string must stay encoded
        assert IMG in unquote(url)


def test_engine_links_skip_urls_too_long_for_buttons():
    long_url = "https://cdn.discordapp.com/attachments/1/2/a.png?hm=" + "f" * 400
    links = fs.engine_links(long_url)
    assert all(len(url) <= fs.BUTTON_URL_LIMIT for _, url in links)
    assert len(links) < len(fs.ENGINES)


async def test_links_view_has_link_buttons():
    view = fs.links_view(IMG)
    assert [item.label for item in view.children] == ["SauceNAO", "ascii2d", "IQDB", "Yandex", "Google Lens"]
    assert all(item.url for item in view.children)


# --- picking the image ------------------------------------------------------------------


def attachment(url, content_type=None, filename="file.bin"):
    return SimpleNamespace(url=url, content_type=content_type, filename=filename)


def embed(image=None, thumbnail=None):
    return SimpleNamespace(image=SimpleNamespace(url=image), thumbnail=SimpleNamespace(url=thumbnail))


def test_pick_image_url_order():
    attachments = [attachment("https://a/vid.mp4", "video/mp4"), attachment("https://a/pic.png", "image/png")]
    assert fs.pick_image_url(attachments, [embed(image="https://e/img")]) == "https://a/pic.png"
    assert fs.pick_image_url([attachment("https://a/x.JPG", None, "x.JPG")], []) == "https://a/x.JPG"
    assert fs.pick_image_url([], [embed(thumbnail="https://e/thumb"), embed(image="https://e/img")]) == (
        "https://e/img"
    )
    assert fs.pick_image_url([], [embed(thumbnail="https://e/thumb")]) == "https://e/thumb"
    assert fs.pick_image_url([attachment("https://a/doc.txt", "text/plain")], [embed()]) is None


# --- the request ------------------------------------------------------------------------


class FakeResp:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def json(self, content_type=None):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.calls = status, data, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResp(self.status, self.data)


async def test_search_saucenao_request_and_parse():
    session = FakeSession(data=RESPONSE)
    results = await fs.search_saucenao(session, "KEY", IMG)
    assert len(results) == 3
    url, params = session.calls[0]
    assert url == "https://saucenao.com/search.php"
    assert params["output_type"] == "2" and params["api_key"] == "KEY" and params["url"] == IMG
    assert params["db"] == "999"


@pytest.mark.parametrize(
    ("status", "data"), [(429, {}), (200, {"header": {"status": -1, "message": "bad key"}}), (200, "html")]
)
async def test_search_saucenao_failures_return_none(status, data):
    assert await fs.search_saucenao(FakeSession(status, data), "KEY", IMG) is None


# --- the cog ----------------------------------------------------------------------------


def make_cog(key="KEY", paused=False):
    settings = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(paused=paused)))
    bot = SimpleNamespace(
        config=SimpleNamespace(saucenao_key=key), http_client=object(), tree=None, settings=settings
    )
    return fs.FindSource(bot)


_users = itertools.count(1)


def make_interaction(user_id=None):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    user = SimpleNamespace(id=next(_users) if user_id is None else user_id)
    return SimpleNamespace(
        response=response, followup=SimpleNamespace(send=AsyncMock()), guild_id=1, user=user
    )


async def test_run_without_image_answers_privately():
    interaction = make_interaction()
    await make_cog().run(interaction, SimpleNamespace(attachments=[], embeds=[]))
    args, kwargs = interaction.response.send_message.await_args
    assert args[0] in strings.variants("findsource.no_image") and kwargs["ephemeral"] is True
    interaction.followup.send.assert_not_awaited()


async def test_run_posts_results_publicly_and_caches(monkeypatch):
    search = AsyncMock(return_value=fs.parse_saucenao(RESPONSE))
    monkeypatch.setattr(fs, "search_saucenao", search)
    cog = make_cog()
    message = SimpleNamespace(attachments=[], embeds=[embed(image=IMG)])
    for _ in range(2):
        interaction = make_interaction()
        await cog.run(interaction, message)
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        args, kwargs = interaction.followup.send.await_args
        assert "pixiv.net/artworks/1234" in args[0]
        assert "ephemeral" not in kwargs
        assert [item.label for item in kwargs["view"].children][0] == "SauceNAO"
    search.assert_awaited_once()


async def test_run_without_key_only_links(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(fs, "search_saucenao", search)
    interaction = make_interaction()
    await make_cog(key=None).run(interaction, SimpleNamespace(attachments=[], embeds=[embed(image=IMG)]))
    search.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()  # nothing to wait for: answer straight away
    args, kwargs = interaction.response.send_message.await_args
    assert args[0] in strings.variants("findsource.manual")  # honest: no search happened, so not "no matches"
    assert "ephemeral" not in kwargs and len(kwargs["view"].children) == 5


async def test_one_search_per_person_per_cooldown(monkeypatch):
    # Every search posts publicly: a person mashing buttons gets told off privately, once BB answered.
    search = AsyncMock(return_value=fs.parse_saucenao(RESPONSE))
    monkeypatch.setattr(fs, "search_saucenao", search)
    cog = make_cog()
    message = SimpleNamespace(attachments=[], embeds=[embed(image=IMG)])
    first, second, other = make_interaction(7), make_interaction(7), make_interaction(8)
    for interaction in (first, second, other):
        await cog.run(interaction, message)
    first.followup.send.assert_awaited_once()
    second.followup.send.assert_not_awaited()
    args, kwargs = second.response.send_message.await_args
    assert args[0] in strings.variants("findsource.cooldown") and kwargs["ephemeral"] is True
    other.followup.send.assert_awaited_once()  # someone else isn't held up


async def test_paused_bb_does_not_search(monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(fs, "search_saucenao", search)
    interaction = make_interaction()
    await make_cog(paused=True).run(interaction, SimpleNamespace(attachments=[], embeds=[embed(image=IMG)]))
    search.assert_not_awaited()
    args, kwargs = interaction.response.send_message.await_args
    assert args[0] in strings.variants("findsource.paused") and kwargs["ephemeral"] is True


async def test_failed_lookup_is_not_cached(monkeypatch):
    search = AsyncMock(return_value=None)
    monkeypatch.setattr(fs, "search_saucenao", search)
    cog = make_cog()
    assert await cog.lookup(IMG) is None  # couldn't search: not the same as "no matches"
    assert await cog.lookup(IMG) is None
    assert search.await_count == 2


async def test_failed_search_says_so(monkeypatch):
    monkeypatch.setattr(fs, "search_saucenao", AsyncMock(return_value=None))
    interaction = make_interaction()
    await make_cog().run(interaction, SimpleNamespace(attachments=[], embeds=[embed(image=IMG)]))
    args, _ = interaction.followup.send.await_args
    assert args[0] in strings.variants("findsource.manual")


async def test_context_menu_is_registered_and_removed():
    added, removed = [], []
    tree = SimpleNamespace(add_command=added.append, remove_command=lambda name, type: removed.append(name))
    cog = make_cog()
    cog.bot.tree = tree
    await cog.cog_load()
    await cog.cog_unload()
    assert added[0].name == "Find source" and removed == ["Find source"]
