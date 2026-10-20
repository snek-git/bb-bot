"""Tests for the shared skeleton: strings, settings, time helpers, DB queries, rendering."""

from __future__ import annotations

import sqlite3

import pytest

from bb import db as dbmod
from bb import strings
from bb.db import Database, HealthRow, RepostRow
from bb.links.model import Fixer, LinkMatch, Site
from bb.render import format_repost, format_upload
from bb.settings_store import GuildSettings, RareLine, SettingsStore
from bb.timeutil import humanize_ago

# --- strings ----------------------------------------------------------------------------


@pytest.mark.parametrize("key", strings.keys())
def test_every_line_fills_with_its_declared_vars(key):
    values = {name: f"<{name}>" for name in strings.declared_vars(key)}
    for variant in strings.variants(key):
        assert strings.fill(variant, **values)


def test_unset_emoji_placeholders_disappear():
    strings.set_emojis({})
    assert strings.fill("{bb_smug} hi") == "hi"
    strings.set_emojis({"bb_smug": "<:bb_smug:1>"})
    assert strings.fill("{bb_smug} hi") == "<:bb_smug:1> hi"
    strings.set_emojis({})


def test_missing_value_raises():
    with pytest.raises(KeyError):
        strings.fill("{author} hi")


# --- time -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(5, "just now"), (60, "1 minute ago"), (7200, "2 hours ago"), (86400 * 3, "3 days ago"),
     (86400 * 21, "3 weeks ago")],
)
def test_humanize_ago(seconds, expected):
    assert humanize_ago(seconds) == expected


# --- settings ---------------------------------------------------------------------------


async def test_settings_roundtrip(db):
    store = SettingsStore(db)
    assert (await store.get(1)).quip_rate == 0  # off by default
    await store.update(1, quip_rate=3, sites_enabled={**GuildSettings().sites_enabled, "tiktok": False})
    fresh = SettingsStore(db)
    loaded = await fresh.get(1)
    assert loaded.quip_rate == 3
    assert loaded.site_enabled("tiktok") is False
    assert loaded.site_enabled("twitter") is True
    assert await db.get_guild_settings(1) == {
        "quip_rate": 3,
        "sites_enabled": {**GuildSettings().sites_enabled, "tiktok": False},
    }


async def test_themed_channels_roundtrip(db):
    store = SettingsStore(db)
    await store.update(1, lewd_channels=[10, 11], chaos_channels=(20,))
    loaded = await SettingsStore(db).get(1)
    assert loaded.lewd_channels == (10, 11) and loaded.chaos_channels == (20,)
    assert loaded.channel_flavor(11) == "lewd"
    assert loaded.channel_flavor(20) == "chaos"
    assert loaded.channel_flavor(30) is None


async def test_old_single_rare_line_carries_over(db):
    # Saved before there were several lines: the one line keeps its text, its ping and its armed state.
    await db.set_guild_settings(1, {"rare_text": "{target} hi", "rare_target_id": 42, "rare_armed": True})
    loaded = await SettingsStore(db).get(1)
    assert loaded.rare_lines == (RareLine("{target} hi", 42),) and loaded.rare_armed == 0
    saved = loaded.to_overrides()
    assert "rare_text" not in saved and saved["rare_lines"] == [{"text": "{target} hi", "target_id": 42}]
    assert GuildSettings.from_overrides(saved) == loaded


async def test_rare_lines_roundtrip(db):
    store = SettingsStore(db)
    await store.update(1, rare_lines=[RareLine("a"), {"text": "b", "target_id": 7}, RareLine("   ")])
    loaded = await SettingsStore(db).get(1)
    assert loaded.rare_lines == (RareLine("a"), RareLine("b", 7))  # blank lines are dropped


def test_channel_modes():
    deny = GuildSettings(channels=(10,))
    assert not deny.channel_enabled(10) and deny.channel_enabled(11)
    allow = GuildSettings(channel_mode="allow", channels=(10,))
    assert allow.channel_enabled(10) and not allow.channel_enabled(11)


# --- db ---------------------------------------------------------------------------------


