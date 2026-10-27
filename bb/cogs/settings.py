"""/settings: the private admin panel. A section picker on top, that section's controls below; changes save
immediately through bot.settings.update. Labels stay plain; BB's voice is only in the title and notices."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Iterable
from datetime import datetime
from functools import cache
from typing import Any
from zoneinfo import available_timezones

import discord
from discord import app_commands
from discord.ext import commands

from bb import news, strings
from bb.cogs.linkfix import rare_line
from bb.links import sites
from bb.settings_store import MAX_RARE_LINES, SITE_IDS, GuildSettings, RareLine
from bb.timeutil import now, zone

log = logging.getLogger(__name__)

PANEL_TIMEOUT = 600

SECTIONS: tuple[tuple[str, str], ...] = (
    ("sites", "Sites"),
    ("channels", "Channels"),
    ("style", "Repost style"),
    ("original", "Original message"),
    ("quips", "Quips"),
    ("rare", "Rare line"),
    ("reruns", "Repost roasts"),
    ("webhooks", "Webhooks"),
    ("roundup", "Roundup"),
    ("alerts", "Alerts channel"),
    ("news", "FGO news"),
    ("skip", "Skip word"),
    ("translation", "Translation language"),
    ("timezone", "Timezone"),
    ("status", "Status"),
)
SECTION_LABELS = dict(SECTIONS)

_SITE_NAMES = {
    "twitter": "Twitter",
    "pixiv": "Pixiv",
    "reddit": "Reddit",
    "tiktok": "TikTok",
    "bluesky": "Bluesky",
    "instagram": "Instagram",
    "ehentai": "e-hentai",
    "nhentai": "nhentai",
}
QUIP_CHOICES: tuple[tuple[str, int], ...] = (
    ("Off", 0),
    ("1 in 100 (1%)", 100),
    ("1 in 50", 50),
    ("1 in 20", 20),
    ("1 in 10", 10),
    ("1 in 5", 5),
    ("Always", 1),
)
RARE_ODDS: tuple[tuple[str, int], ...] = (
    ("Off", 0),
    ("1 in 100", 100),
    ("1 in 500", 500),
    ("1 in 1000", 1000),
    ("1 in 5000", 5000),
)
WINDOW_CHOICES = (1, 3, 7, 14, 30, 60, 90, 180, 365)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ORIGINAL_ACTIONS = (("suppress", "Hide preview"), ("delete", "Delete it"), ("nothing", "Leave it"))
REPLY_MODES = (("reply", "Reply"), ("webhook", "Copy the poster"))

REQUIRED_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("view_channel", "View Channels"),
    ("send_messages", "Send Messages"),
    ("send_messages_in_threads", "Send Messages in Threads"),
    ("embed_links", "Embed Links"),
    ("attach_files", "Attach Files"),
    ("read_message_history", "Read Message History"),
    ("manage_messages", "Manage Messages"),
    ("manage_webhooks", "Manage Webhooks"),
)

_TEXT_CHANNELS = [discord.ChannelType.text, discord.ChannelType.news]
_LIST_CHANNELS = [*_TEXT_CHANNELS, discord.ChannelType.forum, discord.ChannelType.voice]

# --- pure helpers -----------------------------------------------------------------------


def site_name(site_id: str) -> str:
    site = sites.SITES.get(site_id)
    return site.name if site else _SITE_NAMES.get(site_id, site_id.title())


def toggled_sites(settings: GuildSettings, site_id: str) -> dict[str, bool]:
    return {**settings.sites_enabled, site_id: not settings.site_enabled(site_id)}


def quip_label(rate: int) -> str:
    return next((label for label, value in QUIP_CHOICES if value == rate), f"1 in {rate}")


def parse_skip_word(text: str) -> str | None:
    word = text.strip().lower()
    return word if re.fullmatch(r"\w{1,32}", word) else None


def parse_language(text: str) -> str | None:
    code = text.strip().lower()
    return code if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", code) else None


@cache
def _timezones() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def parse_timezone(text: str) -> str | None:
    """Canonical IANA name ('europe/berlin' -> 'Europe/Berlin'), or None if there's no such zone."""
    return _timezones().get(text.strip().lower())


