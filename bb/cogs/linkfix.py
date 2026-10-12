"""Link fixing: detect supported links, repost them through a fixer, fall back, then handle the original.

Decisions (quip choice, edit diffing, what to do with the original, ...) are module-level functions so
they can be tested without Discord. The cog keeps Discord I/O in a handful of small methods.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import tempfile
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import discord
from discord.ext import commands

from bb import ehentai, render, rerun, strings, upload, views
from bb.db import PostRow, RepostRow
from bb.links import extract, resolve, sites
from bb.links.model import Fixer, LinkMatch
from bb.settings_store import GuildSettings

if TYPE_CHECKING:
    from bb.bot import BBBot

log = logging.getLogger(__name__)

MAX_LINKS = 4  # fixed links per message
MAX_SCANNED = 10  # URLs looked at per message (bounds short-link expansion)
EMBED_TIMEOUT = 6.0
RESUPPRESS_DELAY = 1.0
WEBHOOK_NAME = "BB"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SETTLED_OK = ("ok", "uploaded")

OriginalAction = Literal["suppress", "delete", "nothing"]


# --- pure decisions --------------------------------------------------------------------------


def is_skipped(content: str, skip_word: str) -> bool:
    """True if the skip word appears as a whole word, case-insensitive."""
    word = skip_word.strip()
    if not word:
        return False
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", content, re.IGNORECASE) is not None


def channel_allowed(settings: GuildSettings, channel_id: int, parent_id: int | None = None) -> bool:
    """Channel rules. Threads follow their parent channel (/settings only lists normal channels)."""
    return settings.channel_enabled(parent_id if parent_id is not None else channel_id)


def should_handle(
    settings: GuildSettings, *, channel_id: int, parent_id: int | None, is_webhook: bool
) -> bool:
    if is_webhook and not settings.fix_webhooks:
        return False
    return channel_allowed(settings, channel_id, parent_id)


def is_already_fixed(m: LinkMatch) -> bool:
    """Someone posted a fixer link themselves: record it, but don't repost."""
    return m.parts.get("already_fixed") == "1"


def choose_quip(
    settings: GuildSettings,
    *,
    rerun_line: str | None,
    site_name: str,
    channel_id: int | None = None,
    rng: random.Random | Any = random,
) -> str | None:
    """One quip line at most. A rerun callout always wins.

    Otherwise a quip fires at the server's rate, drawn from the channel's themed pool (lewd/chaos) if it
    has one, else the normal pool. channel_id is the parent channel for threads.
    """
    if rerun_line:
        return rerun_line
    if settings.quip_rate <= 0 or rng.randrange(settings.quip_rate) != 0:
        return None
    flavor = settings.channel_flavor(channel_id) if channel_id is not None else None
    if flavor:
        return strings.line(f"quip.{flavor}", site=site_name)
    return strings.line("quip.fix", site=site_name)


def settle_quip(current: str | None, *, is_rerun: bool, outcome: Literal["uploaded", "failed"]) -> str:
    """Quip after the upload fallback: the rerun callout keeps its place, otherwise the outcome line."""
    if is_rerun and current:
        return current
    return strings.line(f"quip.{outcome}")


def embeds_have_image(embeds: Iterable[dict[str, Any]]) -> bool:
    """True if a preview shows a still image (an image, or a thumbnail on a non-video embed)."""
    for embed in embeds:
        if (embed.get("image") or {}).get("url"):
            return True
        if embed.get("video") or embed.get("type") in ("video", "gifv"):
            continue
        if (embed.get("thumbnail") or {}).get("url"):
            return True
    return False


def diff_keys(old_keys: set[str], matches: list[LinkMatch]) -> tuple[set[str], list[LinkMatch]]:
    """(keys whose link was removed, matches that are new) between stored posts and current content."""
    current = {m.canonical_key for m in matches}
    removed = old_keys - current
    added = [m for m in matches if m.canonical_key not in old_keys]
    return removed, added


def original_action(
    setting: OriginalAction, statuses: Iterable[str], *, has_already_fixed: bool
) -> OriginalAction:
    """What to do with the original once every repost settled.

    Only when there is at least one repost and all of them worked (fixed or uploaded). A failed or
    vanished repost keeps the original's own preview, and so does a fixer link the poster posted
    themselves (it is the only preview that link has).
    """
    statuses = list(statuses)
    if not statuses or has_already_fixed:
        return "nothing"
    if any(status not in SETTLED_OK for status in statuses):
        return "nothing"
    return setting