async def test_prior_posts_filters(db):
    common = dict(guild_id=1, site="twitter", canonical_key="twitter:1")
    await db.insert_post(channel_id=5, message_id=100, author_id=7, author_name="a", created_at=10, **common)
    await db.insert_post(channel_id=6, message_id=101, author_id=8, author_name="b", created_at=20, **common)
    rows = await db.find_prior_posts(guild_id=1, canonical_key="twitter:1", since=0)
    assert [r.message_id for r in rows] == [101, 100]
    rows = await db.find_prior_posts(guild_id=1, canonical_key="twitter:1", since=0, exclude_author_id=8)
    assert [r.message_id for r in rows] == [100]
    rows = await db.find_prior_posts(guild_id=1, canonical_key="twitter:1", since=0, channel_id=6)
    assert [r.message_id for r in rows] == [101]
    rows = await db.find_prior_posts(guild_id=1, canonical_key="twitter:1", since=15)
    assert [r.message_id for r in rows] == [101]
    assert await db.post_keys_for_message(100) == {"twitter:1"}


async def test_repost_lifecycle(db):
    row = RepostRow(
        repost_id=500, guild_id=1, channel_id=5, origin_message_id=100, origin_author_id=7,
        site="twitter", canonical_key="twitter:1", original_url="https://x.com/a/status/1",
        fixer_id="fxtwitter", spoiler=True,
    )
    await db.insert_repost(row)
    await db.update_repost(500, status="ok", translated=True, fixer_id="fixupx")
    loaded = await db.get_repost(500)
    assert loaded.status == "ok" and loaded.translated is True and loaded.spoiler is True
    assert loaded.fixer_id == "fixupx"
    assert [r.repost_id for r in await db.get_reposts_for_origin(100)] == [500]
    assert await db.count_fixes_since(0) == 1
    with pytest.raises(ValueError):
        await db.update_repost(500, site="pixiv")
    await db.delete_reposts_for_origin(100)
    assert await db.get_repost(500) is None


async def test_roundup_queries(db):
    for i, (author, site) in enumerate([(7, "twitter"), (7, "pixiv"), (8, "twitter")]):
        await db.insert_post(guild_id=1, channel_id=5, message_id=i, author_id=author,
                             author_name=f"u{author}", site=site, canonical_key=f"{site}:{i}",
                             created_at=100 + i)
    assert await db.top_posters(1, since=0) == [(7, "u7", 2), (8, "u8", 1)]
    assert (await db.top_sites(1, since=0))[0] == ("twitter", 2)
    await db.insert_rerun_event(guild_id=1, offender_id=8, offender_name="u8", canonical_key="twitter:0")
    assert await db.top_rerun_offenders(1, since=0) == [(8, "u8", 1)]


async def test_fixer_health(db):
    await db.set_fixer_health(HealthRow("fxtwitter", False, 2, 10.0, 9.0))
    await db.set_fixer_health(HealthRow("fxtwitter", True, 0, 20.0, 20.0))
    health = await db.get_fixer_health()
    assert health["fxtwitter"].healthy is True and health["fxtwitter"].last_checked == 20.0


async def _user_version(database) -> int:
    async with database.conn.execute("PRAGMA user_version") as cursor:
        return (await cursor.fetchone())[0]


async def test_new_database_gets_the_current_schema_version(db):
    assert await _user_version(db) == dbmod.SCHEMA_VERSION


async def test_migrations_run_in_order(tmp_path, monkeypatch):
    path = str(tmp_path / "bb.db")
    first = Database(path)
    await first.connect()
    await first.close()
    nxt = dbmod.SCHEMA_VERSION + 1
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", nxt)
    steps = {**dbmod.MIGRATIONS, nxt: "ALTER TABLE posts ADD COLUMN note TEXT;"}
    monkeypatch.setattr(dbmod, "MIGRATIONS", steps)
    second = Database(path)
    await second.connect()
    assert await _user_version(second) == nxt
    async with second.conn.execute("PRAGMA table_info(posts)") as cursor:
        assert "note" in {row[1] for row in await cursor.fetchall()}
    await second.close()


async def test_new_database_starts_at_the_latest_version_without_migrating(tmp_path):
    # SCHEMA already has the latest shape: replaying the ALTERs on top of it would break a new install.
    database = Database(str(tmp_path / "bb.db"))
    await database.connect()  # would fail with "duplicate column" if a step ran
    assert await _user_version(database) == dbmod.SCHEMA_VERSION
    await database.close()


def _as_version_one(path, user_version):
    """A database as BB left it before the media column existed (schema version 1, or unversioned)."""
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE reposts DROP COLUMN media")
        conn.execute(f"PRAGMA user_version = {user_version}")
    conn.close()


