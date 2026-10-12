"""Markdown-aware URL extraction. Owned by track A; signatures are the contract."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FoundURL:
    url: str
    spoiler: bool = False


# Discord only treats a fence as a code block once it's closed; an unclosed ``` is plain text.
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
# Inline code: a run of backticks closed by a run of the same length.
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)[\s\S]*?[^`]\1(?!`)")
# <https://...> suppresses the embed, including inside [label](<https://...>).
_NO_PREVIEW = re.compile(r"<https?://[^\s<>]+>", re.I)
_SPOILER = re.compile(r"\|\|[\s\S]+?\|\|")
_URL = re.compile(r"https?://[^\s<]+", re.I)

# Characters Discord doesn't count as the end of a link.
_TRAILING = ".,:;!?'\"|>"
_PAIRS = {")": "(", "]": "["}
# Formatting markers (**bold**, _italic_, ~~strike~~) are only stripped when they also open before the
# link: "_" is a legal last character (Instagram shortcodes) when nothing wraps the URL.
_MARKERS_BEFORE = re.compile(r"[*_~]+\Z")


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    """Replace every match with spaces of the same length, so offsets keep lining up."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _trim(url: str, markers: str = "") -> str:
    url = url.split("||", 1)[0]
    while url:
        last = url[-1]
        if last in _TRAILING or last in markers:
            url = url[:-1]
        elif last in _PAIRS and url.count(_PAIRS[last]) < url.count(last):
            url = url[:-1]  # closing bracket of [label](url) or (see url)
        else:
            break
    return url


def extract_urls(content: str) -> list[FoundURL]:
    """Every link Discord would preview, in order of appearance, without duplicates.

    Skips code blocks, inline code and <url> (no-preview) links. Marks links inside ||spoilers||.
    Strips trailing punctuation that Discord doesn't treat as part of the URL.
    """
    text = _blank(content, _CODE_BLOCK)
    text = _blank(text, _INLINE_CODE)
    text = _blank(text, _NO_PREVIEW)
    spoilers = [(m.start(), m.end()) for m in _SPOILER.finditer(text)]

    found: list[FoundURL] = []
    seen: set[str] = set()
    for m in _URL.finditer(text):
        wrap = _MARKERS_BEFORE.search(text, 0, m.start())
        url = _trim(m.group(0), wrap.group(0) if wrap else "")
        if "://" not in url or url.endswith("://") or url in seen:
            continue
        seen.add(url)
        start = m.start()
        spoiler = any(a < start < b for a, b in spoilers)
        found.append(FoundURL(url=url, spoiler=spoiler))
    return found
