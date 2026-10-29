"""Edit and delete sync: reposts follow their original message."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from bb.fixlog import trail

if TYPE_CHECKING:
    from bb.bot import BBBot
    from bb.cogs.linkfix import LinkFix

log = logging.getLogger(__name__)


def edit_counts(created_at: float, now_ts: float, *, handled_before: bool) -> bool:
    """Discord sends edit events for old messages too (pins, refreshed previews). Only follow an edit if
    BB already handled that message, or it was posted moments ago (someone adding a link right after
    posting). content_changed filters out the events that aren't real edits, and process only adds
    reposts to an old message when the edit itself is fresh."""
    from bb.cogs.linkfix import FRESH_FOR

    return handled_before or (now_ts - created_at) <= FRESH_FOR


def content_changed(data: dict[str, Any], cached: discord.Message | None, now_ts: float) -> bool:
    """Edit events also fire for pins, loading previews and flag changes, with the full content and no
    diff; only real content edits count. With a cached copy, compare. Without one (always the case
    after a restart), only an edit Discord stamped in the last few minutes counts: a pin on an old
    message keeps its old edited_timestamp, or none at all."""
    from bb.cogs.linkfix import FRESH_FOR

    if "content" not in data:
        return False
    if cached is not None:
        return cached.content != data["content"]
    stamp = data.get("edited_timestamp")
    if not stamp:
        return False
    try:
        edited_at = datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return False
    return now_ts - edited_at <= FRESH_FOR


class Sync(commands.Cog):
    def __init__(self, bot: BBBot) -> None:
        self.bot = bot

    @property
    def linkfix(self) -> LinkFix | None:
        return self.bot.get_cog("LinkFix")  # type: ignore[return-value]

    async def handle_deleted(self, message_id: int) -> None:
        db = self.bot.db
        linkfix = self.linkfix
        if linkfix is not None and linkfix.consume_expected_delete(message_id):
            return  # BB deleted the original itself (original_action = delete): keep the reposts
        repost = await db.get_repost(message_id)
        if repost is not None:
            trail(repost.origin_message_id, "someone deleted repost %s directly (not with its button)",
                  message_id)
            await db.delete_repost(message_id)
            return
        rows = await db.get_reposts_for_origin(message_id)
        if rows or await db.post_keys_for_message(message_id):
            trail(message_id, "the original was deleted: taking down %d repost(s)", len(rows))
        if linkfix is not None:
            linkfix.mark_origin_deleted(message_id)  # a fix still in progress drops its repost
            for row in rows:
                await linkfix.delete_repost_message(row)
        if rows:
            await db.delete_reposts_for_origin(message_id)
        await db.delete_posts_for_message(message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        try:
            await self.handle_deleted(payload.message_id)
        except Exception:
            trail(payload.message_id, "delete sync crashed", level=logging.ERROR, exc_info=True)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        for message_id in payload.message_ids:
            try:
                await self.handle_deleted(message_id)
            except Exception:
                trail(message_id, "delete sync crashed", level=logging.ERROR, exc_info=True)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None or not content_changed(payload.data, payload.cached_message, time.time()):
            return
        linkfix = self.linkfix
        if linkfix is None:
            return
        message = payload.message
        try:
            if not await linkfix.accepts(message):
                return
            if await self.bot.db.get_repost(message.id) is not None:
                return  # BB's own repost being edited (fixer switch, translate)
            handled = bool(await self.bot.db.post_keys_for_message(message.id))
            if not edit_counts(message.created_at.timestamp(), time.time(), handled_before=handled):
                return
            await linkfix.process(message, edit=True)
        except Exception:
            trail(payload.message_id, "edit sync crashed", level=logging.ERROR, exc_info=True)


async def setup(bot: BBBot) -> None:
    await bot.add_cog(Sync(bot))
