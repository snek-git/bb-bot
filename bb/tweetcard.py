"""BB's own card for tweets a fixer link can't show whole. A fixer's preview holds at most 4 pictures, so
a tweet with more gets BB's card: the author and text, then the pictures, a page of up to 10 at a time
with ◀ ▶ buttons (the buttons cog rebuilds the card from the API for the page clicked). A row of tall
pictures (CD spines, a picture cut into strips) is meant to be seen side by side, as X shows it, so BB
joins those into one picture first. The data is FxTwitter's API answer the repost already fetches.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord
from PIL import Image

from bb import cards, views
from bb.cards import Cover
from bb.links.model import LinkMatch
from bb.render import BB_PURPLE

log = logging.getLogger(__name__)

PREVIEW_LIMIT = 4  # pictures a fixer's preview shows
GALLERY_SIZE = 10  # items in one Discord media gallery
MAX_MEDIA = 40
TEXT_LIMIT = 1000
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
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=15)
API_TIMEOUT = aiohttp.ClientTimeout(total=3)


@dataclass(frozen=True)
class Media:
    url: str
    video: bool
    width: int
    height: int


@dataclass(frozen=True)
class Tweet:
    user: str
    name: str | None
    text: str
    stats: str  # "💬 58  🔁 2,370  ❤️ 13,836", or ""
    url: str
    media: tuple[Media, ...]

    @property
    def profile(self) -> str:
        return f"https://x.com/{self.user}"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _stats(tweet: dict[str, Any]) -> str:
    counts = (("💬", "replies"), ("🔁", "retweets"), ("❤️", "likes"))
    parts = [(icon, _int(tweet.get(key))) for icon, key in counts]
    return "  ".join(f"{icon} {count:,}" for icon, count in parts if count)


def parse(tweet: dict[str, Any] | None) -> Tweet | None:
    """The tweet as FxTwitter's API gives it, or None when it has no author or no media."""
    if not tweet:
        return None
    author = tweet.get("author") or {}
    user = str(author.get("screen_name") or "")
    media: list[Media] = []
    for item in (tweet.get("media") or {}).get("all") or ():
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            video = item.get("type") in ("video", "gif")
            media.append(Media(url, video, _int(item.get("width")), _int(item.get("height"))))
    if not user or not media:
        return None
    url = str(tweet.get("url") or "")
    if not url.startswith("https://x.com/"):
        url = f"https://x.com/{user}/status/{tweet.get('id')}"
    name = str(author.get("name") or "").strip() or None
    text = str(tweet.get("text") or "").strip()
    return Tweet(user, name, text, _stats(tweet), url, tuple(media[:MAX_MEDIA]))


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


def pages_of(urls: list[str]) -> list[list[str]]:
    """The pictures split into as few pages as a gallery's limit allows, as even as possible (11 is 6 and
    5, never 10 and a lonely 1)."""
    count = max(1, math.ceil(len(urls) / GALLERY_SIZE))
    size = math.ceil(len(urls) / count)
    return [urls[start : start + size] for start in range(0, len(urls), size)] or [[]]


async def fetch(session: aiohttp.ClientSession, m: LinkMatch) -> dict[str, Any] | None:
    """The tweet as FxTwitter's API describes it. None if it doesn't answer in time."""
    if "id" not in m.parts:
        return None
    url = f"https://api.fxtwitter.com/{m.parts.get('user') or 'i'}/status/{m.parts['id']}"
    try:
        async with session.get(url, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    tweet = data.get("tweet") if isinstance(data, dict) else None
    return tweet if isinstance(tweet, dict) else None


def card_text(tweet: Tweet) -> str:
    who = f"{tweet.name} (@{tweet.user})" if tweet.name else f"@{tweet.user}"
    lines = [f"**[{cards.link_text(who)}]({tweet.profile})**"]
    if tweet.text:
        lines.append(discord.utils.escape_markdown(cards.clip(tweet.text, TEXT_LIMIT)))
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
    """Components V2 card: the author and text, the media, then the link and BB's buttons. The media is the
    joined strip row when there's a cover, else one page of the pictures with ◀ 1/2 ▶ under it when
    there's more than one page."""
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
    link = discord.ui.Button(label="Open on X", url=tweet.url)
    container.add_item(discord.ui.ActionRow(link, *buttons))
    view.add_item(container)
    return view
