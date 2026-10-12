"""Edit and delete sync: reposts follow their original message."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bb.bot import BBBot
    from bb.cogs.linkfix import LinkFix

log = logging.getLogger(__name__)


def content_changed(data: dict[str, Any], cached: discord.Message | None) -> bool:
    """Edit events also fire when previews load or flags change; only real content edits count.

    Without a cached copy we can't tell, so assume it changed; the key diff in linkfix is a no-op then.
    """
    if "content" not in data:
        return False
    return cached is None or cached.content != data["content"]


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
        if await db.get_repost(message_id) is not None:
            await db.delete_repost(message_id)  # someone deleted a repost
            return
        rows = await db.get_reposts_for_origin(message_id)
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
            log.exception("delete sync failed for message %s", payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        for message_id in payload.message_ids:
            try:
                await self.handle_deleted(message_id)
            except Exception:
                log.exception("delete sync failed for message %s", message_id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None or not content_changed(payload.data, payload.cached_message):
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
            await linkfix.process(message, edit=True)
        except Exception:
            log.exception("edit sync failed for message %s", payload.message_id)


async def setup(bot: BBBot) -> None:
    await bot.add_cog(Sync(bot))
