"""Track C: repost buttons (views + buttons cog) and ping replies (chat cog). No Discord connection needed."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bb import strings, views
from bb.cogs import buttons, chat
from bb.db import RepostRow
from bb.links import extract, resolve, sites
from bb.links.extract import FoundURL
from bb.links.model import Fixer, LinkMatch, Site
from bb.settings_store import SettingsStore

FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True)
VX = Fixer("vxtwitter", "vxTwitter", "vxtwitter.com", translatable=False)
TWITTER = Site("twitter", "Twitter", "Tweet", (FX, VX), "https://x.com/i/status/1")
FIXERS = {f.id: f for f in (FX, VX)}


@pytest.fixture
def fake_sites(monkeypatch):
    """Stand-ins for track A's site table, so these tests never touch the stubs."""

    def match(url, spoiler=False):
        if "x.com" not in url:
            return None
        return LinkMatch(
            TWITTER, url, "twitter:1", author="bb", author_url="https://x.com/bb", spoiler=spoiler
        )

    def fixed_url(m, fixer, lang=None):
        url = f"https://{fixer.domain}/bb/status/1"
        return f"{url}/{lang}" if lang and fixer.translatable else url

    def chain(site_id, down=()):
        fixers = list(TWITTER.fixers)
        return [f for f in fixers if f.id not in down] + [f for f in fixers if f.id in down]

    monkeypatch.setattr(sites, "match", match)
    monkeypatch.setattr(sites, "fixed_url", fixed_url)
    monkeypatch.setattr(sites, "chain", chain)
    monkeypatch.setattr(sites, "get_fixer", FIXERS.get)


