"""Health checks: probe parsing, hysteresis, alert planning, state persistence. No network, no Discord."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from bb import strings
from bb.cogs import health
from bb.cogs.health import (
    Alert,
    Health,
    backup_for,
    chunk_lines,
    down_ids,
    plan_alerts,
    probe_passed,
    step,
)
from bb.db import HealthRow
from bb.links import sites
from bb.links.model import Fixer, Site
from bb.settings_store import SettingsStore

FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True)
VX = Fixer("vxtwitter", "vxTwitter", "vxtwitter.com")
FIXUP = Fixer("fixupx", "FixupX", "fixupx.com")
PHIX = Fixer("phixiv", "phixiv", "phixiv.net")
TWITTER = Site("twitter", "Twitter", "Tweet", (FX, VX, FIXUP), "https://x.com/i/status/1")
PIXIV = Site("pixiv", "Pixiv", "Pixiv", (PHIX,), "https://www.pixiv.net/artworks/1")
TABLE = {"twitter": TWITTER, "pixiv": PIXIV}


@pytest.fixture
def site_table(monkeypatch):
    monkeypatch.setattr(sites, "SITES", TABLE)
    return TABLE


# --- probe parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '<meta property="og:title" content="hi">',
        "<meta name='twitter:card' content='summary_large_image'>",
        '<META CONTENT="x" PROPERTY="OG:IMAGE">',
        "<meta\n  content=x\n  property=og:video>",
    ],
)
def test_probe_passes_on_preview_tags(body):
    assert probe_passed(200, f"<html><head>{body}</head></html>")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, '<meta property="og:title" content="gone">'),
        (500, '<meta property="og:title" content="x">'),
        (200, '<meta name="description" content="og: twitter: in the text">'),
        (200, "<p>og:title twitter:card</p>"),
        (200, ""),
    ],
)
def test_probe_fails(status, body):
    assert not probe_passed(status, body)


# --- hysteresis -----------------------------------------------------------------------------


def test_unknown_fixer_starts_healthy_and_survives_one_failure():
    row, transition = step(None, "fxtwitter", False, 100.0)
    assert (row.healthy, row.consecutive_failures, transition) == (True, 1, None)
    assert row.last_checked == 100.0


def test_two_failures_go_down_one_success_comes_back():
    row, _ = step(None, "fx", False, 1.0)
    row, transition = step(row, "fx", False, 2.0)
    assert (row.healthy, row.consecutive_failures, transition, row.last_change) == (False, 2, "down", 2.0)
    row, transition = step(row, "fx", False, 3.0)
    assert (row.healthy, row.consecutive_failures, transition, row.last_change) == (False, 3, None, 2.0)
    row, transition = step(row, "fx", True, 4.0)
    assert (row.healthy, row.consecutive_failures, transition, row.last_change) == (True, 0, "up", 4.0)


def test_success_resets_the_failure_count():
    row, _ = step(None, "fx", False, 1.0)
    row, _ = step(row, "fx", True, 2.0)
    row, transition = step(row, "fx", False, 3.0)
    assert (row.healthy, row.consecutive_failures, transition) == (True, 1, None)


def test_down_ids_ignores_unconfigured_fixers():
    rows = [HealthRow("a", False, 2, 0, 0), HealthRow("b", True, 0, 0, 0), HealthRow("gone", False, 5, 0, 0)]
    assert down_ids(rows) == {"a", "gone"}
    assert down_ids(rows, {"a", "b"}) == {"a"}


# --- alerts ---------------------------------------------------------------------------------


def test_backup_is_first_healthy_in_configured_order():
    assert backup_for(TWITTER, {"fxtwitter"}) is VX
    assert backup_for(TWITTER, {"vxtwitter"}) is FX
    assert backup_for(TWITTER, {"fxtwitter", "vxtwitter", "fixupx"}) is None


def test_fixer_down_names_backup():
    alerts = plan_alerts(TABLE, went_down={"fxtwitter"}, came_up=set(), down={"fxtwitter"})
    values = {"fixer": "FxTwitter", "site": "Twitter", "backup": "vxTwitter"}
    assert alerts == [Alert("alert.fixer_down", values)]


def test_backup_accounts_for_fixers_failing_in_the_same_round():
    alerts = plan_alerts(TABLE, {"fxtwitter", "vxtwitter"}, set(), {"fxtwitter", "vxtwitter"})
    assert [a.values["backup"] for a in alerts] == ["FixupX", "FixupX"]


def test_site_down_is_one_alert_when_nothing_is_left():
    alerts = plan_alerts(TABLE, {"vxtwitter", "fixupx"}, set(), {"fxtwitter", "vxtwitter", "fixupx"})
    assert alerts == [Alert("alert.site_down", {"site": "Twitter"})]


def test_fixer_up_and_quiet_rounds():
    assert plan_alerts(TABLE, set(), {"phixiv"}, set()) == [Alert("alert.fixer_up", {"fixer": "phixiv"})]
    assert plan_alerts(TABLE, set(), set(), {"fxtwitter"}) == []


def test_every_planned_alert_renders():
    alerts = [
        *plan_alerts(TABLE, {"fxtwitter"}, set(), {"fxtwitter"}),
        *plan_alerts(TABLE, {"phixiv"}, set(), {"phixiv"}),
        *plan_alerts(TABLE, set(), {"phixiv"}, set()),
    ]
    assert {a.key for a in alerts} == {"alert.fixer_down", "alert.site_down", "alert.fixer_up"}
    for alert in alerts:
        for variant in strings.variants(alert.key):
            assert strings.fill(variant, **alert.values)


def test_alert_lines_are_joined_within_discord_limit():
    assert chunk_lines(["a", "b"]) == ["a\nb"]
    assert chunk_lines(["x" * 6, "y" * 6], limit=10) == ["x" * 6, "y" * 6]
    assert chunk_lines([]) == []


# --- cog state (fake bot, real in-memory DB) ------------------------------------------------


def _bot(db, guilds=()):
    return SimpleNamespace(db=db, settings=SettingsStore(db), down_fixers=set(), guilds=list(guilds))


async def test_load_state_seeds_down_fixers(db, site_table):
    await db.set_fixer_health(HealthRow("fxtwitter", False, 2, 1.0, 1.0))
    await db.set_fixer_health(HealthRow("vxtwitter", True, 1, 1.0, 0.0))
    await db.set_fixer_health(HealthRow("retired", False, 9, 1.0, 0.0))
    bot = _bot(db)
    shared = bot.down_fixers
    await Health(bot).load_state()
    assert bot.down_fixers == {"fxtwitter"}
    assert bot.down_fixers is shared  # updated in place


async def test_record_persists_and_alerts_on_transitions(db, site_table):
    bot = _bot(db)
    cog = Health(bot)
    results = {"fxtwitter": False, "vxtwitter": True, "fixupx": True, "phixiv": True}
    assert await cog.record(results, 10.0) == []
    assert bot.down_fixers == set()

    alerts = await cog.record(results, 20.0)
    assert [a.key for a in alerts] == ["alert.fixer_down"]
    assert alerts[0].values["backup"] == "vxTwitter"
    assert bot.down_fixers == {"fxtwitter"}

    # A restart reads the same state back.
    restarted = _bot(db)
    await Health(restarted).load_state()
    assert restarted.down_fixers == {"fxtwitter"}

    alerts = await cog.record({**results, "fxtwitter": True}, 30.0)
    assert alerts == [Alert("alert.fixer_up", {"fixer": "FxTwitter"})]
    assert bot.down_fixers == set()
    saved = (await db.get_fixer_health())["fxtwitter"]
    assert (saved.healthy, saved.consecutive_failures, saved.last_change) == (True, 0, 30.0)


class _Channel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


async def test_broadcast_goes_to_alert_channels_only(db):
    channel = _Channel()
    with_alerts = SimpleNamespace(id=1, get_channel_or_thread=lambda cid: channel if cid == 99 else None)
    without = SimpleNamespace(id=2, get_channel_or_thread=lambda cid: pytest.fail("no alerts channel set"))
    bot = _bot(db, [with_alerts, without])
    await bot.settings.update(1, alerts_channel_id=99)
    await Health(bot).broadcast("hello")
    assert channel.sent == ["hello"]


async def test_run_once_without_sites_does_nothing(db, monkeypatch):
    monkeypatch.setattr(sites, "SITES", {})
    bot = _bot(db)
    await Health(bot).run_once()
    assert await db.get_fixer_health() == {}


# --- live (pytest -m live) ------------------------------------------------------------------


@pytest.mark.live
async def test_every_fixer_passes_live():
    if not sites.SITES:
        pytest.skip("site table is empty")
    targets = health.probe_targets(sites.SITES)
    assert targets, "no probe targets"
    async with aiohttp.ClientSession() as session:
        failed = [f"{fixer.id}: {url}" for _, fixer, url in targets if not await health.probe(session, url)]
    assert not failed, "fixers failing their probe:\n" + "\n".join(failed)


def test_all_probes_failing_means_bb_is_offline():
    from bb.cogs.health import looks_offline

    assert looks_offline({"a": False, "b": False, "c": False})
    assert not looks_offline({"a": False, "b": True, "c": False})
    assert not looks_offline({"a": False})  # too few probes to tell
