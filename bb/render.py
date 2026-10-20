"""Builds the text of BB's repost messages. Shared by linkfix (sending) and buttons (translate).

Two styles, chosen once at startup with LINK_STYLE in .env:

- "hidden" (default): one small line, "Tweet · FxTwitter", then the quip if there is one. "Tweet" links to
  the real post (no preview); the fixer's name is the link Discord previews, and the one people can copy
  to post elsewhere.
- "subtle": one small grey line, "@user on Twitter", linking to the fixer.

"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from bb.links import sites
from bb.links.model import Fixer, LinkMatch

Style = Literal["hidden", "subtle"]
STYLES: tuple[Style, ...] = ("hidden", "subtle")
BB_PURPLE = 0x9966CC  # cards, roundup, panels
SEPARATOR = " · "

_style: Style = "hidden"


def set_style(style: str) -> None:
    """Called once at startup with Config.link_style. Unknown values fall back to "hidden"."""
    global _style
    _style = style if style in STYLES else "hidden"  # type: ignore[assignment]


def current_style() -> Style:
    return _style


# Characters that end or break a markdown link target. A posted URL can carry them in its query
# ("?x=>https://elsewhere"), which would put a bare, previewed link into BB's message.
_LINK_BREAKERS = {c: f"%{ord(c):02X}" for c in "<>()[]|`\\ \t\n"}


def md_url(url: str) -> str:
    """A URL safe to put inside [text](<url>): the breaking characters percent-encoded."""
    return "".join(_LINK_BREAKERS.get(c, c) for c in url)


def _spoiler(text: str, spoiler: bool) -> str:
    return f"||{text} ||" if spoiler else text


def _who(m: LinkMatch) -> str:
    return f"@{m.author} on {m.site.name}" if m.author else f"{m.site.name} post"


def _head(m: LinkMatch, quip: str | None) -> str:
    """The one visible line: a small link to the post on its real site ("Tweet"), then the quip."""
    source = f"[{m.site.label}](<{md_url(sites.source_url(m))}>)"
    return f"{source}{SEPARATOR}{quip}" if quip else source


def format_repost(
    m: LinkMatch,
    fixer_name: str,
    fixed: str,
    quip: str | None = None,
    style: Style | None = None,
    *,
    more: Sequence[tuple[str, str]] = (),
) -> str:
    """The repost text: a link to the fixer URL (previewed) plus the quip, if any. more: extra
    (label, URL) previews for a tweet that mixes photos and videos, one per item."""
    extra = [_spoiler(f"[{label}]({md_url(url)})", m.spoiler) for label, url in more]
    fixed = md_url(fixed)
    if (style or _style) == "subtle":
        link = _spoiler(f"[{_who(m)}]({fixed})", m.spoiler)
        return "-# " + SEPARATOR.join([link, *extra, *([quip] if quip else [])])
    source = f"[{m.site.label}](<{md_url(sites.source_url(m))}>)"
    link = _spoiler(f"[{fixer_name}]({fixed})", m.spoiler)
    return "-# " + SEPARATOR.join([source, link, *extra, *([quip] if quip else [])])


def repost_text(
    m: LinkMatch,
    fixer: Fixer,
    quip: str | None = None,
    *,
    media: Sequence[str] = (),
    lang: str | None = None,
    style: Style | None = None,
) -> str:
    """format_repost for a fixer, with one preview per item when the tweet mixes photos and videos."""
    (_, first), *rest = sites.media_links(m, fixer, media, lang)
    return format_repost(m, fixer.name, first, quip, style, more=rest)


def format_upload(m: LinkMatch, quip: str | None = None, style: Style | None = None) -> str:
    """Text for the upload fallback (the media is attached, so there's no fixer link to preview)."""
    if (style or _style) == "subtle":
        link = _spoiler(f"[{_who(m)}](<{md_url(m.url)}>)", m.spoiler)
        return f"-# {link}{SEPARATOR}{quip}" if quip else f"-# {link}"
    return f"-# {_head(m, quip)}"
