"""BBTwitter: FxTwitter's tweet previews under BB's name and icon.

Discord builds a tweet preview from the page at the link's address: the name and icon at the bottom come from
that page's og:site_name and favicon (and its oEmbed provider_name), and the post itself from a Mastodon-style
API on the same host (/api/v1/statuses/<id>). So BB answers on her own address (bbtwitter.snek.page, through
nginx on mc-server) and passes FxTwitter's answers along with four things changed: the site name, the icon,
the oEmbed provider, and the fxtwitter.com links that send Discord back for the post data, which now point
here. Everything else (video player, GIFs, polls, quotes, translation) is FxTwitter's own output.

Only tweet-shaped paths are passed on, so this is never an open proxy. Browsers get FxTwitter's redirect to
x.com, as they would on fxtwitter.com.

FxTwitter sometimes answers a post that exists with its "This post is unavailable :(" page (seen 2026-11 on
NSFW posts, about one fetch in five). Discord keeps whatever preview it gets first, so that page is asked for
again, and if it sticks BB answers 502: Discord shows no preview and BB moves on to the next fixer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

DOMAIN = "bbtwitter.snek.page"
BASE = f"https://{DOMAIN}"
UPSTREAM = "https://fxtwitter.com"
NAME = "BBTwitter"
PORT = 8091
ICON_PATH = "/icon.png"
ICON_FILE = Path(__file__).parent / "assets" / "bbtwitter.png"
TIMEOUT = aiohttp.ClientTimeout(total=10)
TRIES = 3
RETRY_DELAY = 0.5

# What Discord may ask for: a tweet page (with /photo/N, /video/N or a language after it), the oEmbed
# file, the activity object and the Mastodon API status. Anything else is a 404.
_PASSED = re.compile(
    r"/(?:[A-Za-z0-9_]{1,15}|i(?:/web)?)/status(?:es)?/\d{1,20}(?:/[A-Za-z0-9_-]{1,10}){0,2}"
    r"|/owoembed"
    r"|/users/[A-Za-z0-9_]{1,15}/statuses/\d{1,80}"
    r"|/api/v1/statuses/\d{1,80}"
)
# Request headers worth passing on: FxTwitter answers bots (Discordbot) with the preview page and
# browsers with a redirect to x.com.
_FORWARDED = ("User-Agent", "Accept", "Accept-Language")
_SITE_NAME = re.compile(r'(<meta property="og:site_name" content=")[^"]*(")')
_ICONS = re.compile(r"<link href='[^']*' rel='icon'[^>]*>")
_ICON_LINK = f"<link href='{BASE}{ICON_PATH}' rel='icon' sizes='128x128' type='image/png'>"
_UNAVAILABLE = '<meta property="og:description" content="This post is unavailable :("/>'


def unavailable(content_type: str, body: str) -> bool:
    """Whether FxTwitter answered with its "This post is unavailable" page instead of the post."""
    return content_type.startswith("text/html") and _UNAVAILABLE in body


def rebrand_page(html: str) -> str:
    """FxTwitter's preview page with BB's name and icon, its links back to fxtwitter.com pointing here."""
    html = _SITE_NAME.sub(rf"\g<1>{NAME}\g<2>", html)
    html, icons = _ICONS.subn("", html)
    if icons:
        html = html.replace("</head>", f"{_ICON_LINK}</head>", 1)
    return html.replace(f"{UPSTREAM}/", f"{BASE}/")


def rebrand_oembed(text: str) -> str:
    """FxTwitter's oEmbed answer with BB as the provider."""
    data = json.loads(text)
    if isinstance(data, dict):
        data["provider_name"] = NAME
        data["provider_url"] = BASE
    return json.dumps(data, ensure_ascii=False)


def rebrand(path: str, content_type: str, body: str) -> str:
    if path == "/owoembed":
        return rebrand_oembed(body)
    if content_type.startswith("text/html"):
        return rebrand_page(body)
    return body.replace(f"{UPSTREAM}/", f"{BASE}/")


class Server:
    """BB's web endpoint. Started by the bot after it connects, stopped when it closes."""

    def __init__(self, session: aiohttp.ClientSession, port: int = PORT) -> None:
        self.session = session
        self.port = port
        self.app = web.Application()
        self.app.router.add_get(ICON_PATH, self.icon)
        self.app.router.add_get("/{path:.*}", self.relay)
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.port).start()
        log.info("BBTwitter listening on port %d for %s", self.port, BASE)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def icon(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(ICON_FILE, headers={"Cache-Control": "public, max-age=86400"})

    async def fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str, str | None, str]:
        """FxTwitter's answer: status, content type, redirect target and body."""
        async with self.session.get(url, headers=headers, allow_redirects=False, timeout=TIMEOUT) as resp:
            return resp.status, resp.content_type, resp.headers.get("Location"), await resp.text()

    async def relay(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        if not _PASSED.fullmatch(path):
            raise web.HTTPNotFound()
        headers = {name: request.headers[name] for name in _FORWARDED if name in request.headers}
        url = f"{UPSTREAM}{path}" + (f"?{request.query_string}" if request.query_string else "")
        for attempt in range(1, TRIES + 1):
            try:
                status, content_type, location, body = await self.fetch(url, headers)
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning("BBTwitter: %s failed upstream: %r", path, exc)
                raise web.HTTPBadGateway() from exc
            if not unavailable(content_type, body):
                break
            log.info("BBTwitter: FxTwitter called %s unavailable (try %d of %d)", path, attempt, TRIES)
            if attempt < TRIES:
                await asyncio.sleep(RETRY_DELAY)
        else:
            log.warning("BBTwitter: FxTwitter kept calling %s unavailable, answering 502", path)
            raise web.HTTPBadGateway()
        if location and 300 <= status < 400:
            return web.Response(status=status, headers={"Location": location})
        try:
            body = rebrand(path, content_type, body)
        except ValueError:
            log.warning("BBTwitter: %s answered with something that isn't what it should be", path)
        return web.Response(status=status, text=body, content_type=content_type, charset="utf-8")