def permission_report(perms: discord.Permissions | None) -> list[tuple[str, bool]]:
    return [(label, bool(perms and getattr(perms, name))) for name, label in REQUIRED_PERMISSIONS]


def fixer_report(site_list: Iterable[Any], down: Collection[str]) -> list[str]:
    lines = [
        f"{site.name}: " + ", ".join(f"{f.name} {'down' if f.id in down else 'up'}" for f in site.fixers)
        for site in site_list
    ]
    if not lines:
        lines = [f"Down: {', '.join(sorted(down))}" if down else "No fixer is marked down."]
    return lines


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def describe_rare(s: GuildSettings) -> str:
    odds = "off" if s.rare_rate <= 0 else f"1 in {s.rare_rate} fixed links"
    if not s.rare_lines:
        return (
            f"No lines yet.\nOdds: {odds}\n"
            "-# Add a line to switch it on. A line can ping someone (put {target} where the ping goes) "
            "or just be said."
        )
    rows: list[str] = []
    for i, line in enumerate(s.rare_lines):
        who = f"pings <@{line.target_id}>" if line.target_id else "no ping"
        armed = " · **next link**" if s.rare_armed == i else ""
        row = f"{i + 1}. {_clip(line.text, 48)} ({who}){armed}"
        if sum(len(r) + 1 for r in rows) + len(row) > 1500:  # Discord caps a message at 2000 characters
            rows.append(f"…and {len(s.rare_lines) - i} more")
            break
        rows.append(row)
    return "Lines (BB picks one at random):\n" + "\n".join(rows) + f"\nOdds: {odds}"


def _channel(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "off"


def _on(value: bool) -> str:
    return "on" if value else "off"


def describe(
    section: str,
    s: GuildSettings,
    *,
    app_permissions: discord.Permissions | None = None,
    down_fixers: Collection[str] = (),
    now: datetime | None = None,
) -> str:
    """Plain-text summary of the current values in a section."""
    if section == "sites":
        on = [site_name(i) for i in SITE_IDS if s.site_enabled(i)]
        off = [site_name(i) for i in SITE_IDS if not s.site_enabled(i)]
        return f"On: {', '.join(on) or 'none'}\nOff: {', '.join(off) or 'none'}"
    if section == "channels":
        listed = " ".join(f"<#{c}>" for c in s.channels) or "none"
        mode = (
            "BB works only in the listed channels."
            if s.channel_mode == "allow"
            else "BB works everywhere except the listed channels."
        )
        return f"{mode}\nListed: {listed}"
    if section == "style":
        style = "copy the poster (webhook)" if s.reply_mode == "webhook" else "reply to the original"
        return f"Repost as: {style}\nSilent replies: {_on(s.silent)}"
    if section == "original":
        return "After a fix, BB will: " + dict(ORIGINAL_ACTIONS)[s.original_action].lower()
    if section == "quips":
        lewd = " ".join(f"<#{c}>" for c in s.lewd_channels) or "none"
        chaos = " ".join(f"<#{c}>" for c in s.chaos_channels) or "none"
        return (
            f"Quips: {quip_label(s.quip_rate).lower()}\n"
            f"Lewd channels: {lewd}\nChaos channels: {chaos}"
        )
    if section == "rare":
        return describe_rare(s)
    if section == "reruns":
        scope = "same channel" if s.rerun_same_channel else "whole server"
        return (
            f"Roasting reposts: {_on(s.reruns_enabled)}\nWindow: {s.rerun_window_days} days\n"
            f"Scope: {scope}\nRoast your own reposts: {_on(s.rerun_roast_self)}"
        )
    if section == "webhooks":
        return f"Fix links posted by webhooks: {_on(s.fix_webhooks)}"
    if section == "roundup":
        return (
            f"Channel: {_channel(s.roundup_channel_id)}\n"
            f"When: {WEEKDAYS[s.roundup_weekday]} {s.roundup_hour:02d}:00 ({s.timezone})\n"
            f"Brainlet award: {_on(s.brainlet_award)}"
        )
    if section == "alerts":
        return f"Fixer alerts go to: {_channel(s.alerts_channel_id)}"
    if section == "news":
        return (
            f"FGO JP news goes to: {_channel(s.news_channel_id)}\n"
            "-# New events and banners as they open, then a reminder 2 days and 1 day before they end."
        )
    if section == "skip":
        return f"Messages containing `{s.skip_word}` are ignored."
    if section == "translation":
        return f"Translate switches tweets to: `{s.translate_lang}`"
    if section == "timezone":
        local = (now or datetime.now(zone(s.timezone))).astimezone(zone(s.timezone))
        return f"Timezone: {s.timezone} (now {local:%H:%M} there)\nUsed for the weekly roundup."
    if section == "status":
        paused = (
            "**BB is paused.** She posts nothing (fixes, cards, replies, roundup, alerts) "
            "until you unpause her.\n\n"
            if s.paused
            else ""
        )
        perms = "\n".join(f"{'+' if ok else '-'} {label}" for label, ok in permission_report(app_permissions))
        fixers = "\n".join(fixer_report(sites.SITES.values(), down_fixers))
        return f"{paused}BB's permissions in this channel:\n```diff\n{perms}\n```\nFixers:\n{fixers}"
    raise KeyError(section)


# --- panel items ------------------------------------------------------------------------


class _Item:
    """Mixin: typed access to the panel this item sits on."""

    @property
    def panel(self) -> SettingsPanel:
        return self.view  # type: ignore[attr-defined,return-value]


class SectionSelect(_Item, discord.ui.Select):
    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=label, value=key, default=key == current) for key, label in SECTIONS
        ]
        super().__init__(placeholder="Section", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.section = self.values[0]
        await self.panel.show(interaction)


class ToggleButton(_Item, discord.ui.Button):
    """Flips a bool setting. Green when on."""

    def __init__(self, field: str, label: str, value: bool, row: int) -> None:
        style = discord.ButtonStyle.success if value else discord.ButtonStyle.secondary
        super().__init__(label=f"{label}: {_on(value)}", style=style, row=row)
        self.field, self.value = field, value

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, **{self.field: not self.value})