def webhook_username(name: str) -> str:
    """Discord refuses webhook names containing 'discord' or 'clyde', and caps them at 80 chars."""
    clean = re.sub(r"(?i)discord", "Disc0rd", name)
    clean = re.sub(r"(?i)clyde", "Clyd3", clean).strip()[:80]
    return clean or "Senpai"


def is_image_file(filename: str) -> bool:
    return filename.lower().endswith(IMAGE_EXTENSIONS)


async def collect_matches(
    content: str, settings: GuildSettings, session: aiohttp.ClientSession | None
) -> list[LinkMatch]:
    """Supported, enabled links in the message, in order, one per post, at most MAX_LINKS."""
    matches: list[LinkMatch] = []
    seen: set[str] = set()
    for found in extract.extract_urls(content)[:MAX_SCANNED]:
        if len(matches) >= MAX_LINKS:
            break
        url = found.url
        if session is not None and resolve.needs_expansion(url):
            url = await resolve.expand(url, session)
        m = sites.match(url, spoiler=found.spoiler)
        if m is None or not settings.site_enabled(m.site.id) or m.canonical_key in seen:
            continue
        seen.add(m.canonical_key)
        matches.append(m)
    return matches


class RecentIds:
    """A bounded set of message ids (oldest forgotten first)."""

    def __init__(self, size: int = 1024) -> None:
        self._size = size
        self._ids: OrderedDict[int, None] = OrderedDict()

    def add(self, item: int) -> None:
        self._ids[item] = None
        self._ids.move_to_end(item)
        while len(self._ids) > self._size:
            self._ids.popitem(last=False)

    def __contains__(self, item: object) -> bool:
        return item in self._ids

    def consume(self, item: int) -> bool:
        """Remove item; True if it was there."""
        return self._ids.pop(item, False) is None


# --- the cog ---------------------------------------------------------------------------------


@dataclass
class Sent:
    message: Any  # discord.Message or discord.WebhookMessage
    webhook: discord.Webhook | None = None
    thread: discord.Thread | None = None


@dataclass
class LiveRepost:
    row: RepostRow
    match: LinkMatch
    chain: list[Fixer]
    sent: Sent
    is_rerun: bool
    max_bytes: int


def _embed_dicts(message: Any) -> list[dict[str, Any]]:
    return [embed.to_dict() for embed in getattr(message, "embeds", None) or []]


def _parent_id(channel: Any) -> int | None:
    return channel.parent_id if isinstance(channel, discord.Thread) else None


