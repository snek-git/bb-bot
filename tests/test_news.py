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


def event(id, name, start, end, type="eventQuest", wars=()):
    return {"id": id, "type": type, "name": name, "startedAt": start, "endedAt": end, "warIds": list(wars)}


def war(*closed_at):
    """A war page whose quests close at these times."""
    return {"spots": [{"quests": [{"closedAt": t} for t in closed_at]}, {"quests": []}]}


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


def test_only_campaigns_worth_a_post_are_kept():
    events = news.parse_events([
        event(71741, "期間限定 ストーム・ポッド消費なし！", T0, T0 + 7 * DAY, type="questCampaign"),
        event(71732, "0 AP Cost!", T0, T0 + 16 * DAY, type="questCampaign"),  # the English export
        event(71733, "消費AP0！", T0 + DAY, T0 + 16 * DAY, type="questCampaign"),
        event(20295, "強化『大成功＆極大成功』率4倍！", T0, T0 + 9 * DAY, type="combineCampaign"),
        event(30065, "強化『大成功＆極大成功』率4倍！", T0, T0 + 9 * DAY, type="svtequipCombineCampaign"),
        event(3, "AP Cost 1/2", T0, T0 + 14 * DAY, type="questCampaign"),
        event(4, "「ハロウィン」関連サーヴァント強化『大成功＆極大成功』率2倍！", T0, T0 + 7 * DAY,
              type="combineCampaign"),
        event(5, "Miyamoto Iori 2X EXP!", T0, T0 + 20 * DAY, type="combineCampaign"),
    ])
    assert [(e.id, e.kind) for e in events] == [
        (71741, news.PODLESS), (71732, news.ZERO_AP), (71733, news.ZERO_AP), (20295, news.GREAT_SUCCESS),
    ]  # the CE copy of the Great Success campaign folds into the servant one


def _campaign(id, name, kind, days=7):
    return news.Event(id, name, T0, T0 + days * DAY, (), kind)


def test_campaign_posts_say_what_they_cover():
    podless = _campaign(71741, "期間限定 ストーム・ポッド消費なし！", news.PODLESS)
    page = news.parse_event_page({"campaignQuests": [{"questId": 94149801}, {"questId": 94149802}]})
    target = news.campaign_target(podless, page, "冠位戴冠戦 ライダー")
    assert news.campaign_start(podless, target).split("\n")[0] == "**Podless Grand Duel** · Rider"
    assert news.campaign_target(podless, page, "冠位戴冠戦 エクストラⅠ") == "ExtraⅠ"
    zero = _campaign(71732, "0 AP Cost!", news.ZERO_AP, days=16)
    assert news.campaign_target(zero, page, "冠位戴冠戦 エクストラ") == "冠位戴冠戦 エクストラ"
    story = news.EventPage(quests=tuple(range(636)))
    assert news.campaign_target(zero, story, "特異点F 炎上汚染都市 冬木") == "636 quests"
    great = _campaign(20295, "強化『大成功＆極大成功』率4倍！", news.GREAT_SUCCESS, days=9)
    first, ends = news.campaign_start(great).split("\n")
    assert first.startswith("**Great Success x4** · [強化『大成功＆極大成功』率4倍！](<")
    assert ends.startswith("-# Ends <t:")
    # the class couldn't be looked up: the post names the campaign instead
    assert news.campaign_start(podless).startswith(f"**Podless Grand Duel** · [{PODLESS_NAME}](<")


PODLESS_NAME = "期間限定 ストーム・ポッド消費なし！"
PODLESS = event(71741, PODLESS_NAME, T0 - 600, T0 + 7 * DAY, type="questCampaign")


def test_campaigns_get_a_start_post_and_no_reminders():
    podless = PODLESS
    (start,) = run([podless])
    assert start.event.kind == news.PODLESS and start.marks == [("event:71741", START)]
    assert run([podless], ts=T0 + 6.5 * DAY, done={("event:71741", START)}) == []


