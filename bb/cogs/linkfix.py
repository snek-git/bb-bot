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
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from dataclasses import replace as copy_row
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import discord
from discord.ext import commands

from bb import ehentai, nhentai, render, rerun, strings, upload, views
from bb.cards import Cover
from bb.db import PostRow, RepostRow
from bb.fixlog import trail
from bb.links import extract, resolve, sites
from bb.links.model import Fixer, LinkMatch
from bb.settings_store import GuildSettings, RareLine
from bb.timeutil import now

if TYPE_CHECKING:
    from bb.bot import BBBot

log = logging.getLogger(__name__)

MAX_LINKS = 4  # fixed links per message
# BB only acts on messages younger than this. Older ones she handled back then still follow real edits;
# an edit only adds reposts to an old message if the edit itself is this recent (see process).
FRESH_FOR = 15 * 60
MAX_SCANNED = 10  # URLs looked at per message (bounds short-link expansion)
EMBED_TIMEOUT = 6.0
# A fixer link the poster sent themselves can preview slowly (fixvx redirects to vxtwitter): give it
# longer before deciding it's dead. 15s is a guess; retractions log the real delays.
RESCUE_TIMEOUT = 15.0
LATE_PREVIEW_WINDOW = 5 * 60  # after a rescue, how long a late preview on the original still undoes it
RESUPPRESS_DELAY = 1.0
WEBHOOK_NAME = "BB"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SETTLED_OK = ("ok", "uploaded")
# Sites no fixer covers, where BB builds her own gallery card. Each module has the same surface:
# SITE_ID, SITE, find_galleries, fetch_metadata, fetch_cover and build_card.
CARD_SOURCES = (ehentai, nhentai)
CARD_SITE_IDS = frozenset(source.SITE_ID for source in CARD_SOURCES)

OriginalAction = Literal["suppress", "delete", "nothing"]


# --- pure decisions --------------------------------------------------------------------------


def is_card_key(canonical_key: str) -> bool:
    return canonical_key.partition(":")[0] in CARD_SITE_IDS


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


def is_history(created_at: float, now_ts: float) -> bool:
    return now_ts - created_at > FRESH_FOR


def edited_recently(message: Any, now_ts: float) -> bool:
    """The message carries an edit from the last FRESH_FOR seconds (pins and refreshed previews keep the
    old edited_at, or none)."""
    edited = getattr(message, "edited_at", None)
    return edited is not None and now_ts - edited.timestamp() <= FRESH_FOR


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
    if settings.rare_lines and settings.rare_rate > 0 and rng.randrange(settings.rare_rate) == 0:
        return rare_line(rng.choice(settings.rare_lines))
    if settings.quip_rate <= 0 or rng.randrange(settings.quip_rate) != 0:
        return None
    flavor = settings.channel_flavor(channel_id) if channel_id is not None else None
    if flavor:
        return strings.line(f"quip.{flavor}", site=site_name)
    return strings.line("quip.fix", site=site_name)


def rare_line(line: RareLine) -> str:
    """A custom rare line as BB posts it. With a target, {target} becomes the ping (put first if the text
    has no {target}); without one it's just the text."""
    text = line.text.strip()
    if not line.target_id:
        return text.replace("{target}", "").strip()
    ping = f"<@{line.target_id}>"
    return text.replace("{target}", ping) if "{target}" in text else f"{ping} {text}"


def pinged_targets(settings: GuildSettings, text: str | None) -> list[int]:
    """Rare-line targets whose ping is in the text. Only BB writes mentions into her messages (names are
    escaped, URLs percent-encoded), so a match means a rare line put it there."""
    if not text:
        return []
    targets = {line.target_id for line in settings.rare_lines if line.target_id}
    return sorted(t for t in targets if f"<@{t}>" in text)


def pings_target(settings: GuildSettings, text: str | None) -> bool:
    return bool(pinged_targets(settings, text))


def send_mentions(settings: GuildSettings, text: str | None, *, reply: bool) -> discord.AllowedMentions:
    """Nobody gets pinged by BB, except a rare line's target (and the poster in ping mode)."""
    mentions = discord.AllowedMentions.none()
    if reply and not settings.silent:
        mentions.replied_user = True
    targets = pinged_targets(settings, text)
    if targets:
        mentions.users = [discord.Object(t) for t in targets]
    return mentions


