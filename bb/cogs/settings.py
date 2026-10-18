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

from bb import strings
from bb.cogs.linkfix import rare_line
from bb.links import sites
from bb.settings_store import SITE_IDS, GuildSettings
from bb.timeutil import zone

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
        text = s.rare_text.strip()
        message = "\n".join(f"> {line}" for line in text.splitlines()) if text else "not written yet"
        who = f"<@{s.rare_target_id}>" if s.rare_target_id else "nobody"
        odds = "off" if s.rare_rate <= 0 else f"1 in {s.rare_rate} fixed links"
        hint = "" if text else "\n-# Write a message to switch it on. Put {target} where the ping should go."
        armed = "\nArmed: the next fixed link gets it." if s.rare_armed and text else ""
        return f"Message:\n{message}\nPings: {who}\nOdds: {odds}{armed}{hint}"
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


class RareTextModal(discord.ui.Modal):
    def __init__(self, panel: SettingsPanel) -> None:
        super().__init__(title="Rare line", timeout=600)
        self.panel = panel
        self.input = discord.ui.TextInput(
            label="Message ({target} = where the ping goes)",
            style=discord.TextStyle.paragraph,
            default=panel.settings.rare_text or None,
            placeholder="{target} BB was thinking about you♡",
            max_length=1500,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.apply(interaction, rare_text=self.input.value.strip())


class RarePreviewButton(_Item, discord.ui.Button):
    """Shows the line as BB would post it, privately and without pinging anyone."""

    def __init__(self, row: int, disabled: bool) -> None:
        super().__init__(label="Preview", style=discord.ButtonStyle.secondary, row=row, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"-# {rare_line(self.panel.settings)}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


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
    p.add_item(UserPicker("rare_target_id", s.rare_target_id, "Who gets pinged", row=1))
    p.add_item(ChoiceSelect("rare_rate", RARE_ODDS, s.rare_rate, "Odds", row=2))
    p.add_item(ModalButton("Write message", RareTextModal, row=3))
    p.add_item(RarePreviewButton(row=3, disabled=not s.rare_text))
    p.add_item(
        SetButton("rare_armed", True, "Use on next link", row=3, disabled=not s.rare_text or s.rare_armed)
    )
    p.add_item(SetButton("rare_text", "", "Turn off", row=3, disabled=not s.rare_text))


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