async def test_campaign_posts_look_up_their_class(db):
    podless = PODLESS
    http = _Http(pages={
        news.EVENT_API.format(id=71741): {"campaignQuests": [{"questId": 94149801}]},
        news.QUEST_API.format(id=94149801): {"warLongName": "冠位戴冠戦\nライダー"},
    })
    cog = _cog(db, [podless], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 1
    assert channel.sent[0].startswith("**Podless Grand Duel** · Rider\n-# Ends <t:")
    assert channel.files == [None]  # campaigns have no picture
    assert not any("official" in url or "fate-go" in url for _, url in http.calls)


PLACEHOLDER = T0 + 1165 * DAY  # "until 2029"


def test_only_recent_events_listed_for_years_need_a_real_end():
    (grand, story, short, no_wars) = news.parse_events([
        event(80637, "冠位名代戦", T0, PLACEHOLDER, wars=[8413]),
        event(2, "Lostbelt No.7", T0 - 400 * DAY, PLACEHOLDER, wars=[307]),
        event(3, "Halloween", T0, T0 + 20 * DAY, wars=[9205]),
        event(4, "Trial quest", T0, PLACEHOLDER),
    ])
    assert grand.war_ids == (8413,)
    assert news.needs_real_end(grand, T0 + 5 * DAY)
    assert not news.needs_real_end(grand, T0 + 61 * DAY)
    assert not any(news.needs_real_end(e, T0 + 5 * DAY) for e in (story, short, no_wars))


def test_quest_end_is_the_last_limited_quest():
    # the Grand event: 8 quests close a week in, one stays open "until 2029"
    assert news.quest_end(war(*[T0 + 6.8 * DAY] * 8, PLACEHOLDER), T0) == T0 + 6.8 * DAY
    assert news.quest_end(war(T0 + 2 * DAY, T0 + 7 * DAY), T0) == T0 + 7 * DAY
    assert news.quest_end(war(PLACEHOLDER), T0) is None  # permanent content
    assert news.quest_end({"spots": []}, T0) is None


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
    assert first.event.id == 80632 and first.image is None  # the cog looks the event's banner up
    assert second.image == "https://static.atlasacademy.io/JP/SummonBanners/img_summon_83838.png"
    done = {("event:80632", START), ("banner:10", START), ("banner:11", START)}
    (reminder,) = run([ev], banners, ts=end - DAY + 60, done=done)
    assert reminder.image is None and reminder.event is None



def _article(day, title, href):
    return (f'<li>\n  <p class="date new">{day}</p>\n  <p class="title">{title}</p>\n'
            f'  <a href="{href}"></a>\n  <svg class="arrow_icon"></svg>\n</li>\n')


# The official news list as it was on 2026-10-28, trimmed to the articles that matter here.
OFFICIAL = '<ul class="list_news">' + "".join([
    _article("2026.10.28", "【予告】「ぐだぐだイベント2026」開幕予定！", "/2026/10/2026gudaguda/"),
    _article("2026.10.23", "「冠位名代戦：タマモアリア」開幕！", "/2026/10/grand_substitute/"),
    _article("2026.10.23", "【期間限定】『「冠位名代戦」 タマモアリアピックアップ召喚』！",
             "/2026/10/grand_substitute_pu/"),
    _article("2026.10.23", "【追記・更新】【期間限定】Fate/Samurai Remnant×Fate/Grand Order"
                           "コラボレーションイベント「復刻版:盈月剣風帖」開幕！",
             "/2026/10/re_sr_collaboration/"),
    _article("2024.08.07", "「盈月剣風帖」開幕！", "/2024/08/sr_collaboration/"),
]) + "</ul>"
OCT_23 = 1_792_746_000.0  # 2026-10-23 18:00 JST, when the Grand event opened
OCT_14 = OCT_23 - 9 * DAY  # when the rerun opened; its article was updated on the 23rd, which the list shows


def test_official_news_is_read_from_the_list():
    articles = news.parse_official_news(OFFICIAL)
    assert len(articles) == 5
    assert articles[1] == news.Article("「冠位名代戦：タマモアリア」開幕！",
                                       "https://news.fate-go.jp/2026/10/grand_substitute/", (2026, 10))
    assert news.parse_official_news(_article("2026.10.22", "TIPS集", "/info/tips/"))[0].posted is None


def test_events_link_the_article_they_opened_with():
    articles = news.parse_official_news(OFFICIAL)
    grand, rerun, original, gudaguda = news.parse_events([
        event(80637, "冠位名代戦", OCT_23, PLACEHOLDER),
        event(80632, "復刻版:盈月剣風帖", OCT_14, OCT_14 + 21 * DAY),
        event(80400, "盈月剣風帖", 1_723_021_200.0, 1_723_021_200.0 + 20 * DAY),  # 2024-08-07
        event(80700, "ぐだぐだイベント2026", OCT_23 + 6 * DAY, OCT_23 + 20 * DAY),
    ])
    assert news.official_page(grand, articles) == "https://news.fate-go.jp/2026/10/grand_substitute/"
    assert news.official_page(rerun, articles) == "https://news.fate-go.jp/2026/10/re_sr_collaboration/"
    assert news.official_page(original, articles) == "https://news.fate-go.jp/2024/08/sr_collaboration/"
    assert news.official_page(gudaguda, articles) is None  # only the "coming soon" article so far
    # a rerun two years on doesn't get the first run's article
    later = news.parse_events([event(1, "盈月剣風帖", OCT_23, OCT_23 + 9 * DAY)])[0]
    assert news.official_page(later, articles[4:]) is None
    # an event opening on the 1st may have its article posted the evening before
    first = news.parse_events([event(2, "冠位名代戦", OCT_23 + 9 * DAY, OCT_23 + 16 * DAY)])[0]  # Nov 1st
    assert news.official_page(first, articles) == "https://news.fate-go.jp/2026/10/grand_substitute/"


def test_the_official_link_goes_after_the_name():
    rerun = news.parse_events([event(80632, "復刻版:盈月剣風帖", OCT_14, OCT_14 + 21 * DAY)])[0]
    url = "https://news.fate-go.jp/2026/10/re_sr_collaboration/"
    first = news.event_start(rerun, official=url).split("\n")[0]
    assert first == ("**Rerun started** · [復刻版:盈月剣風帖](<https://apps.atlasacademy.io/db/JP/event/80632>)"
                     " · [Official page](<https://news.fate-go.jp/2026/10/re_sr_collaboration/>)")


# --- the cog (fake guild and bot, real in-memory DB) ----------------------------------------


class _Channel:
    def __init__(self, error: Exception | None = None, id=55):
        self.id = id
        self.sent: list[str] = []
        self.files: list[tuple[str, bytes] | None] = []
        self.replies: list[int | None] = []  # the message each post replied to
        self.error = error

    async def send(self, content, **kwargs):
        assert kwargs["allowed_mentions"].everyone is False
        assert "embed" not in kwargs  # pictures go up as plain files
        if self.error:
            raise self.error
        self.sent.append(content)
        file = kwargs.get("file")
        self.files.append((file.filename, file.fp.read()) if file else None)
        reference = kwargs.get("reference")
        self.replies.append(reference.message_id if reference else None)
        return SimpleNamespace(id=1000 + len(self.sent))


class _Http:
    """Atlas as BB sees it: pages are JSON answers by URL, pictures the image bytes by URL."""

    def __init__(self, pages=None, pictures=None):
        self.pages, self.pictures, self.calls = pages or {}, pictures or {}, []

    def get(self, url, headers=None):
        self.calls.append(("get", url))
        if url in self.pictures:
            return _Response(200, content_type="image/png", data=self.pictures[url])
        if url in self.pages:
            return _Response(200, self.pages[url])
        return _Response(404, content_type="application/json", data=b'{"detail":"Not Found"}')


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
PNG = b"\x89PNG\r\n\x1a\n" + bytes(200_000)  # bigger than one download chunk


async def test_posts_attach_their_pictures_as_files(db):
    http = _Http(
        pages={news.EVENT_API.format(id=1): {"banner": EVENT_ART}},
        pictures={EVENT_ART: PNG, BANNER_ART: PNG[:5000]},
    )
    halloween_banner = banner(10, "Halloween banner", T0 - 600, T0 + 20 * DAY, [1]) | {"imageId": 83838}
    cog = _cog(db, [HALLOWEEN], [halloween_banner], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 2
    assert channel.sent[0].startswith("**Event started** · [Halloween]")
    assert channel.sent[1].startswith("**New banner** · Halloween banner")
    assert channel.files == [("event_war_80613.png", PNG), ("img_summon_83838.png", PNG[:5000])]


async def test_missing_pictures_fall_back_to_text(db):
    # the event's page names a banner that doesn't exist (Atlas has some of those), the banner art is gone
    http = _Http(pages={news.EVENT_API.format(id=1): {"banner": EVENT_ART}})
    halloween_banner = banner(10, "Halloween banner", T0 - 600, T0 + 20 * DAY, [1]) | {"imageId": 83838}
    cog = _cog(db, [HALLOWEEN], [halloween_banner], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 1
    assert channel.files == [None] and "**New banner**" in channel.sent[0]


GRAND_DUEL = "https://static.atlasacademy.io/JP/Banner/chaldea_category_80520.png"
DEAD_BANNER = "https://static.atlasacademy.io/JP/Banner/event_war_80614.png"
EVENT_PAGE_URL = news.EVENT_API.format(id=80637)
RAID_WAR, GRAND_DUEL_WAR = news.WAR_API.format(id=8413), news.WAR_API.format(id=8395)


def _grand_event_notice():
    grand = news.parse_events([event(80637, "冠位名代戦", T0, T0 + 7 * DAY, wars=[8413])])[0]
    return news.Notice("**Event started** · 冠位名代戦", event=grand)


async def test_an_event_without_its_own_banner_uses_its_wars():
    # Atlas as it is for the Grand event: its own banner is dead, its war (the raid) has the 冠位戴冠戦 strip
    http = _Http(
        pages={EVENT_PAGE_URL: {"banner": DEAD_BANNER},
               RAID_WAR: {"banner": GRAND_DUEL, "parentWarId": 8395}},
        pictures={GRAND_DUEL: PNG[:900]},
    )
    cog = News(SimpleNamespace(http_client=http))
    notice = _grand_event_notice()
    await cog.add_details([notice])
    assert notice.image == GRAND_DUEL
    assert ("get", GRAND_DUEL_WAR) not in http.calls  # found before going up another level
    assert cog.send_kwargs(notice)["file"].filename == "chaldea_category_80520.png"


async def test_then_the_war_above_it_then_nothing():
    http = _Http(
        pages={EVENT_PAGE_URL: {"banner": DEAD_BANNER},
               RAID_WAR: {"banner": None, "parentWarId": 8395},
               GRAND_DUEL_WAR: {"banner": GRAND_DUEL, "parentWarId": 0}},
        pictures={GRAND_DUEL: PNG[:900]},
    )
    cog = News(SimpleNamespace(http_client=http))
    notice = _grand_event_notice()
    await cog.add_details([notice])
    assert notice.image == GRAND_DUEL

    http.pictures.clear()
    cog = News(SimpleNamespace(http_client=http))
    notice = _grand_event_notice()
    await cog.add_details([notice])
    assert notice.image is None and "file" not in cog.send_kwargs(notice)


async def test_war_lookups_stop_after_two_levels():
    # a chain of wars with no banners: BB looks at the event's war and the one above it, no further
    http = _Http(pages={news.WAR_API.format(id=i): {"banner": None, "parentWarId": i + 1}
                        for i in range(8413, 8420)})
    cog = News(SimpleNamespace(http_client=http))
    notice = _grand_event_notice()
    await cog.add_details([notice])
    wars = [url for kind, url in http.calls if "/war/" in url]
    assert notice.image is None and wars == [RAID_WAR, news.WAR_API.format(id=8414)]


async def test_pictures_too_big_to_upload_are_left_out(db, monkeypatch):
    monkeypatch.setattr("bb.cogs.news.MAX_PICTURE", 100_000)
    cog = _cog(db, http=_Http(pictures={BANNER_ART: PNG}))
    assert await cog.picture(BANNER_ART) is None


async def test_the_grand_event_gets_its_real_end_and_reminders(db):
    # listed "until 2029"; its quests close 6.8 days in: that's the end BB uses
    grand = event(80637, "冠位名代戦", T0, PLACEHOLDER, wars=[8413])
    http = _Http(pages={news.WAR_API.format(id=8413): war(*[T0 + 6.8 * DAY] * 8, PLACEHOLDER)})
    cog = _cog(db, http=http)
    (fixed,) = await cog.with_real_ends(news.parse_events([grand]), T0 + 5 * DAY)
    assert fixed.end == T0 + 6.8 * DAY
    cog.data = Data([fixed], [], SERVANTS)
    await cog.bot.settings.update(1, news_channel_id=55)
    await db.mark_news_posted(1, [("event:80637", START)])
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0 + 5 * DAY) == 1
    assert channel.sent[0].startswith("**2 days left** · [冠位名代戦]")
    assert f"<t:{int(T0 + 6.8 * DAY)}:R>" in channel.sent[0]


async def test_reminders_reply_to_their_start_post(db):
    end = T0 + 14 * DAY
    halloween = event(1, "Halloween", T0, end)
    banners = [banner(10, "「冠位名代戦」準備応援 Aピックアップ召喚", T0, T0 + 20 * DAY, [1]),
               banner(11, "「冠位名代戦」準備応援 Bピックアップ召喚", T0, T0 + 20 * DAY, [2])]
    cog = _cog(db, [halloween], banners)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0 + 60) == 1  # both starts in one message
    start_id = 1001
    assert await db.news_messages(1) == {k: (55, start_id) for k in ("event:1", "banner:10", "banner:11")}

    cog.data = Data(news.parse_events([halloween, event(2, "Other", T0 - DAY, end)]),
                    cog.data.groups, SERVANTS)
    await db.mark_news_posted(1, [("event:2", START)])  # posted before BB saved start posts
    assert await cog.post_due(_guild(channel), end - 2 * DAY + 60) == 2
    sent = zip(channel.sent[1:], channel.replies[1:], strict=True)
    replies = {text.split("]")[0]: reply for text, reply in sent}
    assert replies == {"**2 days left** · [Halloween": start_id, "**2 days left** · [Other": None}

    assert await cog.post_due(_guild(channel), T0 + 18 * DAY + 60) == 1  # the banners' reminder
    assert channel.replies[-1] == start_id


async def test_reminders_dont_reply_to_a_post_in_another_channel(db):
    end = T0 + 14 * DAY
    cog = _cog(db, [event(1, "Halloween", T0, end)])
    await cog.bot.settings.update(1, news_channel_id=55)
    await db.mark_news_posted(1, [("event:1", START)])
    await db.save_news_message(1, ["event:1"], 77, 500)  # the news channel was changed since
    channel = _Channel()
    assert await cog.post_due(_guild(channel), end - 2 * DAY + 60) == 1
    assert channel.replies == [None]


def test_reminders_with_a_start_post_get_their_own_message():
    notices = [news.Notice("a", [("event:1", SOON)]), news.Notice("b", [("event:2", SOON)]),
               news.Notice("c", [("event:3", START)])]
    packed = news.messages(notices, {"event:1": 7, "event:3": 9})
    assert [(m.text, m.reply_to) for m in packed] == [("a", 7), ("b\n\nc", None)]


async def test_real_ends_are_cached_and_survive_a_failed_lookup(db):
    grand = news.parse_events([event(80637, "冠位名代戦", T0, PLACEHOLDER, wars=[8413])])[0]
    http = _Http(pages={news.WAR_API.format(id=8413): war(T0 + 7 * DAY)})
    cog = _cog(db, http=http)
    assert await cog.real_end(grand, T0 + DAY) == T0 + 7 * DAY
    assert await cog.real_end(grand, T0 + DAY + 3600) == T0 + 7 * DAY
    assert len(http.calls) == 1  # the second answer came from the cache
    http.pages.clear()  # Atlas is down when the cache runs out
    assert await cog.real_end(grand, T0 + 2 * DAY) == T0 + 7 * DAY
    http.pages[news.WAR_API.format(id=1)] = war(PLACEHOLDER)
    permanent = news.parse_events([event(5, "Trial quest", T0, PLACEHOLDER, wars=[1])])[0]
    assert await cog.real_end(permanent, T0 + DAY) is None
    (unchanged,) = await cog.with_real_ends([permanent], T0 + DAY)
    assert unchanged.end == PLACEHOLDER


async def test_failed_event_lookups_are_tried_again(db):
    http = _Http()
    cog = _cog(db, http=http)
    assert await cog.event_page(1) is None
    http.pages[news.EVENT_API.format(id=1)] = {"banner": None, "noticeBanner": EVENT_ART}
    assert (await cog.event_page(1)).art == EVENT_ART
    assert (await cog.event_page(1)).art == EVENT_ART
    assert [c for c in http.calls if c[0] == "get"] == [("get", news.EVENT_API.format(id=1))] * 2


# Atlas's page for the Samurai Remnant rerun, cut down to what BB reads
SAMURAI_PAGE = {
    "banner": EVENT_ART,
    "eventDetail": {"flags": ["typeExchangeShop", "typeBonusSkill", "typeMission", "typeEventShop",
                              "eventRevival"]},
    "svts": [{"svtId": 7, "type": "join"}, {"svtId": 106001, "type": "none"}],
    "lotteries": [],
}


def test_event_pages_give_type_and_free_servant():
    page = news.parse_event_page(SAMURAI_PAGE)
    assert page == news.EventPage(EVENT_ART, ("Missions", "Shop"), (7,))
    raid = news.parse_event_page({"eventDetail": {"flags": ["typeRaid", "raidDefeatCount"]}})
    assert raid.kinds == ("Raid",) and raid.free == () and raid.art is None
    box = news.parse_event_page({"eventDetail": {"flags": ["typeBoxGacha", "typeEventShop"]},
                                 "lotteries": [{"limited": False}]})
    assert box.kinds == ("Box gacha (unlimited)", "Shop")
    pick_one = news.parse_event_page({"svts": [{"svtId": i, "type": "condJoin"} for i in range(130)]})
    assert pick_one.free == ()
    joins = [{"svtId": 7, "type": "join"}, {"svtId": 7, "type": "directJoin"}]
    twice = news.parse_event_page({"svts": joins})
    assert twice.free == (7,)


def test_start_post_with_details():
    servants = {**SERVANTS, 7: news.Servant("Miyamoto Iori", 4)}
    ev = news.parse_events([event(80632, "復刻版:盈月剣風帖", T0, T0 + 20 * DAY)])[0]
    lines = news.event_start(ev, news.parse_event_page(SAMURAI_PAGE), servants).split("\n")
    assert lines[0].startswith("**Rerun started** · [復刻版:盈月剣風帖]")
    assert lines[1] == "Missions · Shop · Free servant: Miyamoto Iori (4★)"
    assert lines[2].startswith("-# Ends <t:")
    # nothing on the page worth saying: no empty line
    assert news.event_start(ev, news.EventPage(), servants).count("\n") == 1
    grand = news.parse_events([event(80637, "冠位名代戦", T0, PLACEHOLDER)])[0]
    assert news.event_start(grand, news.EventPage(kinds=("Raid",)), servants) == (
        "**Now open** · [冠位名代戦](<https://apps.atlasacademy.io/db/JP/event/80637>)\nRaid"
    )


async def test_posts_fill_in_details_from_the_event_page(db):
    http = _Http(pages={news.EVENT_API.format(id=1): SAMURAI_PAGE}, pictures={EVENT_ART: PNG})
    cog = _cog(db, [HALLOWEEN], http=http)
    cog.data = Data(cog.data.events, [], {**SERVANTS, 7: news.Servant("Miyamoto Iori", 4)})
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), T0) == 1
    assert channel.sent[0].split("\n")[1] == "Missions · Shop · Free servant: Miyamoto Iori (4★)"
    assert channel.files == [("event_war_80613.png", PNG)]


