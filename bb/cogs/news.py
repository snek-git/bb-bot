"""FGO JP news: every few minutes, check Atlas Academy's data and post new events, new banners and
end reminders to each server's news channel. What to post is decided in bb/news.py.

The exports are fetched once per check for all servers, with their ETag, so an unchanged file costs a
304 and no parsing. Each server records what it was sent, so a restart or a new server never repeats
anything, and a channel set today only gets what is new from now on (plus what started in the last
news.FRESH seconds).

Real end dates: some limited events (the Grand event, for one) are listed as running until a
placeholder date years away. Their quests close on the real date, so for each such event that started
recently, BB reads its war's quests and uses that date instead (see news.quest_end).

Pictures: an event's banner comes from its own API page, fetched only when a post needs it. Atlas lists
some that don't exist (the Grand event's), so an event post falls back to the art of a banner named
after the event, then to its war's banner. Pictures are downloaded and attached as files, as they are.
A post with no working picture just goes out as text.
"""

from __future__ import annotations

import io
import logging
import posixpath
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from discord.ext import commands, tasks

from bb import news
from bb.timeutil import now

log = logging.getLogger(__name__)

CHECK_MINUTES = 15
FETCH_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError)
MAX_PICTURE = 8 * 1024 * 1024  # well under Discord's upload limit; summon art is about 1 MB
REAL_END_TTL = 6 * 3600.0  # how long a looked-up end date is trusted (Atlas may extend an event)


@dataclass
class Data:
    events: list[news.Event]
    groups: list[news.Group]
    servants: dict[int, news.Servant]