def settle_quip(current: str | None, *, is_rerun: bool, outcome: Literal["uploaded", "failed"]) -> str:
    """Quip after the upload fallback: the rerun callout keeps its place, otherwise the outcome line."""
    if is_rerun and current:
        return current
    return strings.line(f"quip.{outcome}")


# Sites where the Find source button is worth having: art there is mostly reposted without credit. On
# tweets and Pixiv the post usually *is* the source. (The right-click "Find source" works everywhere.)
FIND_SOURCE_SITES = frozenset({"reddit", "bluesky", "instagram"})

# Twitter's language codes for tweets with no real text (media only, emoji, hashtags, ...).
NO_TEXT_LANGS = frozenset({"zxx", "und", "qme", "qam", "qct", "qht", "qst"})


def mixed_media(tweet: dict[str, Any] | None) -> tuple[str, ...]:
    """The tweet's media kinds when it mixes photos with videos or GIFs, else (). Those need one preview
    per item: Discord folds same-URL previews into an image gallery, where the video can't play."""
    if not tweet:
        return ()
    items = (tweet.get("media") or {}).get("all") or []
    kinds = tuple(str(item.get("type")) for item in items if isinstance(item, dict))
    if "photo" in kinds and any(kind in ("video", "gif") for kind in kinds):
        return kinds[:4]
    return ()


def translate_offered(tweet: dict[str, Any] | None, target: str) -> bool:
    """Translate for tweets in another language; if FxTwitter didn't answer, offer it anyway."""
    return True if tweet is None else offer_translation(tweet.get("lang"), target, tweet.get("text"))


# Twitter's language guess is unreliable on a few Latin-script words ("gem alert" came back as German),
# so for Latin-script text it's only trusted once there's a real sentence to judge.
MIN_LATIN_LETTERS = 20


def offer_translation(lang: str | None, target: str, text: str | None = None) -> bool:
    """Translate is only offered for text in another language than the server's translation language."""
    if not lang or lang.lower() in NO_TEXT_LANGS:
        return False
    if lang.lower().split("-")[0] == target.lower().split("-")[0]:
        return False
    if text is None:
        return True
    letters = [ch for ch in text if ch.isalpha()]
    non_latin = sum(1 for ch in letters if ord(ch) >= 0x250)  # past Latin + Latin Extended
    if non_latin:
        return True  # Japanese, Korean, Cyrillic, ...: a few characters are unambiguous
    return len(letters) >= MIN_LATIN_LETTERS


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
    # What a replacement message needs (see _replace): the original it answers, its settings, its buttons.
    origin: Any = None
    settings: GuildSettings | None = None
    view: Any = None
    media: tuple[str, ...] = ()  # a mixed tweet's media kinds: one preview per item


# Attachments are rebuilt for every send attempt: discord.py closes a File once a send ends, even a
# failed one, so the same File objects can't go out twice.
MakeFiles = Callable[[], list[discord.File]]


class RepostGone(Exception):
    """The repost, or the message it answers, was deleted while BB was still working on it."""


def _changes(content: str, view: Any, make_files: MakeFiles | None) -> dict[str, Any]:
    changes: dict[str, Any] = {"content": content}
    if view is not None:
        changes["view"] = view
    if make_files is not None:
        changes["attachments"] = make_files()
    return changes


def _embed_dicts(message: Any) -> list[dict[str, Any]]:
    return [embed.to_dict() for embed in getattr(message, "embeds", None) or []]


def describe(embeds: Iterable[dict[str, Any]]) -> str:
    """What a preview shows, for the logs: "'pwt (@Feli_NFN)' with image" or "... text only"."""
    parts = []
    for embed in embeds:
        title = embed.get("title") or (embed.get("author") or {}).get("name") or embed.get("url") or "?"
        media = [kind for kind in ("video", "image", "thumbnail") if embed.get(kind)]
        parts.append(f"{title[:60]!r} with {media[0]}" if media else f"{title[:60]!r} text only")
    return "; ".join(parts) or "nothing"


def _parent_id(channel: Any) -> int | None:
    return channel.parent_id if isinstance(channel, discord.Thread) else None


def fixer_preview_keys(embeds: Iterable[dict[str, Any]]) -> set[str]:
    """Which links a batch of previews came from a fixer for. Matched by canonical key, not host
    (fixvx.com previews arrive with vxtwitter.com URLs). A site's own preview (x.com's poor card)
    doesn't count: it's no substitute for BB's fix."""
    keys: set[str] = set()
    for embed in embeds:
        url = embed.get("url")
        m = sites.match(url) if url else None
        if m is not None and is_already_fixed(m):
            keys.add(m.canonical_key)
    return keys