class ChoiceButton(_Item, discord.ui.Button):
    """One option of a radio group. The current one is blue."""

    def __init__(self, field: str, value: Any, label: str, current: Any, row: int) -> None:
        style = discord.ButtonStyle.primary if value == current else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=row)
        self.field, self.value = field, value

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, **{self.field: self.value})


class SiteButton(_Item, discord.ui.Button):
    def __init__(self, site_id: str, enabled: bool, row: int) -> None:
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=site_name(site_id), style=style, row=row)
        self.site_id = site_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, sites_enabled=toggled_sites(self.panel.settings, self.site_id))


class ChoiceSelect(_Item, discord.ui.Select):
    """Picks one int value for a setting."""

    def __init__(
        self, field: str, choices: Iterable[tuple[str, int]], current: int, placeholder: str, row: int
    ):
        choices = list(choices)
        if current not in [value for _, value in choices]:
            choices.append((str(current), current))
        options = [
            discord.SelectOption(label=label, value=str(value), default=value == current)
            for label, value in choices
        ]
        super().__init__(placeholder=placeholder, options=options, row=row)
        self.field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, **{self.field: int(self.values[0])})


class ChannelPicker(_Item, discord.ui.ChannelSelect):
    """Channel list (multi) or a single channel setting."""

    def __init__(
        self, field: str, current: Iterable[int], *, multi: bool, types: list, placeholder: str, row: int
    ):
        defaults = [discord.Object(c) for c in list(current)[:25]]
        super().__init__(
            channel_types=types,
            placeholder=placeholder,
            min_values=0 if multi else 1,
            max_values=25 if multi else 1,
            default_values=defaults,
            row=row,
        )
        self.field, self.multi = field, multi

    async def callback(self, interaction: discord.Interaction) -> None:
        ids = [channel.id for channel in self.values]
        value: Any = tuple(ids) if self.multi else (ids[0] if ids else None)
        await self.panel.apply(interaction, **{self.field: value})


