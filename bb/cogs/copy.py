"""/copy: the channel's last messages and their image links, as a text file only the caller sees.

A bot can't reach anyone's clipboard, so the file is the copy: open it, select all, copy.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bb import strings
from bb.cogs.findsource import image_urls
from bb.cooldown import Cooldown

log = logging.getLogger(__name__)

DEFAULT_COUNT = 500
MAX_COUNT = 1000


def format_message(message: Any) -> str:
    """'[2026-11-17 14:03] name: text', then each image link on its own indented line."""
    when = message.created_at.strftime("%Y-%m-%d %H:%M")
    lines = [f"[{when}] {message.author.display_name}: {message.clean_content}"]
    urls = image_urls(message.attachments, message.embeds, getattr(message, "components", ()))
    lines.extend(f"    {url}" for url in urls)
    return "\n".join(lines)


def format_log(channel_name: str, messages: Iterable[Any]) -> str:
    """Oldest first, under a header naming the channel. Times are UTC."""
    messages = list(messages)
    header = f"#{channel_name} · last {len(messages)} messages · times in UTC"
    return "\n".join([header, "", *(format_message(m) for m in messages)]) + "\n"


class Copy(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.cooldown = Cooldown()  # each run reads up to 10 pages of history

    @app_commands.command(name="copy", description="This channel's last messages and image links, in a file")
    @app_commands.describe(count=f"How many messages (1-{MAX_COUNT}, default {DEFAULT_COUNT})")
    @app_commands.guild_only()
    async def copy(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, MAX_COUNT] = DEFAULT_COUNT
    ) -> None:
        await self.run(interaction, count)

    async def run(self, interaction: discord.Interaction, count: int) -> None:
        if not self.cooldown.ready(interaction.user.id):
            await interaction.response.send_message(strings.line("copy.cooldown"), ephemeral=True)
            return
        channel = interaction.channel
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            newest_first = [m async for m in channel.history(limit=count)]
        except discord.Forbidden:
            await interaction.followup.send(strings.line("copy.forbidden"), ephemeral=True)
            return
        name = getattr(channel, "name", None) or "channel"
        text = format_log(name, reversed(newest_first))
        file = discord.File(io.BytesIO(text.encode()), filename=f"{name}-last-{len(newest_first)}.txt")
        await interaction.followup.send(
            strings.line("copy.done", n=len(newest_first)), file=file, ephemeral=True
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Copy(bot))