class FakeResponse:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    async def send_message(self, *args, **kwargs):
        self._record("send_message", *args, **kwargs)

    async def edit_message(self, *args, **kwargs):
        self._record("edit_message", *args, **kwargs)

    async def defer(self, *args, **kwargs):
        self._record("defer", *args, **kwargs)

    def is_done(self) -> bool:
        return bool(self.calls)

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def make_interaction(*, user_id=1, message=None, manage_messages=False, channel=None, client=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        message=message,
        channel=channel,
        client=client,
        permissions=discord.Permissions(manage_messages=manage_messages),
        response=FakeResponse(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def make_message(message_id=500, *, webhook_id=None, components=()):
    return SimpleNamespace(
        id=message_id,
        webhook_id=webhook_id,
        components=list(components),
        delete=AsyncMock(),
        attachments=[],
        embeds=[],
    )


def make_row(**overrides) -> RepostRow:
    data = dict(
        repost_id=500,
        guild_id=1,
        channel_id=5,
        origin_message_id=100,
        origin_author_id=7,
        site="twitter",
        canonical_key="twitter:1",
        original_url="https://x.com/bb/status/1",
        fixer_id="fxtwitter",
        status="ok",
    )
    data.update(overrides)
    return RepostRow(**data)


def make_bot(db, webhook=None):
    return SimpleNamespace(
        db=db,
        settings=SettingsStore(db),
        down_fixers=set(),
        fetch_webhook=AsyncMock(return_value=webhook),
        get_cog=lambda name: None,
    )


def _ids(view: discord.ui.View) -> list[str]:
    return [item.custom_id for item in view.children]


# --- custom ids and the view ------------------------------------------------------------


def test_custom_id_roundtrip():
    assert views.delete_id(1234567890123456789) == "bb:del:1234567890123456789"
    assert views.parse_delete_id("bb:del:42") == 42
    assert views.parse_delete_id("bb:del:42x") is None
    assert views.parse_delete_id("bb:tr:1") is None
    assert views.translate_id(True) == "bb:tr:1" and views.translate_id(False) == "bb:tr:0"
    assert views.parse_translate_id("bb:tr:1") is True
    assert views.parse_translate_id("bb:tr:0") is False
    assert views.parse_translate_id("bb:tr:2") is None


def test_templates_fullmatch_only_their_ids():
    # discord.py dispatches with pattern.fullmatch, so ids must not bleed into each other.
    templates = [cls.__discord_ui_compiled_template__ for cls in views.DYNAMIC_ITEMS]
    for custom_id, expected in [("bb:del:7", 0), ("bb:tr:0", 1), ("bb:src", 2)]:
        hits = [i for i, t in enumerate(templates) if t.fullmatch(custom_id)]
        assert hits == [expected]
    assert not any(t.fullmatch("bb:srcx") for t in templates)


async def test_view_for_tweet_with_image():
    view = views.make_repost_view(
        site_id="twitter", origin_author_id=7, translated=False, has_image=True,
        original_url="https://x.com/a/status/1",
    )
    assert _ids(view) == ["bb:src", "bb:tr:0", None]
    labels = [getattr(item, "item", item).label for item in view.children]
    assert labels == ["Find source", "Translate", "Open on X"]
    assert view.children[-1].url == "https://x.com/a/status/1"
    assert view.timeout is None and view.is_persistent()


async def test_view_variants():
    assert _ids(views.make_repost_view(site_id="pixiv", origin_author_id=7)) == []
    pixiv = views.make_repost_view(site_id="pixiv", origin_author_id=7, original_url="https://www.pixiv.net/artworks/1")
    assert pixiv.children[0].label == "Open on Pixiv"
    translated = views.make_repost_view(site_id="twitter", origin_author_id=7, translated=True)
    assert _ids(translated) == ["bb:tr:1"]
    assert translated.children[0].item.label == "Original"


async def test_dynamic_items_rebuild_from_custom_id():
    item = await views.DeleteButton.from_custom_id(
        None, None, re.fullmatch(views.DELETE_TEMPLATE, "bb:del:99")
    )
    assert item.origin_author_id == 99 and item.custom_id == "bb:del:99"
    item = await views.TranslateButton.from_custom_id(
        None, None, re.fullmatch(views.TRANSLATE_TEMPLATE, "bb:tr:1")
    )
    assert item.translated is True


def test_has_find_source_walks_action_rows():
    row = SimpleNamespace(
        children=[SimpleNamespace(custom_id="bb:tr:0"), SimpleNamespace(custom_id="bb:src")]
    )
    assert views.has_find_source([row])
    assert not views.has_find_source([SimpleNamespace(children=[SimpleNamespace(custom_id="bb:del:1")])])


async def test_callback_without_cog_answers_with_error():
    interaction = make_interaction(client=SimpleNamespace(get_cog=lambda name: None))
    await views.DeleteButton(7).callback(interaction)
    assert interaction.response.names == ["send_message"]
    assert interaction.response.calls[0][2]["ephemeral"] is True


# --- delete -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "manage", "allowed"), [(7, False, True), (8, True, True), (8, False, False)]
)
def test_can_delete(user, manage, allowed):
    assert buttons.can_delete(user, 7, manage) is allowed


def test_webhook_id_for():
    assert buttons.webhook_id_for(make_row(via_webhook=True, webhook_id=9), make_message()) == 9
    assert buttons.webhook_id_for(make_row(), make_message()) is None
    assert buttons.webhook_id_for(None, make_message(webhook_id=11)) == 11


async def test_delete_denied_for_strangers(db):
    cog = buttons.Buttons(make_bot(db))
    message = make_message()
    interaction = make_interaction(user_id=8, message=message)
    await cog.on_delete(interaction, 7)
    name, args, kwargs = interaction.response.calls[0]
    assert name == "send_message" and kwargs["ephemeral"] is True
    assert args[0] in [strings.fill(v, author="<@7>") for v in strings.variants("delete.denied")]
    message.delete.assert_not_awaited()


async def test_delete_by_poster_restores_hidden_preview(db):
    await db.insert_repost(make_row())
    original = SimpleNamespace(flags=SimpleNamespace(suppress_embeds=True), edit=AsyncMock())
    channel = SimpleNamespace(id=5, fetch_message=AsyncMock(return_value=original))
    message = make_message()
    cog = buttons.Buttons(make_bot(db))
    await cog.on_delete(make_interaction(user_id=7, message=message, channel=channel), 7)
    message.delete.assert_awaited_once()
    assert await db.get_repost(500) is None
    channel.fetch_message.assert_awaited_once_with(100)
    original.edit.assert_awaited_once_with(suppress=False)


async def test_delete_keeps_preview_hidden_while_other_reposts_remain(db):
    await db.insert_repost(make_row())
    await db.insert_repost(make_row(repost_id=501, canonical_key="twitter:2"))
    channel = SimpleNamespace(id=5, fetch_message=AsyncMock())
    cog = buttons.Buttons(make_bot(db))
    await cog.on_delete(
        make_interaction(user_id=8, manage_messages=True, message=make_message(), channel=channel), 7
    )
    assert await db.get_repost(500) is None and await db.get_repost(501) is not None
    channel.fetch_message.assert_not_awaited()


async def test_delete_webhook_repost_goes_through_webhook(db):
    await db.insert_repost(make_row(via_webhook=True, webhook_id=9))
    webhook = SimpleNamespace(delete_message=AsyncMock())
    bot = make_bot(db, webhook)
    message = make_message(webhook_id=9)
    channel = SimpleNamespace(
        id=5,
        fetch_message=AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")
        ),
    )
    await buttons.Buttons(bot).on_delete(make_interaction(user_id=7, message=message, channel=channel), 7)
    bot.fetch_webhook.assert_awaited_once_with(9)
    webhook.delete_message.assert_awaited_once()
    assert webhook.delete_message.await_args.kwargs["thread"] is discord.utils.MISSING
    message.delete.assert_not_awaited()
    assert await db.get_repost(500) is None


