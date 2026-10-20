"""Weekly roundup: once a week per server, an embed with the top poster, top site and Buster Brainlet.

Each server picks a channel, weekday and hour (in its own timezone). The post goes out within
GRACE of that slot; roundup_last_sent is saved after sending, so restarts never double-post.
If the bot was offline for the whole grace period, that week is skipped rather than posted days late.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

import discord
from discord.ext import commands, tasks

from bb import ehentai, nhentai, render, strings
from bb.links import sites
from bb.timeutil import now, zone

log = logging.getLogger(__name__)

BB_PURPLE = discord.Colour(render.BB_PURPLE)
CHECK_MINUTES = 5
WEEK = 7 * 86400.0
GRACE = 12 * 3600.0  # how late a missed slot may still be posted (bot restarting, outages)
MIN_GAP = 86400.0  # never post twice within a day, e.g. after an admin moves the slot

Line = tuple[str, dict[str, Any]]


# --- pure decisions -------------------------------------------------------------------------


def latest_slot(ts: float, tz: str, weekday: int, hour: int) -> float:
    """The most recent `weekday` at `hour`:00 local time (Monday = 0) at or before ts, as a timestamp."""
    tzinfo = zone(tz)
    local = datetime.fromtimestamp(ts, tzinfo)
    days_back = (local.weekday() - weekday % 7) % 7
    slot = datetime.combine(local.date() - timedelta(days=days_back), dtime(hour % 24), tzinfo=tzinfo)
    if slot.timestamp() > ts:
        slot -= timedelta(days=7)  # wall-clock arithmetic, so the hour survives DST changes
    return slot.timestamp()


def roundup_due(ts: float, tz: str, weekday: int, hour: int, last_sent: float) -> bool:
    slot = latest_slot(ts, tz, weekday, hour)
    return last_sent < slot and ts - slot < GRACE and ts - last_sent >= MIN_GAP


_CARD_SITES = {source.SITE_ID: source.SITE for source in (ehentai, nhentai)}


def site_name(site_id: str) -> str:
    site = sites.SITES.get(site_id) or _CARD_SITES.get(site_id)
    return site.name if site else site_id


def _who(name: str) -> str:
    return f"**{discord.utils.escape_markdown(name)}**"


def roundup_content(
    posters: list[tuple[int, str, int]],
    top_sites: list[tuple[str, int]],
    offenders: list[tuple[int, str, int]],
    brainlet_award: bool,
) -> list[Line]:
    """(string key, values) for each paragraph of the roundup, in order."""
    lines: list[Line] = []
    if posters:
        _, name, n = posters[0]
        lines.append(("roundup.top_poster", {"user": _who(name), "n": n}))
    else:
        lines.append(("roundup.quiet", {}))
    if top_sites:
        lines.append(("roundup.top_site", {"site": site_name(top_sites[0][0])}))
    if brainlet_award and offenders:
        _, name, n = offenders[0]
        lines.append(("roundup.brainlet", {"user": _who(name), "times": times(n)}))
    return lines


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def build_embed(lines: list[Line], ts: float | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=strings.line("roundup.title"),
        description="\n\n".join(strings.line(key, **values) for key, values in lines),
        colour=BB_PURPLE,
    )
    if ts is not None:
        embed.timestamp = datetime.fromtimestamp(ts, UTC)
    return embed


# --- cog ------------------------------------------------------------------------------------


class Roundup(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        # guild id -> last roundup handled this run. Checked alongside the saved value, so a failed save
        # can't make every tick post the roundup again.
        self._sent: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(minutes=CHECK_MINUTES)
    async def tick(self) -> None:
        ts = now()
        for guild in list(self.bot.guilds):
            try:
                await self.maybe_post(guild, ts)
            except Exception:
                log.exception("roundup: failed for guild %s", guild.id)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def maybe_post(self, guild: Any, ts: float) -> bool:
        """Post this server's roundup if it's due. Returns True if one was sent."""
        s = await self.bot.settings.get(guild.id)
        if s.paused or not s.roundup_channel_id:
            return False
        last_sent = max(s.roundup_last_sent, self._sent.get(guild.id, 0.0))
        if not roundup_due(ts, s.timezone, s.roundup_weekday, s.roundup_hour, last_sent):
            return False
        channel = guild.get_channel_or_thread(s.roundup_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(s.roundup_channel_id)
            except (discord.NotFound, discord.Forbidden):
                channel = None
        if channel is None or not hasattr(channel, "send"):
            log.warning(
                "roundup: channel %s unusable in guild %s, skipping this week", s.roundup_channel_id, guild.id
            )
            await self._mark_sent(guild.id, ts)
            return False

        embed = await self.build(guild.id, ts, s.brainlet_award)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.NotFound) as exc:
            # Permanent until an admin fixes it: skip the week instead of retrying every tick.
            log.warning("roundup: can't post in guild %s: %s", guild.id, exc)
            await self._mark_sent(guild.id, ts)
            return False
        except discord.HTTPException as exc:
            log.warning("roundup: send failed in guild %s, retrying next tick: %s", guild.id, exc)
            return False
        await self._mark_sent(guild.id, ts)
        log.info("roundup: posted in guild %s", guild.id)
        return True

    async def build(self, guild_id: int, ts: float, brainlet_award: bool) -> discord.Embed:
        since = ts - WEEK
        db = self.bot.db
        posters = await db.top_posters(guild_id, since, limit=1)
        top = await db.top_sites(guild_id, since, limit=1)
        offenders = await db.top_rerun_offenders(guild_id, since, limit=1) if brainlet_award else []
        return build_embed(roundup_content(posters, top, offenders, brainlet_award), ts)

    async def _mark_sent(self, guild_id: int, ts: float) -> None:
        self._sent[guild_id] = ts  # first: the save below can fail
        await self.bot.settings.update(guild_id, roundup_last_sent=ts)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roundup(bot))
