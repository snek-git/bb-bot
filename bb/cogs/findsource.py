"""Find source: SauceNAO (when a key is set) plus link buttons to other reverse image search engines.

Reached two ways: the Find source button on reposts (bb:src, via the buttons cog) and the "Find source"
entry in the right-click Apps menu on any message with an image.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bb import render, strings
from bb.cooldown import Cooldown

log = logging.getLogger(__name__)

SAUCENAO_URL = "https://saucenao.com/search.php"
MIN_SIMILARITY = 60.0
MAX_RESULTS = 3
REQUEST_RESULTS = 8  # ask for a few extra: low-similarity and link-less results get dropped
CACHE_SIZE = 128
BUTTON_URL_LIMIT = 512  # Discord rejects link buttons with longer URLs

# (label, URL template); {} is the percent-encoded image URL.
ENGINES: tuple[tuple[str, str], ...] = (
    ("SauceNAO", "https://saucenao.com/search.php?url={}"),
    ("ascii2d", "https://ascii2d.net/search/url/{}"),
    ("IQDB", "https://iqdb.org/?url={}"),
    ("Yandex", "https://yandex.com/images/search?rpt=imageview&url={}"),
    ("Google Lens", "https://lens.google.com/uploadbyurl?url={}"),
)

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")


@dataclass(frozen=True)
class SauceResult:
    similarity: float
    source: str
    url: str
    characters: tuple[str, ...] = ()  # from the art boards (Danbooru, Gelbooru, ...)
    artist: str | None = None
    original: str | None = None  # where the artist posted it, when the art board knows


# --- pure helpers -----------------------------------------------------------------------


def engine_links(image_url: str) -> list[tuple[str, str]]:
    """(label, url) for each search engine, skipping any whose URL would be too long for a button."""
    encoded = quote(image_url, safe="")
    links = [(label, template.format(encoded)) for label, template in ENGINES]
    return [(label, url) for label, url in links if len(url) <= BUTTON_URL_LIMIT]


def _is_image_attachment(attachment: Any) -> bool:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    if content_type:
        return content_type.startswith("image/")
    filename = (getattr(attachment, "filename", None) or "").lower()
    return filename.endswith(_IMAGE_EXTENSIONS)


def _gallery_images(components: Iterable[Any]) -> Iterable[str]:
    """Image URLs in media galleries (BB's own cards), walking into containers."""
    for component in components:
        for item in getattr(component, "items", None) or ():
            media = getattr(item, "media", None)
            if (getattr(media, "content_type", None) or "").startswith("image/"):
                yield media.url
        yield from _gallery_images(getattr(component, "children", None) or ())


def pick_image_url(
    attachments: Iterable[Any], embeds: Iterable[Any], components: Iterable[Any] = ()
) -> str | None:
    """The first image on a message: image attachments, card galleries, then embed images and thumbnails."""
    for attachment in attachments:
        if _is_image_attachment(attachment):
            return attachment.url
    for url in _gallery_images(components):
        return url
    embeds = list(embeds)
    for embed in embeds:
        url = getattr(getattr(embed, "image", None), "url", None)
        if url:
            return url
    for embed in embeds:
        url = getattr(getattr(embed, "thumbnail", None), "url", None)
        if url:
            return url
    return None


def _source_name(index_name: str, url: str) -> str:
    """'Index #5: Pixiv Images - 1234_p0.jpg' -> 'Pixiv Images'; falls back to the link's host."""
    name = index_name.partition(": ")[2].partition(" - ")[0].strip() if ": " in index_name else ""
    if name:
        return name
    host = urlparse(url).hostname or "link"
    return host.removeprefix("www.")


def parse_saucenao(
    data: Any, min_similarity: float = MIN_SIMILARITY, limit: int = MAX_RESULTS
) -> list[SauceResult]:
    """Results from a SauceNAO output_type=2 response: similar enough, linked, deduplicated, best first."""
    if not isinstance(data, dict):
        return []
    header = data.get("header") or {}
    try:
        status = int(header.get("status", 0))
    except (TypeError, ValueError):
        status = -1
    if status != 0:
        return []
    found: list[SauceResult] = []
    seen: set[str] = set()
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        result_header = result.get("header") or {}
        try:
            similarity = float(result_header.get("similarity", 0))
        except (TypeError, ValueError):
            continue
        if similarity < min_similarity:
            continue
        urls = (result.get("data") or {}).get("ext_urls") or []
        url = next((u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))), None)
        if url is None or url in seen:
            continue
        seen.add(url)
        characters, artist, original = _credits(result.get("data") or {}, url)
        found.append(
            SauceResult(
                similarity, _source_name(str(result_header.get("index_name", "")), url), url,
                characters, artist, original,
            )
        )
    found.sort(key=lambda r: r.similarity, reverse=True)
    return found[:limit]


MAX_CHARACTERS = 3


def _credits(data: dict[str, Any], url: str) -> tuple[tuple[str, ...], str | None, str | None]:
    """(characters, artist, original post) from one result's data. Field names differ per index:
    art boards use characters/creator/source, Pixiv member_name, deviantArt author_name."""
    raw_characters = data.get("characters")
    characters = tuple(
        c.strip() for c in raw_characters.split(",") if c.strip()
    ) if isinstance(raw_characters, str) else ()
    artist: Any = (
        data.get("creator") or data.get("member_name") or data.get("author_name")
        or data.get("twitter_user_handle")
    )
    if isinstance(artist, list):
        artist = ", ".join(str(a) for a in artist if a)
    source = data.get("source")
    original = source if isinstance(source, str) and source.startswith(("https://", "http://")) else None
    if original == url:
        original = None
    return characters, (str(artist).strip() or None) if artist else None, original