async def test_delete_falls_back_to_direct_delete_when_webhook_is_gone(db):
    await db.insert_repost(make_row(via_webhook=True, webhook_id=9))
    bot = make_bot(db)
    bot.fetch_webhook = AsyncMock(
        side_effect=discord.NotFound(SimpleNamespace(status=404, reason="nf"), "gone")
    )
    message = make_message(webhook_id=9)
    channel = SimpleNamespace(
        id=5,
        fetch_message=AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        ),
    )
    interaction = make_interaction(user_id=7, message=message, channel=channel)
    await buttons.Buttons(bot).on_delete(interaction, 7)
    message.delete.assert_awaited_once()
    interaction.followup.send.assert_not_awaited()
    assert await db.get_repost(500) is None


async def test_failed_delete_keeps_the_row(db):
    await db.insert_repost(make_row())
    message = make_message()
    message.delete = AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no"))
    interaction = make_interaction(user_id=7, message=message, channel=SimpleNamespace(id=5))
    await buttons.Buttons(make_bot(db)).on_delete(interaction, 7)
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    assert await db.get_repost(500) is not None


# --- translate --------------------------------------------------------------------------


def test_translated_text_flips_and_back(fake_sites):
    text, fixer, quip = buttons.translated_text(make_row(), True, "en", set())
    assert fixer is FX
    assert "(https://fxtwitter.com/bb/status/1/en)" in text
    assert quip in strings.variants("quip.translated") and text.startswith(f"-# {quip} ")
    text, fixer, quip = buttons.translated_text(make_row(translated=True, quip="x"), False, "en", set())
    assert "(https://fxtwitter.com/bb/status/1)" in text and quip is None and "-#" not in text


def test_translated_text_switches_to_translatable_fixer(fake_sites):
    _, fixer, _ = buttons.translated_text(make_row(fixer_id="vxtwitter"), True, "ja", set())
    assert fixer is FX


@pytest.mark.parametrize(
    "row",
    [make_row(status="uploaded"), make_row(original_url="https://example.com/1")],
)
def test_translated_text_refuses_unrebuildable_rows(fake_sites, row):
    with pytest.raises(LookupError):
        buttons.translated_text(row, True, "en", set())


async def test_translate_edits_message_and_row(db, fake_sites):
    await db.insert_repost(make_row(fixer_id="vxtwitter"))
    await SettingsStore(db).update(1, translate_lang="ja")
    bot = make_bot(db)
    bot.settings = SettingsStore(db)
    row_component = SimpleNamespace(children=[SimpleNamespace(custom_id="bb:src")])
    interaction = make_interaction(message=make_message(components=[row_component]))
    await buttons.Buttons(bot).on_translate(interaction, False)
    name, _, kwargs = interaction.response.calls[0]
    assert name == "edit_message"
    assert "(https://fxtwitter.com/bb/status/1/ja)" in kwargs["content"]
    assert _ids(kwargs["view"])[:2] == ["bb:src", "bb:tr:1"]
    row = await db.get_repost(500)
    assert row.translated is True and row.fixer_id == "fxtwitter" and row.quip


