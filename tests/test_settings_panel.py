"""Track C: the /settings panel. Builds every section and drives its controls without Discord."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bb.cogs import settings as panel_mod
from bb.links import sites
from bb.links.model import Fixer, Site
from bb.settings_store import SITE_IDS, GuildSettings, RareLine, SettingsStore

FX = Fixer("fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True)
VX = Fixer("vxtwitter", "vxTwitter", "vxtwitter.com")
TWITTER = Site("twitter", "Twitter", "Tweet", (FX, VX), "https://x.com/i/status/1")


@pytest.fixture(autouse=True)
def no_site_table(monkeypatch):
    """Track A's table may or may not be filled in; tests choose explicitly."""
    monkeypatch.setattr(sites, "SITES", {})


class FakeResponse:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def edit_message(self, **kwargs):
        self.calls.append(("edit_message", kwargs))

    async def send_message(self, *args, **kwargs):
        self.sent_args = args
        self.calls.append(("send_message", kwargs))

    async def send_modal(self, modal):
        self.calls.append(("send_modal", {"modal": modal}))

    def is_done(self) -> bool:
        return bool(self.calls)


def make_interaction(user_id=7, app_permissions=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), response=FakeResponse(), app_permissions=app_permissions
    )


@pytest.fixture
async def panel(db):
    bot = SimpleNamespace(settings=SettingsStore(db), down_fixers=set())
    return panel_mod.SettingsPanel(bot, 1, 7, await bot.settings.get(1))


def find(panel, cls, **attrs):
    for item in panel.children:
        if isinstance(item, cls) and all(getattr(item, k) == v for k, v in attrs.items()):
            return item
    raise LookupError(f"no {cls.__name__} with {attrs} in {panel.section}")


async def click(panel, item):
    interaction = make_interaction()
    await item.callback(interaction)
    return interaction


# --- pure helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("nobb", "nobb"), ("  NoBB ", "nobb"), ("two words", None), ("", None), ("a" * 33, None)],
)
def test_parse_skip_word(raw, expected):
    assert panel_mod.parse_skip_word(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [("en", "en"), (" JA ", "ja"), ("pt-br", "pt-br"), ("english", None), ("e", None)]
)
def test_parse_language(raw, expected):
    assert panel_mod.parse_language(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Europe/Berlin", "Europe/Berlin"),
        ("europe/berlin", "Europe/Berlin"),
        ("utc", "UTC"),
        ("Mars/Base", None),
    ],
)
def test_parse_timezone(raw, expected):
    assert panel_mod.parse_timezone(raw) == expected


def test_toggled_sites_keeps_the_rest():
    s = GuildSettings()
    flipped = panel_mod.toggled_sites(s, "tiktok")
    assert flipped["tiktok"] is False and all(flipped[i] for i in SITE_IDS if i != "tiktok")
    assert panel_mod.toggled_sites(GuildSettings(sites_enabled=flipped), "tiktok")["tiktok"] is True


def test_permission_report():
    perms = discord.Permissions(view_channel=True, send_messages=True, manage_webhooks=False)
    report = dict(panel_mod.permission_report(perms))
    assert report["View Channels"] and report["Send Messages"] and not report["Manage Webhooks"]
    assert not any(dict(panel_mod.permission_report(None)).values())


def test_fixer_report():
    assert panel_mod.fixer_report([TWITTER], {"vxtwitter"}) == ["Twitter: FxTwitter up, vxTwitter down"]
    assert panel_mod.fixer_report([], {"b", "a"}) == ["Down: a, b"]
    assert panel_mod.fixer_report([], set()) == ["No fixer is marked down."]


def test_site_name_prefers_track_a_table(monkeypatch):
    assert panel_mod.site_name("tiktok") == "TikTok"
    monkeypatch.setattr(sites, "SITES", {"twitter": Site("twitter", "Twitter/X", "Post", (), "")})
    assert panel_mod.site_name("twitter") == "Twitter/X"


@pytest.mark.parametrize("section", [key for key, _ in panel_mod.SECTIONS])
def test_every_section_describes_itself(section):
    text = panel_mod.describe(section, GuildSettings(), now=datetime(2026, 1, 1, 12, 30, tzinfo=UTC))
    assert text and "None" not in text


