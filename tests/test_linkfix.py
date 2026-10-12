"""Link fixing and sync: pure decisions, plus the cog flow with Discord I/O and other tracks faked."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from bb import render, strings, upload, views
from bb.cogs import linkfix as lf
from bb.cogs.sync import Sync, content_changed
from bb.db import RepostRow
from bb.links import extract, resolve, sites
from bb.links.extract import FoundURL
from bb.links.model import Fixer, LinkMatch, Site
from bb.settings_store import GuildSettings, SettingsStore

G, CH = 1, 10
FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True)
VX = Fixer("vxtwitter", "vxTwitter", "vxtwitter.com")
TWITTER = Site("twitter", "Twitter", "Tweet", (FX, VX), "https://x.com/a/status/1")
PIXIV_FX = Fixer("phixiv", "phixiv", "phixiv.net")
PIXIV = Site("pixiv", "Pixiv", "Pixiv", (PIXIV_FX,), "https://www.pixiv.net/artworks/1")
NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC).timestamp()


def tweet(n: int, *, spoiler: bool = False, fixed: bool = False) -> LinkMatch:
    return LinkMatch(
        site=TWITTER,
        url=f"https://x.com/a/status/{n}",
        canonical_key=f"twitter:{n}",
        author="a",
        author_url="https://x.com/a",
        spoiler=spoiler,
        parts={"already_fixed": "1"} if fixed else {},
    )


class FixedRng:
    def __init__(self, value: int) -> None:
        self.value = value

    def randrange(self, n: int) -> int:
        return min(self.value, n - 1)


# --- pure decisions --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [("look nobb", True), ("NoBB please", True), ("nobbing", False), ("snobb", False), ("x", False),
     ("(nobb)", True)],
)
def test_is_skipped(content, expected):
    assert lf.is_skipped(content, "nobb") is expected


def test_empty_skip_word_never_skips():
    assert not lf.is_skipped("anything", "  ")


def test_channel_allowed_deny_and_allow_with_threads():
    deny = replace(GuildSettings(), channels=(5,))
    assert lf.channel_allowed(deny, 4)
    assert not lf.channel_allowed(deny, 5)
    assert not lf.channel_allowed(deny, 99, parent_id=5)  # thread under a denied channel
    assert lf.channel_allowed(deny, 5, parent_id=4)  # only the parent counts for threads
    allow = replace(GuildSettings(), channel_mode="allow", channels=(5,))
    assert lf.channel_allowed(allow, 5)
    assert not lf.channel_allowed(allow, 4)
    assert lf.channel_allowed(allow, 99, parent_id=5)  # thread under an allowed channel
    assert not lf.channel_allowed(allow, 5, parent_id=4)


def test_should_handle_webhooks():
    settings = GuildSettings()
    assert lf.should_handle(settings, channel_id=1, parent_id=None, is_webhook=False)
    assert not lf.should_handle(settings, channel_id=1, parent_id=None, is_webhook=True)
    allow_hooks = replace(settings, fix_webhooks=True)
    assert lf.should_handle(allow_hooks, channel_id=1, parent_id=None, is_webhook=True)


def test_quip_priority():
    settings = replace(GuildSettings(), quip_rate=1)
    assert lf.choose_quip(settings, rerun_line="roast", site_name="Twitter") == "roast"
    fix = lf.choose_quip(settings, rerun_line=None, site_name="Twitter")
    assert fix in {strings.fill(v, site="Twitter") for v in strings.variants("quip.fix")}


def test_quip_rate():
    rate5 = replace(GuildSettings(), quip_rate=5)
    assert lf.choose_quip(rate5, rerun_line=None, site_name="X", rng=FixedRng(0)) is not None
    assert lf.choose_quip(rate5, rerun_line=None, site_name="X", rng=FixedRng(3)) is None
    off = replace(GuildSettings(), quip_rate=0)
    assert lf.choose_quip(off, rerun_line=None, site_name="X", rng=FixedRng(0)) is None
    assert lf.choose_quip(off, rerun_line="roast", site_name="X") == "roast"  # roasts ignore the rate


def test_themed_channel_quips():
    settings = replace(GuildSettings(), quip_rate=1, lewd_channels=(10,), chaos_channels=(20,))
    lewd = lf.choose_quip(settings, rerun_line=None, site_name="X", channel_id=10)
    assert lewd in {strings.fill(v, site="X") for v in strings.variants("quip.lewd")}
    chaos = lf.choose_quip(settings, rerun_line=None, site_name="X", channel_id=20)
    assert chaos in {strings.fill(v, site="X") for v in strings.variants("quip.chaos")}
    plain = lf.choose_quip(settings, rerun_line=None, site_name="X", channel_id=30)
    assert plain in {strings.fill(v, site="X") for v in strings.variants("quip.fix")}
    assert lf.choose_quip(settings, rerun_line="roast", site_name="X", channel_id=10) == "roast"
    rate5 = replace(settings, quip_rate=5)
    miss = lf.choose_quip(rate5, rerun_line=None, site_name="X", channel_id=10, rng=FixedRng(3))
    assert miss is None


def test_settle_quip():
    assert lf.settle_quip("roast", is_rerun=True, outcome="failed") == "roast"
    assert lf.settle_quip("random quip", is_rerun=False, outcome="failed") in strings.variants("quip.failed")
    assert lf.settle_quip(None, is_rerun=False, outcome="uploaded") in strings.variants("quip.uploaded")


@pytest.mark.parametrize(
    ("embeds", "expected"),
    [
        ([], False),
        ([{"type": "rich", "image": {"url": "https://i/x.png"}}], True),
        ([{"type": "rich", "thumbnail": {"url": "https://i/x.png"}}], True),
        ([{"type": "video", "thumbnail": {"url": "https://i/x.png"}, "video": {"url": "v"}}], False),
        ([{"type": "rich", "thumbnail": {"url": "t"}, "video": {"url": "v"}}], False),
        ([{"type": "rich", "description": "text only"}], False),
    ],
)
def test_embeds_have_image(embeds, expected):
    assert lf.embeds_have_image(embeds) is expected


def test_diff_keys():
    removed, added = lf.diff_keys({"twitter:1", "twitter:2"}, [tweet(2), tweet(3)])
    assert removed == {"twitter:1"}
    assert [m.canonical_key for m in added] == ["twitter:3"]


@pytest.mark.parametrize(
    ("statuses", "fixed", "expected"),
    [
        (["ok"], False, "suppress"),
        (["ok", "uploaded"], False, "suppress"),
        (["ok", "failed"], False, "nothing"),
        (["ok", "pending"], False, "nothing"),
        (["ok", "gone"], False, "nothing"),
        ([], False, "nothing"),
        (["ok"], True, "nothing"),
    ],
)
def test_original_action(statuses, fixed, expected):
    assert lf.original_action("suppress", statuses, has_already_fixed=fixed) == expected


def test_original_action_respects_setting():
    assert lf.original_action("delete", ["ok"], has_already_fixed=False) == "delete"
    assert lf.original_action("nothing", ["ok"], has_already_fixed=False) == "nothing"


def test_webhook_username():
    assert lf.webhook_username("Discord Fan") == "Disc0rd Fan"
    assert lf.webhook_username("clyde") == "Clyd3"
    assert lf.webhook_username("   ") == "Senpai"
    assert len(lf.webhook_username("x" * 200)) == 80


def test_recent_ids_bounded_and_consumable():
    ids = lf.RecentIds(size=2)
    for i in (1, 2, 3):
        ids.add(i)
    assert 1 not in ids and 3 in ids
    assert ids.consume(3)
    assert not ids.consume(3)


def test_content_changed():
    cached = SimpleNamespace(content="same")
    assert not content_changed({"embeds": []}, None)
    assert not content_changed({"content": "same"}, cached)
    assert content_changed({"content": "new"}, cached)
    assert content_changed({"content": "same"}, None)


# --- fakes for the other tracks --------------------------------------------------------------


@pytest.fixture
def links(monkeypatch):
    """extract/resolve/sites/views/upload replaced; returns the url -> LinkMatch table to fill."""
    table: dict[str, LinkMatch] = {}

    def fake_extract(content):
        return [FoundURL(word.strip("|"), spoiler=word.startswith("||"))
                for word in content.split() if "http" in word]

    def fake_match(url, spoiler=False):
        m = table.get(url)
        return replace(m, spoiler=spoiler) if m else None

    async def fake_expand(url, session):
        return url.replace("short.link", "x.com")

    async def no_media(m, *, max_bytes, session, workdir):
        return []

    monkeypatch.setattr(extract, "extract_urls", fake_extract)
    monkeypatch.setattr(resolve, "needs_expansion", lambda url: "short.link" in url)
    monkeypatch.setattr(resolve, "expand", fake_expand)
    monkeypatch.setattr(sites, "match", fake_match)
    monkeypatch.setattr(sites, "chain", lambda site_id, down=(): [
        f for f in {"twitter": TWITTER, "pixiv": PIXIV}[site_id].fixers if f.id not in down
    ])
    monkeypatch.setattr(sites, "fixed_url", lambda m, fixer, lang=None: f"https://{fixer.domain}/{m.canonical_key}")
    monkeypatch.setattr(views, "make_repost_view", lambda **kw: dict(kw))
    monkeypatch.setattr(upload, "fetch_media", no_media)
    return table


async def test_collect_matches_expands_dedupes_and_caps(links):
    for n in range(1, 7):
        links[f"https://x.com/a/status/{n}"] = tweet(n)
    pixiv_url = "https://www.pixiv.net/artworks/9"
    links[pixiv_url] = LinkMatch(PIXIV, pixiv_url, "pixiv:9")
    rest = " ".join(f"https://x.com/a/status/{n}" for n in range(2, 7))
    dupes = "https://short.link/a/status/1 https://x.com/a/status/1"
    content = f"{dupes} https://nope.example/ {pixiv_url} {rest}"
    found = await lf.collect_matches(content, GuildSettings(), session=object())
    assert [m.canonical_key for m in found] == ["twitter:1", "pixiv:9", "twitter:2", "twitter:3"]
    no_pixiv = replace(GuildSettings(), sites_enabled={**GuildSettings().sites_enabled, "pixiv": False})
    found = await lf.collect_matches(content, no_pixiv, session=object())
    assert "pixiv:9" not in [m.canonical_key for m in found]


async def test_collect_matches_marks_spoilers(links):
    links["https://x.com/a/status/1"] = tweet(1)
    found = await lf.collect_matches("||https://x.com/a/status/1||", GuildSettings(), session=None)
    assert found[0].spoiler


# --- the cog with Discord faked --------------------------------------------------------------


def image_embed() -> discord.Embed:
    return discord.Embed(title="t").set_image(url="https://pbs.example/img.jpg")


def text_embed() -> discord.Embed:
    return discord.Embed(title="t", description="text only")


class Harness:
    """A LinkFix cog whose Discord I/O methods are replaced with recorders."""

    def __init__(self, db) -> None:
        self.bot = SimpleNamespace(
            db=db, settings=SettingsStore(db), down_fixers=set(), http_session=None, http_client=None,
            user=SimpleNamespace(id=1), cogs={},
        )
        self.bot.get_cog = self.bot.cogs.get
        self.cog = lf.LinkFix(self.bot)
        self.bot.cogs["LinkFix"] = self.cog
        self.ids = itertools.count(9000)
        self.sent: list[tuple[str, object]] = []
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self.original: list[tuple[int, str]] = []
        self.embed_script: list[list[dict]] = []  # what each repost's embed wait returns, in order
        self.original_previews: dict[int, list[dict]] = {}  # preview on a user's own message, by id
        self.cog._send_message = self._send
        self.cog._edit = self._edit
        self.cog._wait_for_embeds = self._wait
        self.cog._apply_original = self._apply
        self.cog.delete_repost_message = self._delete
        self.cog._locate = self._locate

    async def _send(self, message, settings, content, view):
        self.sent.append((content, view))
        return lf.Sent(message=SimpleNamespace(id=next(self.ids), embeds=[]))

    async def _edit(self, repost, **changes):
        self.edits.append({"repost_id": repost.row.repost_id, **changes})
        return SimpleNamespace(id=repost.row.repost_id, embeds=[])

    async def _wait(self, message_id):
        if message_id < 9000:  # a user's message (repost ids come from self.ids, starting at 9000)
            return self.original_previews.get(message_id, [])
        return self.embed_script.pop(0) if self.embed_script else []

    async def _apply(self, message, action):
        self.original.append((message.id, action))

    async def _delete(self, row):
        self.deleted.append(row.repost_id)

    async def _locate(self, post):
        return f"https://discord.com/channels/{post.guild_id}/{post.channel_id}/{post.message_id}"

    async def quiet(self, **changes):
        await self.bot.settings.update(G, quip_rate=0, **changes)


def message(
    content: str,
    *,
    mid: int = 500,
    author_id: int = 200,
    name: str = "bob",
    webhook_id=None,
    bot: bool = False,
):
    return SimpleNamespace(
        id=mid, content=content, webhook_id=webhook_id, type=discord.MessageType.default,
        guild=SimpleNamespace(id=G, filesize_limit=25 * 1024 * 1024),
        channel=SimpleNamespace(id=CH),
        author=SimpleNamespace(id=author_id, display_name=name, bot=bot or webhook_id is not None),
        created_at=datetime.fromtimestamp(NOON, UTC),
    )


async def test_other_bots_are_ignored(h):
    h.bot.user = SimpleNamespace(id=1)
    assert await h.cog.accepts(message("https://x.com/a/status/1"))
    assert not await h.cog.accepts(message("https://x.com/a/status/1", name="FixTweet", bot=True))
    # webhook posts have author.bot set too; they pass here and are decided by the fix_webhooks setting
    assert await h.cog.accepts(message("https://x.com/a/status/1", webhook_id=77))


@pytest.fixture
async def h(db, links):
    harness = Harness(db)
    await harness.quiet()
    for n in range(1, 5):
        links[f"https://x.com/a/status/{n}"] = tweet(n)
    links["https://x.com/a/status/7"] = tweet(7)
    links["https://fxtwitter.com/a/status/7"] = tweet(7, fixed=True)
    return harness


async def test_happy_path_with_image(h, db):
    h.embed_script = [[image_embed().to_dict()]]
    await h.cog.process(message("look https://x.com/a/status/1"))
    assert await db.post_keys_for_message(500) == {"twitter:1"}
    content, view = h.sent[0]
    assert content == f"[{render.BLANK}](https://fxtwitter.com/twitter:1)"
    assert view == {"site_id": "twitter", "origin_author_id": 200, "original_url": "https://x.com/a/status/1"}
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id, row.original_url) == ("ok", "fxtwitter", "https://x.com/a/status/1")
    assert h.edits == [{"repost_id": row.repost_id, "view": {
        "site_id": "twitter", "origin_author_id": 200, "translated": False, "has_image": True,
        "original_url": "https://x.com/a/status/1"}}]
    assert h.original == [(500, "suppress")]


async def test_next_fixer_when_no_preview(h, db):
    h.embed_script = [[], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id) == ("ok", "vxtwitter")
    assert len(h.edits) == 1 and "vxtwitter.com/twitter:1" in h.edits[0]["content"]  # no Find source edit
    assert h.original == [(500, "suppress")]


async def test_down_fixers_go_last(h, db):
    h.bot.down_fixers.add("fxtwitter")
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    assert "(https://vxtwitter.com/twitter:1)" in h.sent[0][0]
    (row,) = await db.get_reposts_for_origin(500)
    assert row.fixer_id == "vxtwitter"


async def test_upload_fallback(h, db, monkeypatch, tmp_path):
    media = tmp_path / "pic.jpg"
    media.write_bytes(b"jpeg")
    seen = {}

    async def fetch(m, *, max_bytes, session, workdir):
        seen["max_bytes"] = max_bytes
        return [upload.MediaFile(path=media, filename="pic.jpg", size=4)]

    monkeypatch.setattr(upload, "fetch_media", fetch)
    await h.cog.process(message("||https://x.com/a/status/1||"))
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id) == ("uploaded", None)
    assert row.quip in strings.variants("quip.uploaded")
    last = h.edits[-1]
    assert last["content"] == f"-# {row.quip}"
    assert [f.filename for f in last["attachments"]] == ["SPOILER_pic.jpg"]
    assert last["view"]["has_image"] is True
    assert seen["max_bytes"] == 25 * 1024 * 1024
    assert h.original == [(500, "suppress")]


async def test_everything_failed_leaves_the_original(h, db):
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert row.status == "failed"
    assert "fxtwitter" not in h.edits[-1]["content"] and "vxtwitter" not in h.edits[-1]["content"]
    assert row.quip in strings.variants("quip.failed")
    assert h.original == []


async def test_one_failure_keeps_the_original(h, db):
    h.embed_script = [[text_embed().to_dict()], [], []]
    await h.cog.process(message("https://x.com/a/status/1 https://x.com/a/status/2"))
    statuses = sorted(r.status for r in await db.get_reposts_for_origin(500))
    assert statuses == ["failed", "ok"]
    assert h.original == []


async def test_already_fixed_link_is_recorded_not_reposted(h, db):
    h.original_previews[500] = [text_embed().to_dict()]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    assert await db.post_keys_for_message(500) == {"twitter:7"}
    assert h.sent == [] and h.original == []


async def test_dead_fixer_link_without_preview_gets_fixed(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))  # no preview on the original
    assert len(h.sent) == 1
    (row,) = await db.get_reposts_for_origin(500)
    assert row.status == "ok" and row.canonical_key == "twitter:7"


async def test_already_fixed_link_keeps_the_original_preview(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    h.original_previews[500] = [text_embed().to_dict()]
    await h.cog.process(message("https://fxtwitter.com/a/status/7 https://x.com/a/status/1"))
    assert len(h.sent) == 1
    assert h.original == []


async def test_rerun_is_roasted_and_logged(h, db):
    h.embed_script = [[text_embed().to_dict()], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1", mid=400, author_id=100, name="alice"))
    await h.cog.process(message("https://x.com/a/status/1", mid=500, author_id=200, name="bob"))
    quip_line = h.sent[1][0].removeprefix("-# ").rsplit(f" [{render.BLANK}]", 1)[0]
    assert "alice" in quip_line and "/channels/1/10/400" in quip_line
    (row,) = await db.get_reposts_for_origin(500)
    assert row.quip == quip_line
    assert await db.top_rerun_offenders(G, since=0) == [(200, "bob", 1)]


async def test_rerun_of_already_fixed_link_is_logged(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    h.original_previews[500] = [text_embed().to_dict()]
    await h.cog.process(message("https://x.com/a/status/7", mid=400, author_id=100))
    await h.cog.process(message("https://fxtwitter.com/a/status/7", mid=500, author_id=200))
    assert await db.top_rerun_offenders(G, since=0) == [(200, "bob", 1)]
    assert len(h.sent) == 1


async def test_skip_word(h, db):
    await h.cog.process(message("https://x.com/a/status/1 nobb"))
    assert h.sent == [] and await db.post_keys_for_message(500) == set()


async def test_disabled_channel_and_webhooks(h, db):
    await h.bot.settings.update(G, channels=(CH,))
    await h.cog.process(message("https://x.com/a/status/1"))
    await h.bot.settings.update(G, channels=())
    await h.cog.process(message("https://x.com/a/status/1", mid=501, webhook_id=77))
    assert h.sent == []


async def test_duplicate_message_is_processed_once(h, db):
    h.embed_script = [[text_embed().to_dict()]] * 2
    msg = message("https://x.com/a/status/1")
    await asyncio.gather(h.cog.process(msg), h.cog.process(msg))
    assert len(h.sent) == 1
    assert len(await db.get_reposts_for_origin(500)) == 1
    assert h.cog._locks == {}


async def test_edit_removes_and_adds_links(h, db):
    h.embed_script = [[text_embed().to_dict()]] * 3
    await h.cog.process(message("https://x.com/a/status/1 https://x.com/a/status/2"))
    rows = {r.canonical_key: r.repost_id for r in await db.get_reposts_for_origin(500)}
    await h.cog.process(message("https://x.com/a/status/2 https://x.com/a/status/3"), edit=True)
    assert h.deleted == [rows["twitter:1"]]
    assert await db.post_keys_for_message(500) == {"twitter:2", "twitter:3"}
    keys = sorted(r.canonical_key for r in await db.get_reposts_for_origin(500))
    assert keys == ["twitter:2", "twitter:3"]
    assert len(h.sent) == 3


async def test_edit_adding_skip_word_removes_reposts(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    await h.cog.process(message("https://x.com/a/status/1 nobb"), edit=True)
    assert len(h.deleted) == 1
    assert await db.get_reposts_for_origin(500) == []


async def test_original_deleted_mid_fix_drops_the_repost(h, db):
    async def wait(repost_id):
        h.cog.mark_origin_deleted(500)
        return []

    h.cog._wait_for_embeds = wait
    await h.cog.process(message("https://x.com/a/status/1"))
    assert len(h.deleted) == 1
    assert await db.get_reposts_for_origin(500) == []
    assert h.original == []


# --- embed waiting ---------------------------------------------------------------------------


def edit_payload(message_id: int, embeds: list[dict], author_id: int = 1):
    return SimpleNamespace(message_id=message_id, data={"embeds": embeds, "author": {"id": str(author_id)}})


async def test_wait_for_embeds_resolves_from_raw_edit(db):
    cog = Harness(db).cog
    cog.embed_timeout = 1.0
    del cog._wait_for_embeds  # use the real method
    waiting = asyncio.create_task(cog._wait_for_embeds(42))
    await asyncio.sleep(0)
    await cog.on_raw_message_edit(edit_payload(42, []))  # our own content edit: no preview yet
    assert not waiting.done()
    await cog.on_raw_message_edit(edit_payload(42, [{"type": "rich"}]))
    assert await waiting == [{"type": "rich"}]
    assert cog._embed_waiters == {}


async def test_wait_for_embeds_uses_preview_that_arrived_early(db):
    cog = Harness(db).cog
    cog.embed_timeout = 0.01
    del cog._wait_for_embeds
    await cog.on_raw_message_edit(edit_payload(43, [{"type": "rich"}]))
    await cog.on_raw_message_edit(edit_payload(44, [{"type": "rich"}], author_id=555))  # not ours
    assert await cog._wait_for_embeds(43) == [{"type": "rich"}]
    assert await cog._wait_for_embeds(44) == []


# --- delete sync -----------------------------------------------------------------------------


async def _repost_row(db, repost_id: int, origin: int = 500, key: str = "twitter:1") -> None:
    await db.insert_repost(RepostRow(
        repost_id=repost_id, guild_id=G, channel_id=CH, origin_message_id=origin, origin_author_id=200,
        site="twitter", canonical_key=key, original_url="https://x.com/a/status/1", status="ok",
    ))


async def test_sync_original_deleted(h, db):
    sync = Sync(h.bot)
    await db.insert_post(guild_id=G, channel_id=CH, message_id=500, author_id=200, author_name="bob",
                         site="twitter", canonical_key="twitter:1")
    await _repost_row(db, 9001)
    await _repost_row(db, 9002, key="twitter:2")
    await sync.handle_deleted(500)
    assert sorted(h.deleted) == [9001, 9002]
    assert await db.get_reposts_for_origin(500) == []
    assert await db.post_keys_for_message(500) == set()
    assert 500 in h.cog._deleted_origins


async def test_sync_repost_deleted_by_someone(h, db):
    sync = Sync(h.bot)
    await _repost_row(db, 9001)
    await sync.handle_deleted(9001)
    assert await db.get_repost(9001) is None
    assert h.deleted == []


async def test_sync_ignores_originals_bb_deleted(h, db):
    sync = Sync(h.bot)
    await _repost_row(db, 9001)
    h.cog._expected_deletes.add(500)
    await sync.handle_deleted(500)
    assert await db.get_repost(9001) is not None
    assert h.deleted == []



# --- e-hentai cards ---------------------------------------------------------------------

EH = "https://e-hentai.org/g/618395/0439fa3666/"


def eh_meta(tags=("parody:touhou project",)):
    meta = {"gid": 618395, "title": "T", "category": "Doujinshi", "filecount": "3", "tags": list(tags)}
    return {618395: meta}


@pytest.fixture
def cards(h, monkeypatch):
    sent = []

    async def send_card(message, settings, view, cover):
        sent.append((view, cover))
        return SimpleNamespace(id=next(h.ids))

    async def no_cover(session, gallery, max_bytes):
        return None

    h.cog._send_card = send_card
    monkeypatch.setattr(lf.ehentai, "fetch_cover", no_cover)
    return sent


async def test_gallery_link_gets_a_card(h, db, cards, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta()

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.cog.process(message(f"look {EH}"))
    ((view, cover),) = cards
    assert cover is None
    (container,) = view.children  # no quip line, just the card
    assert container.children[-1].children[0].url == EH
    assert await db.post_keys_for_message(500) == {"ehentai:618395"}
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.site, row.status) == ("ehentai", "ok")
    assert h.original == [(500, "suppress")]  # the original's own bare preview is hidden


async def test_blocked_gallery_gets_nothing(h, db, cards, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta(tags=("parody:x", "female:lolicon"))

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.cog.process(message(EH))
    assert cards == [] and h.original == []
    assert await db.post_keys_for_message(500) == set()
    assert await db.get_reposts_for_origin(500) == []


async def test_reposted_gallery_gets_roasted(h, db, cards, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta()

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.cog.process(message(EH, mid=400, author_id=100, name="alice"))
    await h.cog.process(message(EH, mid=500, author_id=200, name="bob"))
    roast = cards[1][0].children[0].content
    assert roast.startswith("-# ") and "alice" in roast
    assert await db.top_rerun_offenders(G, since=0) == [(200, "bob", 1)]


async def test_cards_can_be_turned_off(h, db, cards, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta()

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.quiet(sites_enabled={**GuildSettings().sites_enabled, "ehentai": False})
    await h.cog.process(message(EH))
    assert cards == []
