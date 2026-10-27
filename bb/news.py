"""FGO JP news from Atlas Academy's data exports: new events and banners, and reminders before they end.

Pure decisions only (no Discord, no network): parse the exports, group banners, work out what is due
and turn it into short posts. The news cog fetches the data and does the sending.

What gets posted:
- an event (eventQuest) that started in the last FRESH seconds; campaigns like "AP cost 1/2" are skipped
- banners (Saint Quartz and paid ones) that opened in the last FRESH seconds, grouped by campaign
- "2 days left" and "last day" for events and banner groups that run at most LONG seconds. Longer ones
  (story chapters, the trial quest) end on a placeholder date far in the future, so they only get the
  start post. Banners that end together with an event get no reminder of their own.

Some limited events are listed with that placeholder too (the Grand event 冠位名代戦: "until 2029",
while its quests close a week after it opens). For those the cog reads the quests' close time
(quest_end) and swaps it in before anything here sees the event.

Every (item, phase) pair is recorded once posted, so nothing is posted twice.

Start posts carry a picture: a banner post its first new banner's art, an event post the event's banner
(the cog looks that one up, since the exports don't have it, and attaches both as files). Reminders are
text only.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from bb import strings

REGION = "JP"
EXPORT = f"https://api.atlasacademy.io/export/{REGION}"
EVENTS_URL = f"{EXPORT}/basic_event_lang_en.json"
BANNERS_URL = f"{EXPORT}/nice_gacha_lang_en.json"
SERVANTS_URL = f"{EXPORT}/basic_servant_lang_en.json"
EVENT_PAGE = f"https://apps.atlasacademy.io/db/{REGION}/event/{{id}}"
EVENT_API = f"https://api.atlasacademy.io/nice/{REGION}/event/{{id}}"  # has the event's banner image
WAR_API = f"https://api.atlasacademy.io/nice/{REGION}/war/{{id}}"  # has its quests' close times
BANNER_ART = f"https://static.atlasacademy.io/{REGION}/SummonBanners/img_summon_{{id}}.png"

DAY = 86400.0
FRESH = 2 * DAY  # how late a start may still be posted (BB offline, a slow data update)
LONG = 60 * DAY  # longer than this: no end reminders
SAME_END = 3600.0  # a banner group ending this close to an event's end ends "with" the event
MAX_PICKUPS = 10  # servants named per banner post; the rest become "+N more"
MAX_MESSAGE = 2000

RERUN = "復刻"
BANNER_TYPES = {"stone", "chargeStone"}  # Saint Quartz and paid banners; Friend Point ones aren't news
_BANNER_SUFFIX = re.compile(r"(ピックアップ)?召喚$")
MIN_SHARED = 6  # banners ending together whose names share this many leading characters are one group

START, SOON, LAST = "start", "soon", "last"
Mark = tuple[str, str]  # (item key, phase)


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    start: float
    end: float
    war_ids: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        return f"event:{self.id}"


@dataclass(frozen=True)
class Servant:
    name: str
    rarity: int


@dataclass(frozen=True)
class Banner:
    id: int
    name: str
    start: float
    end: float
    pickups: tuple[int, ...]
    image_id: int = 0

    @property
    def key(self) -> str:
        return f"banner:{self.id}"

    @property
    def art(self) -> str | None:
        return BANNER_ART.format(id=self.image_id) if self.image_id else None


@dataclass(frozen=True)
class Group:
    """Banners from one campaign: they end at the same time and their names start the same way."""

    title: str
    end: float
    banners: tuple[Banner, ...]


@dataclass
class Notice:
    """One item of a post. marks: what gets recorded once it's sent. image: a picture shown under the
    text. event_id: the event whose banner should become the image (looked up by the cog)."""

    text: str
    marks: list[Mark] = field(default_factory=list)
    image: str | None = None
    event_id: int | None = None


# --- parsing ----------------------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def parse_events(raw: Iterable[dict[str, Any]]) -> list[Event]:
    """eventQuest entries only. The export lists some events twice (same name and start): keep one."""
    seen: set[tuple[str, float]] = set()
    events = []
    for item in raw:
        if item.get("type") != "eventQuest":
            continue
        event = Event(
            int(item["id"]),
            _flat(item["name"]),
            float(item["startedAt"]),
            float(item["endedAt"]),
            tuple(int(i) for i in item.get("warIds") or ()),
        )
        if (event.name, event.start) in seen:
            continue
        seen.add((event.name, event.start))
        events.append(event)
    return events


def parse_banners(raw: Iterable[dict[str, Any]]) -> list[Banner]:
    return [
        Banner(
            int(item["id"]),
            _flat(item["name"]),
            float(item["openedAt"]),
            float(item["closedAt"]),
            tuple(int(i) for i in item.get("featuredSvtIds") or ()),
            int(item.get("imageId") or 0),
        )
        for item in raw
        if item.get("type") in BANNER_TYPES
    ]


def needs_real_end(event: Event, ts: float) -> bool:
    """Listed as running for years, started recently and has quests: worth asking its quests when it
    really ends. Story chapters started long ago are left alone."""
    return event.end - event.start > LONG and event.start <= ts < event.start + LONG and bool(event.war_ids)


def quest_end(war: dict[str, Any], start: float) -> float | None:
    """When an event's quests really close: the latest quest close time within LONG of its start.
    None when every quest stays open for good (permanent content)."""
    ends = [
        float(quest["closedAt"])
        for spot in war.get("spots") or ()
        for quest in spot.get("quests") or ()
        if start < float(quest.get("closedAt") or 0) <= start + LONG
    ]
    return max(ends) if ends else None


def _class_label(class_name: str) -> str:
    """'alterEgo' -> 'Alter Ego'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", class_name).title()


