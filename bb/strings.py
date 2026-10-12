"""BB's lines. line("quip.fix", site="Twitter") picks a random variant and fills it in."""

from __future__ import annotations

import random
import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

STRINGS_PATH = Path(__file__).with_name("strings.yaml")
EMOJI_PLACEHOLDERS = ("bb_smug", "bb_annoyed", "bb_wink")

_emojis: dict[str, str] = {}


def set_emojis(emojis: dict[str, str]) -> None:
    """Called once at startup with Config.emojis."""
    _emojis.clear()
    _emojis.update(emojis)


@cache
def _data() -> dict[str, dict[str, Any]]:
    with STRINGS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def keys() -> list[str]:
    return list(_data())


def declared_vars(key: str) -> list[str]:
    return list(_data()[key].get("vars") or [])


def variants(key: str) -> list[str]:
    return list(_data()[key]["lines"])


class _Fill(dict):
    def __missing__(self, name: str) -> str:
        if name in EMOJI_PLACEHOLDERS:
            return ""
        raise KeyError(f"string placeholder {{{name}}} was not provided")


def fill(text: str, **values: Any) -> str:
    out = text.format_map(_Fill({**{name: _emojis.get(name, "") for name in EMOJI_PLACEHOLDERS}, **values}))
    return re.sub(r" {2,}", " ", out).strip()


_bags: dict[str, list[str]] = {}
_last: dict[str, str] = {}


def next_variant(key: str) -> str:
    """Shuffle-bag pick: every variant comes up once (in random order) before any repeats, and the same
    line never comes twice in a row, even across reshuffles."""
    options = variants(key)
    if len(options) == 1:
        return options[0]
    bag = _bags.get(key)
    if not bag:
        bag = options[:]
        random.shuffle(bag)
        if bag[-1] == _last.get(key):  # picks pop from the end
            bag[0], bag[-1] = bag[-1], bag[0]
        _bags[key] = bag
    choice = bag.pop()
    _last[key] = choice
    return choice


def line(key: str, **values: Any) -> str:
    """The next variant of `key`, filled in. Raises KeyError for unknown keys or missing values."""
    return fill(next_variant(key), **values)
