"""FGO JP shop and quest resets: a heads-up a day before ("spend your prisms"), then a post when it
happens. Pure decisions, like bb/news.py: the news cog posts them with the rest of the news.

The resets are fixed dates in Japan time, so they come from this table, not from Atlas (Atlas only lists
a shop's items once the game data for that month is out):
- Mana Prism shop: the 1st, 00:00 (its monthly items run from the 1st to the end of the month)
- Rare Prism shop: the 15th, 00:00 (its monthly items run from the 15th to the 14th)
- Craftable grails: the 1st, 00:00 (as the server's players describe it; not in Atlas's data)
- Hellish Ordeal quests and missions: the 1st, 18:00
- Hellish Ordeal shop (the Azure Flame of Hell exchange, items marked 【3ヶ月更新】): the 1st, 18:00, every
  3 months. Its first items ran from 2026-08-02 18:00 to 2026-11-01 17:59, so it resets in February, May,
  August and November.

Resets at the same time go out as one post. Each one is recorded once posted, like the rest of the news,
so a restart never repeats one, and a reset is only announced for RESET_FRESH after it happens: BB being
offline, or a news channel set today, never posts "has reset" a day late.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from bb import strings
from bb.news import DAY, JST, SOON, START, Mark, Notice

RESET_FRESH = 6 * 3600.0


@dataclass(frozen=True)
class Reset:
    slug: str
    name: str
    day: int
    hour: int
    months: tuple[int, ...] = tuple(range(1, 13))


RESETS = (
    Reset("mana", "Mana Prism shop", day=1, hour=0),
    Reset("grails", "Craftable grails", day=1, hour=0),
    Reset("rare", "Rare Prism shop", day=15, hour=0),
    Reset("ordeal", "Hellish Ordeal", day=1, hour=18),
    Reset("ordeal_shop", "Hellish Ordeal shop", day=1, hour=18, months=(2, 5, 8, 11)),
)


def occurrences(reset: Reset, ts: float) -> list[float]:
    """The reset's times from a month before ts to two months after: enough for both windows."""
    here = datetime.fromtimestamp(ts, JST)
    times = []
    for offset in range(-1, 3):
        month0 = here.month - 1 + offset
        year, month = here.year + month0 // 12, month0 % 12 + 1
        if month in reset.months:
            times.append(datetime(year, month, reset.day, reset.hour, tzinfo=JST).timestamp())
    return times


def _key(reset: Reset, at: float) -> str:
    return f"reset:{reset.slug}:{datetime.fromtimestamp(at, JST):%Y-%m-%dT%H}"


def due(ts: float, done: set[Mark]) -> list[Notice]:
    """The heads-ups (a day before) and the "has reset" posts due now and not posted yet, resets at the
    same time together. Recording a reset also records its heads-up: once it's happened, it's old news."""
    soon: dict[float, list[Reset]] = defaultdict(list)
    happened: dict[float, list[Reset]] = defaultdict(list)
    for reset in RESETS:
        for at in occurrences(reset, ts):
            key = _key(reset, at)
            if at <= ts < at + RESET_FRESH and (key, START) not in done:
                happened[at].append(reset)
            elif at - DAY <= ts < at and (key, SOON) not in done:
                soon[at].append(reset)
    notices = []
    for at, resets in sorted(happened.items()):
        names = ", ".join(r.name for r in resets)
        marks = [(_key(r, at), phase) for r in resets for phase in (SOON, START)]
        notices.append(Notice(strings.line("news.reset", names=names), marks))
    for at, resets in sorted(soon.items()):
        stamp = int(at)
        text = strings.line("news.reset_soon", names=", ".join(r.name for r in resets)) + "\n" + strings.line(
            "news.resets_at", when=f"<t:{stamp}:f>", left=f"<t:{stamp}:R>"
        )
        notices.append(Notice(text, [(_key(r, at), SOON) for r in resets]))
    return notices
