"""Fixer health checks: probe every fixer, keep bot.down_fixers current, alert admins on changes.

Every 10 minutes each fixer is asked for its site's test post with Discord's crawler user agent.
A pass is HTTP 200 plus og:/twitter: meta tags. 2 failures in a row mark a fixer down, 1 pass
marks it up again. State lives in the fixer_health table, so it survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import aiohttp
import discord
from discord.ext import commands, tasks

from bb import strings
from bb.db import HealthRow
from bb.links import sites
from bb.links.model import Fixer, Site
from bb.timeutil import now

log = logging.getLogger(__name__)

DISCORDBOT_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
CHECK_MINUTES = 10
FAILURES_TO_GO_DOWN = 2
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=20)
PROBE_CONCURRENCY = 6
MAX_BODY_BYTES = 512 * 1024

Transition = Literal["down", "up"]

_PREVIEW_META = re.compile(
    r"""<meta\b[^>]*?\b(?:property|name)\s*=\s*["']?\s*(?:og|twitter):""",
    re.IGNORECASE,
)


# --- pure decisions -------------------------------------------------------------------------


def probe_passed(status: int, body: str) -> bool:
    """A fixer works if it answers 200 with Open Graph or Twitter Card meta tags."""
    return status == 200 and _PREVIEW_META.search(body) is not None


def step(prev: HealthRow | None, fixer_id: str, ok: bool, ts: float) -> tuple[HealthRow, Transition | None]:
    """Apply one probe result. Unknown fixers start healthy; 2 failures in a row = down, 1 pass = up."""
    if prev is None:
        prev = HealthRow(fixer_id, healthy=True, consecutive_failures=0, last_checked=ts, last_change=ts)
    failures = 0 if ok else prev.consecutive_failures + 1
    healthy = ok or (prev.healthy and failures < FAILURES_TO_GO_DOWN)
    transition: Transition | None = None
    if healthy != prev.healthy:
        transition = "up" if healthy else "down"
    row = replace(
        prev,
        healthy=healthy,
        consecutive_failures=failures,
        last_checked=ts,
        last_change=ts if transition else prev.last_change,
    )
    return row, transition


def looks_offline(results: Mapping[str, bool]) -> bool:
    """Every fixer of every site failing at once means BB lost its connection, not that they all died."""
    return len(results) >= 3 and not any(results.values())


def backup_for(site: Site, down: Collection[str]) -> Fixer | None:
    """The fixer BB uses now: the first one in configured order that isn't down."""
    return next((f for f in site.fixers if f.id not in down), None)


def down_ids(rows: Iterable[HealthRow], known: Collection[str] | None = None) -> set[str]:
    """Fixer ids marked down. With `known`, rows for fixers no longer configured are ignored."""
    return {r.fixer_id for r in rows if not r.healthy and (known is None or r.fixer_id in known)}


def _known_fixer_ids() -> set[str] | None:
    """Every configured fixer id, or None while the site table is empty."""
    return {f.id for site in sites.SITES.values() for f in site.fixers} or None


@dataclass(frozen=True)
class Alert:
    key: str
    values: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return strings.line(self.key, **self.values)


def plan_alerts(
    site_table: Mapping[str, Site],
    went_down: Collection[str],
    came_up: Collection[str],
    down: Collection[str],
) -> list[Alert]:
    """Alerts for one round. `down` is the down set after every result of the round was applied."""
    alerts: list[Alert] = []
    announced_up: set[str] = set()
    for site in site_table.values():
        dropped = [f for f in site.fixers if f.id in went_down]
        if dropped:
            backup = backup_for(site, down)
            if backup is None:
                alerts.append(Alert("alert.site_down", {"site": site.name}))
            else:
                alerts.extend(
                    Alert("alert.fixer_down", {"fixer": f.name, "site": site.name, "backup": backup.name})
                    for f in dropped
                )
        for f in site.fixers:
            if f.id in came_up and f.id not in announced_up:
                announced_up.add(f.id)
                alerts.append(Alert("alert.fixer_up", {"fixer": f.name}))
    return alerts


def chunk_lines(lines: Iterable[str], limit: int = 2000) -> list[str]:
    """Join lines into as few messages as fit Discord's length limit."""
    chunks: list[str] = []
    for line in lines:
        line = line[:limit]
        if chunks and len(chunks[-1]) + 1 + len(line) <= limit:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    return chunks


# --- network --------------------------------------------------------------------------------


