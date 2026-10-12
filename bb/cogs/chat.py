"""Pings: when someone @-mentions BB without posting a supported link, she answers ("help" or a ping line)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import discord
from discord.ext import commands

from bb import strings
from bb.links import extract, resolve, sites

log = logging.getLogger(__name__)

COOLDOWN_SECONDS = 30.0
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


class Cooldown:
    """At most one hit per key per `seconds`."""

    def __init__(self, seconds: float = COOLDOWN_SECONDS) -> None:
        self.seconds = seconds
        self._last: dict[int, float] = {}

    def ready(self, key: int, now: float | None = None) -> bool:
        """True (and starts the cooldown) if `key` may act now."""
        now = time.monotonic() if now is None else now
        if len(self._last) > 512:
            self._last = {k: t for k, t in self._last.items() if now - t < self.seconds}
        last = self._last.get(key)
        if last is not None and now - last < self.seconds:
            return False
        self._last[key] = now
        return True


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
        settings = await self.bot.settings.get(message.guild.id)
        if not settings.channel_enabled(settings_channel_id(message.channel)):
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


async def setup(bot: Any) -> None:
    await bot.add_cog(Chat(bot))
