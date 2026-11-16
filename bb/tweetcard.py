"""BB's own card for tweets a fixer link can't show whole. A fixer's preview holds at most 4 pictures, so
a tweet with more gets BB's card: the author and text, then the pictures, a page of up to 9 at a time
with ◀ ▶ buttons (the buttons cog rebuilds the card from the API for the page clicked). A row of tall
pictures (CD spines, a picture cut into strips) is meant to be seen side by side, as X shows it, so BB
joins those into one picture first. The data is FxTwitter's API answer the repost already fetches.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote as quote_path

import aiohttp
import discord
from PIL import Image

from bb import cards, views
from bb.cards import Cover
from bb.links.model import LinkMatch
from bb.render import BB_PURPLE

log = logging.getLogger(__name__)

PREVIEW_LIMIT = 4  # pictures a fixer's preview shows
# Pictures on one page of a card. A Discord gallery holds 10, but shows 10 (and 7) as one wide banner on
# top of a grid, and the banner crops a tall picture to a strip of its middle. So pages hold 9 at most
# and never 7.
PAGE_SIZES = (9, 8, 6, 5, 4, 3, 2, 1)
MAX_MEDIA = 40
TEXT_LIMIT = 1000
QUOTE_LIMIT = 500
# A strip row: at least this many pictures, each this many times taller than wide, all about one height.
STRIP_COUNT = 3
STRIP_RATIO = 3.0
STRIP_HEIGHT_SPREAD = 0.05
STRIP_MAX_WIDE = 3.0  # a joined row wider than this many heights shows as a sliver: galleries instead
STRIP_MAX_HEIGHT = 2000
# The see-through gap between joined pictures, as X shows them: Discord's background shows through.
STRIP_GAP = 1 / 200  # of the row's height
STRIP_FILENAME = "row.webp"
MEDIA_HOST = "https://pbs.twimg.com/"
# The card's footer, where a fixer's preview says "FxTwitter": BB's name and her app emoji of that name
# (Developer Portal -> Emojis), looked up at startup. Without the emoji the footer is just the name.
ICON_NAME = "BBTwitter"
ICON_EMOJI = "bb_twitter"
_icon: str | None = None


def set_icon(markup: str | None) -> None:
    """Called once at startup with the app emoji's markup (<:bb_twitter:123>), or None when it's missing."""
    global _icon
    _icon = markup
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
API_TIMEOUT = aiohttp.ClientTimeout(total=3)


@dataclass(frozen=True)
class Media:
    url: str
    video: bool
    width: int
    height: int


@dataclass(frozen=True)
class Quote:
    """The post a tweet quotes: who, what they said, and the link to it."""

    user: str
    name: str | None
    text: str
    url: str


@dataclass(frozen=True)
class Tweet:
    user: str
    name: str | None
    text: str
    stats: str  # "💬 58  🔁 2,370  ❤️ 13,836", or ""
    url: str
    media: tuple[Media, ...]
    quote: Quote | None = None
    translated_from: str | None = None  # "ja" (the source language) when text is a translation
    posted: int = 0  # when it was posted, as a Unix timestamp; 0 when the API didn't say

    @property
    def profile(self) -> str:
        return f"https://x.com/{self.user}"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _stats(tweet: dict[str, Any]) -> str:
    counts = (("💬", "replies"), ("🔁", "retweets"), ("❤️", "likes"))
    parts = [(icon, _int(tweet.get(key))) for icon, key in counts]
    return "  ".join(f"{icon} {count:,}" for icon, count in parts if count)


_GIF_MP4 = re.compile(r"https://video\.twimg\.com/tweet_video/([\w-]+)\.mp4")


def gif_image(url: str) -> str:
    """An X GIF is really an .mp4, which a Discord gallery shows as a video with a play button. FxTwitter
    serves each one as an animated .webp image (the same file its own previews use), which loops on its
    own like a GIF. Any other URL comes back unchanged."""
    found = _GIF_MP4.fullmatch(url)
    return f"https://gif.fxtwitter.com/tweet_video/{found[1]}.webp" if found else url


def _translation(tweet: dict[str, Any]) -> dict[str, Any] | None:
    """FxTwitter's translation of a post fetched with a language, when it has text."""
    found = tweet.get("translation")
    return found if isinstance(found, dict) and str(found.get("text") or "").strip() else None


def _text(tweet: dict[str, Any], translated: bool) -> str:
    found = _translation(tweet) if translated else None
    return str((found or tweet).get("text") or "").strip()


def _who(author: dict[str, Any]) -> tuple[str, str | None]:
    return str(author.get("screen_name") or ""), str(author.get("name") or "").strip() or None