class LinkFix(commands.Cog):
    def __init__(self, bot: BBBot) -> None:
        self.bot = bot
        self.embed_timeout = EMBED_TIMEOUT
        self.resuppress_delay = RESUPPRESS_DELAY
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self._embed_waiters: dict[int, asyncio.Future[list[dict[str, Any]]]] = {}
        self._recent_embeds: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()
        self._webhooks: dict[int, discord.Webhook] = {}  # parent channel id -> BB's webhook there
        self._webhook_lock = asyncio.Lock()
        self._expected_deletes = RecentIds()  # originals BB deleted itself (original_action = delete)
        self._deleted_origins = RecentIds()  # originals deleted by someone while BB was working

    # --- shared with sync.py --------------------------------------------------------------

    @asynccontextmanager
    async def message_lock(self, message_id: int) -> AsyncIterator[None]:
        """One handler per message at a time (on_message, edits, duplicate events)."""
        lock = self._locks.setdefault(message_id, asyncio.Lock())
        self._lock_users[message_id] = self._lock_users.get(message_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users[message_id] - 1
            if remaining:
                self._lock_users[message_id] = remaining
            else:
                del self._lock_users[message_id]
                del self._locks[message_id]

    def own_webhook_ids(self) -> set[int]:
        return {hook.id for hook in self._webhooks.values()}

    def consume_expected_delete(self, message_id: int) -> bool:
        return self._expected_deletes.consume(message_id)

    def mark_origin_deleted(self, message_id: int) -> None:
        self._deleted_origins.add(message_id)

    async def accepts(self, message: discord.Message) -> bool:
        """Cheap checks before settings: not BB, not a DM, not a system message, not BB's own webhook."""
        if message.guild is None:
            return False
        me = self.bot.user
        if me is not None and message.author.id == me.id:
            return False
        if message.author.bot and message.webhook_id is None:
            # Other bots (FixTweet, saucebot, ...): never fix, record or roast them, or bots feed on each
            # other. Webhook messages also have author.bot set; those go by the fix_webhooks setting.
            return False
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        if message.webhook_id is not None:
            if message.webhook_id in self.own_webhook_ids():
                return False
            if await self.bot.db.get_repost(message.id) is not None:
                return False
        return True

    async def process(self, message: discord.Message, *, edit: bool = False) -> None:
        """Fix a new message, or (edit=True) bring an edited one's reposts in line with its links."""
        guild = message.guild
        if guild is None:
            return
        async with self.message_lock(message.id):
            settings = await self.bot.settings.get(guild.id)
            if not should_handle(
                settings,
                channel_id=message.channel.id,
                parent_id=_parent_id(message.channel),
                is_webhook=message.webhook_id is not None,
            ):
                return
            db = self.bot.db
            old_keys = await db.post_keys_for_message(message.id)
            if old_keys and not edit:
                return  # already handled (duplicate event)
            cards = 0
            if is_skipped(message.content, settings.skip_word):
                if not edit:
                    return
                matches: list[LinkMatch] = []
            else:
                matches = await collect_matches(message.content, settings, self.bot.http_session)
                if not edit and settings.site_enabled(ehentai.SITE_ID):
                    cards = await self._post_cards(message, settings)
            # Cards aren't edit-synced: an edit that doesn't touch the gallery link shouldn't drop its card.
            link_keys = {key for key in old_keys if not key.startswith(f"{ehentai.SITE_ID}:")}
            removed, added = diff_keys(link_keys, matches)
            for key in removed:
                await self._remove_link(message.id, key)
            if not added:
                if cards and message.id not in self._deleted_origins:
                    # Only cards: the original's own (bare) e-hentai preview goes, like a fixed link's would.
                    action = original_action(
                        settings.original_action,
                        ["ok"] * cards,
                        has_already_fixed=any(is_already_fixed(m) for m in matches),
                    )
                    if action != "nothing":
                        await self._apply_original(message, action)
                return
            outcomes = await self._fix_links(message, settings, added, edit=edit)
            if not outcomes or message.id in self._deleted_origins:
                return
            statuses = [row.status for row in await db.get_reposts_for_origin(message.id)]
            statuses += [o for o in outcomes if o not in SETTLED_OK]
            action = original_action(
                settings.original_action,
                statuses,
                has_already_fixed=any(is_already_fixed(m) for m in matches),
            )
            if action != "nothing":
                await self._apply_original(message, action)

    async def delete_repost_message(self, row: RepostRow) -> None:
        """Delete one of BB's reposts from Discord (the DB row is the caller's business)."""
        channel = self.bot.get_partial_messageable(row.channel_id, guild_id=row.guild_id)
        try:
            await channel.get_partial_message(row.repost_id).delete()
            return
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            if not row.via_webhook:
                log.warning("could not delete repost %s: %s", row.repost_id, exc)
                return
        hook = next((h for h in self._webhooks.values() if h.id == row.webhook_id), None)
        if hook is None:
            log.warning("could not delete webhook repost %s: webhook not cached", row.repost_id)
            return
        in_thread = hook.channel_id != row.channel_id
        thread = discord.Object(row.channel_id) if in_thread else discord.utils.MISSING
        try:
            await hook.delete_message(row.repost_id, thread=thread)
        except discord.HTTPException as exc:
            log.warning("could not delete webhook repost %s: %s", row.repost_id, exc)

    # --- listeners ------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self.accepts(message):
            return
        try:
            await self.process(message)
        except Exception:
            log.exception("link fixing failed for message %s", message.id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Previews arrive as message updates; hand them to whoever waits for that repost."""
        embeds = payload.data.get("embeds")
        if not embeds:
            return
        waiter = self._embed_waiters.get(payload.message_id)
        if waiter is not None:
            if not waiter.done():
                waiter.set_result(list(embeds))
            return
        if self._is_own_message(payload.data):
            # The preview can land before send() returns; keep it for the waiter that comes next.
            self._recent_embeds[payload.message_id] = list(embeds)
            while len(self._recent_embeds) > 256:
                self._recent_embeds.popitem(last=False)

    def _is_own_message(self, data: Any) -> bool:
        me = self.bot.user
        author_id = (data.get("author") or {}).get("id")
        if me is not None and author_id is not None and int(author_id) == me.id:
            return True
        webhook_id = data.get("webhook_id")
        return webhook_id is not None and int(webhook_id) in self.own_webhook_ids()

    # --- fixing ---------------------------------------------------------------------------

    async def _fix_links(
        self, message: discord.Message, settings: GuildSettings, matches: list[LinkMatch], *, edit: bool
    ) -> list[str]:
        """Record, roast and repost each link; returns each repost's final status."""
        db = self.bot.db
        guild = message.guild
        assert guild is not None
        quip_channel = _parent_id(message.channel) or message.channel.id
        live: list[LiveRepost] = []
        already_fixed: list[tuple[LinkMatch, str | None]] = []
        for m in matches:
            if message.id in self._deleted_origins:
                break
            rerun_line = await self._check_rerun(message, settings, m)
            await db.insert_post(
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                author_name=message.author.display_name,
                site=m.site.id,
                canonical_key=m.canonical_key,
            )
            if is_already_fixed(m):
                already_fixed.append((m, rerun_line))
                continue
            repost = await self._repost(message, settings, m, rerun_line, quip_channel=quip_channel)
            if repost is not None:
                live.append(repost)
        outcomes, rescued = await asyncio.gather(
            self._settle_all(live),
            self._rescue_dead_fixer_links(message, settings, already_fixed, quip_channel=quip_channel),
        )
        return outcomes + rescued

    async def _repost(
        self,
        message: discord.Message,
        settings: GuildSettings,
        m: LinkMatch,
        rerun_line: str | None,
        *,
        quip_channel: int,
    ) -> LiveRepost | None:
        quip = choose_quip(
            settings, rerun_line=rerun_line, site_name=m.site.name, channel_id=quip_channel
        )
        return await self._send_repost(message, settings, m, quip, is_rerun=rerun_line is not None)

    async def _settle_all(self, live: list[LiveRepost]) -> list[str]:
        results = await asyncio.gather(*(self._settle(r) for r in live), return_exceptions=True)
        outcomes: list[str] = []
        for repost, result in zip(live, results, strict=True):
            if isinstance(result, BaseException):
                log.error("repost %s failed to settle", repost.row.repost_id, exc_info=result)
                outcomes.append("failed")
            else:
                outcomes.append(result)
        return outcomes

    async def _rescue_dead_fixer_links(
        self,
        message: discord.Message,
        settings: GuildSettings,
        already_fixed: list[tuple[LinkMatch, str | None]],
        *,
        quip_channel: int,
    ) -> list[str]:
        """Links already on a fixer or meme-mirror domain are left alone, unless Discord shows no preview
        on the message at all (a dead meme domain). Then BB fixes them like any other link."""
        if not already_fixed:
            return []
        previewed = _embed_dicts(message) or await self._wait_for_embeds(message.id) or _embed_dicts(message)
        if previewed or message.id in self._deleted_origins:
            return []
        live: list[LiveRepost] = []
        for m, rerun_line in already_fixed:
            log.info("no preview for %s on message %s; fixing it", m.url, message.id)
            repost = await self._repost(message, settings, m, rerun_line, quip_channel=quip_channel)
            if repost is not None:
                live.append(repost)
        return await self._settle_all(live)

    async def _check_rerun(
        self, message: discord.Message, settings: GuildSettings, m: LinkMatch
    ) -> str | None:
        guild = message.guild
        assert guild is not None
        hit = await rerun.find_rerun(
            self.bot.db,
            guild_id=guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            author_id=message.author.id,
            canonical_key=m.canonical_key,
            settings=settings,
            locate=self._locate,
        )
        if hit is None:
            return None
        await rerun.record_rerun(
            self.bot.db,
            guild_id=guild.id,
            offender_id=message.author.id,
            offender_name=message.author.display_name,
            canonical_key=m.canonical_key,
        )
        return rerun.rerun_quip(hit)

    async def _send_repost(
        self,
        message: discord.Message,
        settings: GuildSettings,
        m: LinkMatch,
        quip: str | None,
        *,
        is_rerun: bool,
    ) -> LiveRepost | None:
        guild = message.guild
        assert guild is not None
        fixers = list(sites.chain(m.site.id, self.bot.down_fixers))
        first = fixers[0] if fixers else None
        if first is not None:
            content = render.format_repost(m, first.name, sites.fixed_url(m, first), quip)
        else:
            content = render.format_upload(m, quip)
        view = views.make_repost_view(
            site_id=m.site.id, origin_author_id=message.author.id, original_url=sites.source_url(m)
        )
        try:
            sent = await self._send_message(message, settings, content, view)
        except discord.HTTPException as exc:
            log.warning("could not repost in channel %s: %s", message.channel.id, exc)
            return None
        row = RepostRow(
            repost_id=sent.message.id,
            guild_id=guild.id,
            channel_id=message.channel.id,
            origin_message_id=message.id,
            origin_author_id=message.author.id,
            site=m.site.id,
            canonical_key=m.canonical_key,
            original_url=m.url,
            spoiler=m.spoiler,
            fixer_id=first.id if first is not None else None,
            quip=quip,
            via_webhook=sent.webhook is not None,
            webhook_id=sent.webhook.id if sent.webhook is not None else None,
        )
        await self.bot.db.insert_repost(row)
        repost = LiveRepost(
            row=row, match=m, chain=fixers, sent=sent, is_rerun=is_rerun, max_bytes=guild.filesize_limit
        )
        if message.id in self._deleted_origins:
            await self._drop(repost)
            return None
        return repost

    async def _settle(self, repost: LiveRepost) -> str:
        """Wait for the preview, walk the fixer chain, then upload or give up. Returns the final status."""
        try:
            return await self._walk_chain(repost)
        except discord.NotFound:
            await self.bot.db.delete_repost(repost.row.repost_id)  # someone deleted it mid-fix
            return "gone"

    async def _walk_chain(self, repost: LiveRepost) -> str:
        db = self.bot.db
        row, m = repost.row, repost.match
        embeds = _embed_dicts(repost.sent.message)
        for index, fixer in enumerate(repost.chain):
            if repost.row.origin_message_id in self._deleted_origins:
                await self._drop(repost)
                return "gone"
            if index > 0:
                self._recent_embeds.pop(row.repost_id, None)
                content = render.format_repost(m, fixer.name, sites.fixed_url(m, fixer), row.quip)
                edited = await self._edit(repost, content=content)
                row.fixer_id = fixer.id
                await db.update_repost(row.repost_id, fixer_id=fixer.id)
                embeds = _embed_dicts(edited)
            if not embeds:
                embeds = await self._wait_for_embeds(row.repost_id)
            if embeds:
                row.status = "ok"
                await db.update_repost(row.repost_id, status="ok")
                if embeds_have_image(embeds):
                    await self._add_find_source(repost)
                return "ok"
        if row.origin_message_id in self._deleted_origins:
            await self._drop(repost)
            return "gone"
        return await self._upload_fallback(repost)

    async def _add_find_source(self, repost: LiveRepost) -> None:
        current = await self.bot.db.get_repost(repost.row.repost_id)
        if current is None:
            return
        view = views.make_repost_view(
            site_id=current.site,
            origin_author_id=current.origin_author_id,
            translated=current.translated,
            has_image=True,
            original_url=sites.source_url(repost.match),
        )
        await self._edit(repost, view=view)

    async def _upload_fallback(self, repost: LiveRepost) -> str:
        db = self.bot.db
        row, m = repost.row, repost.match
        with tempfile.TemporaryDirectory(prefix="bb-upload-") as tmp:
            media = await upload.fetch_media(
                m, max_bytes=repost.max_bytes, session=self.bot.http_client, workdir=Path(tmp)
            )
            if media:
                quip = settle_quip(row.quip, is_rerun=repost.is_rerun, outcome="uploaded")
                files = [discord.File(f.path, filename=f.filename, spoiler=m.spoiler) for f in media]
                view = views.make_repost_view(
                    site_id=m.site.id,
                    origin_author_id=row.origin_author_id,
                    translated=False,
                    has_image=any(is_image_file(f.filename) for f in media),
                    original_url=sites.source_url(m),
                )
                try:
                    await self._edit(
                        repost, content=render.format_upload(m, quip), attachments=files, view=view
                    )
                finally:
                    for file in files:
                        file.close()
                row.status, row.quip, row.fixer_id = "uploaded", quip, None
                await db.update_repost(row.repost_id, status="uploaded", quip=quip, fixer_id=None)
                return "uploaded"
        quip = settle_quip(row.quip, is_rerun=repost.is_rerun, outcome="failed")
        await self._edit(repost, content=render.format_upload(m, quip))
        row.status, row.quip, row.fixer_id = "failed", quip, None
        await db.update_repost(row.repost_id, status="failed", quip=quip, fixer_id=None)
        return "failed"

    async def _wait_for_embeds(self, repost_id: int) -> list[dict[str, Any]]:
        early = self._recent_embeds.pop(repost_id, None)
        if early:
            return early
        future: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()
        self._embed_waiters[repost_id] = future
        try:
            return await asyncio.wait_for(future, self.embed_timeout)
        except TimeoutError:
            return []
        finally:
            self._embed_waiters.pop(repost_id, None)

    async def _drop(self, repost: LiveRepost) -> None:
        """The original vanished while BB was working: take the repost down too."""
        await self.delete_repost_message(repost.row)
        await self.bot.db.delete_repost(repost.row.repost_id)

    async def _remove_link(self, origin_id: int, canonical_key: str) -> None:
        db = self.bot.db
        for row in await db.get_reposts_for_origin(origin_id):
            if row.canonical_key == canonical_key:
                await self.delete_repost_message(row)
                await db.delete_repost(row.repost_id)
        await db.delete_posts_for_message(origin_id, canonical_key)

    # --- Discord I/O ----------------------------------------------------------------------

    # --- e-hentai cards -------------------------------------------------------------------

    async def _post_cards(self, message: discord.Message, settings: GuildSettings) -> int:
        """BB's own card for each e-hentai gallery link (blocked tags: nothing at all, not even a record).

        Returns how many cards were posted."""
        galleries = ehentai.find_galleries(message.content)
        if not galleries:
            return 0
        posted = 0
        metas = await ehentai.fetch_metadata(self.bot.http_client, galleries)
        guild = message.guild
        assert guild is not None
        quip_channel = _parent_id(message.channel) or message.channel.id
        kept: list[tuple[ehentai.Gallery, dict[str, Any]]] = []
        for gallery in galleries:
            meta = metas.get(gallery.gid)
            if meta is None:
                continue
            if ehentai.is_blocked(meta):
                log.info("skipped gallery %s on message %s: blocked tag", gallery.gid, message.id)
                continue
            kept.append((gallery, meta))
        covers = await asyncio.gather(
            *(ehentai.fetch_cover(self.bot.http_client, g, guild.filesize_limit) for g, _ in kept)
        )
        for (gallery, meta), cover in zip(kept, covers, strict=True):
            m = gallery.as_match()
            rerun_line = await self._check_rerun(message, settings, m)
            await self.bot.db.insert_post(
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                author_id=message.author.id,
                author_name=message.author.display_name,
                site=ehentai.SITE_ID,
                canonical_key=m.canonical_key,
            )
            quip = choose_quip(
                settings, rerun_line=rerun_line, site_name=ehentai.SITE.name, channel_id=quip_channel
            )
            view = ehentai.build_card(meta, gallery, cover=cover, quip=quip)
            try:
                sent = await self._send_card(message, settings, view, cover)
            except discord.HTTPException:
                log.exception("couldn't post the card for gallery %s", gallery.gid)
                continue
            await self.bot.db.insert_repost(
                RepostRow(
                    repost_id=sent.id,
                    guild_id=guild.id,
                    channel_id=message.channel.id,
                    origin_message_id=message.id,
                    origin_author_id=message.author.id,
                    site=ehentai.SITE_ID,
                    canonical_key=m.canonical_key,
                    original_url=gallery.url,
                    spoiler=gallery.spoiler,
                    quip=quip,
                    status="ok",
                )
            )
            posted += 1
        return posted

    async def _send_card(
        self,
        message: discord.Message,
        settings: GuildSettings,
        view: discord.ui.LayoutView,
        cover: ehentai.Cover | None,
    ) -> discord.Message:
        """Cards always reply as BB (the card is BB's own, so copy-the-poster mode doesn't apply)."""
        mentions = discord.AllowedMentions.none()
        if not settings.silent:
            mentions.replied_user = True
        files = [discord.File(io.BytesIO(cover.data), filename=cover.filename)] if cover else []
        return await message.channel.send(
            view=view,
            files=files,
            reference=message.to_reference(fail_if_not_exists=False),
            allowed_mentions=mentions,
            silent=settings.silent,
        )

    async def _send_message(
        self, message: discord.Message, settings: GuildSettings, content: str, view: discord.ui.View
    ) -> Sent:
        if settings.reply_mode == "webhook":
            sent = await self._send_as_poster(message, settings, content, view)
            if sent is not None:
                return sent
        mentions = discord.AllowedMentions.none()
        if not settings.silent:
            mentions.replied_user = True
        reply = await message.channel.send(
            content,
            reference=message.to_reference(fail_if_not_exists=False),
            view=view,
            allowed_mentions=mentions,
            silent=settings.silent,
        )
        return Sent(message=reply)

    async def _send_as_poster(
        self, message: discord.Message, settings: GuildSettings, content: str, view: discord.ui.View
    ) -> Sent | None:
        """Copy-the-poster mode. None means "use reply mode instead" (no permission, odd channel...)."""
        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        for _ in range(2):
            hook = await self._webhook_for(message.channel)
            if hook is None:
                return None
            try:
                sent = await hook.send(
                    content,
                    username=webhook_username(message.author.display_name),
                    avatar_url=message.author.display_avatar.url,
                    view=view,
                    thread=thread if thread is not None else discord.utils.MISSING,
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=settings.silent,
                    wait=True,
                )
                return Sent(message=sent, webhook=hook, thread=thread)
            except discord.NotFound:
                self._webhooks.pop(hook.channel_id or 0, None)  # deleted by someone; make a new one
            except (discord.HTTPException, ValueError) as exc:
                log.warning("webhook send failed in %s, replying instead: %s", message.channel.id, exc)
                return None
        return None

    async def _webhook_for(self, channel: Any) -> discord.Webhook | None:
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(
            parent, discord.TextChannel | discord.ForumChannel | discord.VoiceChannel | discord.StageChannel
        ):
            return None
        async with self._webhook_lock:
            cached = self._webhooks.get(parent.id)
            if cached is not None:
                return cached
            my_id = self.bot.user.id if self.bot.user is not None else None
            try:
                hooks = await parent.webhooks()
                hook = next((h for h in hooks if h.token and h.user is not None and h.user.id == my_id), None)
                if hook is None:
                    hook = await parent.create_webhook(name=WEBHOOK_NAME, reason="BB copy-the-poster reposts")
            except discord.HTTPException as exc:
                log.warning("no webhook for channel %s: %s", parent.id, exc)
                return None
            self._webhooks[parent.id] = hook
            return hook

    async def _edit(self, repost: LiveRepost, **changes: Any) -> Any:
        """Edit a repost; webhook reposts go through their webhook (with thread= inside threads)."""
        sent = repost.sent
        if sent.webhook is not None:
            edited = await sent.webhook.edit_message(
                repost.row.repost_id,
                thread=sent.thread if sent.thread is not None else discord.utils.MISSING,
                **changes,
            )
        else:
            edited = await sent.message.edit(**changes)
        sent.message = edited
        return edited

    async def _locate(self, post: PostRow) -> str | None:
        """Jump URL for an earlier post if it still exists, else BB's repost of it (delete mode)."""
        channel = self.bot.get_partial_messageable(post.channel_id, guild_id=post.guild_id)
        try:
            await channel.fetch_message(post.message_id)
            return rerun.jump_url(post.guild_id, post.channel_id, post.message_id)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            # Can't check (permissions, outage): assume it's there rather than forget the post.
            return rerun.jump_url(post.guild_id, post.channel_id, post.message_id)
        for row in await self.bot.db.get_reposts_for_origin(post.message_id):
            if row.canonical_key != post.canonical_key:
                continue
            try:
                await self.bot.get_partial_messageable(row.channel_id).fetch_message(row.repost_id)
            except discord.HTTPException:
                continue
            return rerun.jump_url(row.guild_id, row.channel_id, row.repost_id)
        return None

    async def _apply_original(self, message: discord.Message, action: OriginalAction) -> None:
        try:
            if action == "suppress":
                await message.edit(suppress=True)
                # Previews that were still resolving can come back after the first suppress.
                await asyncio.sleep(self.resuppress_delay)
                fresh = await message.channel.fetch_message(message.id)
                if fresh.embeds or not fresh.flags.suppress_embeds:
                    await fresh.edit(suppress=True)
            elif action == "delete":
                self._expected_deletes.add(message.id)
                await message.delete()
        except discord.HTTPException as exc:
            self._expected_deletes.consume(message.id)
            if not isinstance(exc, discord.NotFound):
                log.warning("could not %s original %s: %s", action, message.id, exc)


async def setup(bot: BBBot) -> None:
    await bot.add_cog(LinkFix(bot))