@dataclass
class RescueWatch:
    """A rescued message whose own preview may still show up late."""

    keys: set[str]  # rescued links still being watched
    posted_at: float
    deadline: float
    seen: set[str]  # rescued links whose late preview has arrived
    # link key -> the one repost BB may take down for it. Only silent rescues: a repost roast or the
    # rare line is something BB said, and stays up.
    reposts: dict[str, int] = field(default_factory=dict)
    settled: bool = False  # BB's reposts are done settling; safe to delete them now


class LinkFix(commands.Cog):
    def __init__(self, bot: BBBot) -> None:
        self.bot = bot
        self.embed_timeout = EMBED_TIMEOUT
        self.rescue_timeout = RESCUE_TIMEOUT
        self._rare_lock = asyncio.Lock()
        self.resuppress_delay = RESUPPRESS_DELAY
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self._embed_waiters: dict[int, asyncio.Future[list[dict[str, Any]]]] = {}
        self._recent_embeds: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()
        self._rescue_watches: OrderedDict[int, RescueWatch] = OrderedDict()  # by original message id
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
        # Messages without a link are most of chat: only ones with a link get a trail.
        has_link = bool(extract.extract_urls(message.content))
        async with self.message_lock(message.id):
            settings = await self.bot.settings.get(guild.id)
            if settings.paused:
                if has_link:
                    trail(message, "BB is paused in this server, not fixing")
                return
            if not should_handle(
                settings,
                channel_id=message.channel.id,
                parent_id=_parent_id(message.channel),
                is_webhook=message.webhook_id is not None,
            ):
                if has_link:
                    trail(message, "BB is off in channel %s (or it's a webhook post), not fixing",
                          message.channel.id)
                return
            db = self.bot.db
            old_keys = await db.post_keys_for_message(message.id)
            if old_keys and not edit:
                return  # already handled (duplicate event)
            if not old_keys and is_history(message.created_at.timestamp(), now()):
                # Never touch old messages BB didn't handle when they were new, whatever the event says
                # (edit events for scrolled-to history, replays after a reconnect, ...).
                if has_link and not edit:
                    trail(message, "too old to fix (BB only fixes new messages)")
                return
            cards = 0
            if is_skipped(message.content, settings.skip_word):
                if has_link:
                    trail(message, "has the skip word %r, not fixing", settings.skip_word)
                if not edit:
                    return
                matches: list[LinkMatch] = []
            else:
                matches = await collect_matches(message.content, settings, self.bot.http_session)
                if has_link and not matches and not edit:
                    trail(message, "no supported link in it")
                if not edit:
                    cards = await self._post_cards(message, settings)
            # Cards aren't edit-synced: an edit that doesn't touch the gallery link shouldn't drop its card.
            link_keys = {key for key in old_keys if not is_card_key(key)}
            removed, added = diff_keys(link_keys, matches)
            ts = now()
            if added and edit and is_history(message.created_at.timestamp(), ts) and not edited_recently(
                message, ts
            ):
                # An event on an old message that isn't a fresh edit (a misread pin, a refresh) can take a
                # link away, but never brings new reposts into old history.
                trail(message, "edit on an old message: not fixing %d new link(s)", len(added))
                added = []
            for key in removed:
                trail(message, "edited: %s is gone from it, removing its repost", key)
                await self._remove_link(message.id, key)
            if added:
                trail(
                    message, "%s %s from %s (%s) in channel %s",
                    "edited in" if edit else "found", ", ".join(m.url for m in added),
                    message.author.display_name, message.author.id, message.channel.id,
                )
            if not added:
                if cards and message.id not in self._deleted_origins:
                    # Only cards: the original's own (bare) gallery preview goes, like a fixed link's would.
                    action = original_action(
                        settings.original_action,
                        ["ok"] * cards,
                        has_already_fixed=any(is_already_fixed(m) for m in matches),
                    )
                    if action != "nothing":
                        await self._apply_original(message, action)
                return
            outcomes = await self._fix_links(message, settings, added, edit=edit)
            if message.id in self._deleted_origins:
                trail(message, "the original was deleted, done")
                return
            if not outcomes:
                trail(message, "no repost made, the original keeps its own preview")
                return
            statuses = [row.status for row in await db.get_reposts_for_origin(message.id)]
            statuses += [o for o in outcomes if o not in SETTLED_OK]
            has_already_fixed = any(is_already_fixed(m) for m in matches)
            action = original_action(settings.original_action, statuses, has_already_fixed=has_already_fixed)
            if action == "nothing":
                why = (
                    "the server setting says so" if settings.original_action == "nothing"
                    else "the poster used a fixer link themselves" if has_already_fixed
                    else f"not every repost worked ({', '.join(statuses) or 'none left'})"
                )
                trail(message, "leaving the original as it is: %s", why)
            else:
                await self._apply_original(message, action)

    async def delete_repost_message(self, row: RepostRow) -> bool:
        """Delete one of BB's reposts from Discord (the DB row is the caller's business).
        True when it's gone (or already was)."""
        channel = self.bot.get_partial_messageable(row.channel_id, guild_id=row.guild_id)
        try:
            await channel.get_partial_message(row.repost_id).delete()
            return True
        except discord.NotFound:
            return True
        except discord.HTTPException as exc:
            if not row.via_webhook:
                trail(row.origin_message_id, "could not delete repost %s: %s", row.repost_id, exc,
                      level=logging.WARNING)
                return False
        hook = next((h for h in self._webhooks.values() if h.id == row.webhook_id), None)
        if hook is None:
            trail(row.origin_message_id, "could not delete webhook repost %s: webhook not cached",
                  row.repost_id, level=logging.WARNING)
            return False
        in_thread = hook.channel_id != row.channel_id
        thread = discord.Object(row.channel_id) if in_thread else discord.utils.MISSING
        try:
            await hook.delete_message(row.repost_id, thread=thread)
        except discord.NotFound:
            return True
        except discord.HTTPException as exc:
            trail(row.origin_message_id, "could not delete webhook repost %s: %s", row.repost_id, exc,
                  level=logging.WARNING)
            return False
        return True

    # --- listeners ------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self.accepts(message):
            return
        try:
            await self.process(message)
        except Exception as exc:
            trail(message, "crashed, BB stopped here", level=logging.ERROR, exc_info=exc)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Previews arrive as message updates; hand them to whoever waits for that repost."""
        embeds = payload.data.get("embeds")
        if not embeds:
            return
        waiter = self._embed_waiters.get(payload.message_id)
        watch = self._rescue_watches.get(payload.message_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(list(embeds))
        if watch is not None:  # also while waiting: an edit can start a second rescue on the same message
            try:
                await self._note_late_preview(payload.message_id, watch, embeds)
            except Exception:
                trail(payload.message_id, "undoing a rescue crashed", level=logging.ERROR, exc_info=True)
        if waiter is not None or watch is not None:
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
            if rerun_line:
                trail(message, "%s was posted before: roasting it", m.canonical_key)
            if is_already_fixed(m):
                trail(message, "%s is already a fixer link: BB only steps in if it shows no preview", m.url)
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
        quip = await self._choose_quip(
            message, settings, rerun_line=rerun_line, site_name=m.site.name, channel_id=quip_channel
        )
        return await self._send_repost(message, settings, m, quip, is_rerun=rerun_line is not None)

    async def _choose_quip(
        self,
        message: discord.Message,
        settings: GuildSettings,
        *,
        rerun_line: str | None,
        site_name: str,
        channel_id: int,
    ) -> str | None:
        """choose_quip, plus the armed rare line: used once on the next link (not over a repost roast)."""
        guild = message.guild
        if guild is not None and not rerun_line:
            async with self._rare_lock:  # two links at once must not both use the one-shot line
                current = await self.bot.settings.get(guild.id)  # fresh: an earlier link may have used it
                armed = current.rare_armed
                if armed is not None:
                    await self.bot.settings.update(guild.id, rare_armed=None)
                    if 0 <= armed < len(current.rare_lines):
                        return rare_line(current.rare_lines[armed])
        return choose_quip(settings, rerun_line=rerun_line, site_name=site_name, channel_id=channel_id)

    async def _settle_all(self, live: list[LiveRepost]) -> list[str]:
        results = await asyncio.gather(*(self._settle(r) for r in live), return_exceptions=True)
        outcomes: list[str] = []
        for repost, result in zip(live, results, strict=True):
            if isinstance(result, BaseException):
                trail(repost.origin or repost.row.origin_message_id, "repost %s crashed while settling",
                      repost.row.repost_id, level=logging.ERROR, exc_info=result)
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
        previewed = (
            _embed_dicts(message)
            or await self._wait_for_embeds(message.id, seconds=self.rescue_timeout)
            or _embed_dicts(message)
        )
        if message.id in self._deleted_origins:
            return []
        if previewed:
            trail(message, "the fixer link has its own preview, leaving it")
            return []
        watch = self._watch_rescue(message, {m.canonical_key for m, _ in already_fixed})
        try:
            live: list[LiveRepost] = []
            silent: list[tuple[str, LiveRepost]] = []
            for m, rerun_line in already_fixed:
                trail(message, "no preview for %s after %.0fs, fixing it", m.url, self.rescue_timeout)
                repost = await self._repost(message, settings, m, rerun_line, quip_channel=quip_channel)
                if repost is not None:
                    live.append(repost)
                    if repost.row.quip is None:
                        silent.append((m.canonical_key, repost))
            outcomes = await self._settle_all(live)
            for key, repost in silent:  # after settling: a fixer switch replaces the message (new id)
                watch.reposts[key] = repost.row.repost_id
        finally:
            watch.settled = True
        try:
            await self._undo_rescues(message.id)  # a late preview that landed while BB was still settling
        except Exception:
            trail(message, "undoing a rescue crashed", level=logging.ERROR, exc_info=True)
        return outcomes

    def _watch_rescue(self, message: discord.Message, keys: set[str]) -> RescueWatch:
        watch = self._rescue_watches.get(message.id)
        if watch is not None and now() <= watch.deadline:
            # an edit rescued more links on the same message: keep watching the first ones too
            watch.keys |= keys
            watch.deadline = now() + LATE_PREVIEW_WINDOW
            watch.settled = False
            return watch
        watch = RescueWatch(
            keys=set(keys), posted_at=message.created_at.timestamp(),
            deadline=now() + LATE_PREVIEW_WINDOW, seen=set(),
        )
        self._rescue_watches[message.id] = watch
        while len(self._rescue_watches) > 256:
            self._rescue_watches.popitem(last=False)
        return watch

    async def _note_late_preview(
        self, origin_id: int, watch: RescueWatch, embeds: list[dict[str, Any]]
    ) -> None:
        if now() > watch.deadline:
            self._rescue_watches.pop(origin_id, None)
            return
        watch.seen |= fixer_preview_keys(embeds) & watch.keys
        if watch.settled and watch.seen:
            async with self.message_lock(origin_id):
                await self._undo_rescues(origin_id)

    async def _undo_rescues(self, origin_id: int) -> None:
        """The original's own preview showed up after all, so BB's rescue repost is a duplicate: take it
        down. Only the exact repost the rescue made, never another one for the same link. The post
        record stays, so repost roasts still know about the link."""
        watch = self._rescue_watches.get(origin_id)
        if watch is None or not watch.seen:
            return
        db = self.bot.db
        done: set[str] = set()
        for key in sorted(watch.seen):
            repost_id = watch.reposts.get(key)
            row = await db.get_repost(repost_id) if repost_id is not None else None
            if row is not None:  # None: it said something (kept on purpose), or it's already gone
                trail(
                    origin_id, "late preview for %s, %.0fs after it was posted: taking repost %s down",
                    row.original_url, now() - watch.posted_at, row.repost_id,
                )
                if not await self.delete_repost_message(row):
                    continue  # still up: keep watching, the next preview update retries
                await db.delete_repost(row.repost_id)
            watch.reposts.pop(key, None)
            done.add(key)
        watch.keys -= done
        watch.seen.clear()
        if not watch.keys:
            self._rescue_watches.pop(origin_id, None)

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
        tweet = await self._tweet_info(m) if m.site.id == "twitter" else None
        fixers = list(sites.chain(m.site.id, self.bot.down_fixers))
        first = fixers[0] if fixers else None
        media = mixed_media(tweet)
        if first is not None:
            content = render.repost_text(m, first, quip, media=media)
        else:
            content = render.format_upload(m, quip)
        view = views.make_repost_view(
            site_id=m.site.id,
            origin_author_id=message.author.id,
            has_image=await self._predict_image(m),
            translatable=m.site.id == "twitter" and translate_offered(tweet, settings.translate_lang),
        )
        try:
            sent = await self._send_message(message, settings, content, view)
        except discord.HTTPException as exc:
            trail(message, "could not send the repost of %s: %s", m.url, exc, level=logging.WARNING)
            return None
        trail(
            message, "sent repost %s for %s via %s%s", sent.message.id, m.canonical_key,
            first.name if first is not None else "upload", " (as the poster)" if sent.webhook else "",
        )
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
            media=",".join(media),
        )
        await self.bot.db.insert_repost(row)
        repost = LiveRepost(
            row=row, match=m, chain=fixers, sent=sent, is_rerun=is_rerun, max_bytes=guild.filesize_limit,
            origin=message, settings=settings, view=view, media=media,
        )
        if message.id in self._deleted_origins:
            await self._drop(repost)
            return None
        return repost

    async def _settle(self, repost: LiveRepost) -> str:
        """Wait for the preview, walk the fixer chain, then upload or give up. Returns the final status."""
        try:
            return await self._walk_chain(repost)
        except (discord.NotFound, RepostGone):
            trail(repost.origin or repost.row.origin_message_id,
                  "repost %s was deleted by someone while BB was still fixing it", repost.row.repost_id)
            await self.bot.db.delete_repost(repost.row.repost_id)
            return "gone"

    async def _walk_chain(self, repost: LiveRepost) -> str:
        db = self.bot.db
        row, m = repost.row, repost.match
        origin = repost.origin or row.origin_message_id
        embeds = _embed_dicts(repost.sent.message)
        for index, fixer in enumerate(repost.chain):
            if repost.row.origin_message_id in self._deleted_origins:
                await self._drop(repost)
                return "gone"
            if index > 0:
                self._recent_embeds.pop(row.repost_id, None)
                content = render.repost_text(m, fixer, row.quip, media=repost.media)
                row.fixer_id = fixer.id
                edited = await self._replace(repost, content=content)
                await db.update_repost(row.repost_id, fixer_id=fixer.id)
                embeds = _embed_dicts(edited)
            if not embeds:
                embeds = await self._wait_for_embeds(row.repost_id)
            if embeds and row.origin_message_id in self._deleted_origins:
                await self._drop(repost)
                return "gone"
            if embeds:
                trail(origin, "%s showed a preview for %s: %s", fixer.name, m.canonical_key, describe(embeds))
                row.status = "ok"
                await db.update_repost(row.repost_id, status="ok")
                return "ok"
            following = repost.chain[index + 1].name if index + 1 < len(repost.chain) else "uploading it"
            trail(origin, "no preview from %s for %s within %.0fs, next: %s",
                  fixer.name, m.canonical_key, self.embed_timeout, following)
        if row.origin_message_id in self._deleted_origins:
            await self._drop(repost)
            return "gone"
        return await self._upload_fallback(repost)

    async def _tweet_info(self, m: LinkMatch) -> dict[str, Any] | None:
        """The tweet as FxTwitter's API describes it (for its language), fetched before sending so nothing
        needs an edit later. None if it doesn't answer in time."""
        if "id" not in m.parts:
            return None
        url = f"https://api.fxtwitter.com/{m.parts.get('user') or 'i'}/status/{m.parts['id']}"
        try:
            async with self.bot.http_client.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json(content_type=None)).get("tweet") or None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    async def _predict_image(self, m: LinkMatch) -> bool:
        """Whether this repost gets a Find source button. Decided before sending, so the button never has to
        be added by an edit (Discord marks any edit "(edited)")."""
        return m.site.id in FIND_SOURCE_SITES

    async def _upload_fallback(self, repost: LiveRepost) -> str:
        db = self.bot.db
        row, m = repost.row, repost.match
        with tempfile.TemporaryDirectory(prefix="bb-upload-") as tmp:
            media = await upload.fetch_media(
                m, max_bytes=repost.max_bytes, session=self.bot.http_client, workdir=Path(tmp)
            )
            if media:
                quip = settle_quip(row.quip, is_rerun=repost.is_rerun, outcome="uploaded")

                def make_files() -> list[discord.File]:
                    return [discord.File(f.path, filename=f.filename, spoiler=m.spoiler) for f in media]

                view = views.make_repost_view(
                    site_id=m.site.id,
                    origin_author_id=row.origin_author_id,
                    translated=False,
                    has_image=m.site.id in FIND_SOURCE_SITES
                    and any(is_image_file(f.filename) for f in media),
                    translatable=False,  # an upload has no fixer link to translate
                )
                await self._replace(
                    repost, content=render.format_upload(m, quip), make_files=make_files, view=view
                )
                trail(repost.origin or row.origin_message_id, "uploaded %d file(s) for %s itself",
                      len(media), m.canonical_key)
                row.status, row.quip, row.fixer_id = "uploaded", quip, None
                await db.update_repost(row.repost_id, status="uploaded", quip=quip, fixer_id=None)
                return "uploaded"
        trail(repost.origin or row.origin_message_id,
              "every fixer and the upload failed for %s: posting the failure line", m.canonical_key)
        quip = settle_quip(row.quip, is_rerun=repost.is_rerun, outcome="failed")
        await self._replace(repost, content=render.format_upload(m, quip))
        row.status, row.quip, row.fixer_id = "failed", quip, None
        await db.update_repost(row.repost_id, status="failed", quip=quip, fixer_id=None)
        return "failed"

    async def _wait_for_embeds(self, repost_id: int, *, seconds: float | None = None) -> list[dict[str, Any]]:
        early = self._recent_embeds.pop(repost_id, None)
        if early:
            return early
        future: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()
        self._embed_waiters[repost_id] = future
        try:
            return await asyncio.wait_for(future, self.embed_timeout if seconds is None else seconds)
        except TimeoutError:
            return []
        finally:
            self._embed_waiters.pop(repost_id, None)

    async def _drop(self, repost: LiveRepost) -> None:
        """The original vanished while BB was working: take the repost down too."""
        trail(repost.row.origin_message_id, "the original was deleted mid-fix: taking repost %s down",
              repost.row.repost_id)
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

    # --- gallery cards (e-hentai, nhentai) ------------------------------------------------

    async def _post_cards(self, message: discord.Message, settings: GuildSettings) -> int:
        """BB's own card for each gallery link on a card site that's turned on. Returns how many cards
        were posted."""
        posted = 0
        for source in CARD_SOURCES:
            if settings.site_enabled(source.SITE_ID):
                posted += await self._post_source_cards(message, settings, source)
        return posted

    async def _post_source_cards(self, message: discord.Message, settings: GuildSettings, source: Any) -> int:
        galleries = source.find_galleries(message.content)
        if not galleries:
            return 0
        posted = 0
        metas = await source.fetch_metadata(self.bot.http_client, galleries)
        guild = message.guild
        assert guild is not None
        quip_channel = _parent_id(message.channel) or message.channel.id
        kept: list[tuple[Any, dict[str, Any]]] = [
            (gallery, metas[gallery.gid]) for gallery in galleries if gallery.gid in metas
        ]
        covers = await asyncio.gather(
            *(source.fetch_cover(self.bot.http_client, g, meta, guild.filesize_limit) for g, meta in kept)
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
                site=source.SITE_ID,
                canonical_key=m.canonical_key,
            )
            quip = await self._choose_quip(
                message, settings, rerun_line=rerun_line, site_name=source.SITE.name, channel_id=quip_channel
            )
            view = source.build_card(meta, gallery, cover=cover, quip=quip)
            try:
                sent = await self._send_card(message, settings, view, cover, quip=quip)
            except discord.HTTPException:
                log.exception("couldn't post the card for %s gallery %s", source.SITE_ID, gallery.gid)
                continue
            await self.bot.db.insert_repost(
                RepostRow(
                    repost_id=sent.id,
                    guild_id=guild.id,
                    channel_id=message.channel.id,
                    origin_message_id=message.id,
                    origin_author_id=message.author.id,
                    site=source.SITE_ID,
                    canonical_key=m.canonical_key,
                    original_url=gallery.link,
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
        cover: Cover | None,
        *,
        quip: str | None = None,
    ) -> discord.Message:
        """Cards always reply as BB (the card is BB's own, so copy-the-poster mode doesn't apply)."""
        files = [discord.File(io.BytesIO(cover.data), filename=cover.filename)] if cover else []
        return await message.channel.send(
            view=view,
            files=files,
            reference=message.to_reference(fail_if_not_exists=False),
            allowed_mentions=send_mentions(settings, quip, reply=True),
            silent=settings.silent and not pings_target(settings, quip),
        )

    async def _send_message(
        self,
        message: discord.Message,
        settings: GuildSettings,
        content: str,
        view: discord.ui.View,
        *,
        make_files: MakeFiles | None = None,
        quiet: bool = False,
    ) -> Sent:
        """quiet: ping nobody at all (a replacement for a message that already did)."""
        if settings.reply_mode == "webhook":
            sent = await self._send_as_poster(
                message, settings, content, view, make_files=make_files, quiet=quiet
            )
            if sent is not None:
                return sent
        extra: dict[str, Any] = {"files": make_files()} if make_files is not None else {}
        reply = await message.channel.send(
            content,
            reference=message.to_reference(fail_if_not_exists=False),
            view=view,
            allowed_mentions=(
                discord.AllowedMentions.none() if quiet else send_mentions(settings, content, reply=True)
            ),
            silent=quiet or (settings.silent and not pings_target(settings, content)),
            **extra,
        )
        return Sent(message=reply)

    async def _send_as_poster(
        self,
        message: discord.Message,
        settings: GuildSettings,
        content: str,
        view: discord.ui.View,
        *,
        make_files: MakeFiles | None = None,
        quiet: bool = False,
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
                    allowed_mentions=(
                        discord.AllowedMentions.none()
                        if quiet
                        else send_mentions(settings, content, reply=False)
                    ),
                    silent=quiet or (settings.silent and not pings_target(settings, content)),
                    files=make_files() if make_files is not None else discord.utils.MISSING,
                    wait=True,
                )
                return Sent(message=sent, webhook=hook, thread=thread)
            except discord.NotFound:
                self._webhooks.pop(hook.channel_id or 0, None)  # deleted by someone; make a new one
            except (discord.HTTPException, ValueError) as exc:
                trail(message, "posting as the poster failed, replying instead: %s", exc,
                      level=logging.WARNING)
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

    async def _replace(
        self,
        repost: LiveRepost,
        *,
        content: str,
        view: Any = None,
        make_files: MakeFiles | None = None,
    ) -> Any:
        """Swap a repost for a fresh message instead of editing it: Discord marks every edit "(edited)".
        The new one goes out first, so the channel never shows a gap, and pings nobody (the first one
        already did). If BB can't send it, she edits after all: a label beats a stuck repost.
        Raises RepostGone if the repost or its original was deleted meanwhile; nothing comes back."""
        row = repost.row
        db = self.bot.db
        if await db.get_repost(row.repost_id) is None:
            raise RepostGone  # someone deleted it: don't bring it back
        if repost.origin is None or repost.settings is None:
            return await self._edit(repost, **_changes(content, view, make_files))
        old = copy_row(row)  # the message to delete afterwards
        try:
            sent = await self._send_message(
                repost.origin, repost.settings, content, view or repost.view,
                make_files=make_files, quiet=True,
            )
        except discord.HTTPException as exc:
            trail(repost.origin, "could not send a new repost to replace %s, editing it instead: %s",
                  row.repost_id, exc, level=logging.WARNING)
            return await self._edit(repost, **_changes(content, view, make_files))
        trail(repost.origin, "replaced repost %s with %s", row.repost_id, sent.message.id)
        row.repost_id = sent.message.id
        row.via_webhook = sent.webhook is not None
        row.webhook_id = sent.webhook.id if sent.webhook is not None else None
        repost.sent = sent
        if view is not None:
            repost.view = view
        await db.insert_repost(row)
        if row.origin_message_id in self._deleted_origins or await db.get_repost(old.repost_id) is None:
            # The original, or the old repost, was deleted while the new one was on its way: it goes too.
            trail(repost.origin, "the original or repost %s was deleted meanwhile: taking %s down too",
                  old.repost_id, row.repost_id)
            await self.delete_repost_message(row)
            await db.delete_repost(row.repost_id)
            raise RepostGone
        if await self.delete_repost_message(old):
            await db.delete_repost(old.repost_id)
        else:  # still up: keep tracking it, so edits and deletes of the original still reach it
            trail(repost.origin, "replaced repost %s, but the old message %s is still up",
                  row.repost_id, old.repost_id, level=logging.WARNING)
        return sent.message

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
                trail(message, "every repost worked: hid the original's preview")
                # Previews that were still resolving can come back after the first suppress.
                await asyncio.sleep(self.resuppress_delay)
                fresh = await message.channel.fetch_message(message.id)
                if fresh.embeds or not fresh.flags.suppress_embeds:
                    trail(message, "the original's preview came back: hid it again")
                    await fresh.edit(suppress=True)
            elif action == "delete":
                self._expected_deletes.add(message.id)
                await message.delete()
                trail(message, "every repost worked: deleted the original (server setting)")
        except discord.HTTPException as exc:
            self._expected_deletes.consume(message.id)
            if not isinstance(exc, discord.NotFound):
                trail(message, "could not %s the original: %s", action, exc, level=logging.WARNING)


async def setup(bot: BBBot) -> None:
    await bot.add_cog(LinkFix(bot))
