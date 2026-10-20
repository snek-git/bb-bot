"""Shared pieces of BB's own gallery cards (e-hentai, nhentai): sites no fixer service covers, where BB
builds the card herself from the site's API."""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp
import discord

from bb.render import BB_PURPLE

LINE_LIMIT = 300
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class Cover:
    """An image uploaded with the card."""

    data: bytes
    filename: str


def clip(text: str, limit: int = LINE_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip(", ") + "…"


def link_text(text: str) -> str:
    """Brackets would end a markdown link label early; titles are full of them."""
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


async def read_image(resp: aiohttp.ClientResponse, max_bytes: int) -> tuple[bytes, str] | None:
    """(bytes, extension) when resp is a 200 image of at most max_bytes, else None."""
    ext = _EXT.get(resp.content_type)
    if resp.status != 200 or ext is None or (resp.content_length or 0) > max_bytes:
        return None
    data = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        data.extend(chunk)
        if len(data) > max_bytes:
            return None
    return bytes(data), ext


def card_body(title: str, link: str, stats: list[str], tag_lines: list[str]) -> str:
    """The linked title, one stats line, then the tag lines."""
    lines = [
        f"### [{link_text(clip(title, 256))}]({link})",
        " · ".join(s for s in stats if s),
        "",
        *tag_lines,
    ]
    return "\n".join(lines)[:3500]


def card_view(
    text: str, *, link: str, label: str, media: str | None, spoiler: bool, quip: str | None = None
) -> discord.ui.LayoutView:
    """Components V2 card: big image on top, then the text, then the link button, all in a purple
    container. media is "attachment://<file>" for an uploaded cover, or an https URL. Spoilered links
    get a blurred image."""
    view = discord.ui.LayoutView(timeout=None)
    if quip:
        view.add_item(discord.ui.TextDisplay(f"-# {quip}"))
    container = discord.ui.Container(accent_colour=BB_PURPLE)
    if media:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media, spoiler=spoiler)))
    container.add_item(discord.ui.TextDisplay(text))
    container.add_item(discord.ui.ActionRow(discord.ui.Button(label=label, url=link)))
    view.add_item(container)
    return view
