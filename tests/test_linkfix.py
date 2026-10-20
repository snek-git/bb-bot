"""Link fixing and sync: pure decisions, plus the cog flow with Discord I/O and other tracks faked."""

from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from bb import strings, upload, views
from bb.cogs import linkfix as lf
from bb.cogs.sync import Sync, content_changed
from bb.db import RepostRow
from bb.links import extract, resolve, sites
from bb.links.extract import FoundURL
from bb.links.model import Fixer, LinkMatch, Site
from bb.settings_store import GuildSettings, RareLine, SettingsStore

G, CH = 1, 10
FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True, direct_domain="d.fxtwitter.com")
VX = Fixer("vxtwitter", "vxTwitter", "vxtwitter.com", direct_domain="d.vxtwitter.com")
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

    def choice(self, seq):
        return seq[min(self.value, len(seq) - 1)]


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
    ts = datetime(2026, 10, 18, 12, 0, tzinfo=UTC).timestamp()
    just_now = datetime.fromtimestamp(ts - 30, UTC).isoformat()
    last_year = datetime(2025, 10, 18, tzinfo=UTC).isoformat()
    assert not content_changed({"embeds": []}, None, ts)
    assert not content_changed({"content": "same"}, cached, ts)
    assert content_changed({"content": "new"}, cached, ts)
    # nothing cached (after a restart): only an edit Discord stamped just now counts
    assert content_changed({"content": "same", "edited_timestamp": just_now}, None, ts)
    assert not content_changed({"content": "same", "edited_timestamp": last_year}, None, ts)
    assert not content_changed({"content": "same", "edited_timestamp": None}, None, ts)  # e.g. a pin
    assert not content_changed({"content": "same"}, None, ts)
    assert not content_changed({"content": "same", "edited_timestamp": "garbage"}, None, ts)


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
        self.sends: list[dict] = []  # the same sends, with their files and quiet flag
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self.delete_ok = True  # what Discord says when BB deletes a repost
        self.original: list[tuple[int, str]] = []
        self.embed_script: list[list[dict]] = []  # what each repost's embed wait returns, in order
        self.original_previews: dict[int, list[dict]] = {}  # preview on a user's own message, by id
        self.waits: list[tuple[int, float | None]] = []  # (message id, seconds) for every embed wait
        self.cog._send_message = self._send
        self.cog._edit = self._edit
        self.cog._wait_for_embeds = self._wait
        self.cog._apply_original = self._apply
        self.cog.delete_repost_message = self._delete
        self.cog._locate = self._locate
        self.predict_image = True
        self.cog._predict_image = self._predict
        self.tweet_info = None  # what FxTwitter's API says about a tweet (None = no answer)
        self.cog._tweet_info = self._tweet

    async def _send(self, message, settings, content, view, *, make_files=None, quiet=False):
        self.sent.append((content, view))
        sent_id = next(self.ids)
        files = make_files() if make_files is not None else None
        self.sends.append({"id": sent_id, "content": content, "view": view, "files": files, "quiet": quiet})
        return lf.Sent(message=SimpleNamespace(id=sent_id, embeds=[]))

    async def _edit(self, repost, **changes):
        self.edits.append({"repost_id": repost.row.repost_id, **changes})
        return SimpleNamespace(id=repost.row.repost_id, embeds=[])

    async def _wait(self, message_id, seconds=None):
        self.waits.append((message_id, seconds))
        if message_id < 9000:  # a user's message (repost ids come from self.ids, starting at 9000)
            return self.original_previews.get(message_id, [])
        return self.embed_script.pop(0) if self.embed_script else []

    async def _predict(self, m):
        return self.predict_image

    async def _tweet(self, m):
        return self.tweet_info

    async def _apply(self, message, action):
        self.original.append((message.id, action))

    async def _delete(self, row):
        self.deleted.append(row.repost_id)
        return self.delete_ok

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
    created_at: datetime | None = None,
):
    return SimpleNamespace(
        id=mid, content=content, webhook_id=webhook_id, type=discord.MessageType.default,
        guild=SimpleNamespace(id=G, filesize_limit=25 * 1024 * 1024),
        channel=SimpleNamespace(id=CH),
        author=SimpleNamespace(id=author_id, display_name=name, bot=bot or webhook_id is not None),
        created_at=created_at or datetime.now(UTC),  # fresh by default: old history is never touched
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
    assert content.startswith("-# [") and "](<https://x.com/a/status/1>)" in content
    assert content.endswith(" · [FxTwitter](https://fxtwitter.com/twitter:1)")
    assert view == {"site_id": "twitter", "origin_author_id": 200, "has_image": True, "translatable": True}
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id, row.original_url) == ("ok", "fxtwitter", "https://x.com/a/status/1")
    assert h.edits == []  # Find source was decided before sending: no "(edited)" on the repost
    assert h.original == [(500, "suppress")]