async def test_version_one_database_gets_the_media_column(tmp_path):
    # production's path on the deploy that adds mixed-media support: old rows stay, with media ""
    path = tmp_path / "bb.db"
    first = Database(str(path))
    await first.connect()
    await first.insert_repost(RepostRow(
        repost_id=1, guild_id=1, channel_id=2, origin_message_id=3, origin_author_id=4,
        site="twitter", canonical_key="twitter:5", original_url="https://x.com/a/status/5",
    ))
    await first.close()
    _as_version_one(path, 1)
    second = Database(str(path))
    await second.connect()
    assert await _user_version(second) == 2
    row = await second.get_repost(1)
    assert row is not None and row.media == "" and row.canonical_key == "twitter:5"
    await second.close()


async def test_unversioned_database_counts_as_version_one(tmp_path):
    path = tmp_path / "bb.db"
    first = Database(str(path))
    await first.connect()
    await first.close()
    _as_version_one(path, 0)  # made before versioning existed: that's the version 1 shape
    second = Database(str(path))
    await second.connect()
    assert await _user_version(second) == dbmod.SCHEMA_VERSION  # upgraded from 1 like any old database
    await second.close()


async def test_database_from_a_newer_bb_is_refused(tmp_path):
    path = tmp_path / "bb.db"
    first = Database(str(path))
    await first.connect()
    await first.close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {dbmod.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        await Database(str(path)).connect()


async def test_missing_repost_column_fails_at_startup(tmp_path):
    path = tmp_path / "bb.db"
    with sqlite3.connect(path) as conn:
        # an older table: the indexed columns exist, others (e.g. translated) don't
        conn.execute(
            "CREATE TABLE reposts (repost_id INTEGER PRIMARY KEY, origin_message_id INTEGER, created_at REAL)"
        )
    with pytest.raises(RuntimeError, match="missing columns"):
        await Database(str(path)).connect()


# --- render -----------------------------------------------------------------------------

_FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True)
_SITE = Site("twitter", "Twitter", "Tweet", (_FX,), "https://x.com/i/status/1")


_FIXED = "https://fxtwitter.com/bb/status/1"


def test_format_repost_hidden():
    m = LinkMatch(_SITE, "https://x.com/bb/status/1", "twitter:1", author="bb", author_url="https://x.com/bb")
    src = "[Tweet](<https://x.com/bb/status/1>)"  # the real post, not previewed
    fx = f"[FxTwitter]({_FIXED})"  # the previewed link, and the one people copy to post elsewhere
    assert format_repost(m, "FxTwitter", _FIXED, style="hidden") == f"-# {src} · {fx}"
    assert format_repost(m, "FxTwitter", _FIXED, quip="hi", style="hidden") == f"-# {src} · {fx} · hi"
    assert format_upload(m, "hi", style="hidden") == f"-# {src} · hi"
    assert format_upload(m, style="hidden") == f"-# {src}"


def test_tweet_link_points_to_the_real_post_for_meme_domains():
    m = LinkMatch(
        _SITE, "https://girlcockx.com/bb/status/1", "twitter:1",
        parts={"already_fixed": "1", "mirror": "1", "user": "bb", "id": "1"},
    )
    assert format_repost(m, "FxTwitter", _FIXED, style="hidden").startswith(
        "-# [Tweet](<https://x.com/bb/status/1>) "
    )


def test_format_repost_subtle():
    m = LinkMatch(_SITE, "https://x.com/bb/status/1", "twitter:1", author="bb", author_url="https://x.com/bb")
    assert format_repost(m, "FxTwitter", _FIXED, quip="hi", style="subtle") == (
        f"-# [@bb on Twitter]({_FIXED}) · hi"
    )
    anon = LinkMatch(_SITE, "https://x.com/i/status/1", "twitter:1")
    assert format_repost(anon, "FxTwitter", _FIXED, style="subtle") == f"-# [Twitter post]({_FIXED})"
    assert format_upload(anon, style="subtle") == "-# [Twitter post](<https://x.com/i/status/1>)"


def test_format_spoiler():
    m = LinkMatch(_SITE, "https://x.com/i/status/1", "twitter:1", spoiler=True)
    assert format_repost(m, "FxTwitter", _FIXED, style="hidden") == (
        f"-# [Tweet](<https://x.com/i/status/1>) · ||[FxTwitter]({_FIXED}) ||"
    )
    assert format_repost(m, "FxTwitter", _FIXED, style="subtle") == f"-# ||[Twitter post]({_FIXED}) ||"


def test_shuffle_bag_cycles_without_repeats():
    key = "quip.fix"
    n = len(strings.variants(key))
    picks = [strings.next_variant(key) for _ in range(n * 6)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))  # never twice in a row
    assert set(picks[:n]) == set(strings.variants(key))  # first bag: every line exactly once
