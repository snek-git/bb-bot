"""Per-server settings: code defaults merged with whatever /settings saved for that server."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

from bb.db import Database

SITE_IDS = ("twitter", "pixiv", "reddit", "tiktok", "bluesky", "instagram", "ehentai", "nhentai")
CHANNEL_LIST_FIELDS = ("channels", "lewd_channels", "chaos_channels")


def _all_sites_on() -> dict[str, bool]:
    return {site_id: True for site_id in SITE_IDS}


MAX_RARE_LINES = 25  # a select menu holds 25 options


@dataclass(frozen=True)
class RareLine:
    """One custom rare line. target_id: who it pings ({target} marks where), or None for a plain line."""

    text: str
    target_id: int | None = None


@dataclass(frozen=True)
class GuildSettings:
    # Emergency stop: BB posts nothing at all (fixes, cards, ping replies, roundup, alerts) while set.
    paused: bool = False
    # Sites
    sites_enabled: dict[str, bool] = field(default_factory=_all_sites_on)
    # Channels: "deny" = works everywhere except `channels`; "allow" = only in `channels`.
    channel_mode: Literal["deny", "allow"] = "deny"
    channels: tuple[int, ...] = ()
    # Repost style
    reply_mode: Literal["reply", "webhook"] = "reply"
    silent: bool = True
    # Original message after a successful fix
    original_action: Literal["suppress", "delete", "nothing"] = "suppress"
    # Quips: 0 = off, 1 = every fix, N = on 1 in N fixes
    # Random quips on successful fixes: off by default (roasts, failures and the rare line still talk).
    quip_rate: int = 0
    # Channels whose quips come from a themed pool instead of the normal one.
    lewd_channels: tuple[int, ...] = ()  # e.g. #2d-cum-zone: flirty teasing
    chaos_channels: tuple[int, ...] = ()  # e.g. #brazil (/b/): disgusted-but-amused
    # Custom lines: on 1 in rare_rate fixes, BB says one of them at random.
    rare_lines: tuple[RareLine, ...] = ()
    rare_rate: int = 1000
    rare_armed: int | None = None  # "Use on next link": index of the line the next fixed link gets
    timezone: str = "UTC"
    # Reruns
    reruns_enabled: bool = True
    rerun_window_days: int = 30
    rerun_same_channel: bool = False
    rerun_roast_self: bool = False
    # Messages from webhooks
    fix_webhooks: bool = False
    # Weekly roundup (weekday: Monday = 0)
    roundup_channel_id: int | None = None
    roundup_weekday: int = 6
    roundup_hour: int = 18
    brainlet_award: bool = True
    roundup_last_sent: float = 0.0  # set by the roundup cog, not shown in /settings
    # Admin alerts
    alerts_channel_id: int | None = None
    # FGO JP news (new events and banners, end reminders). Off until a channel is set.
    news_channel_id: int | None = None
    # Misc
    skip_word: str = "nobb"
    translate_lang: str = "en"

    def site_enabled(self, site_id: str) -> bool:
        return self.sites_enabled.get(site_id, True)

    def channel_enabled(self, channel_id: int) -> bool:
        listed = channel_id in self.channels
        return listed if self.channel_mode == "allow" else not listed

    def channel_flavor(self, channel_id: int) -> Literal["lewd", "chaos"] | None:
        """Which themed quip pool a channel uses, if any. Pass the parent channel for threads."""
        if channel_id in self.lewd_channels:
            return "lewd"
        if channel_id in self.chaos_channels:
            return "chaos"
        return None

    @classmethod
    def from_overrides(cls, data: dict[str, Any]) -> GuildSettings:
        data = _upgrade_rare(data)
        known = {f.name for f in fields(cls)}
        clean = {key: value for key, value in data.items() if key in known}
        if "sites_enabled" in clean:
            clean["sites_enabled"] = {**_all_sites_on(), **clean["sites_enabled"]}
        for key in CHANNEL_LIST_FIELDS:
            if key in clean:
                clean[key] = tuple(int(c) for c in clean[key])
        if "rare_lines" in clean:
            clean["rare_lines"] = rare_lines(clean["rare_lines"])
        return cls(**clean)

    def to_overrides(self) -> dict[str, Any]:
        """Only the values that differ from the defaults, JSON-friendly."""
        defaults = asdict(GuildSettings())
        current = asdict(self)
        for key in (*CHANNEL_LIST_FIELDS, "rare_lines"):
            current[key] = list(current[key])
            defaults[key] = list(defaults[key])
        return {key: value for key, value in current.items() if value != defaults[key]}


def rare_lines(raw: Any) -> tuple[RareLine, ...]:
    """Lines from saved JSON (dicts) or code (RareLine), blanks dropped."""
    lines = []
    for item in raw or ():
        line = item if isinstance(item, RareLine) else RareLine(
            str(item.get("text") or ""), int(item["target_id"]) if item.get("target_id") else None
        )
        if line.text.strip():
            lines.append(line)
    return tuple(lines[:MAX_RARE_LINES])


def _upgrade_rare(data: dict[str, Any]) -> dict[str, Any]:
    """Settings saved before there were several lines: one rare_text/rare_target_id, a bool rare_armed."""
    data = dict(data)
    text = data.pop("rare_text", None)
    target = data.pop("rare_target_id", None)
    if "rare_lines" not in data and isinstance(text, str) and text.strip():
        data["rare_lines"] = [{"text": text, "target_id": target}]
    armed = data.get("rare_armed")
    if isinstance(armed, bool):
        data["rare_armed"] = 0 if armed and data.get("rare_lines") else None
    return data


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[int, GuildSettings] = {}
        # One update at a time: two writers reading the same "current" would make one of them lost
        # (e.g. the roundup saving roundup_last_sent while an admin clicks in /settings).
        self._write_lock = asyncio.Lock()

    async def get(self, guild_id: int) -> GuildSettings:
        cached = self._cache.get(guild_id)
        if cached is None:
            loaded = GuildSettings.from_overrides(await self._db.get_guild_settings(guild_id))
            # an update may have landed while this read was waiting: keep the newer value
            cached = self._cache.setdefault(guild_id, loaded)
        return cached

    async def update(self, guild_id: int, **changes: Any) -> GuildSettings:
        for key in CHANNEL_LIST_FIELDS:
            if key in changes:
                changes[key] = tuple(int(c) for c in changes[key])
        if "rare_lines" in changes:
            changes["rare_lines"] = rare_lines(changes["rare_lines"])
        async with self._write_lock:
            current = await self.get(guild_id)
            updated = replace(current, **changes)
            await self._db.set_guild_settings(guild_id, updated.to_overrides())
            self._cache[guild_id] = updated
        return updated
