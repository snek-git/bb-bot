"""The bot object every cog receives. Holds shared state; cogs hold behaviour."""

from __future__ import annotations

import logging

import aiohttp
import discord
from discord.ext import commands

from bb import nhentai, render, strings, upload
from bb.config import Config
from bb.db import Database
from bb.settings_store import SettingsStore

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bb.cogs.linkfix",  # B
    "bb.cogs.sync",  # B
    "bb.cogs.buttons",  # C
    "bb.cogs.findsource",  # C
    "bb.cogs.settings",  # C
    "bb.cogs.chat",  # C
    "bb.cogs.health",  # D
    "bb.cogs.status",  # D
    "bb.cogs.roundup",  # D
    "bb.cogs.heartbeat",
    "bb.cogs.news",
)

USER_AGENT = "BB-bot (+https://github.com/; Discord link fixer)"


def make_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True  # privileged: enable it in the Developer Portal
    return intents


class BBBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,  # unused: no text commands, see on_message
            intents=make_intents(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.db = Database(config.db_path)
        self.settings = SettingsStore(self.db)
        # Fixer ids the health checker currently considers down. Owned by cogs.health.
        self.down_fixers: set[str] = set()
        self.http_session: aiohttp.ClientSession | None = None
        strings.set_emojis(config.emojis)
        render.set_style(config.link_style)
        nhentai.set_api_key(config.nhentai_key)

    @property
    def http_client(self) -> aiohttp.ClientSession:
        """Shared aiohttp session for fixers, APIs and downloads."""
        if self.http_session is None:
            raise RuntimeError("bot.prepare() was not called")
        return self.http_session

    async def prepare(self) -> None:
        """Everything except connecting to Discord. Used by setup_hook and by --check."""
        await self.db.connect()
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": USER_AGENT},
            connector=upload.public_connector(),  # nothing BB fetches may reach its own network
        )
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("loaded %s", extension)

    async def setup_hook(self) -> None:
        await self.prepare()
        if self.config.dev_guild_id:
            guild = discord.Object(self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to dev guild %s", len(synced), self.config.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global commands", len(synced))

    async def on_message(self, message: discord.Message) -> None:
        # Deliberately empty: BB has no text commands. Cogs listen to on_message themselves.
        return

    async def close(self) -> None:
        if self.http_session is not None:
            await self.http_session.close()
            self.http_session = None
        await self.db.close()
        await super().close()
