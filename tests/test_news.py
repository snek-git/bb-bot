"""FGO JP news: parsing Atlas exports, grouping banners, what's due, and posting. No network, no Discord."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import discord

from bb import news
from bb.cogs.news import Data, News
from bb.news import DAY, LAST, SOON, START
from bb.settings_store import SettingsStore

T0 = 1_800_000_000.0  # some Tuesday


def event(id, name, start, end, type="eventQuest"):
    return {"id": id, "type": type, "name": name, "startedAt": start, "endedAt": end}


def banner(id, name, start, end, pickups=(), type="stone"):
    return {"id": id, "type": type, "name": name, "openedAt": start, "closedAt": end,
            "featuredSvtIds": list(pickups)}


def servant(id, name, rarity=5, class_name="saber", type="normal"):
    return {"id": id, "type": type, "name": name, "rarity": rarity, "className": class_name}


SERVANTS = news.parse_servants([
    servant(1, "Yamato Takeru"),
    servant(2, "Jeanne d'Arc", class_name="ruler"),
    servant(3, "Jeanne d'Arc", class_name="archer"),
    servant(4, "Yui Shousetsu", rarity=4),
    servant(5, "Jeanne d'Arc (Alter)", class_name="avenger"),
    servant(6, "Jeanne d'Arc (Alter)", class_name="berserker"),
])

COLLAB = "復刻 Fate/Samurai Remnant×Fate/Grand Order\nコラボレーションイベント \n"


def texts(notices):
    return [n.text for n in notices]


# --- parsing --------------------------------------------------------------------------------


def test_events_keep_event_quests_once():
    events = news.parse_events([
        event(1, "Halloween\n2026", T0, T0 + 20 * DAY),
        event(2, "Halloween 2026", T0, T0 + 20 * DAY),  # the same event listed twice
        event(3, "AP Cost 1/2", T0, T0 + 20 * DAY, type="questCampaign"),
    ])
    assert [(e.id, e.name) for e in events] == [(1, "Halloween 2026")]


def test_shared_servant_names_get_their_class():
    assert SERVANTS[1].name == "Yamato Takeru"
    assert SERVANTS[2].name == "Jeanne d'Arc (Ruler)"
    assert SERVANTS[5].name == "Jeanne d'Arc (Alter, Avenger)"
    assert news.parse_servants([servant(9, "Kiara", class_name="moonCancer")])[9].name == "Kiara"
    assert news._class_label("alterEgo") == "Alter Ego"


def test_friend_point_banners_are_not_news():
    banners = news.parse_banners([
        banner(1, "Quartz", T0, T0 + DAY),
        banner(2, "Lucky bag", T0, T0 + DAY, type="chargeStone"),
        banner(3, "FP", T0, T0 + DAY, type="friendPoint"),
    ])
    assert [b.id for b in banners] == [1, 2]


def test_banners_group_by_campaign():
    end = T0 + 20 * DAY
    groups = news.group_banners(news.parse_banners([
        banner(10, COLLAB + "ヤマトタケルピックアップ召喚", T0, end, [1]),
        banner(11, COLLAB + "ジャンヌ･ダルクピックアップ召喚", T0 + 2 * DAY, end, [2]),  # opens later
        banner(12, "京まふ2026出展記念 \nモレーピックアップ召喚", T0, end, [4]),  # same end, other campaign
        banner(13, "「冠位名代戦」 タマモアリアピックアップ召喚", T0, T0 + 30 * DAY, [1]),
    ]))
    by_title = {g.title: [b.id for b in g.banners] for g in groups}
    assert by_title == {
        "復刻 Fate/Samurai Remnant×Fate/Grand Order コラボレーションイベント": [10, 11],
        "京まふ2026出展記念 モレー": [12],
        "「冠位名代戦」 タマモアリア": [13],
    }


# --- what's due -----------------------------------------------------------------------------


def run(events=(), banners=(), ts=T0, done=frozenset()):
    return news.due(
        news.parse_events(events), news.group_banners(news.parse_banners(banners)), SERVANTS, ts, set(done)
    )


def test_new_event_is_posted_with_its_end_and_link():
    (notice,) = run([event(80632, "復刻版:盈月剣風帖", T0 - 600, T0 + 20 * DAY)])
    assert notice.text.startswith("**Rerun started** · [復刻版:盈月剣風帖]")
    assert "(<https://apps.atlasacademy.io/db/JP/event/80632>)" in notice.text
    assert f"<t:{int(T0 + 20 * DAY)}:R>" in notice.text
    assert notice.marks == [("event:80632", START)]


def test_posted_and_stale_starts_are_not_posted():
    ev = event(1, "Halloween", T0 - 600, T0 + 20 * DAY)
    assert run([ev], done={("event:1", START)}) == []
    assert run([ev], ts=T0 + 3 * DAY) == []  # BB was offline too long: no news days late
    assert run([event(2, "Future", T0 + 600, T0 + 20 * DAY)]) == []  # datamined, not started yet


def test_long_content_is_announced_without_reminders():
    grand = event(80637, "冠位名代戦", T0 - 600, T0 + 1000 * DAY)
    (notice,) = run([grand])
    assert notice.text == "**Now open** · [冠位名代戦](<https://apps.atlasacademy.io/db/JP/event/80637>)"
    assert run([grand], ts=T0 + 999.5 * DAY, done={("event:80637", START)}) == []


def test_reminders_two_days_then_last_day():
    ev = event(1, "Halloween", T0, T0 + 14 * DAY)
    done = {("event:1", START)}
    assert run([ev], ts=T0 + 11 * DAY, done=done) == []
    (soon,) = run([ev], ts=T0 + 12 * DAY + 60, done=done)
    assert soon.text.startswith("**2 days left** · [Halloween]") and soon.marks == [("event:1", SOON)]
    done.add(("event:1", SOON))
    assert run([ev], ts=T0 + 12.5 * DAY, done=done) == []
    (last,) = run([ev], ts=T0 + 13 * DAY + 60, done=done)
    assert last.text.startswith("**Last day** · ") and ("event:1", LAST) in last.marks
    assert run([ev], ts=T0 + 14 * DAY + 60, done=done | {("event:1", LAST)}) == []


def test_missed_reminder_jumps_to_last_day_and_records_both():
    (last,) = run([event(1, "Halloween", T0, T0 + 14 * DAY)], ts=T0 + 13.5 * DAY, done={("event:1", START)})
    assert last.text.startswith("**Last day**") and set(last.marks) == {("event:1", SOON), ("event:1", LAST)}


def test_short_events_skip_reminders_that_land_on_their_start():
    ev = event(1, "Two-day thing", T0, T0 + 2 * DAY)
    (start,) = run([ev], ts=T0 + 60)
    assert start.marks == [("event:1", START)]
    assert run([ev], ts=T0 + 1.5 * DAY, done={("event:1", START)}) == []


def test_late_start_post_covers_the_open_reminder():
    (start,) = run([event(1, "Halloween", T0, T0 + 2.5 * DAY)], ts=T0 + 1.8 * DAY)  # last day already
    assert start.text.startswith("**Event started**")
    assert set(start.marks) == {("event:1", START), ("event:1", SOON), ("event:1", LAST)}


def test_new_banner_lists_pickups_by_rarity():
    name = COLLAB + "ヤマトタケルピックアップ召喚"
    (notice,) = run(banners=[banner(10, name, T0, T0 + 20 * DAY, [4, 1, 2])])
    lines = notice.text.split("\n")
    # a banner alone keeps its whole name, minus the "pickup summon" ending
    assert lines[0] == "**Rerun banner** · " + " ".join(name.split()).removesuffix("ピックアップ召喚")
    assert lines[1] == "5★ Yamato Takeru, Jeanne d'Arc (Ruler) · 4★ Yui Shousetsu"
    assert lines[2].startswith("-# Ends <t:")


def test_banners_opening_later_post_only_the_new_pickups():
    end = T0 + 20 * DAY
    banners = [
        banner(10, COLLAB + "ヤマトタケルピックアップ召喚", T0, end, [1]),
        banner(11, COLLAB + "ジャンヌ･ダルクピックアップ召喚", T0 + 2 * DAY, end, [2]),
    ]
    (first,) = run(banners=banners)
    assert "Yamato Takeru" in first.text and "Jeanne" not in first.text
    assert first.marks == [("banner:10", START)]
    (second,) = run(banners=banners, ts=T0 + 2 * DAY + 60, done={("banner:10", START)})
    assert "Jeanne d'Arc (Ruler)" in second.text and "Yamato" not in second.text


def test_many_pickups_are_cut_with_a_count():
    ids = list(range(100, 115))
    servants = news.parse_servants([servant(i, f"S{i}") for i in ids])
    bn = news.parse_banners([banner(1, "Grand prep", T0, T0 + DAY, ids)])
    assert news.pickups(bn, servants).endswith(" · +5 more")


def test_banners_ending_with_an_event_get_no_reminder_of_their_own():
    end = T0 + 14 * DAY
    done = {("event:1", START), ("banner:10", START)}
    notices = run([event(1, "Collab", T0, end)], [banner(10, "Collab banner", T0, end, [1])],
                  ts=T0 + 13 * DAY + 60, done=done)
    assert [n.marks[0][0] for n in notices] == ["event:1"]


def test_banner_group_reminder_counts_its_banners():
    end = T0 + 20 * DAY
    banners = [banner(10, "「冠位名代戦」準備応援 Aピックアップ召喚", T0, end, [1]),
               banner(11, "「冠位名代戦」準備応援 Bピックアップ召喚", T0, end, [2])]
    done = {("banner:10", START), ("banner:11", START)}
    (soon,) = run(banners=banners, ts=T0 + 18 * DAY + 60, done=done)
    assert soon.text.startswith("**2 days left** · 「冠位名代戦」準備応援 banners (2)")
    assert set(soon.marks) == {("banner:10", SOON), ("banner:11", SOON)}
    single = [banner(12, "「冠位名代戦」 タマモアリアピックアップ召喚", T0, end, [1])]
    (one,) = run(banners=single, ts=T0 + 18 * DAY + 60, done={("banner:12", START)})
    assert one.text.startswith("**2 days left** · 「冠位名代戦」 タマモアリア banner\n")


def test_starts_come_before_reminders():
    notices = run(
        [event(1, "Ending", T0 - 13 * DAY, T0 + 0.5 * DAY), event(2, "Starting", T0 - 60, T0 + 14 * DAY)],
        done={("event:1", START)},
    )
    assert [n.text.split("\n")[0].split("**")[1] for n in notices] == ["Event started", "Last day"]


def test_live_shows_what_is_running_except_long_content():
    notices = news.live(
        news.parse_events([event(1, "Halloween", T0 - 5 * DAY, T0 + 5 * DAY),
                           event(2, "Story", T0 - 500 * DAY, T0 + 500 * DAY),
                           event(3, "Over", T0 - 9 * DAY, T0 - DAY)]),
        news.group_banners(news.parse_banners([banner(10, "Halloween banner", T0 - DAY, T0 + 5 * DAY, [1])])),
        SERVANTS,
        T0,
    )
    assert [t.split("\n")[0] for t in texts(notices)] == [
        "**Event started** · [Halloween](<https://apps.atlasacademy.io/db/JP/event/1>)",
        "**New banner** · Halloween banner",
    ]


def test_messages_pack_items_under_the_limit():
    notices = [news.Notice("x" * 900, [(f"event:{i}", START)]) for i in range(3)]
    packed = news.messages(notices)
    assert [len(m.text) for m in packed] == [1802, 900]
    assert packed[0].marks == [("event:0", START), ("event:1", START)]


def test_items_with_a_picture_get_their_own_message():
    packed = news.messages([
        news.Notice("a"), news.Notice("b"), news.Notice("c", image="https://x/c.png"), news.Notice("d"),
    ])
    assert [(m.text, m.image) for m in packed] == [("a\n\nb", None), ("c", "https://x/c.png"), ("d", None)]


def test_start_posts_carry_pictures_and_reminders_dont():
    end = T0 + 20 * DAY
    banners = [banner(10, COLLAB + "Aピックアップ召喚", T0, end, [1]) | {"imageId": 83838},
               banner(11, COLLAB + "Bピックアップ召喚", T0, end, [2]) | {"imageId": 83839}]
    ev = event(80632, "復刻版:盈月剣風帖", T0, end)
    first, second = run([ev], banners)
    assert first.event_id == 80632 and first.image is None  # the cog looks the event's banner up
    assert second.image == "https://static.atlasacademy.io/JP/SummonBanners/img_summon_83838.png"
    done = {("event:80632", START), ("banner:10", START), ("banner:11", START)}
    (reminder,) = run([ev], banners, ts=end - DAY + 60, done=done)
    assert reminder.image is None and reminder.event_id is None


# --- the cog (fake guild and bot, real in-memory DB) ----------------------------------------


class _Channel:
    def __init__(self, error: Exception | None = None):
        self.sent: list[str] = []
        self.embeds: list[discord.Embed | None] = []
        self.error = error

    async def send(self, content, **kwargs):
        assert kwargs["allowed_mentions"].everyone is False
        if self.error:
            raise self.error
        self.sent.append(content)
        self.embeds.append(kwargs.get("embed"))


class _Http:
    """Atlas as BB sees it: pages are JSON answers by URL, pictures the image URLs that exist."""

    def __init__(self, pages=None, pictures=()):
        self.pages, self.pictures, self.calls = pages or {}, set(pictures), []

    def get(self, url, headers=None):
        self.calls.append(("get", url))
        return _Response(200, self.pages[url]) if url in self.pages else _Response(404)

    def head(self, url):
        self.calls.append(("head", url))
        if url in self.pictures:
            return _Response(200, content_type="image/png")
        return _Response(404, content_type="application/json")


def _guild(channel, channel_id=55):
    async def fetch_channel(cid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")

    return SimpleNamespace(
        id=1,
        get_channel_or_thread=lambda cid: channel if cid == channel_id else None,
        fetch_channel=fetch_channel,
    )


def _cog(db, events=(), banners=(), http=None):
    cog = News(SimpleNamespace(db=db, settings=SettingsStore(db), guilds=[], http_client=http or _Http()))
    cog.data = Data(
        news.parse_events(events), news.group_banners(news.parse_banners(banners)), SERVANTS
    )
    return cog


HALLOWEEN = event(1, "Halloween", T0 - 600, T0 + 20 * DAY)


async def test_posts_once_and_survives_restart(db):
    cog = _cog(db, [HALLOWEEN], [banner(10, "Halloween banner", T0 - 600, T0 + 20 * DAY, [1])])
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 1
    (text,) = channel.sent
    assert "**Event started** · [Halloween]" in text and "**New banner** · Halloween banner" in text
    restarted = _cog(db, [HALLOWEEN])
    assert await restarted.post_due(_guild(channel), T0 + 900) == 0
    assert len(channel.sent) == 1


EVENT_ART = "https://static.atlasacademy.io/JP/Banner/event_war_80613.png"
BANNER_ART = "https://static.atlasacademy.io/JP/SummonBanners/img_summon_83838.png"


async def test_posts_show_pictures_under_their_text(db):
    http = _Http(
        pages={news.EVENT_API.format(id=1): {"banner": EVENT_ART}},
        pictures={EVENT_ART, BANNER_ART},
    )
    halloween_banner = banner(10, "Halloween banner", T0 - 600, T0 + 20 * DAY, [1]) | {"imageId": 83838}
    cog = _cog(db, [HALLOWEEN], [halloween_banner], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 2
    assert channel.sent[0].startswith("**Event started** · [Halloween]")
    assert channel.sent[1].startswith("**New banner** · Halloween banner")
    assert [e.image.url for e in channel.embeds] == [EVENT_ART, BANNER_ART]


async def test_missing_pictures_fall_back_to_text(db):
    # the event's page names a banner that doesn't exist (Atlas has some of those), the banner art is gone
    http = _Http(pages={news.EVENT_API.format(id=1): {"banner": EVENT_ART}})
    halloween_banner = banner(10, "Halloween banner", T0 - 600, T0 + 20 * DAY, [1]) | {"imageId": 83838}
    cog = _cog(db, [HALLOWEEN], [halloween_banner], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 1
    assert channel.embeds == [None] and "**New banner**" in channel.sent[0]


async def test_failed_event_lookups_are_tried_again(db):
    http = _Http()
    cog = _cog(db, http=http)
    assert await cog.event_art(1) is None
    http.pages[news.EVENT_API.format(id=1)] = {"banner": None, "noticeBanner": EVENT_ART}
    assert await cog.event_art(1) == EVENT_ART
    assert await cog.event_art(1) == EVENT_ART
    assert [c for c in http.calls if c[0] == "get"] == [("get", news.EVENT_API.format(id=1))] * 2


async def test_no_channel_paused_or_no_data_posts_nothing(db):
    channel = _Channel()
    cog = _cog(db, [HALLOWEEN])
    assert await cog.post_due(_guild(channel), T0) == 0
    await cog.bot.settings.update(1, news_channel_id=55, paused=True)
    assert await cog.post_due(_guild(channel), T0) == 0
    await cog.bot.settings.update(1, paused=False)
    cog.data = None
    assert await cog.post_due(_guild(channel), T0) == 0
    assert channel.sent == [] and await db.news_posted(1) == set()


async def test_server_errors_retry_but_forbidden_and_deleted_channels_skip(db):
    cog = _cog(db, [HALLOWEEN])
    await cog.bot.settings.update(1, news_channel_id=55)
    flaky = _Channel(discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom"))
    assert await cog.post_due(_guild(flaky), T0) == 0
    assert await db.news_posted(1) == set()

    forbidden = _Channel(discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
    assert await cog.post_due(_guild(forbidden), T0) == 0
    assert await db.news_posted(1) == {("event:1", START)}

    later = _cog(db, [event(2, "Next", T0 + DAY, T0 + 20 * DAY)])
    await later.post_due(_guild(_Channel(), channel_id=99), T0 + DAY + 60)  # the channel was deleted
    assert ("event:2", START) in await db.news_posted(1)


class _Response:
    def __init__(self, status, body=None, etag=None, content_type=None):
        self.status, self.body = status, body
        self.headers = {"ETag": etag} if etag else {}
        if content_type:
            self.headers["Content-Type"] = content_type

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(SimpleNamespace(real_url="x"), (), status=self.status)

    async def json(self, content_type=None):
        return self.body


async def test_refresh_reuses_unchanged_exports(db):
    bodies = {
        news.EVENTS_URL: [HALLOWEEN],
        news.BANNERS_URL: [banner(10, "Halloween banner", T0, T0 + DAY, [1])],
        news.SERVANTS_URL: [servant(1, "Yamato Takeru")],
    }
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        if headers.get("If-None-Match") == f'"{url}"':
            return _Response(304)
        return _Response(200, bodies[url], etag=f'"{url}"')

    cog = News(SimpleNamespace(http_client=SimpleNamespace(get=get)))
    assert await cog.refresh() is True
    assert await cog.refresh() is True
    assert all(headers == {} for _, headers in calls[:3])
    assert all(headers for _, headers in calls[3:])
    assert [e.name for e in cog.data.events] == ["Halloween"] and cog.data.servants[1].name == "Yamato Takeru"


async def test_failed_refresh_keeps_the_old_data(db):
    def get(url, headers):
        return _Response(503)

    cog = _cog(db, [HALLOWEEN])
    old = cog.data
    cog.bot.http_client = SimpleNamespace(get=get)
    assert await cog.refresh() is False
    assert cog.data is old


async def test_a_broken_export_keeps_the_loop_and_the_old_data(db, monkeypatch):
    cog = _cog(db, [HALLOWEEN])
    old = cog.data

    async def broken():
        raise KeyError("startedAt")

    monkeypatch.setattr(cog, "refresh", broken)
    posted = []

    async def post_due(guild, ts):
        posted.append(guild)

    monkeypatch.setattr(cog, "post_due", post_due)
    cog.bot.guilds = ["guild"]
    await cog.tick.coro(cog)
    assert cog.data is old and posted == ["guild"]


def test_live_needs_data(db):
    cog = _cog(db, [HALLOWEEN])
    assert len(cog.live(T0)) == 1
    cog.data = None
    assert cog.live(T0) is None
