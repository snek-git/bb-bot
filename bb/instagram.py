"""Instagram cards. OGInstagram's preview shows the whole caption, the pile of hashtags included, so BB
builds the card herself from the ActivityPub note OGInstagram serves for each post (the same data
Discord's preview of an oginstagram.com link reads). The caption keeps its text and drops the hashtag
block after it. When the note can't be had, BB falls back to the usual fixer link.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from bb import cards
from bb.links.model import LinkMatch
from bb.render import BB_PURPLE

log = logging.getLogger(__name__)

BASE = "https://oginstagram.com"
AP_HEADERS = {"Accept": "application/activity+json"}
TIMEOUT = aiohttp.ClientTimeout(total=5)
MAX_MEDIA = 10  # a media gallery's limit, and Instagram's carousel limit
CAPTION_LIMIT = 1000

_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.S)
_STATS = re.compile(r"<b>(.*?)</b>", re.S)
_BREAK = re.compile(r"<br\s*/?>", re.I)
_TAG_HTML = re.compile(r"<[^>]+>")
_HASHTAG = r"#\w+"
# The hashtag block starts at the first hashtag after a line break, a double space or the end of a
# sentence. A hashtag inside a sentence ("off to #paris today") stays.
_BLOCK_START = re.compile(rf"(?:\n|\s\s|(?<=[.!?…])\s)\s*{_HASHTAG}")
_TAIL = re.compile(rf"(?:\s*{_HASHTAG})+\s*$")


@dataclass(frozen=True)
class Media:
    url: str
    video: bool


@dataclass(frozen=True)
class Post:
    user: str
    name: str | None  # display name, when the author lookup worked
    caption: str
    stats: str  # "❤️ 18,409  💬 70", or ""
    media: tuple[Media, ...]

    @property
    def profile(self) -> str:
        return f"https://www.instagram.com/{self.user}/"

    @property
    def has_image(self) -> bool:
        return any(not item.video for item in self.media)


def _plain(fragment: str) -> str:
    return html.unescape(_TAG_HTML.sub("", _BREAK.sub("\n", fragment))).strip()


def strip_hashtags(text: str) -> str:
    """The caption without its hashtag block: everything from the first hashtag that follows a break or
    a sentence, then any hashtags left at the very end."""
    found = _BLOCK_START.search(text)
    if found:
        text = text[: found.start()]
    return _TAIL.sub("", text).strip()


def split_content(content: str) -> tuple[str, str]:
    """(stats, caption) from a note's HTML: a bold stats paragraph first, then the caption's paragraphs."""
    paragraphs = _PARAGRAPH.findall(content)
    stats = ""
    if paragraphs:
        bold = _STATS.fullmatch(paragraphs[0].strip())
        if bold:
            stats = _plain(bold.group(1))
            paragraphs = paragraphs[1:]
    caption = "\n\n".join(text for text in (_plain(p) for p in paragraphs) if text)
    return stats, caption


def parse_note(note: dict[str, Any], *, name: str | None = None) -> Post | None:
    """The post, or None when the note has no author or no media (nothing worth a card)."""
    author = str(note.get("attributedTo") or "").rstrip("/").rpartition("/")[2]
    media: list[Media] = []
    for item in note.get("attachment") or ():
        if not isinstance(item, dict):
            continue
        url, kind = item.get("url"), str(item.get("mediaType") or "")
        if isinstance(url, str) and url.startswith("https://") and kind.startswith(("image/", "video/")):
            media.append(Media(url, kind.startswith("video/")))
    if not author or not media:
        return None
    stats, caption = split_content(str(note.get("content") or ""))
    return Post(author, name, strip_hashtags(caption), stats, tuple(media[:MAX_MEDIA]))


async def _get_json(session: aiohttp.ClientSession, url: str) -> dict[str, Any] | None:
    async with session.get(url, headers=AP_HEADERS, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else None


async def fetch_post(session: aiohttp.ClientSession, m: LinkMatch) -> Post | None:
    """The post from OGInstagram's note, plus the author's display name. None on any failure."""
    url = f"{BASE}/{m.parts.get('kind', 'p')}/{m.parts['shortcode']}"
    try:
        note = await _get_json(session, url)
        if note is None:
            log.info("no OGInstagram note for %s", m.canonical_key)
            return None
        post = parse_note(note)
        if post is None:
            return None
        actor = await _get_json(session, f"{BASE}/{post.user}")
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("OGInstagram note for %s failed: %r", m.canonical_key, exc)
        return None
    name = str((actor or {}).get("name") or "").strip() or None
    return parse_note(note, name=name)


def card_text(post: Post) -> str:
    who = f"{post.name} (@{post.user})" if post.name else f"@{post.user}"
    lines = [f"**[{cards.link_text(who)}]({post.profile})**"]
    if post.caption:
        lines.append(discord.utils.escape_markdown(cards.clip(post.caption, CAPTION_LIMIT)))
    if post.stats:
        lines.append(f"-# {post.stats}")
    return "\n".join(lines)


def post_link(m: LinkMatch) -> str:
    return f"https://www.instagram.com/{m.parts.get('kind', 'p')}/{m.parts['shortcode']}/"


def build_card(
    post: Post, m: LinkMatch, *, buttons: list[discord.ui.Item[Any]], quip: str | None = None
) -> discord.ui.LayoutView:
    """Components V2 card: the author and caption, the post's media, then the link and BB's buttons."""
    view = discord.ui.LayoutView(timeout=None)
    if quip:
        view.add_item(discord.ui.TextDisplay(f"-# {quip}"))
    container = discord.ui.Container(accent_colour=BB_PURPLE)
    container.add_item(discord.ui.TextDisplay(card_text(post)))
    items = [discord.MediaGalleryItem(item.url, spoiler=m.spoiler) for item in post.media]
    container.add_item(discord.ui.MediaGallery(*items))
    link = discord.ui.Button(label="Open on Instagram", url=post_link(m))
    container.add_item(discord.ui.ActionRow(link, *buttons))
    view.add_item(container)
    return view