def test_describe_details(monkeypatch):
    s = GuildSettings(
        channel_mode="allow", channels=(5, 6), roundup_channel_id=9, roundup_weekday=0, quip_rate=3
    )
    assert "only in the listed" in panel_mod.describe("channels", s) and "<#5> <#6>" in panel_mod.describe(
        "channels", s
    )
    assert "Monday 18:00" in panel_mod.describe("roundup", s) and "<#9>" in panel_mod.describe("roundup", s)
    assert "1 in 3" in panel_mod.describe("quips", s)
    when = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    assert "now 13:30" in panel_mod.describe("timezone", GuildSettings(timezone="Europe/Berlin"), now=when)
    monkeypatch.setattr(sites, "SITES", {"twitter": TWITTER})
    status = panel_mod.describe(
        "status", s, app_permissions=discord.Permissions(view_channel=True), down_fixers={"vxtwitter"}
    )
    assert "+ View Channels" in status and "- Manage Webhooks" in status
    assert "Twitter: FxTwitter up, vxTwitter down" in status


# --- building the panel -----------------------------------------------------------------


@pytest.mark.parametrize("section", [key for key, _ in panel_mod.SECTIONS])
async def test_every_section_builds_within_discord_limits(panel, section):
    panel.section = section
    panel.rebuild()
    rows = panel.to_components()
    assert 2 <= len(rows) <= 5
    assert isinstance(panel.children[0], panel_mod.SectionSelect)
    assert [o.value for o in panel.children[0].options if o.default] == [section]
    for item in panel.children:
        label = getattr(item, "label", None)
        if label:
            assert len(label) <= 80
    text = panel.render()
    assert text.startswith(f"**{panel.title}**") and len(text) <= 2000


async def test_section_select_switches_section(panel):
    select = panel.children[0]
    select._values = ["reruns"]
    interaction = await click(panel, select)
    assert panel.section == "reruns"
    name, kwargs = interaction.response.calls[0]
    assert name == "edit_message" and kwargs["view"] is panel
    assert "### Repost roasts" in kwargs["content"]
    assert find(panel, panel_mod.ToggleButton, field="reruns_enabled")


async def test_only_the_opener_can_use_the_panel(panel):
    assert await panel.interaction_check(make_interaction(user_id=7))
    assert not await panel.interaction_check(make_interaction(user_id=8))


# --- changes save immediately -----------------------------------------------------------


async def test_site_toggle_saves_and_redraws(panel, db):
    button = find(panel, panel_mod.SiteButton, site_id="tiktok")
    assert button.style is discord.ButtonStyle.success
    interaction = await click(panel, button)
    assert (await db.get_guild_settings(1))["sites_enabled"]["tiktok"] is False
    assert find(panel, panel_mod.SiteButton, site_id="tiktok").style is discord.ButtonStyle.secondary
    content = interaction.response.calls[0][1]["content"]
    assert content.endswith("-# Saved.")
    assert "Off: TikTok" in content


async def test_toggles_and_choices(panel):
    panel.section = "style"
    panel.rebuild()
    await click(panel, find(panel, panel_mod.ChoiceButton, field="reply_mode", value="webhook"))
    assert panel.settings.reply_mode == "webhook"
    assert find(panel, panel_mod.ChoiceButton, value="webhook").style is discord.ButtonStyle.primary
    await click(panel, find(panel, panel_mod.ToggleButton, field="silent"))
    assert panel.settings.silent is False
    assert find(panel, panel_mod.ToggleButton, field="silent").label == "Silent: off"

    panel.section = "reruns"
    panel.rebuild()
    await click(panel, find(panel, panel_mod.ChoiceButton, field="rerun_same_channel", value=True))
    assert panel.settings.rerun_same_channel is True
    select = find(panel, panel_mod.ChoiceSelect, field="rerun_window_days")
    select._values = ["7"]
    await click(panel, select)
    assert panel.settings.rerun_window_days == 7


async def test_quip_select_keeps_custom_rates(panel, db):
    await panel.bot.settings.update(1, quip_rate=4)
    panel.settings = await panel.bot.settings.get(1)
    panel.section = "quips"
    panel.rebuild()
    select = find(panel, panel_mod.ChoiceSelect, field="quip_rate")
    assert [o.value for o in select.options if o.default] == ["4"]
    select._values = ["10"]
    await click(panel, select)
    assert (await db.get_guild_settings(1))["quip_rate"] == 10