class UserPicker(_Item, discord.ui.UserSelect):
    """One user (or nobody) for a setting."""

    def __init__(self, field: str, current: int | None, placeholder: str, row: int) -> None:
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=1,
            default_values=[discord.Object(current)] if current else [],
            row=row,
        )
        self.field = field

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, **{self.field: self.values[0].id if self.values else None})


class RareLineModal(discord.ui.Modal):
    """Writes a new line (index None) or rewrites the text of an existing one (its ping stays)."""

    def __init__(self, panel: SettingsPanel, index: int | None) -> None:
        super().__init__(title="Rare line", timeout=600)
        self.panel, self.index = panel, index
        lines = panel.settings.rare_lines
        current = lines[index].text if index is not None and index < len(lines) else None
        self.input = discord.ui.TextInput(
            label="Message ({target} marks the ping, optional)",
            style=discord.TextStyle.paragraph,
            default=current,
            placeholder="{target} BB was thinking about you♡",
            max_length=1500,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.input.value.strip()
        lines = list(self.panel.settings.rare_lines)
        if self.index is None:
            if len(lines) >= MAX_RARE_LINES:
                await self.panel.show(interaction, notice=f"Not saved: {MAX_RARE_LINES} lines is the limit.")
                return
            lines.append(RareLine(text))
            self.panel.rare_index = len(lines) - 1
        elif self.index < len(lines):
            lines[self.index] = RareLine(text, lines[self.index].target_id)
        await self.panel.apply(interaction, rare_lines=tuple(lines))


class RareLineSelect(_Item, discord.ui.Select):
    """Which line the buttons below act on."""

    def __init__(self, lines: tuple[RareLine, ...], selected: int, row: int) -> None:
        options = [
            discord.SelectOption(
                label=_clip(f"{i + 1}. {line.text}", 100), value=str(i), default=i == selected
            )
            for i, line in enumerate(lines)
        ]
        super().__init__(placeholder="Line", options=options, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.panel.rare_index = int(self.values[0])
        await self.panel.show(interaction)


class RareTargetPicker(_Item, discord.ui.UserSelect):
    """Who the selected line pings, or nobody (then it's just said)."""

    def __init__(self, line: RareLine, row: int) -> None:
        super().__init__(
            placeholder="Who this line pings (optional)",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(line.target_id)] if line.target_id else [],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.panel
        lines = list(panel.settings.rare_lines)
        if panel.rare_index < len(lines):
            target = self.values[0].id if self.values else None
            lines[panel.rare_index] = RareLine(lines[panel.rare_index].text, target)
        await panel.apply(interaction, rare_lines=tuple(lines))


class RareRemoveButton(_Item, discord.ui.Button):
    def __init__(self, row: int) -> None:
        super().__init__(label="Remove", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.panel
        lines = list(panel.settings.rare_lines)
        index = panel.rare_index
        if index >= len(lines):
            await panel.show(interaction)
            return
        del lines[index]
        armed = panel.settings.rare_armed
        if armed is not None:
            armed = None if armed == index else (armed - 1 if armed > index else armed)
        panel.rare_index = max(0, min(index, len(lines) - 1))
        await panel.apply(interaction, rare_lines=tuple(lines), rare_armed=armed)


class RarePreviewButton(_Item, discord.ui.Button):
    """Shows the selected line as BB would post it, privately and without pinging anyone."""

    def __init__(self, row: int) -> None:
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        lines = self.panel.settings.rare_lines
        index = self.panel.rare_index
        text = rare_line(lines[index]) if index < len(lines) else "No line selected."
        await interaction.response.send_message(
            f"-# {text}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


class RoundupPreviewButton(_Item, discord.ui.Button):
    """This week's roundup as it stands now, privately: nothing is posted or marked as sent."""

    def __init__(self, row: int) -> None:
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = self.panel
        roundup = panel.bot.get_cog("Roundup")
        if roundup is None:
            await interaction.response.send_message(strings.line("error.generic"), ephemeral=True)
            return
        embed = await roundup.build(panel.guild_id, now(), panel.settings.brainlet_award)
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


class NewsPreviewButton(_Item, discord.ui.Button):
    """What's running in FGO JP right now, in the news format, privately: nothing is posted or recorded."""

    def __init__(self, row: int) -> None:
        super().__init__(label="What's on now", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Looking up the pictures can take longer than Discord's 3 seconds to answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.panel.bot.get_cog("News")
        notices = cog.live(now()) if cog is not None else None
        if cog is None or notices is None:
            await interaction.followup.send(
                strings.line("news.no_data"), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        await cog.add_details(notices)
        notices = notices or [news.Notice(strings.line("news.nothing"))]
        notices.insert(0, news.Notice(strings.line("news.preview_title")))
        for message in news.messages(notices):
            await interaction.followup.send(ephemeral=True, **cog.send_kwargs(message))


class SetButton(_Item, discord.ui.Button):
    """Sets a field to a fixed value (e.g. 'Turn off' clears a channel)."""

    def __init__(self, field: str, value: Any, label: str, row: int, disabled: bool = False) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row, disabled=disabled)
        self.field, self.value = field, value

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, **{self.field: self.value})


class RefreshButton(_Item, discord.ui.Button):
    def __init__(self, row: int) -> None:
        super().__init__(label="Refresh", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.panel.show(interaction)


class TextSettingModal(discord.ui.Modal):
    def __init__(
        self,
        panel: SettingsPanel,
        field: str,
        title: str,
        label: str,
        parse: Callable[[str], Any],
        what: str,
        max_length: int,
    ) -> None:
        super().__init__(title=title, timeout=300)
        self.panel, self.field, self.parse, self.what = panel, field, parse, what
        self.input = discord.ui.TextInput(
            label=label, default=str(getattr(panel.settings, field)), max_length=max_length, required=True
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.input.value
        value = self.parse(raw)
        if value is None:
            shown = raw.strip().replace("`", "'")[:64]
            await self.panel.show(interaction, notice=f"Not saved: `{shown}` isn't a valid {self.what}.")
            return
        await self.panel.apply(interaction, **{self.field: value})


class ModalButton(_Item, discord.ui.Button):
    def __init__(self, label: str, make_modal: Callable[[SettingsPanel], discord.ui.Modal], row: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=row)
        self.make_modal = make_modal

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(self.make_modal(self.panel))


# --- sections ---------------------------------------------------------------------------


def _build_sites(p: SettingsPanel) -> None:
    for i, site_id in enumerate(SITE_IDS):
        p.add_item(SiteButton(site_id, p.settings.site_enabled(site_id), row=1 + i // 5))


def _build_channels(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(
        ChannelPicker(
            "channels",
            p.existing(s.channels),
            multi=True,
            types=_LIST_CHANNELS,
            placeholder="Channels on the list",
            row=1,
        )
    )
    p.add_item(ChoiceButton("channel_mode", "deny", "Everywhere except these", s.channel_mode, row=2))
    p.add_item(ChoiceButton("channel_mode", "allow", "Only these", s.channel_mode, row=2))


def _build_style(p: SettingsPanel) -> None:
    for value, label in REPLY_MODES:
        p.add_item(ChoiceButton("reply_mode", value, label, p.settings.reply_mode, row=1))
    p.add_item(ToggleButton("silent", "Silent", p.settings.silent, row=2))


def _build_original(p: SettingsPanel) -> None:
    for value, label in ORIGINAL_ACTIONS:
        p.add_item(ChoiceButton("original_action", value, label, p.settings.original_action, row=1))


def _build_quips(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(ChoiceSelect("quip_rate", QUIP_CHOICES, s.quip_rate, "Quip rate", row=1))
    p.add_item(
        ChannelPicker(
            "lewd_channels",
            p.existing(s.lewd_channels),
            multi=True,
            types=_LIST_CHANNELS,
            placeholder="Lewd channels (flirty lines)",
            row=2,
        )
    )
    p.add_item(
        ChannelPicker(
            "chaos_channels",
            p.existing(s.chaos_channels),
            multi=True,
            types=_LIST_CHANNELS,
            placeholder="Chaos channels (/b/-style lines)",
            row=3,
        )
    )


def _build_rare(p: SettingsPanel) -> None:
    s = p.settings
    lines = s.rare_lines
    p.rare_index = max(0, min(p.rare_index, len(lines) - 1))
    if lines:
        p.add_item(RareLineSelect(lines, p.rare_index, row=1))
        p.add_item(RareTargetPicker(lines[p.rare_index], row=2))
    p.add_item(ChoiceSelect("rare_rate", RARE_ODDS, s.rare_rate, "Odds", row=3))
    add = ModalButton("Add line", lambda panel: RareLineModal(panel, None), row=4)
    add.disabled = len(lines) >= MAX_RARE_LINES
    p.add_item(add)
    if lines:
        p.add_item(ModalButton("Edit", lambda panel: RareLineModal(panel, panel.rare_index), row=4))
        p.add_item(RarePreviewButton(row=4))
        p.add_item(
            SetButton(
                "rare_armed", p.rare_index, "Use on next link", row=4, disabled=s.rare_armed == p.rare_index
            )
        )
        p.add_item(RareRemoveButton(row=4))


def _build_reruns(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(ToggleButton("reruns_enabled", "Roast reposts", s.reruns_enabled, row=1))
    p.add_item(ToggleButton("rerun_roast_self", "Roast your own reposts", s.rerun_roast_self, row=1))
    p.add_item(ChoiceButton("rerun_same_channel", False, "Whole server", s.rerun_same_channel, row=2))
    p.add_item(ChoiceButton("rerun_same_channel", True, "Same channel", s.rerun_same_channel, row=2))
    windows = [(f"{d} day" if d == 1 else f"{d} days", d) for d in WINDOW_CHOICES]
    p.add_item(ChoiceSelect("rerun_window_days", windows, s.rerun_window_days, "Window", row=3))


def _build_webhooks(p: SettingsPanel) -> None:
    p.add_item(ToggleButton("fix_webhooks", "Fix webhook posts", p.settings.fix_webhooks, row=1))


def _build_roundup(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(
        ChannelPicker(
            "roundup_channel_id",
            p.existing([s.roundup_channel_id]),
            multi=False,
            types=_TEXT_CHANNELS,
            placeholder="Roundup channel",
            row=1,
        )
    )
    p.add_item(
        ChoiceSelect(
            "roundup_weekday", [(d, i) for i, d in enumerate(WEEKDAYS)], s.roundup_weekday, "Day", row=2
        )
    )
    p.add_item(
        ChoiceSelect("roundup_hour", [(f"{h:02d}:00", h) for h in range(24)], s.roundup_hour, "Hour", row=3)
    )
    p.add_item(
        SetButton("roundup_channel_id", None, "Turn off", row=4, disabled=s.roundup_channel_id is None)
    )
    p.add_item(ToggleButton("brainlet_award", "Brainlet award", s.brainlet_award, row=4))
    p.add_item(RoundupPreviewButton(row=4))


def _build_alerts(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(
        ChannelPicker(
            "alerts_channel_id",
            p.existing([s.alerts_channel_id]),
            multi=False,
            types=_TEXT_CHANNELS,
            placeholder="Alerts channel",
            row=1,
        )
    )
    p.add_item(SetButton("alerts_channel_id", None, "Turn off", row=2, disabled=s.alerts_channel_id is None))


def _build_news(p: SettingsPanel) -> None:
    s = p.settings
    p.add_item(
        ChannelPicker(
            "news_channel_id",
            p.existing([s.news_channel_id]),
            multi=False,
            types=_TEXT_CHANNELS,
            placeholder="News channel",
            row=1,
        )
    )
    p.add_item(SetButton("news_channel_id", None, "Turn off", row=2, disabled=s.news_channel_id is None))
    p.add_item(NewsPreviewButton(row=2))


def _build_skip(p: SettingsPanel) -> None:
    p.add_item(
        ModalButton(
            "Change skip word",
            lambda panel: TextSettingModal(
                panel,
                "skip_word",
                "Skip word",
                "Word that makes BB ignore a message",
                parse_skip_word,
                "skip word",
                32,
            ),
            row=1,
        )
    )


def _build_translation(p: SettingsPanel) -> None:
    p.add_item(
        ModalButton(
            "Change language",
            lambda panel: TextSettingModal(
                panel,
                "translate_lang",
                "Translation language",
                "Language code (en, ja, es, ...)",
                parse_language,
                "language code",
                12,
            ),
            row=1,
        )
    )


def _build_timezone(p: SettingsPanel) -> None:
    p.add_item(
        ModalButton(
            "Change timezone",
            lambda panel: TextSettingModal(
                panel,
                "timezone",
                "Timezone",
                "IANA timezone (e.g. Europe/Berlin)",
                parse_timezone,
                "timezone",
                64,
            ),
            row=1,
        )
    )


def _build_status(p: SettingsPanel) -> None:
    p.add_item(ToggleButton("paused", "Paused", p.settings.paused, row=1))
    p.add_item(RefreshButton(row=1))


BUILDERS: dict[str, Callable[[SettingsPanel], None]] = {
    "sites": _build_sites,
    "channels": _build_channels,
    "style": _build_style,
    "original": _build_original,
    "quips": _build_quips,
    "rare": _build_rare,
    "reruns": _build_reruns,
    "webhooks": _build_webhooks,
    "roundup": _build_roundup,
    "alerts": _build_alerts,
    "news": _build_news,
    "skip": _build_skip,
    "translation": _build_translation,
    "timezone": _build_timezone,
    "status": _build_status,
}


# --- the panel --------------------------------------------------------------------------


class SettingsPanel(discord.ui.View):
    def __init__(
        self,
        bot: Any,
        guild_id: int,
        owner_id: int,
        settings: GuildSettings,
        *,
        guild: discord.Guild | None = None,
        app_permissions: discord.Permissions | None = None,
        section: str = "sites",
    ) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.bot, self.guild_id, self.owner_id, self.guild = bot, guild_id, owner_id, guild
        self.settings = settings
        self.section = section
        self.rare_index = 0  # the line the rare-line buttons act on
        self.app_permissions = app_permissions
        self.title = strings.line("settings.title")
        self.notice: str | None = None
        self.last_interaction: discord.Interaction | None = None
        self.rebuild()

    def existing(self, channel_ids: Iterable[int | None]) -> list[int]:
        """Drop unset and deleted channels; Discord rejects select defaults it can't resolve."""
        ids = [c for c in channel_ids if c]
        if self.guild is None:
            return ids
        return [c for c in ids if self.guild.get_channel_or_thread(c) is not None]

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(SectionSelect(self.section))
        BUILDERS[self.section](self)

    def render(self) -> str:
        body = describe(
            self.section,
            self.settings,
            app_permissions=self.app_permissions,
            down_fixers=self.bot.down_fixers,
        )
        text = f"**{self.title}**\n### {SECTION_LABELS[self.section]}\n{body}"
        if self.notice:
            text += f"\n-# {self.notice}"
        return text

    async def show(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        """Redraw the panel on the message the interaction came from."""
        self.notice = notice
        self.last_interaction = interaction
        if interaction.app_permissions is not None:
            self.app_permissions = interaction.app_permissions
        self.rebuild()
        await interaction.response.edit_message(
            content=self.render(), view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    async def apply(self, interaction: discord.Interaction, **changes: Any) -> None:
        self.settings = await self.bot.settings.update(self.guild_id, **changes)
        await self.show(interaction, notice="Saved.")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("settings panel error in %r", item, exc_info=error)
        send = (
            interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        )
        try:
            await send(strings.line("error.generic"), ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.last_interaction is None:
            return
        try:
            await self.last_interaction.edit_original_response(view=None)
        except discord.HTTPException:
            pass  # the interaction token expired (15 minutes); the buttons just stop working


class Settings(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(name="settings", description="BB's settings for this server")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings_command(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None  # guild_only
        panel = SettingsPanel(
            self.bot,
            interaction.guild_id,
            interaction.user.id,
            await self.bot.settings.get(interaction.guild_id),
            guild=interaction.guild,
            app_permissions=interaction.app_permissions,
        )
        await interaction.response.send_message(
            panel.render(), view=panel, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
        panel.last_interaction = interaction


async def setup(bot: Any) -> None:
    await bot.add_cog(Settings(bot))