def _quote(tweet: dict[str, Any], translated: bool) -> Quote | None:
    quoted = tweet.get("quote")
    if not isinstance(quoted, dict):
        return None
    user, name = _who(quoted.get("author") or {})
    url = str(quoted.get("url") or "")
    if not user or not url.startswith("https://x.com/"):
        return None
    return Quote(user, name, _text(quoted, translated), url)


def parse(tweet: dict[str, Any] | None, *, translated: bool = False) -> Tweet | None:
    """The tweet as FxTwitter's API gives it, or None when it has no author or no media. translated: use
    the translation the API sends when the tweet was fetched with a language (fetch's lang)."""
    if not tweet:
        return None
    user, name = _who(tweet.get("author") or {})
    media: list[Media] = []
    for item in (tweet.get("media") or {}).get("all") or ():
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            kind = item.get("type")
            if kind == "gif":
                url = gif_image(url)
            size = _int(item.get("width")), _int(item.get("height"))
            media.append(Media(url, kind in ("video", "gif"), *size))
    if not user or not media:
        return None
    url = str(tweet.get("url") or "")
    if not url.startswith("https://x.com/"):
        url = f"https://x.com/{user}/status/{tweet.get('id')}"
    found = _translation(tweet) if translated else None
    source = (str(found.get("source_lang") or "").strip() or "another language") if found else None
    return Tweet(
        user, name, _text(tweet, translated), _stats(tweet), url, tuple(media[:MAX_MEDIA]),
        quote=_quote(tweet, translated), translated_from=source, posted=_int(tweet.get("created_timestamp")),
    )


def is_strip_row(media: tuple[Media, ...]) -> bool:
    """Pictures meant to sit side by side: 3 or more, no videos, all about one height, at least half of
    them tall and thin (a wider panel among the strips is part of the picture), and not so many that the
    joined row comes out wider than STRIP_MAX_WIDE heights."""
    if len(media) < STRIP_COUNT or any(item.video or not item.width for item in media):
        return False
    strips = sum(item.height >= item.width * STRIP_RATIO for item in media)
    if strips * 2 < len(media):
        return False
    tallest = max(item.height for item in media)
    if any(item.height < tallest * (1 - STRIP_HEIGHT_SPREAD) for item in media):
        return False
    return sum(item.width / item.height for item in media) <= STRIP_MAX_WIDE


def wants_card(tweet: Tweet) -> bool:
    """More pictures than a fixer's preview shows, or a strip row to join."""
    return len(tweet.media) > PREVIEW_LIMIT or is_strip_row(tweet.media)


def stitch(images: list[bytes]) -> bytes:
    """The pictures side by side with see-through gaps, scaled to the shortest one's height (at most
    STRIP_MAX_HEIGHT), as WebP (JPEG has no transparency, and PNG is too big for photos)."""
    opened = [Image.open(io.BytesIO(data)) for data in images]
    height = min(STRIP_MAX_HEIGHT, *(image.height for image in opened))
    gap = max(2, round(height * STRIP_GAP))
    scaled = []
    for image in opened:
        width = max(1, round(image.width * height / image.height))
        scaled.append(image.convert("RGBA").resize((width, height), Image.LANCZOS))
    width = sum(image.width for image in scaled) + gap * (len(scaled) - 1)
    row = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = 0
    for image in scaled:
        row.paste(image, (x, 0))
        x += image.width + gap
    out = io.BytesIO()
    row.save(out, "WEBP", quality=90)
    return out.getvalue()


