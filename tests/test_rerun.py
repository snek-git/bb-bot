"""Rerun lookup on an in-memory database. Discord is replaced by a `locate` callable."""

from __future__ import annotations

from dataclasses import replace

from bb import rerun
from bb.settings_store import GuildSettings

NOW = 1_800_000_000.0
DAY = 86400
G, CH, OTHER_CH = 1, 10, 11
KEY = "twitter:1234"


async def _post(db, *, message_id, author_id=100, author_name="alice", channel_id=CH, age=DAY, key=KEY):
    return await db.insert_post(
        guild_id=G,
        channel_id=channel_id,
        message_id=message_id,
        author_id=author_id,
        author_name=author_name,
        site="twitter",
        canonical_key=key,
        created_at=NOW - age,
    )


def _alive(*gone: int):
    async def locate(post):
        if post.message_id in gone:
            return None
        return rerun.jump_url(post.guild_id, post.channel_id, post.message_id)

    return locate


async def _find(db, settings=None, *, author_id=200, channel_id=CH, message_id=999, locate=None):
    return await rerun.find_rerun(
        db,
        guild_id=G,
        channel_id=channel_id,
        message_id=message_id,
        author_id=author_id,
        canonical_key=KEY,
        settings=settings or GuildSettings(),
        locate=locate or _alive(),
        now_ts=NOW,
    )


async def test_hit_returns_most_recent_earlier_post(db):
    await _post(db, message_id=1, age=3 * DAY, author_name="old")
    await _post(db, message_id=2, age=2 * 3600, author_name="recent")
    hit = await _find(db)
    assert hit is not None
    assert hit.post.message_id == 2
    assert hit.ago == "2 hours ago"
    assert hit.jump == f"https://discord.com/channels/{G}/{CH}/2"


async def test_no_hit_for_other_keys_or_disabled(db):
    await _post(db, message_id=1, key="twitter:999")
    assert await _find(db) is None
    await _post(db, message_id=2)
    assert await _find(db, replace(GuildSettings(), reruns_enabled=False)) is None


async def test_window(db):
    await _post(db, message_id=1, age=31 * DAY)
    assert await _find(db) is None
    assert await _find(db, replace(GuildSettings(), rerun_window_days=60)) is not None


async def test_same_author_skipped_unless_roasting_self(db):
    await _post(db, message_id=1, author_id=200)
    assert await _find(db, author_id=200) is None
    assert await _find(db, replace(GuildSettings(), rerun_roast_self=True), author_id=200) is not None


async def test_same_author_does_not_hide_an_older_post_by_someone_else(db):
    await _post(db, message_id=1, author_id=100, age=2 * DAY)
    await _post(db, message_id=2, author_id=200, age=DAY)
    hit = await _find(db, author_id=200)
    assert hit is not None and hit.post.message_id == 1


async def test_same_channel_setting(db):
    await _post(db, message_id=1, channel_id=OTHER_CH)
    assert await _find(db) is not None
    assert await _find(db, replace(GuildSettings(), rerun_same_channel=True)) is None


async def test_never_matches_its_own_message(db):
    await _post(db, message_id=999)
    assert await _find(db, message_id=999) is None


async def test_deleted_original_is_dropped_and_next_older_used(db):
    await _post(db, message_id=1, age=3 * DAY)
    await _post(db, message_id=2, age=DAY)
    hit = await _find(db, locate=_alive(2))
    assert hit is not None and hit.post.message_id == 1
    remaining = await db.find_prior_posts(guild_id=G, canonical_key=KEY, since=0)
    assert [p.message_id for p in remaining] == [1]


async def test_all_gone_across_batches(db):
    for i in range(1, 13):  # more than one batch of rows, all deleted
        await _post(db, message_id=i, age=DAY + i)
    assert await _find(db, locate=_alive(*range(1, 13))) is None
    assert await db.find_prior_posts(guild_id=G, canonical_key=KEY, since=0) == []


async def test_locate_can_point_at_the_repost(db):
    await _post(db, message_id=1)

    async def to_repost(post):
        return rerun.jump_url(post.guild_id, post.channel_id, 5555)

    hit = await _find(db, locate=to_repost)
    assert hit is not None and hit.jump.endswith("/5555")


async def test_record_rerun_feeds_the_brainlet_award(db):
    await rerun.record_rerun(db, guild_id=G, offender_id=200, offender_name="bob", canonical_key=KEY,
                             created_at=NOW)
    assert await db.top_rerun_offenders(G, since=NOW - DAY) == [(200, "bob", 1)]


async def test_rerun_quip_names_without_markdown_or_ping(db):
    post_id = await _post(db, message_id=1, author_name="*star*")
    rows = await db.find_prior_posts(guild_id=G, canonical_key=KEY, since=0)
    assert rows[0].id == post_id
    hit = rerun.RerunHit(post=rows[0], jump="https://discord.com/channels/1/10/1", ago="1 day ago")
    text = rerun.rerun_quip(hit)
    assert "\\*star\\*" in text
    assert "(<https://discord.com/channels/1/10/1>)" in text
    assert "1 day ago" in text
    assert "<@" not in text