async def test_translate_webhook_repost_in_thread(db, fake_sites, monkeypatch):
    await db.insert_repost(make_row(via_webhook=True, webhook_id=9, translated=True))
    webhook = SimpleNamespace(edit_message=AsyncMock())
    bot = make_bot(db, webhook)

    class FakeThread:
        id = 77

    monkeypatch.setattr(discord, "Thread", FakeThread)
    interaction = make_interaction(message=make_message(webhook_id=9), channel=FakeThread())
    await buttons.Buttons(bot).on_translate(interaction, True)
    assert interaction.response.names == ["defer"]
    kwargs = webhook.edit_message.await_args.kwargs
    assert kwargs["thread"].id == 77
    assert "(https://fxtwitter.com/bb/status/1)" in kwargs["content"]
    assert _ids(kwargs["view"])[:1] == ["bb:tr:0"]
    row = await db.get_repost(500)
    assert row.translated is False and row.quip is None


async def test_translate_without_row_is_an_ephemeral_error(db, fake_sites):
    interaction = make_interaction(message=make_message())
    await buttons.Buttons(make_bot(db)).on_translate(interaction, False)
    name, args, kwargs = interaction.response.calls[0]
    assert name == "send_message" and kwargs["ephemeral"] is True
    assert args[0] in strings.variants("error.generic")


# --- chat (pings) -----------------------------------------------------------------------


def test_ping_detection():
    assert chat.pings_bot("hey <@99>", 99) and chat.pings_bot("<@!99> hi", 99)
    assert not chat.pings_bot("hey <@990>", 99) and not chat.pings_bot("hey", 99)
    assert chat.reply_key("<@99> help pls") == "help"
    assert chat.reply_key("<@99> HELP") == "help"
    assert chat.reply_key("<@99> you're helpless") == "ping"


def test_cooldown():
    cd = chat.Cooldown(30)
    assert cd.ready(1, now=0)
    assert not cd.ready(1, now=29)
    assert cd.ready(2, now=29)
    assert cd.ready(1, now=30)


@pytest.fixture
def fake_links(monkeypatch):
    monkeypatch.setattr(
        extract,
        "extract_urls",
        lambda content: [FoundURL(word) for word in content.split() if word.startswith("http")],
    )
    monkeypatch.setattr(resolve, "needs_expansion", lambda url: "t.co" in url)

    async def expand(url, session):
        return "https://x.com/bb/status/1"

    monkeypatch.setattr(resolve, "expand", expand)
    monkeypatch.setattr(sites, "match", lambda url, spoiler=False: object() if "x.com" in url else None)


def make_chat_message(content, author_id=3):
    return SimpleNamespace(
        content=content,
        guild=SimpleNamespace(id=1),
        webhook_id=None,
        channel=SimpleNamespace(id=5),
        author=SimpleNamespace(id=author_id, bot=False),
        reply=AsyncMock(),
    )


async def test_chat_replies_once_per_cooldown(db, fake_links):
    bot = SimpleNamespace(user=SimpleNamespace(id=99), settings=SettingsStore(db), http_client=None)
    cog = chat.Chat(bot)
    first = make_chat_message("<@99> help")
    await cog.on_message(first)
    text = first.reply.await_args.args[0]
    assert "`nobb`" in text
    second = make_chat_message("<@99> hi")
    await cog.on_message(second)
    second.reply.assert_not_awaited()  # same user within 30 s
    other = make_chat_message("<@99> hi", author_id=4)
    await cog.on_message(other)
    assert other.reply.await_args.args[0] in strings.variants("ping")


@pytest.mark.parametrize(
    "content", ["<@99> https://x.com/bb/status/1", "<@99> https://t.co/abc", "no ping here"]
)
async def test_chat_stays_quiet(db, fake_links, content):
    bot = SimpleNamespace(user=SimpleNamespace(id=99), settings=SettingsStore(db), http_client=None)
    message = make_chat_message(content)
    await chat.Chat(bot).on_message(message)
    message.reply.assert_not_awaited()


async def test_chat_respects_disabled_channels(db, fake_links):
    store = SettingsStore(db)
    await store.update(1, channels=(5,))
    bot = SimpleNamespace(user=SimpleNamespace(id=99), settings=store, http_client=None)
    message = make_chat_message("<@99> help")
    await chat.Chat(bot).on_message(message)
    message.reply.assert_not_awaited()
