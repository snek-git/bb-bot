"""Runtime configuration from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_EMOJI_PREFIX = "EMOJI_"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables win over the file."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _optional_int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


@dataclass(frozen=True)
class Config:
    token: str | None = None
    saucenao_key: str | None = None
    db_path: str = "data/bb.sqlite"
    dev_guild_id: int | None = None
    log_level: str = "INFO"
    link_style: str = "hidden"  # "hidden" (just the preview) or "subtle" (small "@user on Twitter" line)
    # Placeholder name used in strings.yaml (e.g. "bb_smug") -> Discord emoji markup.
    emojis: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, dotenv: Path | None = Path(".env")) -> Config:
        if dotenv is not None:
            load_dotenv(dotenv)
        env = os.environ
        emojis = {
            key[len(_EMOJI_PREFIX):].lower(): value.strip()
            for key, value in env.items()
            if key.startswith(_EMOJI_PREFIX) and value.strip()
        }
        return cls(
            token=env.get("DISCORD_TOKEN") or None,
            saucenao_key=env.get("SAUCENAO_API_KEY") or None,
            db_path=env.get("DATABASE_PATH") or "data/bb.sqlite",
            dev_guild_id=_optional_int(env.get("DEV_GUILD_ID")),
            log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
            link_style=(env.get("LINK_STYLE") or "hidden").strip().lower(),
            emojis=emojis,
        )
