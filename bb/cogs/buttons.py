"""Repost buttons (Delete, Translate, Find source) and the "Remove BB's repost" right-click menu.

The DynamicItems live in bb.views; this cog registers them and does the work.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bb import render, strings, views
from bb.db import RepostRow
from bb.links import sites
from bb.links.model import Fixer

log = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


# --- pure decisions ---------------------------------------------------------------------


def can_delete(user_id: int, origin_author_id: int, manage_messages: bool) -> bool:
    """The original poster or anyone with Manage Messages may delete a repost."""
    return user_id == origin_author_id or manage_messages


def translation_fixer(current: Fixer | None, site_id: str, down: set[str]) -> Fixer | None:
    """The fixer to use for a translated link: the current one if it can translate, else the first
    translatable fixer in the site's chain (healthy ones first)."""
    if current is not None and current.translatable:
        return current
    return next((f for f in sites.chain(site_id, down) if f.translatable), None)


def translated_text(
    row: RepostRow, translate: bool, lang: str, down: set[str]
) -> tuple[str, Fixer, str | None]:
    """(new repost text, fixer used, new quip) for flipping a repost to translated or back.

    Translating is silent: the repost keeps whatever line it had (a repost roast, the rare line).
    Raises LookupError if the row can't be rebuilt.
    """
    if row.status in ("uploaded", "failed"):
        raise LookupError(f"repost {row.repost_id} has no fixer link (status {row.status})")
    match = sites.match(row.original_url, spoiler=row.spoiler)
    current = sites.get_fixer(row.fixer_id) if row.fixer_id else None
    if match is None:
        raise LookupError(f"repost {row.repost_id}: {row.original_url} no longer matches a site")
    fixer = translation_fixer(current, row.site, down) if translate else current
    if fixer is None:
        raise LookupError(f"repost {row.repost_id}: no usable fixer")
    quip = row.quip
    url = sites.fixed_url(match, fixer, lang=lang if translate else None)
    return render.format_repost(match, fixer.name, url, quip=quip), fixer, quip


def _thread_of(channel: Any) -> Any:
    """thread= for webhook calls: the thread when the repost lives in one, else MISSING."""
    return discord.Object(channel.id) if isinstance(channel, discord.Thread) else discord.utils.MISSING


def webhook_id_for(row: RepostRow | None, message: Any) -> int | None:
    """The webhook that sent this repost, if any. Falls back to the message itself when the row is gone."""
    if row is not None and row.via_webhook and row.webhook_id:
        return row.webhook_id
    return getattr(message, "webhook_id", None) or None


# --- the cog ----------------------------------------------------------------------------