def parse_servants(raw: Iterable[dict[str, Any]]) -> dict[int, Servant]:
    """English names. Names several servants share (Altria Pendragon, Gilgamesh, ...) get their class."""
    items = [item for item in raw if item.get("type") in ("normal", "heroine")]
    shared = {name for name, n in Counter(item["name"] for item in items).items() if n > 1}
    servants = {}
    for item in items:
        name = item["name"]
        if name in shared:
            label = _class_label(item["className"])
            name = f"{name[:-1]}, {label})" if name.endswith(")") else f"{name} ({label})"
        servants[int(item["id"])] = Servant(name, int(item.get("rarity") or 0))
    return servants


def _shared_start(a: str, b: str) -> str:
    return os.path.commonprefix([a, b])


def _clean_title(title: str) -> str:
    return title.strip(" 　・･-:：")


def group_banners(banners: Iterable[Banner]) -> list[Group]:
    """Banners ending at the same time with names that start the same way form one group, titled by
    the shared start of their names (the campaign name: "復刻 Fate/Samurai Remnant×Fate/Grand Order ...").
    """
    by_end: dict[float, list[Banner]] = defaultdict(list)
    for banner in banners:
        by_end[banner.end].append(banner)
    groups = []
    for end, members in by_end.items():
        clusters: list[tuple[str, list[Banner]]] = []
        for banner in sorted(members, key=lambda b: b.id):
            for i, (title, cluster) in enumerate(clusters):
                shared = _shared_start(title, banner.name)
                if len(shared) >= MIN_SHARED:
                    clusters[i] = (shared, [*cluster, banner])
                    break
            else:
                clusters.append((banner.name, [banner]))
        for title, cluster in clusters:
            if len(cluster) == 1:
                title = _BANNER_SUFFIX.sub("", title)
            groups.append(Group(_clean_title(title) or title, end, tuple(cluster)))
    return groups


# --- what's due -------------------------------------------------------------------------------


def _ending_phase(start: float, end: float, ts: float) -> str | None:
    """Which reminder is open right now, if any. Short items skip the ones that would land on their start."""
    duration, left = end - start, end - ts
    if duration > LONG or left <= 0:
        return None
    if left <= DAY and duration > 2 * DAY:
        return LAST
    if left <= 2 * DAY and duration > 4 * DAY:
        return SOON
    return None


def _ending_marks(key: str, phase: str | None) -> list[Mark]:
    """Recording LAST also records SOON: once it's the last day, "2 days left" is old news."""
    if phase is None:
        return []
    return [(key, SOON), (key, LAST)] if phase == LAST else [(key, SOON)]


def _is_fresh(start: float, end: float, ts: float) -> bool:
    return 0 <= ts - start < FRESH and ts < end


def due(
    events: list[Event],
    groups: list[Group],
    servants: dict[int, Servant],
    ts: float,
    done: set[Mark],
) -> list[Notice]:
    """Everything that should be posted now and wasn't yet: starts first, then reminders."""
    starts: list[Notice] = []
    endings: list[tuple[float, Notice]] = []

    for event in sorted(events, key=lambda e: e.start):
        phase = _ending_phase(event.start, event.end, ts)
        if _is_fresh(event.start, event.end, ts) and (event.key, START) not in done:
            # a late start post already says when it ends, so an open reminder is covered too
            starts.append(start_notice(event, [(event.key, START), *_ending_marks(event.key, phase)]))
        elif phase and (event.key, phase) not in done:
            endings.append((event.end, Notice(event_ending(event, phase), _ending_marks(event.key, phase))))

    event_ends = [e.end for e in events if e.end - e.start <= LONG]
    for group in sorted(groups, key=lambda g: min(b.start for b in g.banners)):
        new = [b for b in group.banners if _is_fresh(b.start, b.end, ts) and (b.key, START) not in done]
        if new:
            marks = [(b.key, START) for b in new]
            for b in new:
                marks += _ending_marks(b.key, _ending_phase(b.start, b.end, ts))
            starts.append(banner_notice(group, new, servants, marks))
            continue
        if any(abs(group.end - end) <= SAME_END for end in event_ends):
            continue
        opened = [b for b in group.banners if b.start <= ts]
        phases = {b: _ending_phase(b.start, b.end, ts) for b in opened}
        waiting = [b for b, phase in phases.items() if phase and (b.key, phase) not in done]
        if waiting:
            phase = LAST if any(phases[b] == LAST for b in waiting) else SOON
            marks = [m for b in waiting for m in _ending_marks(b.key, phases[b])]
            endings.append((group.end, Notice(banner_ending(group, phase, len(opened)), marks)))

    return starts + [notice for _, notice in sorted(endings, key=lambda pair: pair[0])]


