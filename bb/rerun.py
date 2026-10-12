"""Rerun detection: has this post already been shared in the server? DB-only, Discord stays outside.

The one Discord question (does the earlier message still exist?) is asked through `locate`, an async
callable the caller supplies, so the lookup can be tested against an in-memory database.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from bb import strings
from bb.db import Database, PostRow
from bb.settings_store import GuildSettings
from bb.timeutil import humanize_ago, now

DAY = 86400
BATCH = 5
MAX_ROUNDS = 6  # at most 30 dead rows cleaned up per lookup

# Given an earlier post, return a jump URL to it (or to BB's repost of it), or None if it's gone.
Locate = Callable[[PostRow], Awaitable[str | None]]


@dataclass(frozen=True)
class RerunHit:
    post: PostRow
    jump: str
    ago: str


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


async def find_rerun(
    db: Database,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int,
    canonical_key: str,
    settings: GuildSettings,
    locate: Locate,
    now_ts: float | None = None,
) -> RerunHit | None:
    """The most recent earlier post of this key that still exists, within the server's rerun rules.

    Rows whose message is gone are deleted on the way, and the next older one is checked.
    Call this before inserting the new post, so a link never matches itself.
    """
    if not settings.reruns_enabled:
        return None
    ts = now() if now_ts is None else now_ts
    since = ts - settings.rerun_window_days * DAY
    for _ in range(MAX_ROUNDS):
        rows = await db.find_prior_posts(
            guild_id=guild_id,
            canonical_key=canonical_key,
            since=since,
            channel_id=channel_id if settings.rerun_same_channel else None,
            exclude_author_id=None if settings.rerun_roast_self else author_id,
            exclude_message_id=message_id,
            limit=BATCH,
        )
        if not rows:
            return None
        for row in rows:
            jump = await locate(row)
            if jump is not None:
                return RerunHit(post=row, jump=jump, ago=humanize_ago(ts - row.created_at))
            await db.delete_post(row.id)
    return None


async def record_rerun(
    db: Database,
    *,
    guild_id: int,
    offender_id: int,
    offender_name: str,
    canonical_key: str,
    created_at: float | None = None,
) -> None:
    """Log the roast for the weekly Buster Brainlet award."""
    await db.insert_rerun_event(
        guild_id=guild_id,
        offender_id=offender_id,
        offender_name=offender_name,
        canonical_key=canonical_key,
        created_at=created_at,
    )


def rerun_quip(hit: RerunHit) -> str:
    """The rerun callout. The earlier poster is named by stored display name, never pinged."""
    author = discord.utils.escape_markdown(hit.post.author_name)
    return strings.line("quip.rerun", author=author, ago=hit.ago, jump=hit.jump)
