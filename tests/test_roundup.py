"""Weekly roundup: scheduling, content, and once-per-week posting. No Discord connection."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

from bb import strings
from bb.cogs.roundup import (
    BB_PURPLE,
    GRACE,
    WEEK,
    Roundup,
    build_embed,
    latest_slot,
    roundup_content,
    roundup_due,
)
from bb.links import sites
from bb.links.model import Site
from bb.settings_store import SettingsStore

SUNDAY, HOUR = 6, 18


def ts(year, month, day, hour, minute=0, tz="UTC"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


SLOT = ts(2026, 10, 11, 18)  # Sunday 18:00 UTC


@pytest.fixture
def site_table(monkeypatch):
    table = {
        "twitter": Site("twitter", "Twitter", "Tweet", (), ""),
        "pixiv": Site("pixiv", "Pixiv", "Pixiv", (), ""),
    }
    monkeypatch.setattr(sites, "SITES", table)
    return table


# --- scheduling -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now_ts", "expected"),
    [
        (ts(2026, 10, 13, 12), SLOT),  # Tuesday: last Sunday
        (SLOT, SLOT),  # exactly on time
        (ts(2026, 10, 11, 17, 59), ts(2026, 10, 4, 18)),  # Sunday, an hour early: the week before
        (ts(2026, 10, 17, 23, 59), SLOT),  # Saturday night
    ],
)
def test_latest_slot_utc(now_ts, expected):
    assert latest_slot(now_ts, "UTC", SUNDAY, HOUR) == expected


def test_latest_slot_uses_server_timezone():
    # 18:00 in Berlin (CEST, UTC+2) is 16:00 UTC.
    assert latest_slot(ts(2026, 10, 11, 17), "Europe/Berlin", SUNDAY, HOUR) == ts(2026, 10, 11, 16)
    # Unknown zones fall back to UTC instead of crashing.
    assert latest_slot(ts(2026, 10, 13, 12), "Not/AZone", SUNDAY, HOUR) == SLOT


def test_latest_slot_keeps_local_hour_across_dst():
    # New York leaves DST on 2026-11-01. Both slots are 18:00 local, but 22:00 vs 23:00 UTC.
    assert latest_slot(ts(2026, 11, 1, 23, 30), "America/New_York", SUNDAY, HOUR) == ts(2026, 11, 1, 23)
    assert latest_slot(ts(2026, 11, 1, 22, 30), "America/New_York", SUNDAY, HOUR) == ts(2026, 10, 25, 22)


@pytest.mark.parametrize(
    ("now_ts", "last_sent", "due"),
    [
        (SLOT + 300, 0.0, True),  # first ever roundup, on time
        (SLOT + 300, SLOT - WEEK + 120, True),  # last week's went out, this week's hasn't
        (SLOT + GRACE - 1, 0.0, True),  # late but within grace (bot restarted)
        (SLOT + GRACE + 1, 0.0, False),  # missed the slot by too much: skip the week
        (SLOT - 60, 0.0, False),  # not yet
        (SLOT + 600, SLOT + 300, False),  # already sent this week (restart must not double-post)
        (SLOT + 300, SLOT - 3600, False),  # sent an hour before the slot moved: not twice in a day
        (ts(2026, 10, 13, 12), 0.0, False),  # channel set up mid-week: wait for the next slot
    ],
)
def test_roundup_due(now_ts, last_sent, due):
    assert roundup_due(now_ts, "UTC", SUNDAY, HOUR, last_sent) is due


# --- content --------------------------------------------------------------------------------


def test_full_roundup(site_table):
    lines = roundup_content([(7, "Gudako", 12)], [("pixiv", 9)], [(8, "Ritsuka", 3)], brainlet_award=True)
    assert lines == [
        ("roundup.top_poster", {"user": "**Gudako**", "n": 12}),
        ("roundup.top_site", {"site": "Pixiv"}),
        ("roundup.brainlet", {"user": "**Ritsuka**", "times": "3 times"}),
    ]


def test_quiet_week(site_table):
    assert roundup_content([], [], [], brainlet_award=True) == [("roundup.quiet", {})]


def test_brainlet_needs_award_and_offenders(site_table):
    posters, top = [(7, "a", 1)], [("twitter", 1)]
    keys = [k for k, _ in roundup_content(posters, top, [(8, "b", 2)], brainlet_award=False)]
    assert "roundup.brainlet" not in keys
    keys = [k for k, _ in roundup_content(posters, top, [], brainlet_award=True)]
    assert "roundup.brainlet" not in keys


def test_names_are_escaped_and_unknown_sites_fall_back_to_id(monkeypatch):
    monkeypatch.setattr(sites, "SITES", {})
    lines = dict(roundup_content([(7, "snake_case*", 1)], [("bluesky", 1)], [], brainlet_award=True))
    assert lines["roundup.top_poster"]["user"] == r"**snake\_case\***"
    assert lines["roundup.top_site"]["site"] == "bluesky"


def test_embed(site_table):
    lines = roundup_content([(7, "Gudako", 12)], [("twitter", 9)], [], brainlet_award=True)
    embed = build_embed(lines, SLOT)
    assert embed.colour == BB_PURPLE
    assert embed.title in strings.variants("roundup.title")
    assert "**Gudako**" in embed.description and "Twitter" in embed.description
    assert embed.timestamp == datetime.fromtimestamp(SLOT, UTC)


# --- posting (fake guild, real in-memory DB) ------------------------------------------------


class _Channel:
    def __init__(self, error: Exception | None = None):
        self.embeds: list[discord.Embed] = []
        self.error = error

    async def send(self, *, embed, **kwargs):
        if self.error:
            raise self.error
        self.embeds.append(embed)


def _guild(channel, channel_id=55):
    async def fetch_channel(cid):
        pytest.fail("cached channel should be used")

    return SimpleNamespace(
        id=1,
        get_channel_or_thread=lambda cid: channel if cid == channel_id else None,
        fetch_channel=fetch_channel,
    )


def _bot(db):
    return SimpleNamespace(db=db, settings=SettingsStore(db), guilds=[])


async def test_posts_once_and_survives_restart(db, site_table):
    await db.insert_post(
        guild_id=1,
        channel_id=5,
        message_id=1,
        author_id=7,
        author_name="Gudako",
        site="twitter",
        canonical_key="twitter:1",
        created_at=SLOT - 3600,
    )
    bot = _bot(db)
    await bot.settings.update(1, roundup_channel_id=55)
    channel = _Channel()
    assert await Roundup(bot).maybe_post(_guild(channel), SLOT + 60) is True
    assert len(channel.embeds) == 1 and "**Gudako**" in channel.embeds[0].description
    assert (await bot.settings.get(1)).roundup_last_sent == SLOT + 60

    restarted = _bot(db)
    assert await Roundup(restarted).maybe_post(_guild(channel), SLOT + 360) is False
    assert len(channel.embeds) == 1


async def test_no_channel_means_no_roundup(db):
    bot = _bot(db)
    channel = _Channel()
    assert await Roundup(bot).maybe_post(_guild(channel), SLOT + 60) is False
    assert channel.embeds == []


async def test_forbidden_skips_the_week_but_server_errors_retry(db, site_table):
    bot = _bot(db)
    await bot.settings.update(1, roundup_channel_id=55)
    response = SimpleNamespace(status=500, reason="Server Error")
    flaky = _Channel(discord.HTTPException(response, "boom"))
    assert await Roundup(bot).maybe_post(_guild(flaky), SLOT + 60) is False
    assert (await bot.settings.get(1)).roundup_last_sent == 0.0

    forbidden = _Channel(discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    assert await Roundup(bot).maybe_post(_guild(forbidden), SLOT + 120) is False
    assert (await bot.settings.get(1)).roundup_last_sent == SLOT + 120


def test_times_reads_naturally():
    from bb.cogs.roundup import times

    assert [times(n) for n in (1, 2, 3)] == ["once", "twice", "3 times"]