async def test_channel_pickers(panel):
    panel.section = "channels"
    panel.rebuild()
    picker = find(panel, panel_mod.ChannelPicker, field="channels")
    picker._values = [SimpleNamespace(id=5), SimpleNamespace(id=6)]
    await click(panel, picker)
    assert panel.settings.channels == (5, 6)
    await click(panel, find(panel, panel_mod.ChoiceButton, field="channel_mode", value="allow"))
    assert not panel.settings.channel_enabled(7) and panel.settings.channel_enabled(5)
    assert [v.id for v in find(panel, panel_mod.ChannelPicker, field="channels").default_values] == [5, 6]

    panel.section = "roundup"
    panel.rebuild()
    assert find(panel, panel_mod.SetButton, field="roundup_channel_id").disabled
    picker = find(panel, panel_mod.ChannelPicker, field="roundup_channel_id")
    picker._values = [SimpleNamespace(id=9)]
    await click(panel, picker)
    assert panel.settings.roundup_channel_id == 9
    await click(panel, find(panel, panel_mod.SetButton, field="roundup_channel_id"))
    assert panel.settings.roundup_channel_id is None


async def test_deleted_channels_are_left_out_of_defaults(db):
    store = SettingsStore(db)
    await store.update(1, channels=(5, 6))
    guild = SimpleNamespace(get_channel_or_thread=lambda cid: object() if cid == 5 else None)
    panel = panel_mod.SettingsPanel(
        SimpleNamespace(settings=store, down_fixers=set()),
        1,
        7,
        await store.get(1),
        guild=guild,
        section="channels",
    )
    assert [v.id for v in find(panel, panel_mod.ChannelPicker, field="channels").default_values] == [5]


async def test_modal_valid_and_invalid_input(panel):
    panel.section = "timezone"
    panel.rebuild()
    interaction = await click(panel, find(panel, panel_mod.ModalButton))
    modal = interaction.response.calls[0][1]["modal"]
    assert modal.input.default == "UTC"

    modal.input._value = "Mars/Base"
    bad = make_interaction()
    await modal.on_submit(bad)
    assert panel.settings.timezone == "UTC"
    assert "Not saved" in bad.response.calls[0][1]["content"]

    modal.input._value = "asia/tokyo"
    good = make_interaction()
    await modal.on_submit(good)
    assert panel.settings.timezone == "Asia/Tokyo"


async def test_status_uses_bot_permissions_from_the_interaction(panel):
    panel.section = "status"
    panel.rebuild()
    interaction = make_interaction(app_permissions=discord.Permissions(manage_webhooks=True))
    await find(panel, panel_mod.RefreshButton).callback(interaction)
    content = interaction.response.calls[0][1]["content"]
    assert "+ Manage Webhooks" in content and "- Send Messages" in content


def test_settings_command_is_admin_only_and_guild_only():
    cmd = panel_mod.Settings.settings_command
    assert cmd.name == "settings"
    assert cmd.default_permissions == discord.Permissions(manage_guild=True)
    assert cmd.guild_only is True


async def test_rare_line_section(panel):
    panel.section = "rare"
    panel.rebuild()
    assert "No lines yet" in panel_mod.describe("rare", panel.settings)
    with pytest.raises(LookupError):
        find(panel, panel_mod.RarePreviewButton)  # nothing to preview yet

    interaction = await click(panel, find(panel, panel_mod.ModalButton, label="Add line"))
    modal = interaction.response.calls[0][1]["modal"]
    modal.input._value = "  {target} BB was thinking about you♡  "
    await modal.on_submit(make_interaction())
    assert panel.settings.rare_lines == (RareLine("{target} BB was thinking about you♡"),)

    picker = find(panel, panel_mod.RareTargetPicker)
    picker._values = [SimpleNamespace(id=42)]
    await click(panel, picker)
    assert panel.settings.rare_lines[0].target_id == 42
    text = panel_mod.describe("rare", panel.settings)
    assert "BB was thinking about you♡" in text and "<@42>" in text and "1 in 1000" in text

    preview = await click(panel, find(panel, panel_mod.RarePreviewButton))
    kwargs = preview.response.calls[0][1]
    assert kwargs["ephemeral"] is True and kwargs["allowed_mentions"].users is False
    assert preview_text(preview) == "-# <@42> BB was thinking about you♡"

    # a second line without anyone to ping: BB just says it
    interaction = await click(panel, find(panel, panel_mod.ModalButton, label="Add line"))
    modal = interaction.response.calls[0][1]["modal"]
    modal.input._value = "BB is bored♡"
    await modal.on_submit(make_interaction())
    assert panel.settings.rare_lines[1] == RareLine("BB is bored♡") and panel.rare_index == 1
    assert "no ping" in panel_mod.describe("rare", panel.settings)

    interaction = await click(panel, find(panel, panel_mod.ModalButton, label="Edit"))
    modal = interaction.response.calls[0][1]["modal"]
    modal.input._value = "BB is very bored♡"
    await modal.on_submit(make_interaction())
    assert panel.settings.rare_lines[1] == RareLine("BB is very bored♡")
    assert panel.settings.rare_lines[0].target_id == 42  # the other line is untouched


