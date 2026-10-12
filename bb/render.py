"""Builds the text of BB's repost messages. Shared by linkfix (sending) and buttons (translate).

Two styles, chosen once at startup with LINK_STYLE in .env:

- "hidden" (default): no visible link text at all, just the preview (and a quip line when there is one).
  The fixer link is a masked link whose text is a Braille blank (U+2800): it renders as nothing but
  isn't whitespace, so Discord keeps the link and previews it.
- "subtle": one small grey line, "@user on Twitter", linking to the fixer.

The original post is reachable through the "Open on X" link button either way (see views.py).
"""

from __future__ import annotations

from typing import Literal

from bb.links.model import LinkMatch

Style = Literal["hidden", "subtle"]
STYLES: tuple[Style, ...] = ("hidden", "subtle")
BLANK = "\u2800"  # Braille blank: invisible, but not whitespace
BB_PURPLE = 0x9966CC  # cards, roundup, panels
SEPARATOR = " · "

_style: Style = "hidden"


def set_style(style: str) -> None:
    """Called once at startup with Config.link_style. Unknown values fall back to "hidden"."""
    global _style
    _style = style if style in STYLES else "hidden"  # type: ignore[assignment]


def current_style() -> Style:
    return _style


def _spoiler(text: str, spoiler: bool) -> str:
    return f"||{text} ||" if spoiler else text


def _who(m: LinkMatch) -> str:
    return f"@{m.author} on {m.site.name}" if m.author else f"{m.site.name} post"


def format_repost(
    m: LinkMatch, fixer_name: str, fixed: str, quip: str | None = None, style: Style | None = None
) -> str:
    """The repost text: a link to the fixer URL (the only previewed link) plus the quip, if any.

    fixer_name is kept for callers; the preview card itself names the fixer.
    """
    if (style or _style) == "subtle":
        link = _spoiler(f"[{_who(m)}]({fixed})", m.spoiler)
        return f"-# {link}{SEPARATOR}{quip}" if quip else f"-# {link}"
    link = _spoiler(f"[{BLANK}]({fixed})", m.spoiler)
    return f"-# {quip} {link}" if quip else link


def format_upload(m: LinkMatch, quip: str | None = None, style: Style | None = None) -> str:
    """Text for the upload fallback (the media is attached, so there's no fixer link to preview)."""
    if (style or _style) == "subtle":
        link = _spoiler(f"[{_who(m)}](<{m.url}>)", m.spoiler)
        return f"-# {link}{SEPARATOR}{quip}" if quip else f"-# {link}"
    return f"-# {quip}" if quip else ""