async def probe(session: aiohttp.ClientSession, url: str) -> bool:
    """Fetch url the way Discord's crawler does and check it has a preview. Never raises."""
    try:
        async with session.get(
            url, headers={"User-Agent": DISCORDBOT_UA}, timeout=PROBE_TIMEOUT, allow_redirects=True
        ) as resp:
            if resp.status != 200:
                return False
            body = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) >= MAX_BODY_BYTES or b"</head>" in body.lower():
                    break
            return probe_passed(resp.status, body.decode("utf-8", errors="replace"))
    except (aiohttp.ClientError, TimeoutError, UnicodeError, ValueError) as exc:
        log.debug("probe %s failed: %r", url, exc)
        return False


def probe_targets(site_table: Mapping[str, Site]) -> list[tuple[Site, Fixer, str]]:
    """(site, fixer, fixer url for the site's test post) for every configured fixer."""
    targets: list[tuple[Site, Fixer, str]] = []
    for site in site_table.values():
        try:
            m = sites.match(site.test_url)
            if m is None:
                log.warning("health: test_url for %s doesn't match its own site: %s", site.id, site.test_url)
                continue
            targets.extend((site, fixer, sites.fixed_url(m, fixer)) for fixer in site.fixers)
        except Exception:
            log.exception("health: couldn't build probe urls for %s", site.id)
    return targets


# --- cog ------------------------------------------------------------------------------------


class Health(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await self.load_state()
        self.check.start()

    async def cog_unload(self) -> None:
        self.check.cancel()

    async def load_state(self) -> None:
        """Seed bot.down_fixers from the database (in place: other cogs may hold the set)."""
        rows = (await self.bot.db.get_fixer_health()).values()
        self.bot.down_fixers.clear()
        self.bot.down_fixers.update(down_ids(rows, _known_fixer_ids()))
        if self.bot.down_fixers:
            log.info("health: fixers down at startup: %s", ", ".join(sorted(self.bot.down_fixers)))

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check(self) -> None:
        try:
            await self.run_once()
        except Exception:
            log.exception("health: check round failed")

    @check.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def run_once(self) -> None:
        site_table = sites.SITES
        targets = probe_targets(site_table)
        if not targets:
            return
        session = self.bot.http_client
        limit = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def one(url: str) -> bool:
            async with limit:
                return await probe(session, url)

        oks = await asyncio.gather(*(one(url) for _, _, url in targets))
        results: dict[str, bool] = {}
        for (_, fixer, _), ok in zip(targets, oks, strict=True):
            # A fixer listed under two sites counts as up if either test post works.
            results[fixer.id] = results.get(fixer.id, False) or ok
        if looks_offline(results):
            log.warning("health: every probe failed; assuming BB's own connection is down, skipping round")
            return
        alerts = await self.record(results, now())
        for text in chunk_lines([a.render() for a in alerts]):
            await self.broadcast(text)

    async def record(self, results: Mapping[str, bool], ts: float) -> list[Alert]:
        """Persist one round of results, update bot.down_fixers, and return the alerts to send."""
        saved = await self.bot.db.get_fixer_health()
        went_down: set[str] = set()
        came_up: set[str] = set()
        for fixer_id, ok in results.items():
            row, transition = step(saved.get(fixer_id), fixer_id, ok, ts)
            await self.bot.db.set_fixer_health(row)
            saved[fixer_id] = row
            if transition == "down":
                went_down.add(fixer_id)
                log.warning("health: %s is down", fixer_id)
            elif transition == "up":
                came_up.add(fixer_id)
                log.info("health: %s is back up", fixer_id)
        down = down_ids(saved.values(), _known_fixer_ids())
        self.bot.down_fixers.clear()
        self.bot.down_fixers.update(down)
        return plan_alerts(sites.SITES, went_down, came_up, down)

    async def broadcast(self, text: str) -> None:
        """Send text to every server's alerts channel, if it has one."""
        for guild in list(self.bot.guilds):
            settings = await self.bot.settings.get(guild.id)
            if not settings.alerts_channel_id:
                continue
            channel = guild.get_channel_or_thread(settings.alerts_channel_id)
            if channel is None or not hasattr(channel, "send"):
                log.warning(
                    "health: alerts channel %s missing in guild %s", settings.alerts_channel_id, guild.id
                )
                continue
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("health: couldn't alert in guild %s: %s", guild.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Health(bot))
