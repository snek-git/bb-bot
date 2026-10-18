"""The button row under a repost. Owned by track C; make_repost_view's signature is track B's contract.

The buttons are DynamicItems: all their state lives in the custom_id, so they keep working after a restart.
This module only knows what the buttons look like. The buttons cog registers them and does the work; each
callback finds that cog at click time, so this module never imports bb.cogs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

import discord

from bb import strings

log = logging.getLogger(__name__)

BUTTONS_COG = "Buttons"

DELETE_TEMPLATE = r"bb:del:(?P<author_id>[0-9]{1,20})"
TRANSLATE_TEMPLATE = r"bb:tr:(?P<state>[01])"
FIND_SOURCE_ID = "bb:src"


def delete_id(origin_author_id: int) -> str:
    return f"bb:del:{origin_author_id}"


def translate_id(translated: bool) -> str:
    return f"bb:tr:{int(translated)}"


def parse_delete_id(custom_id: str) -> int | None:
    m = re.fullmatch(DELETE_TEMPLATE, custom_id)
    return int(m["author_id"]) if m else None


def parse_translate_id(custom_id: str) -> bool | None:
    m = re.fullmatch(TRANSLATE_TEMPLATE, custom_id)
    return m["state"] == "1" if m else None


def component_custom_ids(components: Iterable[Any]) -> list[str]:
    """Every custom_id in a message's components, walking into action rows."""
    found: list[str] = []
    for component in components:
        custom_id = getattr(component, "custom_id", None)
        if custom_id:
            found.append(custom_id)
        found.extend(component_custom_ids(getattr(component, "children", None) or ()))
    return found


def has_find_source(components: Iterable[Any]) -> bool:
    return FIND_SOURCE_ID in component_custom_ids(components)


async def _dispatch(interaction: discord.Interaction, method: str, *args: Any) -> None:
    cog = interaction.client.get_cog(BUTTONS_COG)  # type: ignore[attr-defined]
    if cog is None:
        log.error("button %s clicked but the %s cog isn't loaded", method, BUTTONS_COG)
        if not interaction.response.is_done():
            await interaction.response.send_message(strings.line("error.generic"), ephemeral=True)
        return
    await getattr(cog, method)(interaction, *args)


class DeleteButton(discord.ui.DynamicItem[discord.ui.Button], template=DELETE_TEMPLATE):
    def __init__(self, origin_author_id: int) -> None:
        self.origin_author_id = origin_author_id
        super().__init__(
            discord.ui.Button(
                label="Delete", style=discord.ButtonStyle.secondary, custom_id=delete_id(origin_author_id)
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> DeleteButton:
        return cls(int(match["author_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dispatch(interaction, "on_delete", self.origin_author_id)


class TranslateButton(discord.ui.DynamicItem[discord.ui.Button], template=TRANSLATE_TEMPLATE):
    def __init__(self, translated: bool) -> None:
        self.translated = translated
        super().__init__(
            discord.ui.Button(
                label="Original" if translated else "Translate",
                style=discord.ButtonStyle.secondary,
                custom_id=translate_id(translated),
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> TranslateButton:
        return cls(match["state"] == "1")

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dispatch(interaction, "on_translate", self.translated)


class FindSourceButton(discord.ui.DynamicItem[discord.ui.Button], template=re.escape(FIND_SOURCE_ID)):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Find source", style=discord.ButtonStyle.secondary, custom_id=FIND_SOURCE_ID
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> FindSourceButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await _dispatch(interaction, "on_find_source")


DYNAMIC_ITEMS = (DeleteButton, TranslateButton, FindSourceButton)


SHOW_DELETE = False  # Delete is parked until we decide who may use it; its handler still works.

def make_repost_view(
    *,
    site_id: str,
    origin_author_id: int,
    translated: bool = False,
    has_image: bool = False,
    translatable: bool | None = None,
) -> discord.ui.View:
    """Find source when has_image; Translate (or Original, when translated) for twitter. (The link to the
    original post is the small "Tweet" line in the message text.)

    Buttons are DynamicItems registered by the buttons cog, so this view needs no timeout or state.
    """
    view = discord.ui.View(timeout=None)
    if has_image:
        view.add_item(FindSourceButton())
    if site_id == "twitter" if translatable is None else translatable:
        view.add_item(TranslateButton(translated))
    if SHOW_DELETE:
        view.add_item(DeleteButton(origin_author_id))
    return view