def live(events: list[Event], groups: list[Group], servants: dict[int, Servant], ts: float) -> list[Notice]:
    """Everything running right now, posted or not (the /settings preview). Long-running content is left
    out: it would be the whole story mode."""
    notices = [
        start_notice(event)
        for event in sorted(events, key=lambda e: e.end)
        if event.start <= ts < event.end and event.end - event.start <= LONG
    ]
    for group in sorted(groups, key=lambda g: g.end):
        opened = [b for b in group.banners if b.start <= ts < b.end]
        if opened and group.end - min(b.start for b in opened) <= LONG:
            notices.append(banner_notice(group, opened, servants))
    return notices


def start_notice(event: Event, marks: list[Mark] | None = None) -> Notice:
    return Notice(event_start(event), marks or [], event_id=event.id)


def banner_notice(
    group: Group, new: list[Banner], servants: dict[int, Servant], marks: list[Mark] | None = None
) -> Notice:
    art = next((b.art for b in sorted(new, key=lambda b: b.id) if b.art), None)
    return Notice(banner_start(group, new, servants), marks or [], image=art)


# --- text -------------------------------------------------------------------------------------


def _ends(end: float) -> str:
    stamp = int(end)
    return strings.line("news.ends", when=f"<t:{stamp}:f>", left=f"<t:{stamp}:R>")


def _event_link(event: Event) -> str:
    name = event.name.replace("[", "(").replace("]", ")")
    return f"[{name}](<{EVENT_PAGE.format(id=event.id)}>)"


def event_start(event: Event) -> str:
    if event.end - event.start > LONG:
        return strings.line("news.new_content", name=_event_link(event))
    key = "news.rerun_start" if RERUN in event.name else "news.event_start"
    return strings.line(key, name=_event_link(event)) + "\n" + _ends(event.end)


def event_ending(event: Event, phase: str) -> str:
    key = "news.last_day" if phase == LAST else "news.two_days"
    return strings.line(key, name=_event_link(event)) + "\n" + _ends(event.end)


def pickups(banners: Iterable[Banner], servants: dict[int, Servant]) -> str:
    """'5★ Yamato Takeru, Jeanne d'Arc (Ruler) · 4★ ...', strongest first, at most MAX_PICKUPS names."""
    ids = list(dict.fromkeys(i for b in banners for i in b.pickups if i in servants))
    ids.sort(key=lambda i: -servants[i].rarity)
    shown, rest = ids[:MAX_PICKUPS], len(ids) - MAX_PICKUPS
    by_rarity: dict[int, list[str]] = defaultdict(list)
    for i in shown:
        by_rarity[servants[i].rarity].append(servants[i].name)
    text = " · ".join(f"{rarity}★ {', '.join(names)}" for rarity, names in by_rarity.items())
    return f"{text} · +{rest} more" if rest > 0 else text


def banner_start(group: Group, new: list[Banner], servants: dict[int, Servant]) -> str:
    key = "news.rerun_banner" if RERUN in group.title else "news.banner"
    lines = [strings.line(key, title=group.title)]
    names = pickups(new, servants)
    if names:
        lines.append(names)
    lines.append(_ends(group.end))
    return "\n".join(lines)


def banner_ending(group: Group, phase: str, count: int) -> str:
    key = "news.last_day" if phase == LAST else "news.two_days"
    banners = "banner" if count == 1 else f"banners ({count})"
    name = strings.line("news.banner_group", title=group.title, banners=banners)
    return strings.line(key, name=name) + "\n" + _ends(group.end)


def messages(notices: list[Notice]) -> list[Notice]:
    """Notices packed into as few Discord messages as fit, a blank line between items. An item with a
    picture gets a message of its own, so each picture sits right under its own text."""
    packed: list[Notice] = []
    for notice in notices:
        last = packed[-1] if packed else None
        if (
            last is not None
            and last.image is None
            and notice.image is None
            and len(last.text) + 2 + len(notice.text) <= MAX_MESSAGE
        ):
            last.text += "\n\n" + notice.text
            last.marks.extend(notice.marks)
        else:  # MAX_PICKUPS keeps one item well under MAX_MESSAGE
            packed.append(Notice(notice.text, list(notice.marks), image=notice.image))
    return packed