class Buttons(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        # Removing a repost lives in the right-click menu (Apps), so reposts carry no Delete button.
        self.remove_menu = app_commands.ContextMenu(name="Remove BB's repost", callback=self.remove_menu_used)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*views.DYNAMIC_ITEMS)
        self.bot.tree.add_command(self.remove_menu)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(*views.DYNAMIC_ITEMS)
        self.bot.tree.remove_command(self.remove_menu.name, type=self.remove_menu.type)

    # --- delete -------------------------------------------------------------------------

    async def on_delete(self, interaction: discord.Interaction, origin_author_id: int) -> None:
        message = interaction.message
        if message is None:
            return
        await self._remove(interaction, message, origin_author_id, from_menu=False)

    async def remove_menu_used(self, interaction: discord.Interaction, message: discord.Message) -> None:
        row = await self.bot.db.get_repost(message.id)
        if row is None:
            await interaction.response.send_message(
                strings.line("delete.not_repost"), ephemeral=True, allowed_mentions=_NO_MENTIONS
            )
            return
        await self._remove(interaction, message, row.origin_author_id, from_menu=True)

    async def _remove(
        self, interaction: discord.Interaction, message: Any, origin_author_id: int, *, from_menu: bool
    ) -> None:
        """Take one repost down: only its poster or someone with Manage Messages. The post record stays
        (repost roasts still know the link); the original gets its own preview back."""
        manage = bool(interaction.permissions.manage_messages)
        if not can_delete(interaction.user.id, origin_author_id, manage):
            await interaction.response.send_message(
                strings.line("delete.denied", author=f"<@{origin_author_id}>"),
                ephemeral=True,
                allowed_mentions=_NO_MENTIONS,
            )
            return
        if from_menu:
            await interaction.response.defer(ephemeral=True, thinking=True)
        else:
            await interaction.response.defer()
        row = await self.bot.db.get_repost(message.id)
        try:
            await self._delete_message(message, webhook_id_for(row, message), interaction.channel)
        except discord.HTTPException:
            log.exception("deleting repost %s failed", message.id)
            await interaction.followup.send(strings.line("error.generic"), ephemeral=True)
            return
        await self.bot.db.delete_repost(message.id)
        if row is not None:
            await self._restore_original(interaction.channel, row.origin_message_id)
        if from_menu:
            await interaction.followup.send(strings.line("delete.done"), ephemeral=True)

    async def _delete_message(self, message: Any, webhook_id: int | None, channel: Any) -> None:
        """Through the webhook when the repost came from one; directly otherwise, or if that fails
        (BB's Manage Messages covers webhook messages too). A message that's already gone is fine."""
        if webhook_id is not None:
            try:
                webhook = await self.bot.fetch_webhook(webhook_id)
                await webhook.delete_message(message.id, thread=_thread_of(channel))
                return
            except discord.HTTPException as exc:
                log.info("webhook delete of %s failed (%s); deleting directly", message.id, exc.status)
        try:
            await message.delete()
        except discord.NotFound:
            pass

    async def _restore_original(self, channel: Any, origin_message_id: int) -> None:
        """Bring back the original's preview, but only once none of its links has a repost left;
        otherwise the other links would show twice."""
        if channel is None or await self.bot.db.get_reposts_for_origin(origin_message_id):
            return
        try:
            original = await channel.fetch_message(origin_message_id)
            if original.flags.suppress_embeds:
                await original.edit(suppress=False)
        except discord.HTTPException:
            pass  # original deleted, or no permission: nothing to restore

    # --- translate ----------------------------------------------------------------------

    async def on_translate(self, interaction: discord.Interaction, translated: bool) -> None:
        message = interaction.message
        if message is None:
            return
        row = await self.bot.db.get_repost(message.id)
        if row is None:
            await interaction.response.send_message(strings.line("error.generic"), ephemeral=True)
            return
        translate = not row.translated
        settings = await self.bot.settings.get(row.guild_id)
        try:
            text, fixer, quip = translated_text(row, translate, settings.translate_lang, self.bot.down_fixers)
        except LookupError as exc:
            log.warning("translate failed: %s", exc)
            await interaction.response.send_message(strings.line("error.generic"), ephemeral=True)
            return
        view = views.make_repost_view(
            site_id=row.site,
            origin_author_id=row.origin_author_id,
            translated=translate,
            has_image=views.has_find_source(message.components),
        )
        webhook_id = webhook_id_for(row, message)
        try:
            if webhook_id is None:
                await interaction.response.edit_message(
                    content=text, view=view, allowed_mentions=_NO_MENTIONS
                )
            else:
                await interaction.response.defer()
                webhook = await self.bot.fetch_webhook(webhook_id)
                await webhook.edit_message(
                    message.id,
                    content=text,
                    view=view,
                    allowed_mentions=_NO_MENTIONS,
                    thread=_thread_of(interaction.channel),
                )
        except discord.HTTPException:
            log.exception("editing repost %s for translate failed", message.id)
            send = (
                interaction.followup.send
                if interaction.response.is_done()
                else interaction.response.send_message
            )
            await send(strings.line("error.generic"), ephemeral=True)
            return
        changes: dict[str, Any] = {"translated": translate, "quip": quip}
        if fixer.id != row.fixer_id:
            changes["fixer_id"] = fixer.id
        await self.bot.db.update_repost(message.id, **changes)

    # --- find source --------------------------------------------------------------------

    async def on_find_source(self, interaction: discord.Interaction) -> None:
        cog = self.bot.get_cog("FindSource")
        if cog is None or interaction.message is None:
            await interaction.response.send_message(strings.line("error.generic"), ephemeral=True)
            return
        await cog.run(interaction, interaction.message)


async def setup(bot: Any) -> None:
    await bot.add_cog(Buttons(bot))