class News(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._etags: dict[str, str] = {}
        self._raw: dict[str, Any] = {}
        self.data: Data | None = None
        self._event_art: dict[int, str | None] = {}  # event id -> its banner picture (None: it has none)
        self._war_art: dict[int, str | None] = {}  # war id -> its banner picture
        self._pictures: dict[str, bytes | None] = {}  # picture URL -> its bytes (None: it doesn't exist)
        self._real_ends: dict[int, tuple[float, float | None]] = {}  # event id -> (looked up at, end)

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(minutes=CHECK_MINUTES)
    async def tick(self) -> None:
        ts = now()
        try:
            await self.refresh(ts)
        except Exception:  # a changed export format must not stop the loop for good
            log.exception("news: couldn't read the Atlas Academy data")
        if self.data is None:
            return
        self._pictures.clear()  # downloaded again only when a post needs them
        for guild in list(self.bot.guilds):
            try:
                await self.post_due(guild, ts)
            except Exception:
                log.exception("news: failed for guild %s", guild.id)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _fetch(self, url: str) -> Any:
        """The export's JSON, or the copy from last time when it hasn't changed (HTTP 304)."""
        headers = {"If-None-Match": self._etags[url]} if url in self._raw and url in self._etags else {}
        async with self.bot.http_client.get(url, headers=headers) as resp:
            if resp.status == 304:
                return self._raw[url]
            resp.raise_for_status()
            body = await resp.json(content_type=None)
            self._raw[url] = body
            if etag := resp.headers.get("ETag"):
                self._etags[url] = etag
            return body

    async def _get_json(self, url: str) -> Any:
        async with self.bot.http_client.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def refresh(self, ts: float) -> bool:
        """Fetch the exports and rebuild self.data (with real end dates). False, and the old data kept
        (end times don't move, so its reminders stay right), if any export couldn't be fetched."""
        try:
            events = await self._fetch(news.EVENTS_URL)
            banners = await self._fetch(news.BANNERS_URL)
            servants = await self._fetch(news.SERVANTS_URL)
        except FETCH_ERRORS as exc:
            log.warning("news: couldn't fetch Atlas Academy data: %r", exc)
            return False
        self.data = Data(
            await self.with_real_ends(news.parse_events(events), ts),
            news.group_banners(news.parse_banners(banners)),
            news.parse_servants(servants),
        )
        return True

    async def with_real_ends(self, events: list[news.Event], ts: float) -> list[news.Event]:
        """Events listed as running for years get the date their quests close, when they have one."""
        fixed = []
        for event in events:
            if news.needs_real_end(event, ts):
                end = await self.real_end(event, ts)
                if end is not None:
                    event = replace(event, end=end)
            fixed.append(event)
        return fixed

    async def real_end(self, event: news.Event, ts: float) -> float | None:
        cached = self._real_ends.get(event.id)
        if cached is not None and ts - cached[0] < REAL_END_TTL:
            return cached[1]
        ends = []
        try:
            for war_id in event.war_ids:
                end = news.quest_end(await self._get_json(news.WAR_API.format(id=war_id)), event.start)
                if end is not None:
                    ends.append(end)
        except FETCH_ERRORS as exc:
            log.info("news: couldn't look up the quests of event %s: %r", event.id, exc)
            return cached[1] if cached else None
        end = max(ends) if ends else None
        self._real_ends[event.id] = (ts, end)
        return end

    async def event_art(self, event_id: int) -> str | None:
        """The event's banner picture. A failed lookup isn't remembered, so the next post tries again."""
        if event_id in self._event_art:
            return self._event_art[event_id]
        try:
            body = await self._get_json(news.EVENT_API.format(id=event_id))
        except FETCH_ERRORS as exc:
            log.info("news: couldn't look up event %s: %r", event_id, exc)
            return None
        art = body.get("banner") or body.get("noticeBanner") or None
        self._event_art[event_id] = art
        return art

    async def picture(self, url: str) -> bytes | None:
        """The picture's bytes, or None if it doesn't exist, isn't an image, or is too big to upload."""
        if url in self._pictures:
            return self._pictures[url]
        try:
            async with self.bot.http_client.get(url) as resp:
                data: bytes | None = None
                if resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        body += chunk
                        if len(body) > MAX_PICTURE:
                            log.info("news: picture %s is over %d bytes, left out", url, MAX_PICTURE)
                            break
                    else:
                        data = bytes(body)
        except FETCH_ERRORS as exc:
            log.info("news: couldn't download picture %s: %r", url, exc)
            return None
        self._pictures[url] = data
        return data

    async def war_art(self, war_id: int) -> str | None:
        """The war's banner: for the Grand event, the 冠位戴冠戦 Grand Duel strip it sits under."""
        if war_id in self._war_art:
            return self._war_art[war_id]
        try:
            body = await self._get_json(news.WAR_API.format(id=war_id))
        except FETCH_ERRORS as exc:
            log.info("news: couldn't look up war %s: %r", war_id, exc)
            return None
        art = body.get("banner") or None
        self._war_art[war_id] = art
        return art

    async def _event_pictures(self, notice: news.Notice) -> AsyncIterator[str | None]:
        """An event post's pictures, best first: its own banner, banners named after it, its war's banner.
        Each is looked up only if the ones before it are missing."""
        assert notice.event is not None
        yield await self.event_art(notice.event.id)
        for url in notice.fallbacks:
            yield url
        for war_id in notice.event.war_ids:
            yield await self.war_art(war_id)

    async def add_images(self, notices: list[news.Notice]) -> None:
        """Pick and download every post's picture, in place; a post with no working picture gets none."""
        for notice in notices:
            if notice.event is not None and notice.image is None:
                async for url in self._event_pictures(notice):
                    if url and await self.picture(url) is not None:
                        notice.image = url
                        break
            elif notice.image and await self.picture(notice.image) is None:
                notice.image = None

    def send_kwargs(self, message: news.Notice) -> dict[str, Any]:
        """channel.send / followup.send arguments: the text, with the picture attached as a file.
        Call add_images first: a picture that wasn't downloaded is left out."""
        kwargs: dict[str, Any] = {"content": message.text, "allowed_mentions": discord.AllowedMentions.none()}
        data = self._pictures.get(message.image) if message.image else None
        if data:
            name = posixpath.basename(urlsplit(message.image).path) or "picture.png"
            kwargs["file"] = discord.File(io.BytesIO(data), filename=name)
        return kwargs

    def live(self, ts: float) -> list[news.Notice] | None:
        """What's running right now (the /settings preview); None before the first fetch."""
        if self.data is None:
            return None
        return news.live(self.data.events, self.data.groups, self.data.servants, ts)

    async def post_due(self, guild: Any, ts: float) -> int:
        """Post this server's news if anything is due. Returns how many messages were sent."""
        s = await self.bot.settings.get(guild.id)
        if s.paused or not s.news_channel_id or self.data is None:
            return 0
        done = await self.bot.db.news_posted(guild.id)
        due = news.due(self.data.events, self.data.groups, self.data.servants, ts, done)
        if not due:
            return 0
        channel = guild.get_channel_or_thread(s.news_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(s.news_channel_id)
            except (discord.NotFound, discord.Forbidden):
                channel = None
        if channel is None or not hasattr(channel, "send"):
            # Skipped, not kept for later: news days late is no news.
            log.warning("news: channel %s unusable in guild %s, skipping", s.news_channel_id, guild.id)
            await self.bot.db.mark_news_posted(guild.id, [m for n in due for m in n.marks], ts)
            return 0

        await self.add_images(due)
        sent = 0
        for message in news.messages(due):
            try:
                await channel.send(**self.send_kwargs(message))
            except (discord.Forbidden, discord.NotFound) as exc:
                log.warning("news: can't post in guild %s: %s", guild.id, exc)
            except discord.HTTPException as exc:
                log.warning("news: send failed in guild %s, retrying next check: %s", guild.id, exc)
                return sent
            else:
                sent += 1
            await self.bot.db.mark_news_posted(guild.id, message.marks, ts)
        if sent:
            log.info("news: posted %d message(s) in guild %s", sent, guild.id)
        return sent


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(News(bot))
