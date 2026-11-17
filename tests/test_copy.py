"""/copy: the text file of a channel's last messages and their image links."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bb import strings
from bb.cogs import copy as cp
from bb.cogs import findsource as fs


def message(text, minute, attachments=(), embeds=(), name="snek"):
    return SimpleNamespace(
        created_at=datetime(2026, 11, 17, 14, minute, tzinfo=UTC),
        author=SimpleNamespace(display_name=name),
        clean_content=text,
        attachments=list(attachments),
        embeds=list(embeds),
        components=[],
    )


def attachment(url, content_type="image/png"):
    return SimpleNamespace(url=url, content_type=content_type, filename=url.rsplit("/", 1)[-1])


def embed(image=None, thumbnail=None):
    return SimpleNamespace(
        image=SimpleNamespace(url=image) if image else None,
        thumbnail=SimpleNamespace(url=thumbnail) if thumbnail else None,
    )


def test_image_urls_lists_every_image_once():
    urls = fs.image_urls(
        [attachment("https://cdn/a.png"), attachment("https://cdn/v.mp4", "video/mp4")],
        [embed("https://x/1.jpg", "https://x/t.jpg"), embed("https://x/2.jpg"), embed("https://x/1.jpg")],
    )
    assert urls == ["https://cdn/a.png", "https://x/1.jpg", "https://x/2.jpg", "https://x/t.jpg"]


def test_format_log_oldest_first_with_image_links():
    messages = [
        message("look", 3, attachments=[attachment("https://cdn/a.png")]),
        message("nice", 4, name="BB"),
    ]
    assert cp.format_log("art", messages) == (
        "#art · last 2 messages · times in UTC\n"
        "\n"
        "[2026-11-17 14:03] snek: look\n"
        "    https://cdn/a.png\n"
        "[2026-11-17 14:04] BB: nice\n"
    )


class FakeChannel:
    name = "art"

    def __init__(self, messages=(), error=None):
        self.messages, self.error, self.limits = list(messages), error, []

    async def history(self, limit):
        self.limits.append(limit)
        if self.error:
            raise self.error
        for m in self.messages[:limit]:
            yield m


def make_interaction(channel, user_id=1):
    return SimpleNamespace(
        channel=channel,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_run_sends_a_private_file_oldest_first():
    newest_first = [message("second", 5), message("first", 4)]
    channel = FakeChannel(newest_first)
    interaction = make_interaction(channel)
    await cp.Copy(SimpleNamespace()).run(interaction, 500)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert channel.limits == [500]
    args, kwargs = interaction.followup.send.await_args
    assert args[0] in {strings.fill(v, n=2) for v in strings.variants("copy.done")}
    assert kwargs["ephemeral"] is True
    file = kwargs["file"]
    assert file.filename == "art-last-2.txt"
    text = file.fp.read().decode()
    assert text.index("first") < text.index("second")


async def test_run_without_history_access_says_so():
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
    interaction = make_interaction(FakeChannel(error=forbidden))
    await cp.Copy(SimpleNamespace()).run(interaction, 10)
    args, kwargs = interaction.followup.send.await_args
    assert args[0] in strings.variants("copy.forbidden") and kwargs["ephemeral"] is True
    assert "file" not in kwargs


async def test_one_copy_per_person_per_cooldown():
    cog = cp.Copy(SimpleNamespace())
    first, second = make_interaction(FakeChannel()), make_interaction(FakeChannel())
    await cog.run(first, 5)
    await cog.run(second, 5)
    first.followup.send.assert_awaited_once()
    second.response.defer.assert_not_awaited()
    args, kwargs = second.response.send_message.await_args
    assert args[0] in strings.variants("copy.cooldown") and kwargs["ephemeral"] is True
