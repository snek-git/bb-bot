"""Dry run: read a server's recent history as BB, without going online, and report what she would do.

    python -m bb.dryrun                         # last 200 links in BB's only server
    python -m bb.dryrun --links 300 --try-upload --lewd 2d-cum-zone --chaos brazil

Posts nothing and changes nothing: BB logs in over HTTP only (she stays offline), reads channel
history, and replays it through the real pipeline (link detection, matching, reruns, quips, repost
text) against a throwaway in-memory database. Each fixer link is fetched the way Discord's crawler
fetches it. The report goes to dryrun-report.md (gitignored: it contains the server's links and names).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

import aiohttp
import discord

from bb import render, rerun, upload
from bb.bot import USER_AGENT
from bb.cogs.health import probe
from bb.cogs.linkfix import choose_quip, collect_matches, is_already_fixed
from bb.config import Config
from bb.db import Database
from bb.links import sites
from bb.links.model import Fixer, LinkMatch
from bb.settings_store import GuildSettings

PROBE_CONCURRENCY = 6


@dataclass
class Result:
    message: discord.Message
    match: LinkMatch
    rerun_line: str | None = None
    quip: str | None = None
    outcome: str = ""  # fixed | backup | failed | uploaded | upload_failed | left_alone | would_rescue
    fixer: Fixer | None = None
    tried: list[str] = field(default_factory=list)
    repost_text: str | None = None
    upload_note: str = ""


def channel_ref(message: discord.Message) -> str:
    return f"#{getattr(message.channel, 'name', message.channel.id)}"


def parent_or_self(channel: discord.abc.GuildChannel | discord.Thread) -> int:
    return channel.parent_id if isinstance(channel, discord.Thread) and channel.parent_id else channel.id


def newest_messages_with_links(
    found: list[tuple[discord.Message, list[LinkMatch]]], links: int
) -> list[tuple[discord.Message, list[LinkMatch]]]:
    """Newest messages until they hold at least `links` links, returned oldest first for replay."""
    picked: list[tuple[discord.Message, list[LinkMatch]]] = []
    total = 0
    for item in sorted(found, key=lambda pair: pair[0].created_at, reverse=True):
        if total >= links:
            break
        picked.append(item)
        total += len(item[1])
    return sorted(picked, key=lambda pair: pair[0].created_at)


def resolve_channels(spec: str | None, channels: list[discord.abc.GuildChannel]) -> tuple[int, ...]:
    if not spec:
        return ()
    wanted = {part.strip().lstrip("#").lower() for part in spec.split(",") if part.strip()}
    return tuple(c.id for c in channels if c.name.lower() in wanted or str(c.id) in wanted)


async def read_history(
    guild: discord.Guild, per_channel: int
) -> tuple[list[discord.Message], list[str], list[discord.abc.GuildChannel]]:
    channels = await guild.fetch_channels()
    readable = [c for c in channels if isinstance(c, (discord.TextChannel, discord.VoiceChannel))]
    try:
        threads = await guild.active_threads()
    except discord.HTTPException:
        threads = []
    messages: list[discord.Message] = []
    skipped: list[str] = []
    for channel in [*readable, *threads]:
        try:
            async for msg in channel.history(limit=per_channel):
                if "http" in msg.content and not msg.author.bot:
                    messages.append(msg)
        except discord.Forbidden:
            skipped.append(f"#{channel.name}")
        except discord.HTTPException as exc:
            skipped.append(f"#{channel.name} ({exc.status})")
    return messages, skipped, list(channels)


async def check_link(
    r: Result, session: aiohttp.ClientSession, limit: asyncio.Semaphore, max_bytes: int, try_upload: bool
) -> None:
    m = r.match
    if is_already_fixed(m):
        r.outcome = "left_alone" if r.message.embeds else "would_rescue"
        if r.outcome == "left_alone":
            return
    for i, fixer in enumerate(sites.chain(m.site.id)):
        url = sites.fixed_url(m, fixer)
        r.tried.append(fixer.name)
        async with limit:
            ok = await probe(session, url)
        if ok:
            r.fixer = fixer
            if r.outcome != "would_rescue":
                r.outcome = "fixed" if i == 0 else "backup"
            r.repost_text = render.format_repost(m, fixer.name, url, r.quip)
            return
    r.outcome = "failed"
    if try_upload:
        with tempfile.TemporaryDirectory(prefix="bb-dryrun-") as tmp:
            async with limit:
                files = await upload.fetch_media(m, max_bytes=max_bytes, session=session, workdir=Path(tmp))
        if files:
            r.outcome = "uploaded"
            size = sum(f.size for f in files) / 1_000_000
            r.upload_note = f"{len(files)} file(s), {size:.1f} MB"
        else:
            r.outcome = "upload_failed"


def report(guild: discord.Guild, results: list[Result], skipped: list[str], scanned: int) -> str:
    outcomes = Counter(r.outcome for r in results)
    by_site = Counter(r.match.site.name for r in results)
    reruns = [r for r in results if r.rerun_line]
    failed = [r for r in results if r.outcome in ("failed", "upload_failed")]
    lines = [
        f"# BB dry run: {guild.name}",
        "",
        f"Replayed the newest {len(results)} supported links ({scanned} messages with links scanned). "
        "Nothing was posted.",
        "",
        "## What BB would have done",
        "",
        "| Outcome | Links |",
        "|---|---|",
        f"| Fixed with the first fixer | {outcomes['fixed']} |",
        f"| Fixed with a backup fixer | {outcomes['backup']} |",
        f"| Every fixer failed, upload worked | {outcomes['uploaded']} |",
        f"| Every fixer failed, upload failed too | {outcomes['upload_failed']} |",
        f"| Every fixer failed (upload not tried) | {outcomes['failed']} |",
        f"| Fixer/meme link with a working preview, left alone | {outcomes['left_alone']} |",
        f"| Fixer/meme link with no preview, BB would fix it | {outcomes['would_rescue']} |",
        f"| Repost roasts | {len(reruns)} |",
        "",
        "Links by site: " + ", ".join(f"{site} {n}" for site, n in by_site.most_common()),
        "",
    ]
    if skipped:
        lines += [f"Channels BB can't read (skipped): {', '.join(skipped)}", ""]
    lines += ["## Failures", ""]
    lines += [
        f"- {r.match.site.name} `{r.match.url}` in {channel_ref(r.message)} "
        f"(tried {', '.join(r.tried) or 'nothing'})"
        for r in failed
    ] or ["None."]
    lines += ["", "## Reposts BB would roast", ""]
    lines += [
        f"- {r.message.author.display_name} in {channel_ref(r.message)}: {r.rerun_line}" for r in reruns
    ] or ["None."]
    lines += ["", "## Sample reposts (newest 15)", ""]
    samples = [r for r in results if r.repost_text][-15:]
    for r in samples:
        who = f"**{channel_ref(r.message)}**, {r.message.author.display_name}:"
        lines += [who, "", "```", r.repost_text or "", "```", ""]
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if not config.token:
        print("DISCORD_TOKEN is missing from .env", file=sys.stderr)
        return 1
    client = discord.Client(intents=discord.Intents.none())
    db = Database(":memory:")
    await db.connect()
    async with (
        client,
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), headers={"User-Agent": USER_AGENT}
        ) as session,
    ):
        await client.login(config.token)  # HTTP only: BB stays offline
        partials = [g async for g in client.fetch_guilds()]
        if args.guild:
            partials = [g for g in partials if str(g.id) == args.guild or g.name == args.guild]
        if len(partials) != 1:
            names = ", ".join(g.name for g in partials) or "none"
            print(f"Pick one server with --guild (BB is in: {names})", file=sys.stderr)
            return 1
        guild = await client.fetch_guild(partials[0].id)
        print(f"Reading history in {guild.name}...", file=sys.stderr)
        messages, skipped, channels = await read_history(guild, args.per_channel)

        settings = replace(
            GuildSettings(),
            lewd_channels=resolve_channels(args.lewd, channels),
            chaos_channels=resolve_channels(args.chaos, channels),
        )
        found = []
        for msg in messages:
            matches = await collect_matches(msg.content, settings, session)
            if matches:
                found.append((msg, matches))
        picked = newest_messages_with_links(found, args.links)
        print(
            f"Replaying {sum(len(ms) for _, ms in picked)} links from {len(picked)} messages...",
            file=sys.stderr,
        )

        # Pass 1, in posting order: reruns and quips (DB only, like the live bot).
        results: list[Result] = []
        rng = random.Random(0)
        for msg, matches in picked:
            ts = msg.created_at.timestamp()
            for m in matches:

                async def locate(row, _g=guild.id):
                    return rerun.jump_url(_g, row.channel_id, row.message_id)

                hit = await rerun.find_rerun(
                    db,
                    guild_id=guild.id,
                    channel_id=msg.channel.id,
                    message_id=msg.id,
                    author_id=msg.author.id,
                    canonical_key=m.canonical_key,
                    settings=settings,
                    locate=locate,
                    now_ts=ts,
                )
                await db.insert_post(
                    guild_id=guild.id,
                    channel_id=msg.channel.id,
                    message_id=msg.id,
                    author_id=msg.author.id,
                    author_name=msg.author.display_name,
                    site=m.site.id,
                    canonical_key=m.canonical_key,
                    created_at=ts,
                )
                line = rerun.rerun_quip(hit) if hit else None
                quip = choose_quip(
                    settings,
                    rerun_line=line,
                    site_name=m.site.name,
                    channel_id=parent_or_self(msg.channel),
                    rng=rng,
                )
                results.append(Result(message=msg, match=m, rerun_line=line, quip=quip))

        # Pass 2, concurrently: would the fixers (and the upload fallback) actually deliver?
        limit = asyncio.Semaphore(PROBE_CONCURRENCY)
        await asyncio.gather(
            *(check_link(r, session, limit, guild.filesize_limit, args.try_upload) for r in results)
        )
    await db.close()
    await asyncio.to_thread(write_report, Path(args.out), report(guild, results, skipped, len(found)))
    return 0


def write_report(out: Path, text: str) -> None:
    out.write_text(text, encoding="utf-8")
    print(f"Report written to {out}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m bb.dryrun", description=__doc__.split("\n\n")[0])
    parser.add_argument("--links", type=int, default=200, help="how many of the newest links to replay")
    parser.add_argument("--per-channel", type=int, default=400, help="messages to read per channel")
    parser.add_argument("--guild", help="server name or ID (needed only if BB is in several)")
    parser.add_argument("--lewd", help="comma-separated lewd channel names, e.g. 2d-cum-zone")
    parser.add_argument("--chaos", help="comma-separated chaos channel names, e.g. brazil")
    parser.add_argument("--try-upload", action="store_true", help="also test the upload fallback on failures")
    parser.add_argument("--out", default="dryrun-report.md")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