async def test_next_fixer_when_no_preview(h, db):
    h.embed_script = [[], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id) == ("ok", "vxtwitter")
    # The next fixer goes out as a fresh message and the first one is deleted: no edit, no "(edited)".
    first, second = h.sends
    assert h.edits == [] and h.deleted == [first["id"]] and row.repost_id == second["id"]
    assert "vxtwitter.com/twitter:1" in second["content"] and second["view"] == first["view"]
    assert second["quiet"] and not first["quiet"]  # the replacement pings nobody
    assert h.original == [(500, "suppress")]


async def test_replacement_that_cannot_be_sent_edits_instead(h, db):
    h.embed_script = [[], [text_embed().to_dict()]]
    real_send = h.cog._send_message

    async def flaky(message, settings, content, view, **kw):
        if h.sends:  # the replacement
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        return await real_send(message, settings, content, view, **kw)

    h.cog._send_message = flaky
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.status, row.fixer_id, row.repost_id) == ("ok", "vxtwitter", h.sends[0]["id"])
    # a label beats a stuck repost
    assert len(h.edits) == 1 and "vxtwitter.com/twitter:1" in h.edits[0]["content"]
    assert h.deleted == []


async def test_replacement_never_brings_back_a_deleted_repost(h, db):
    async def wait(message_id, seconds=None):
        await db.delete_repost(message_id)  # a mod deletes the repost while BB waits for its preview
        return []

    h.cog._wait_for_embeds = wait
    await h.cog.process(message("https://x.com/a/status/1"))
    assert len(h.sends) == 1 and h.edits == []  # no fresh copy went out
    assert await db.get_reposts_for_origin(500) == [] and h.original == []


async def test_original_deleted_during_replacement_takes_the_new_one_too(h, db):
    real_send = h.cog._send_message

    async def send(message, settings, content, view, **kw):
        sent = await real_send(message, settings, content, view, **kw)
        if len(h.sends) == 2:
            h.cog.mark_origin_deleted(500)  # the original is deleted while the replacement is on its way
        return sent

    h.cog._send_message = send
    await h.cog.process(message("https://x.com/a/status/1"))
    replacement = h.sends[1]["id"]
    assert replacement in h.deleted and await db.get_repost(replacement) is None


async def test_old_repost_that_will_not_delete_stays_tracked(h, db):
    h.embed_script = [[], [text_embed().to_dict()]]
    h.delete_ok = False
    await h.cog.process(message("https://x.com/a/status/1"))
    ids = sorted(r.repost_id for r in await db.get_reposts_for_origin(500))
    assert ids == [h.sends[0]["id"], h.sends[1]["id"]]  # still up, so still known: sync can reach it


async def test_failed_replacement_upload_edits_with_fresh_files(h, db, monkeypatch, tmp_path):
    media = tmp_path / "pic.jpg"
    media.write_bytes(b"jpeg")

    async def fetch(m, *, max_bytes, session, workdir):
        return [upload.MediaFile(path=media, filename="pic.jpg", size=4)]

    monkeypatch.setattr(upload, "fetch_media", fetch)
    real_send = h.cog._send_message

    async def flaky(message, settings, content, view, **kw):
        if h.sends:
            if kw.get("make_files"):
                for file in kw["make_files"]():  # what discord.py does to a failed send's files
                    file.close()
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        return await real_send(message, settings, content, view, **kw)

    h.cog._send_message = flaky
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert row.status == "uploaded"
    (file,) = h.edits[-1]["attachments"]
    assert file.filename == "pic.jpg" and not file.fp.closed  # built for this edit, not reused
    file.close()


