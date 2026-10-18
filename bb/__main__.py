"""python -m bb            run the bot
python -m bb --check    load config, database, strings and every cog, then exit (no Discord connection)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace

import discord

from bb import strings
from bb.bot import BBBot
from bb.config import Config


async def _check(config: Config) -> int:
    bot = BBBot(replace(config, db_path=":memory:"))
    async with bot:
        await bot.prepare()
        print(f"strings: {len(strings.keys())} keys")
        print(f"cogs: {', '.join(sorted(bot.cogs))}")
        print(f"app commands: {', '.join(sorted(c.name for c in bot.tree.get_commands())) or '(none)'}")
        print("ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bb")
    parser.add_argument("--check", action="store_true", help="load everything without connecting, then exit")
    args = parser.parse_args()

    config = Config.from_env()
    discord.utils.setup_logging(level=getattr(logging, config.log_level, logging.INFO), root=True)
    # discord.py's own DEBUG output includes webhook URLs (with their tokens) and request payloads:
    # LOG_LEVEL=DEBUG is for BB's logs only.
    logging.getLogger("discord").setLevel(max(logging.getLogger().level, logging.INFO))

    if args.check:
        return asyncio.run(_check(config))

    if not config.token:
        print("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1
    BBBot(config).run(config.token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
