"""English summaries of official FGO JP news, by Claude Code's `claude -p` (Haiku, no tools).

Used for "coming soon" posts: nothing in English covers an announcement that early (Atlas only has events
once they're in the game data, the wiki takes days). Needs the `claude` command on the PATH and a login;
on mc-server both are the hermes user's (compose.claude.yml). Without either, summarize returns None and
the post goes out with the Japanese title.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)

TIMEOUT = 120.0
MAX_SUMMARY = 600  # characters; longer means the model rambled, and the post falls back to the title
MAX_LINES = 4

SYSTEM = """\
You turn official Fate/Grand Order (Japan) news articles into a short English note for a Discord server \
of players who don't read Japanese.

Reply with plain text only: no markdown, no quotes, no greeting, no notes about the translation.
Line 1: the English name of what's announced. Use FGO's official English name when you know one, \
otherwise a faithful translation; keep series names as players say them (GudaGuda, Lostbelt...).
Then one or two short lines (under 120 characters each) with what players need: when it opens and ends \
(as the article gives it, in JST), who can join, and what's new (servants, rewards). Leave out \
disclaimers and "more details later". Say only what the article says.

Names players use: 冠位戴冠戦 Grand Duel, 冠位研鑽戦 Grand Duel training quests, \
冠位名代戦 Grand Substitute, ストーム・ポッド Storm Pod, ぐだぐだ GudaGuda, 復刻 rerun, 聖晶石 Saint Quartz, \
呼符 Summon Ticket, マナプリズム Mana Prism, レアプリズム Rare Prism, 地獄の試練 Hellish Ordeal, \
アズライールの廟 Azrael Mausoleum, 第2部 Part 2, 終章 Final Chapter, 亜種特異点 Epic of Remnant, \
奏章 Ordeal Call."""


async def summarize(title: str, text: str) -> str | None:
    """The article in English: its name on the first line, the key facts after it. None when Claude
    Code isn't installed or logged in, times out, or answers with something that isn't a short note."""
    claude = shutil.which("claude")
    if claude is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            claude, "-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
            "--system-prompt", SYSTEM, "--output-format", "text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",  # nowhere near a CLAUDE.md
        )
    except OSError as exc:
        log.warning("translate: couldn't start claude: %r", exc)
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(f"Title: {title}\n\n{text}".encode()), TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("translate: claude took over %.0fs, gave up", TIMEOUT)
        return None
    answer = out.decode(errors="replace").strip()
    if proc.returncode != 0:
        log.warning("translate: claude failed (exit %s): %s", proc.returncode,
                    (answer or err.decode(errors="replace")).strip())
        return None
    lines = [line for line in answer.splitlines() if line.strip()]
    if not lines or len(answer) > MAX_SUMMARY or len(lines) > MAX_LINES:
        log.warning("translate: unusable answer, posting the title instead: %r", answer)
        return None
    return "\n".join(lines)