async def _fetch(session: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes | None:
    async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
        found = await cards.read_image(resp, max_bytes)
    return found[0] if found else None


async def strip_cover(session: aiohttp.ClientSession, tweet: Tweet, max_bytes: int) -> Cover | None:
    """The strip row joined into one picture, or None when it isn't one or a picture can't be had."""
    if not is_strip_row(tweet.media) or not all(item.url.startswith(MEDIA_HOST) for item in tweet.media):
        return None
    try:
        images = await asyncio.gather(*(_fetch(session, item.url, max_bytes) for item in tweet.media))
        if not all(images):
            log.info("a picture of @%s's strip row didn't download", tweet.user)
            return None
        data = await asyncio.to_thread(stitch, [image for image in images if image])
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        log.warning("couldn't join @%s's strip row: %r", tweet.user, exc)
        return None
    if len(data) > max_bytes:
        log.info("@%s's joined strip row is %d bytes, over %d", tweet.user, len(data), max_bytes)
        return None
    return Cover(data, STRIP_FILENAME)


def _splits(total: int, count: int, top: int) -> Iterator[tuple[int, ...]]:
    """Every way to write total as count page sizes from PAGE_SIZES, largest first, none over top."""
    if count == 0:
        if total == 0:
            yield ()
        return
    for size in PAGE_SIZES:
        if size <= top and size <= total:
            for rest in _splits(total - size, count - 1, size):
                yield (size, *rest)


def page_sizes(total: int) -> tuple[int, ...]:
    """As few pages as fit, as even as possible, and never a page Discord shows with a cropped banner."""
    for count in range(max(1, math.ceil(total / PAGE_SIZES[0])), total + 1):
        splits = _splits(total, count, PAGE_SIZES[0])
        found = min(splits, key=lambda sizes: sizes[0] - sizes[-1], default=None)
        if found:
            return found
    return (total,)


def pages_of(urls: list[str]) -> list[list[str]]:
    """The pictures split by page_sizes: 11 is 6 and 5, never 10 and a lonely 1, and 7 is 4 and 3."""
    pages, start = [], 0
    for size in page_sizes(len(urls)):
        pages.append(urls[start : start + size])
        start += size
    return pages or [[]]


async def fetch(
    session: aiohttp.ClientSession, m: LinkMatch, lang: str | None = None
) -> dict[str, Any] | None:
    """The tweet as FxTwitter's API describes it. With lang ("en"), it also carries a translation into
    that language, for the tweet and its quote. None if it doesn't answer in time."""
    if "id" not in m.parts:
        return None
    url = f"https://api.fxtwitter.com/{m.parts.get('user') or 'i'}/status/{m.parts['id']}"
    if lang:
        url += f"/{quote_path(lang)}"
    try:
        async with session.get(url, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    tweet = data.get("tweet") if isinstance(data, dict) else None
    return tweet if isinstance(tweet, dict) else None


def _quote_block(quote: Quote) -> str:
    """The quoted post as a Discord block quote: who (linking to the post), then what they said."""
    who = f"{quote.name} (@{quote.user})" if quote.name else f"@{quote.user}"
    lines = [f"**[{cards.link_text(who)}]({quote.url})**"]
    if quote.text:
        lines += discord.utils.escape_markdown(cards.clip(quote.text, QUOTE_LIMIT)).split("\n")
    return "\n".join(f"> {line}" for line in lines)


def footer(tweet: Tweet) -> str:
    """"<icon> BBTwitter · <when it was posted>", the time in each reader's own timezone."""
    parts = [f"{_icon} {ICON_NAME}" if _icon else ICON_NAME]
    if tweet.posted:
        parts.append(f"<t:{tweet.posted}:f>")
    return "-# " + " · ".join(parts)


def card_text(tweet: Tweet) -> str:
    who = f"{tweet.name} (@{tweet.user})" if tweet.name else f"@{tweet.user}"
    lines = [f"**[{cards.link_text(who)}]({tweet.profile})**"]
    if tweet.text:
        lines.append(discord.utils.escape_markdown(cards.clip(tweet.text, TEXT_LIMIT)))
    if tweet.translated_from:
        lines.append(f"-# Translated from {tweet.translated_from}")
    if tweet.quote:
        lines.append(_quote_block(tweet.quote))
    if tweet.stats:
        lines.append(f"-# {tweet.stats}")
    return "\n".join(lines)


def build_card(
    tweet: Tweet,
    m: LinkMatch,
    *,
    buttons: list[discord.ui.Item[Any]],
    quip: str | None = None,
    cover: Cover | None = None,
    page: int = 0,
) -> discord.ui.LayoutView:
    """Components V2 card: the author and text, the media, the BBTwitter footer, then the link and BB's
    buttons. The media is the joined strip row when there's a cover, else one page of the pictures with
    ◀ 1/2 ▶ under it when there's more than one page."""
    view = discord.ui.LayoutView(timeout=None)
    if quip:
        view.add_item(discord.ui.TextDisplay(f"-# {quip}"))
    container = discord.ui.Container(accent_colour=BB_PURPLE)
    container.add_item(discord.ui.TextDisplay(card_text(tweet)))
    pages = [[f"attachment://{cover.filename}"]] if cover else pages_of([item.url for item in tweet.media])
    page = min(max(page, 0), len(pages) - 1)
    items = [discord.MediaGalleryItem(url, spoiler=m.spoiler) for url in pages[page]]
    container.add_item(discord.ui.MediaGallery(*items))
    if len(pages) > 1:
        container.add_item(discord.ui.ActionRow(*views.page_buttons(page, len(pages))))
    container.add_item(discord.ui.TextDisplay(footer(tweet)))
    link = discord.ui.Button(label="Open on X", url=tweet.url)
    container.add_item(discord.ui.ActionRow(link, *buttons))
    view.add_item(container)
    return view
