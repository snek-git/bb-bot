"""Pings: when someone @-mentions BB without posting a supported link, she answers ("help" or a ping line)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import discord
from discord.ext import commands

from bb import strings
from bb.cogs.findsource import pick_image_url
from bb.cogs.linkfix import is_history
from bb.cooldown import Cooldown
from bb.links import extract, resolve, sites

log = logging.getLogger(__name__)

_HELP = re.compile(r"\bhelp\b", re.IGNORECASE)


def pings_bot(content: str, bot_id: int) -> bool:
    """An explicit @BB in the text. Reply pings don't count: replying to a repost isn't calling her."""
    return f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content


def wants_help(content: str) -> bool:
    return _HELP.search(content) is not None


def reply_key(content: str) -> str:
    return "help" if wants_help(content) else "ping"


def settings_channel_id(channel: Any) -> int:
    """Threads follow their parent channel's on/off setting."""
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return channel.parent_id
    return channel.id


async def has_supported_link(content: str, session: Any) -> bool:
    """True if linkfix would act on this message, so BB shouldn't answer the ping as well."""
    for found in extract.extract_urls(content):
        url = found.url
        if resolve.needs_expansion(url):
            url = await resolve.expand(url, session)
        if sites.match(url) is not None:
            return True
    return False


class Chat(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.cooldown = Cooldown()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        me = self.bot.user
        if me is None or message.guild is None or message.author.bot or message.webhook_id:
            return
        if not pings_bot(message.content, me.id):
            return
        if is_history(message.created_at.timestamp(), time.time()):
            return  # a replayed or old message: never answer pings from the past
        settings = await self.bot.settings.get(message.guild.id)
        if settings.paused or not settings.channel_enabled(settings_channel_id(message.channel)):
            return
        image_url = await self.image_to_search(message)
        if image_url is not None:
            await self.find_source(message, image_url)
            return
        try:
            if await has_supported_link(message.content, self.bot.http_client):
                return
        except Exception:
            log.exception("link check failed; staying quiet")
            return
        if not self.cooldown.ready(message.author.id):
            return
        key = reply_key(message.content)
        text = strings.line(key, skip_word=settings.skip_word) if key == "help" else strings.line(key)
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.debug("ping reply failed: %r", exc)


    async def image_to_search(self, message: discord.Message) -> str | None:
        """@BB with an image: the image uploaded with the ping, else the image in the message it replies to
        (an upload, or a repost's preview)."""
        url = pick_image_url(message.attachments, ())  # the ping's own previews aren't ready yet
        if url is not None:
            return url
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if target is None:
            try:
                target = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        return pick_image_url(target.attachments, target.embeds)

    async def find_source(self, message: discord.Message, image_url: str) -> None:
        finder = self.bot.get_cog("FindSource")
        if finder is None or not self.cooldown.ready(message.author.id):
            return
        try:
            async with message.channel.typing():
                text, view = await finder.answer(image_url)
            await message.reply(
                text, view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            log.debug("find source reply failed: %r", exc)


async def setup(bot: Any) -> None:
    await bot.add_cog(Chat(bot))
