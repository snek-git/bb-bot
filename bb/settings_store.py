"""Per-server settings: code defaults merged with whatever /settings saved for that server."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

from bb.db import Database

SITE_IDS = ("twitter", "pixiv", "reddit", "tiktok", "bluesky", "instagram", "ehentai")
CHANNEL_LIST_FIELDS = ("channels", "lewd_channels", "chaos_channels")


def _all_sites_on() -> dict[str, bool]:
    return {site_id: True for site_id in SITE_IDS}


@dataclass(frozen=True)
class GuildSettings:
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
    quip_rate: int = 100
    # Channels whose quips come from a themed pool instead of the normal one.
    lewd_channels: tuple[int, ...] = ()  # e.g. #2d-cum-zone: flirty teasing
    chaos_channels: tuple[int, ...] = ()  # e.g. #brazil (/b/): disgusted-but-amused
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
        known = {f.name for f in fields(cls)}
        clean = {key: value for key, value in data.items() if key in known}
        if "sites_enabled" in clean:
            clean["sites_enabled"] = {**_all_sites_on(), **clean["sites_enabled"]}
        for key in CHANNEL_LIST_FIELDS:
            if key in clean:
                clean[key] = tuple(int(c) for c in clean[key])
        return cls(**clean)

    def to_overrides(self) -> dict[str, Any]:
        """Only the values that differ from the defaults, JSON-friendly."""
        defaults = asdict(GuildSettings())
        current = asdict(self)
        for key in CHANNEL_LIST_FIELDS:
            current[key] = list(current[key])
            defaults[key] = list(defaults[key])
        return {key: value for key, value in current.items() if value != defaults[key]}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[int, GuildSettings] = {}

    async def get(self, guild_id: int) -> GuildSettings:
        cached = self._cache.get(guild_id)
        if cached is None:
            cached = GuildSettings.from_overrides(await self._db.get_guild_settings(guild_id))
            self._cache[guild_id] = cached
        return cached

    async def update(self, guild_id: int, **changes: Any) -> GuildSettings:
        current = await self.get(guild_id)
        for key in CHANNEL_LIST_FIELDS:
            if key in changes:
                changes[key] = tuple(int(c) for c in changes[key])
        updated = replace(current, **changes)
        await self._db.set_guild_settings(guild_id, updated.to_overrides())
        self._cache[guild_id] = updated
        return updated