def preview_text(interaction):
    return interaction.response.sent_args[0]


async def test_use_on_next_link_arms_the_selected_line(panel):
    await panel.bot.settings.update(1, rare_lines=(RareLine("a"), RareLine("{target} b", 42)))
    panel.settings = await panel.bot.settings.get(1)
    panel.section = "rare"
    panel.rebuild()
    select = find(panel, panel_mod.RareLineSelect)
    select._values = ["1"]
    await click(panel, select)
    await click(panel, find(panel, panel_mod.SetButton, field="rare_armed"))
    assert panel.settings.rare_armed == 1
    assert "next link" in panel_mod.describe("rare", panel.settings)
    assert find(panel, panel_mod.SetButton, field="rare_armed").disabled  # can't double-arm


async def test_removing_lines_keeps_the_armed_one_right(panel):
    await panel.bot.settings.update(1, rare_lines=(RareLine("a"), RareLine("b"), RareLine("c")), rare_armed=2)
    panel.settings = await panel.bot.settings.get(1)
    panel.section = "rare"
    panel.rebuild()
    await click(panel, find(panel, panel_mod.RareRemoveButton))  # removes "a", the selected line
    assert [line.text for line in panel.settings.rare_lines] == ["b", "c"]
    assert panel.settings.rare_armed == 1  # still "c"
    panel.rare_index = 1
    panel.rebuild()
    await click(panel, find(panel, panel_mod.RareRemoveButton))  # removes "c", the armed line
    assert panel.settings.rare_armed is None and panel.rare_index == 0


async def test_roundup_preview_is_private(panel):
    embed = discord.Embed(title="Weekly report☆")
    panel.bot.get_cog = lambda name: SimpleNamespace(build=AsyncMock(return_value=embed))
    panel.section = "roundup"
    panel.rebuild()
    interaction = await click(panel, find(panel, panel_mod.RoundupPreviewButton))
    kwargs = interaction.response.calls[0][1]
    assert kwargs["embed"] is embed and kwargs["ephemeral"] is True


async def test_news_section_and_private_preview(panel):
    from bb import news

    panel.section = "news"
    panel.rebuild()
    await click(panel, find(panel, panel_mod.SetButton, field="news_channel_id"))
    assert "goes to: off" in panel_mod.describe("news", panel.settings)

    async def add_images(notices):
        notices[0].image = "https://x/art.png"

    def preview(live):
        panel.bot.get_cog = lambda name: SimpleNamespace(live=lambda ts: live, add_images=add_images)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        return interaction

    interaction = preview([news.Notice("**New banner** · X")])
    await find(panel, panel_mod.NewsPreviewButton).callback(interaction)
    assert interaction.response.defer.await_args.kwargs["ephemeral"] is True
    title, item = [call.kwargs for call in interaction.followup.send.await_args_list]
    assert title["ephemeral"] and "Only you can see this" in title["content"]
    assert item["ephemeral"] and item["content"] == "**New banner** · X"
    assert item["embed"].image.url == "https://x/art.png"

    interaction = preview(None)  # nothing fetched yet
    await find(panel, panel_mod.NewsPreviewButton).callback(interaction)
    assert "hasn't got the FGO data" in interaction.followup.send.await_args.kwargs["content"]


async def test_pause_switch(panel):
    panel.section = "status"
    panel.rebuild()
    await click(panel, find(panel, panel_mod.ToggleButton, field="paused"))
    assert panel.settings.paused is True
    assert "BB is paused" in panel_mod.describe("status", panel.settings)