def test_mixed_media():
    def tweet_with(*kinds):
        return {"media": {"all": [{"type": kind} for kind in kinds]}}

    assert lf.mixed_media(tweet_with("photo", "video")) == ("photo", "video")
    assert lf.mixed_media(tweet_with("gif", "photo", "photo")) == ("gif", "photo", "photo")
    assert lf.mixed_media(tweet_with("photo", "photo")) == ()  # an image gallery works as it is
    assert lf.mixed_media(tweet_with("video")) == ()
    assert lf.mixed_media(None) == () and lf.mixed_media({}) == ()


MIXED = {"lang": "en", "text": "art and animation", "media": {"all": [{"type": "photo"}, {"type": "video"}]}}


async def test_mixed_media_tweet_gets_one_preview_per_item(h, db):
    h.tweet_info = MIXED
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    (content, _), = h.sent
    assert "[FxTwitter](https://fxtwitter.com/twitter:1/photo/1)" in content  # the card, with the text
    assert "[video](https://d.fxtwitter.com/twitter:1/video/2)" in content  # just the video: no text twice
    (row,) = await db.get_reposts_for_origin(500)
    assert row.media == "photo,video" and row.status == "ok"


async def test_mixed_media_survives_a_fixer_switch(h, db):
    h.tweet_info = MIXED
    h.embed_script = [[], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    content = h.sends[1]["content"]
    assert "(https://vxtwitter.com/twitter:1/photo/1)" in content
    assert "(https://d.vxtwitter.com/twitter:1/video/2)" in content


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
    last = h.sends[-1]
    # fxtwitter, then vxtwitter, then the upload: each one replaced the one before
    assert h.edits == [] and row.repost_id == last["id"] and h.deleted == [s["id"] for s in h.sends[:-1]]
    assert last["content"].startswith("-# [") and last["content"].endswith(f" · {row.quip}")
    assert [f.filename for f in last["files"]] == ["SPOILER_pic.jpg"]
    assert last["view"]["has_image"] is False  # a tweet: no Find source, even for an uploaded image
    assert seen["max_bytes"] == 25 * 1024 * 1024
    assert h.original == [(500, "suppress")]


async def test_everything_failed_leaves_the_original(h, db):
    await h.cog.process(message("https://x.com/a/status/1"))
    (row,) = await db.get_reposts_for_origin(500)
    assert row.status == "failed"
    last = h.sends[-1]["content"]
    assert h.edits == [] and "fxtwitter" not in last and "vxtwitter" not in last
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
    quip_line = h.sent[1][0].split(" · ", 2)[2]  # "-# Tweet · FxTwitter · <roast>"
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


LATE_VX = {"type": "rich", "url": "https://vxtwitter.com/a/status/7"}  # fixvx previews arrive as vxtwitter


@pytest.fixture
def late_vx(links):
    links["https://vxtwitter.com/a/status/7"] = tweet(7, fixed=True)


async def test_rescue_waits_longer_than_the_fixer_chain(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    assert h.waits[0] == (500, h.cog.rescue_timeout)  # the poster's own fixer link gets the long wait
    assert h.waits[1][1] is None  # BB's repost: the normal fixer-chain timeout
    assert h.cog.rescue_timeout > h.cog.embed_timeout


async def test_late_preview_takes_the_rescue_down(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    (row,) = await db.get_reposts_for_origin(500)
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [row.repost_id]
    assert await db.get_reposts_for_origin(500) == []
    assert await db.post_keys_for_message(500) == {"twitter:7"}  # repost roasts still know the link
    assert 500 not in h.cog._rescue_watches


async def test_late_preview_of_another_link_keeps_the_rescue(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()], [text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7 https://x.com/a/status/1"))
    # x.com's own late preview is for the other link: the rescue of status/7 is not a duplicate
    await h.cog.on_raw_message_edit(edit_payload(
        500, [{"type": "rich", "url": "https://x.com/a/status/1"}], author_id=200
    ))
    await h.cog.on_raw_message_edit(edit_payload(500, [{"type": "rich"}], author_id=200))  # no URL
    assert h.deleted == []
    assert sorted(r.canonical_key for r in await db.get_reposts_for_origin(500)) == ["twitter:1", "twitter:7"]


async def test_late_preview_while_settling_waits_for_the_settle(h, db, late_vx):
    deleted_mid_settle = []

    async def wait(message_id, seconds=None):
        if message_id < 9000:
            return []  # the original: no preview yet, so BB rescues
        await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
        deleted_mid_settle.extend(h.deleted)
        return [text_embed().to_dict()]

    h.cog._wait_for_embeds = wait
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    assert deleted_mid_settle == []  # never deleted while the repost was still being edited
    assert len(h.deleted) == 1 and await db.get_reposts_for_origin(500) == []


async def test_sites_own_late_preview_keeps_the_rescue(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    # x.com's own card for the same tweet is no substitute for BB's fix
    await h.cog.on_raw_message_edit(edit_payload(
        500, [{"type": "rich", "url": "https://x.com/a/status/7"}], author_id=200
    ))
    assert h.deleted == [] and len(await db.get_reposts_for_origin(500)) == 1


async def test_rescue_that_roasts_a_repost_stays_up(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/7", mid=400, author_id=100, name="alice"))
    await h.cog.process(message("https://fxtwitter.com/a/status/7", mid=500, author_id=200))
    (row,) = await db.get_reposts_for_origin(500)
    assert row.quip  # the roast
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [] and await db.get_repost(row.repost_id) is not None


async def test_late_preview_only_takes_down_the_rescue_itself(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    (rescue,) = await db.get_reposts_for_origin(500)
    # edits removed the rescue and BB made a normal repost for the same link
    await db.delete_repost(rescue.repost_id)
    await _repost_row(db, 9555, key="twitter:7")
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [] and await db.get_repost(9555) is not None


async def test_failed_delete_keeps_the_record(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    (row,) = await db.get_reposts_for_origin(500)
    h.delete_ok = False  # Discord refused: the message is still up, so BB keeps knowing about it
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [row.repost_id] and await db.get_repost(row.repost_id) is not None
    h.delete_ok = True  # the next preview update retries
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [row.repost_id, row.repost_id] and await db.get_repost(row.repost_id) is None


async def test_second_rescue_keeps_watching_the_first(h, db):
    msg = message("https://fxtwitter.com/a/status/7")
    first = h.cog._watch_rescue(msg, {"twitter:7"})
    first.reposts["twitter:7"] = 9100
    first.settled = True
    second = h.cog._watch_rescue(msg, {"twitter:8"})
    assert second is first and second.keys == {"twitter:7", "twitter:8"}
    assert second.reposts == {"twitter:7": 9100} and not second.settled
    second.deadline = 0  # expired: a later rescue starts fresh instead of reviving the old one
    third = h.cog._watch_rescue(msg, {"twitter:9"})
    assert third is not first and third.keys == {"twitter:9"} and third.reposts == {}


async def test_rescue_replaced_by_the_next_fixer_can_still_be_undone(h, db, late_vx):
    h.embed_script = [[], [text_embed().to_dict()]]  # BB's first fixer shows nothing: she switches
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    first, second = h.sends
    (row,) = await db.get_reposts_for_origin(500)
    assert row.repost_id == second["id"]
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [first["id"], second["id"]] and await db.get_reposts_for_origin(500) == []


async def test_late_preview_after_the_window_is_ignored(h, db, late_vx):
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://fxtwitter.com/a/status/7"))
    h.cog._rescue_watches[500].deadline = 0
    await h.cog.on_raw_message_edit(edit_payload(500, [LATE_VX], author_id=200))
    assert h.deleted == [] and len(await db.get_reposts_for_origin(500)) == 1
    assert 500 not in h.cog._rescue_watches


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

    async def send_card(message, settings, view, cover, quip=None):
        sent.append((view, cover))
        return SimpleNamespace(id=next(h.ids))

    async def no_cover(session, gallery, meta, max_bytes):
        return None

    h.cog._send_card = send_card
    monkeypatch.setattr(lf.ehentai, "fetch_cover", no_cover)
    monkeypatch.setattr(lf.nhentai, "fetch_cover", no_cover)
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


NH = "https://nhentai.net/g/450000/"


def nh_meta(tags=({"type": "parody", "name": "touhou project"},)):
    meta = {"id": 450000, "title": {"english": "T", "pretty": "T"}, "num_pages": 3, "tags": list(tags)}
    return {450000: meta}


@pytest.fixture
def nh_fetch(monkeypatch):
    calls = []

    async def fetch(session, galleries):
        calls.append([g.gid for g in galleries])
        return nh_meta()

    monkeypatch.setattr(lf.nhentai, "fetch_metadata", fetch)
    return calls


async def test_nhentai_link_gets_a_card(h, db, cards, nh_fetch):
    await h.cog.process(message(f"look {NH}"))
    ((view, cover),) = cards
    assert cover is None
    (container,) = view.children
    assert container.children[-1].children[0].url == NH
    assert await db.post_keys_for_message(500) == {"nhentai:450000"}
    (row,) = await db.get_reposts_for_origin(500)
    assert (row.site, row.status, row.original_url) == ("nhentai", "ok", NH)
    assert h.original == [(500, "suppress")]


async def test_both_gallery_sites_in_one_message(h, db, cards, nh_fetch, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta()

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.cog.process(message(f"{EH} {NH}"))
    assert len(cards) == 2
    assert await db.post_keys_for_message(500) == {"ehentai:618395", "nhentai:450000"}


async def test_editing_a_message_keeps_its_nhentai_card(h, db, cards, nh_fetch):
    await h.cog.process(message(NH))
    await h.cog.process(message(f"{NH} (edited: typo)"), edit=True)
    assert h.deleted == []  # the card isn't a fixed link, so the edit diff must leave it alone
    assert [r.canonical_key for r in await db.get_reposts_for_origin(500)] == ["nhentai:450000"]
    assert nh_fetch == [[450000]]  # and edits don't post a second card


async def test_blocked_nhentai_gallery_gets_nothing(h, db, cards, monkeypatch):
    async def fetch(session, galleries):
        return nh_meta(tags=[{"type": "tag", "name": "lolicon"}])

    monkeypatch.setattr(lf.nhentai, "fetch_metadata", fetch)
    await h.cog.process(message(NH))
    assert cards == []
    assert await db.post_keys_for_message(500) == set()  # not even a record
    assert h.original == []


async def test_nhentai_cards_can_be_turned_off_alone(h, db, cards, nh_fetch, monkeypatch):
    async def fetch(session, galleries):
        return eh_meta()

    monkeypatch.setattr(lf.ehentai, "fetch_metadata", fetch)
    await h.quiet(sites_enabled={**GuildSettings().sites_enabled, "nhentai": False})
    await h.cog.process(message(f"{EH} {NH}"))
    assert nh_fetch == []  # never even asked the API
    assert await db.post_keys_for_message(500) == {"ehentai:618395"}


def test_card_keys():
    assert lf.is_card_key("nhentai:450000") and lf.is_card_key("ehentai:618395")
    assert not lf.is_card_key("twitter:1") and not lf.is_card_key("bsky:a.bsky.social/3abc")


# --- the rare custom line ----------------------------------------------------------------

PING = RareLine("{target} BB thinks about you a lot♡", 42)
PLAIN = RareLine("BB is bored. Entertain me♡")  # no target: just said, pings nobody
RARE = replace(GuildSettings(), rare_lines=(PING,))


def test_rare_line_pings_its_target():
    assert lf.rare_line(PING) == "<@42> BB thinks about you a lot♡"
    assert lf.rare_line(RareLine("hello", 42)) == "<@42> hello"  # ping goes first without a {target}
    assert lf.rare_line(RareLine("{target} BB thinks about you a lot♡")) == "BB thinks about you a lot♡"
    assert lf.rare_line(PLAIN) == "BB is bored. Entertain me♡"


def test_rare_line_odds_and_priority():
    hit = lf.choose_quip(RARE, rerun_line=None, site_name="X", rng=FixedRng(0))
    assert hit == "<@42> BB thinks about you a lot♡"
    assert lf.choose_quip(RARE, rerun_line=None, site_name="X", rng=FixedRng(7)) is None  # 7 != 0
    assert lf.choose_quip(RARE, rerun_line="roast", site_name="X", rng=FixedRng(0)) == "roast"
    off = replace(RARE, rare_lines=())
    normal = lf.choose_quip(off, rerun_line=None, site_name="X", rng=FixedRng(0))
    assert normal != hit and "<@42>" not in (normal or "")


def test_rare_lines_are_picked_at_random():
    both = replace(GuildSettings(), rare_lines=(PING, PLAIN), rare_rate=1)  # always fires
    seen = {
        lf.choose_quip(both, rerun_line=None, site_name="X", rng=random.Random(seed)) for seed in range(400)
    }
    assert {lf.rare_line(PING), lf.rare_line(PLAIN)} <= seen


def test_only_rare_targets_can_be_pinged():
    mentions = lf.send_mentions(RARE, "-# <@42> hi <@99>", reply=True)
    assert [u.id for u in mentions.users] == [42]
    assert mentions.everyone is False and mentions.roles is False
    quiet = lf.send_mentions(RARE, "-# a normal quip", reply=True)
    assert quiet.users is False and quiet.replied_user is False  # silent mode: nobody
    assert lf.pings_target(RARE, "x <@42>") and not lf.pings_target(RARE, None)
    plain = replace(GuildSettings(), rare_lines=(PLAIN,))
    assert lf.send_mentions(plain, f"-# {lf.rare_line(PLAIN)}", reply=True).users is False


async def test_armed_rare_line_fires_once(h, db):
    h.embed_script = [[text_embed().to_dict()], [text_embed().to_dict()]]
    await h.quiet(rare_lines=(PLAIN, RareLine("{target} hi♡", 42)), rare_armed=1)
    await h.cog.process(message("https://x.com/a/status/1", mid=400))
    await h.cog.process(message("https://x.com/a/status/2", mid=500))
    first, second = h.sent[0][0], h.sent[1][0]
    assert first.endswith(" · <@42> hi♡")  # the armed line, not a random one
    assert "<@42>" not in second
    assert (await h.bot.settings.get(G)).rare_armed is None


async def test_armed_rare_line_waits_out_a_repost_roast(h, db):
    h.embed_script = [[text_embed().to_dict()], [text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1", mid=400, author_id=100, name="alice"))
    await h.quiet(rare_lines=(RareLine("{target} hi♡", 42),), rare_armed=0)
    await h.cog.process(message("https://x.com/a/status/1", mid=500, author_id=200, name="bob"))
    assert "alice" in h.sent[1][0] and "<@42>" not in h.sent[1][0]  # the roast wins
    assert (await h.bot.settings.get(G)).rare_armed == 0  # still armed for the next link


async def test_image_guess_without_the_network(db):
    cog = lf.LinkFix(SimpleNamespace(settings=None, db=db))
    pixiv = SimpleNamespace(site=SimpleNamespace(id="pixiv"), parts={"id": "1"})
    tiktok = SimpleNamespace(site=SimpleNamespace(id="tiktok"), parts={"id": "1"})
    reddit = SimpleNamespace(site=SimpleNamespace(id="reddit"), parts={"id": "1"})
    tweet = SimpleNamespace(site=SimpleNamespace(id="twitter"), parts={"id": "1"})
    assert await cog._predict_image(reddit) is True  # reposted art: worth a source search
    assert await cog._predict_image(tweet) is False  # the tweet is usually the source itself
    assert await cog._predict_image(pixiv) is False
    assert await cog._predict_image(tiktok) is False


async def test_text_only_tweet_gets_no_find_source(h, db):
    h.predict_image = False
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    assert h.sent[0][1]["has_image"] is False and h.edits == []


async def test_edit_event_for_an_old_unhandled_message_is_ignored(h, db):
    # Discord sends edit events for years-old messages (pins, refreshed previews): never fix those.
    h.embed_script = [[text_embed().to_dict()]]
    old = datetime.fromtimestamp(NOON, UTC)
    await h.cog.process(message("https://x.com/a/status/1", created_at=old), edit=True)
    assert h.sent == [] and await db.post_keys_for_message(500) == set()


async def test_pin_on_old_handled_message_does_nothing(h, db, links):
    # The audit's repro: after a restart nothing is cached, and someone pins a months-old message BB
    # handled back then. Links that only match since an update must not be reposted.
    pixiv_url = "https://www.pixiv.net/artworks/9"
    links[pixiv_url] = LinkMatch(PIXIV, pixiv_url, "pixiv:9")
    content = f"https://x.com/a/status/1 {pixiv_url}"
    await db.insert_post(guild_id=G, channel_id=CH, message_id=500, author_id=200, author_name="bob",
                         site="twitter", canonical_key="twitter:1", created_at=NOON)
    h.embed_script = [[text_embed().to_dict()]]
    msg = message(content, created_at=datetime.fromtimestamp(NOON, UTC))
    payload = SimpleNamespace(guild_id=G, message_id=500, data={"content": content, "pinned": True},
                              cached_message=None, message=msg)
    await Sync(h.bot).on_raw_message_edit(payload)
    assert h.sent == [] and h.deleted == []


async def test_fresh_edit_on_old_message_can_add(h, db):
    old = datetime.fromtimestamp(NOON, UTC)
    await db.insert_post(guild_id=G, channel_id=CH, message_id=500, author_id=200, author_name="bob",
                         site="twitter", canonical_key="twitter:1", created_at=NOON)
    h.embed_script = [[text_embed().to_dict()]]
    edited = message("https://x.com/a/status/1 https://x.com/a/status/3", created_at=old)
    edited.edited_at = datetime.now(UTC)  # someone really just edited it
    await h.cog.process(edited, edit=True)
    assert len(h.sent) == 1 and "twitter:3" in h.sent[0][0]


async def test_edit_on_old_message_removes_but_never_adds(h, db):
    old = datetime.fromtimestamp(NOON, UTC)
    for n in (1, 2):
        await db.insert_post(guild_id=G, channel_id=CH, message_id=500, author_id=200, author_name="bob",
                             site="twitter", canonical_key=f"twitter:{n}", created_at=NOON)
    await _repost_row(db, 9001, key="twitter:1")
    h.embed_script = [[text_embed().to_dict()]]
    edited = message("https://x.com/a/status/2 https://x.com/a/status/3", created_at=old)
    await h.cog.process(edited, edit=True)
    assert h.sent == []  # status/3 is new, but the message is old: no repost
    assert h.deleted == [9001]  # status/1 was edited out: its repost goes
    assert await db.post_keys_for_message(500) == {"twitter:2"}


def test_edit_counts_rules():
    from bb.cogs.linkfix import FRESH_FOR as EDIT_WINDOW
    from bb.cogs.sync import edit_counts

    assert edit_counts(1000.0, 1000.0 + 60, handled_before=False)  # fresh message, link added
    assert not edit_counts(1000.0, 1000.0 + EDIT_WINDOW + 1, handled_before=False)  # old, never handled
    assert edit_counts(1000.0, 1000.0 + 10**8, handled_before=True)  # BB's own message history: keep syncing



async def test_old_history_is_never_touched_even_as_a_new_message(h, db):
    # e.g. Discord replaying events after a reconnect: no fix, no card, no record, no hiding the original
    h.embed_script = [[text_embed().to_dict()]]
    old = datetime.fromtimestamp(NOON, UTC)
    await h.cog.process(message("https://x.com/a/status/1", created_at=old))
    assert h.sent == [] and h.original == [] and await db.post_keys_for_message(500) == set()


async def test_paused_bb_does_nothing(h, db):
    h.embed_script = [[text_embed().to_dict()]]
    await h.quiet(paused=True)
    await h.cog.process(message("https://x.com/a/status/1"))
    assert h.sent == [] and await db.post_keys_for_message(500) == set()


@pytest.mark.parametrize(
    ("lang", "target", "expected"),
    [
        ("ja", "en", True), ("en", "en", False), ("en-gb", "en", False), ("ko", "ko", False),
        ("en", "ja", True),
        (None, "en", False), ("zxx", "en", False), ("qme", "en", False), ("und", "en", False),
    ],
)
def test_offer_translation(lang, target, expected):
    assert lf.offer_translation(lang, target) is expected


async def test_english_tweet_gets_no_translate_button(h, db):
    h.tweet_info = {"lang": "en", "media": {"all": [{"type": "photo"}]}}
    h.embed_script = [[text_embed().to_dict()]]
    await h.cog.process(message("https://x.com/a/status/1"))
    assert h.sent[0][1]["translatable"] is False



def test_translate_offered_without_an_answer():
    assert lf.translate_offered(None, "en") is True
    assert lf.translate_offered({"lang": "en"}, "en") is False
    assert lf.translate_offered({"lang": "ja"}, "en") is True



@pytest.mark.parametrize(
    ("lang", "text", "expected"),
    [
        ("de", "gem alert 💎", False),  # Twitter mislabels short English; too little text to trust it
        ("de", "Das ist wirklich ein sehr schönes Bild, danke!", True),
        ("ja", "かわいい", True),  # non-Latin script: a few characters are enough
        ("ko", "최고", True),
        ("en", "かわいい", False),  # labelled as the server's language: never
        ("tl", "lol ok", False),
        ("de", "", False),
    ],
)
def test_translate_needs_enough_text(lang, text, expected):
    assert lf.offer_translation(lang, "en", text) is expected
    assert lf.translate_offered({"lang": lang, "text": text}, "en") is expected