def _plain(text: str) -> str:
    """Names come from other sites: no formatting, no pings."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _character_name(tag: str) -> str:
    """'okita souji (fate)' -> 'Okita Souji (Fate)'."""
    return _WORD_START.sub(lambda m: m.group(1) + m.group(2).upper(), " ".join(tag.split()))


_WORD_START = re.compile(r"(^|[\s(/])([a-z])")


def format_credits(r: SauceResult) -> str:
    parts = []
    if r.characters:
        shown = ", ".join(_plain(_character_name(c)) for c in r.characters[:MAX_CHARACTERS])
        more = len(r.characters) - MAX_CHARACTERS
        parts.append(f"{shown} +{more}" if more > 0 else shown)
    if r.artist:
        parts.append(f"by {_plain(r.artist)}")
    if r.original:
        link = f"[original](<{render.md_url(r.original)}>)"
        return f"{' '.join(parts)} · {link}" if parts else link
    return " ".join(parts)


def format_similarity(similarity: float) -> str:
    return f"{similarity:.1f}".removesuffix(".0")


def format_results(results: list[SauceResult]) -> str:
    """Matches are plain: one line each, best first, links in <> so they don't preview.
    BB only talks when there's nothing to show."""
    if not results:
        return strings.line("findsource.none")
    lines = []
    for r in results:
        line = f"- [{r.source}](<{render.md_url(r.url)}>) {format_similarity(r.similarity)}%"
        credits = format_credits(r)
        lines.append(f"{line} · {credits}" if credits else line)
    return "\n".join(lines)


def links_view(image_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in engine_links(image_url):
        view.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))
    return view


# --- SauceNAO ---------------------------------------------------------------------------


async def search_saucenao(
    session: aiohttp.ClientSession, api_key: str, image_url: str
) -> list[SauceResult] | None:
    """Matches for image_url, or None if SauceNAO couldn't answer (rate limit, bad key, outage)."""
    params = {
        "output_type": "2",
        "api_key": api_key,
        "db": "999",
        "numres": str(REQUEST_RESULTS),
        "url": image_url,
    }
    try:
        async with session.get(SAUCENAO_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning("SauceNAO answered HTTP %s", resp.status)
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("SauceNAO request failed: %s", type(exc).__name__)  # str(exc) may hold the key
        return None
    header = data.get("header") if isinstance(data, dict) else None
    if not isinstance(header, dict) or header.get("status") not in (0, "0"):
        log.warning("SauceNAO error: %s", (header or {}).get("message") or (header or {}).get("status"))
        return None
    return parse_saucenao(data)


# --- the cog ----------------------------------------------------------------------------


class FindSource(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._cache: OrderedDict[str, list[SauceResult]] = OrderedDict()
        self.cooldown = Cooldown()  # every search posts publicly: one per person per 30s
        self.menu = app_commands.ContextMenu(name="Find source", callback=self.find_source_menu)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def find_source_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self.run(interaction, message)

    async def lookup(self, image_url: str) -> list[SauceResult] | None:
        """SauceNAO matches ([] = searched, nothing found), or None when BB couldn't search at all."""
        key = self.bot.config.saucenao_key
        if not key:
            return None
        if image_url in self._cache:
            self._cache.move_to_end(image_url)
            return self._cache[image_url]
        results = await search_saucenao(self.bot.http_client, key, image_url)
        if results is None:
            return None
        self._cache[image_url] = results
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return results

    async def run(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Search the first image on `message` and post the answer publicly."""
        image_url = pick_image_url(message.attachments, message.embeds, message.components)
        if image_url is None:
            await interaction.response.send_message(strings.line("findsource.no_image"), ephemeral=True)
            return
        if interaction.guild_id is not None and (await self.bot.settings.get(interaction.guild_id)).paused:
            await interaction.response.send_message(strings.line("findsource.paused"), ephemeral=True)
            return
        if not self.cooldown.ready(interaction.user.id):
            await interaction.response.send_message(strings.line("findsource.cooldown"), ephemeral=True)
            return
        if not self.bot.config.saucenao_key:
            # No key, no search: say so honestly and hand over the engines instead of claiming "nothing".
            text, view = await self.answer(image_url)
            await interaction.response.send_message(
                text, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        await interaction.response.defer(thinking=True)
        text, view = await self.answer(image_url)
        await interaction.followup.send(
            text,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def answer(self, image_url: str) -> tuple[str, discord.ui.View]:
        """What to post for an image: plain matches, or BB's line when there are none (or no search ran),
        always with the search-engine buttons. Shared by the button, the right-click menu and @BB pings."""
        try:
            results = await self.lookup(image_url)
        except Exception:
            log.exception("find source lookup failed")
            results = None
        text = strings.line("findsource.manual") if results is None else format_results(results)
        return text, links_view(image_url)


async def setup(bot: Any) -> None:
    await bot.add_cog(FindSource(bot))