async def test_posts_link_the_official_page(db):
    rerun = event(80632, "復刻版:盈月剣風帖", OCT_23 - 600, OCT_23 + 20 * DAY)
    http = _Http(pages={news.OFFICIAL_NEWS: OFFICIAL})  # page 2 is missing: page 1 is still used
    cog = _cog(db, [rerun, event(80637, "冠位名代戦", OCT_23, OCT_23 + 7 * DAY)], http=http)
    await cog.bot.settings.update(1, news_channel_id=55)
    channel = _Channel()
    assert await cog.post_due(_guild(channel), OCT_23 + 60) == 1
    assert "[Official page](<https://news.fate-go.jp/2026/10/re_sr_collaboration/>)" in channel.sent[0]
    assert "[Official page](<https://news.fate-go.jp/2026/10/grand_substitute/>)" in channel.sent[0]
    assert [url for _, url in http.calls].count(news.OFFICIAL_NEWS) == 1  # read once for both


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


class _Body:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i : i + size]


class _Response:
    def __init__(self, status, body=None, etag=None, content_type=None, data=b""):
        self.status, self.body, self.content = status, body, _Body(data)
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

    async def text(self):
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
    assert await cog.refresh(T0) is True
    assert await cog.refresh(T0 + 900) is True
    assert all(headers == {} for _, headers in calls[:3])
    assert all(headers for _, headers in calls[3:])
    assert [e.name for e in cog.data.events] == ["Halloween"] and cog.data.servants[1].name == "Yamato Takeru"


async def test_failed_refresh_keeps_the_old_data(db):
    def get(url, headers):
        return _Response(503)

    cog = _cog(db, [HALLOWEEN])
    old = cog.data
    cog.bot.http_client = SimpleNamespace(get=get)
    assert await cog.refresh(T0) is False
    assert cog.data is old


async def test_a_broken_export_keeps_the_loop_and_the_old_data(db, monkeypatch):
    cog = _cog(db, [HALLOWEEN])
    old = cog.data

    async def broken(ts):
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
