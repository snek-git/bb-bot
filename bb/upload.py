"""Media download for the upload fallback. Owned by track E; signatures are the contract.

One strategy per site (PLAN.md, "Upload fallback"):
- Twitter/X: FxTwitter API, then vxTwitter API. yt-dlp only as a last resort (both APIs down, or a
  video that no API variant could deliver).
- Pixiv: www.pixiv.net ajax API; images from i.pximg.net with a pixiv Referer. R-18 needs login: [].
- Bluesky: public AppView getPostThread (it resolves handles itself). Images from the CDN, video as
  the original blob from the author's PDS, or HLS through yt-dlp when that blob is too big.
- Reddit: post JSON for images and galleries, yt-dlp for everything else (video, external hosts).
- TikTok, Instagram: yt-dlp. Instagram usually wants cookies, so it often comes back empty.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import shutil
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import aiohttp
import aiohttp.abc
import yt_dlp

from bb.links.model import LinkMatch

log = logging.getLogger(__name__)

TOTAL_TIMEOUT = 60.0  # seconds for the whole fetch_media call (read at call time; tests patch it)
MAX_FILES = 4
CHUNK = 64 * 1024
MAX_REDIRECTS = 5
# Media URLs come from API answers and post data that posters control (a Reddit post's link, a
# Bluesky DID document): never let one reach BB's own network (the Docker host, the LAN). Tests
# serve from 127.0.0.1 and switch this on.
ALLOW_PRIVATE_HOSTS = False
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

FXTWITTER_API = "https://api.fxtwitter.com"
VXTWITTER_API = "https://api.vxtwitter.com"
BSKY_APPVIEW = "https://public.api.bsky.app"
PLC_DIRECTORY = "https://plc.directory"
PIXIV = "https://www.pixiv.net"
REDDIT = "https://www.reddit.com"
OLD_REDDIT = "https://old.reddit.com"
I_REDD_IT = "https://i.redd.it"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
PIXIV_HEADERS = {"Referer": "https://www.pixiv.net/", "User-Agent": BROWSER_UA}
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=20)

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
_URL_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "webm", "m4v"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}


@dataclass(frozen=True)
class MediaFile:
    path: Path
    filename: str
    size: int


async def fetch_media(
    m: LinkMatch,
    *,
    max_bytes: int,
    session: aiohttp.ClientSession,
    workdir: Path,
) -> list[MediaFile]:
    """Download the post's media into workdir. At most 4 files, each <= max_bytes.

    Returns [] on any failure or timeout (60s total). Never raises. The caller owns workdir.
    """
    key = getattr(m, "canonical_key", None)
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            target = _target(m)
            if target is None:
                log.info("upload: can't tell which post %r is (%s)", key, getattr(m, "url", None))
                return []
            site, ident = target
            ctx = _Ctx(session=session, workdir=Path(workdir), max_bytes=max_bytes, stem=_stem(site, ident))
            files = await _HANDLERS[site](ctx, ident)
            return files[:MAX_FILES]
    except TimeoutError:
        log.warning("upload: gave up on %s after %ss", key, TOTAL_TIMEOUT)
    except Exception:
        log.exception("upload: failed for %s", key)
    return []


# --- which post ---------------------------------------------------------------------------

_SITE_ALIASES = {"bluesky": "bsky", "x": "twitter"}
_ID_RE = {
    "twitter": re.compile(r"\d{1,25}"),
    "pixiv": re.compile(r"\d{1,15}"),
    "reddit": re.compile(r"[a-z0-9]{1,16}"),
    "tiktok": re.compile(r"\d{1,25}"),
    "instagram": re.compile(r"[A-Za-z0-9_-]{1,64}"),
}
_BSKY_ACTOR_RE = re.compile(r"did:[a-z]+:[A-Za-z0-9._:%-]+|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_BSKY_RKEY_RE = re.compile(r"[A-Za-z0-9._:~-]{1,512}")
_URL_RE = {
    "twitter": re.compile(r"/status(?:es)?/(\d+)"),
    "pixiv": re.compile(r"(?:/artworks/|/i/|[?&]illust_id=)(\d+)"),
    "reddit": re.compile(r"(?:/comments/|/gallery/|//(?:www\.)?redd\.it/)([a-z0-9]+)", re.I),
    "tiktok": re.compile(r"/(?:video|photo|v)/(\d+)"),
    "instagram": re.compile(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)"),
    "bsky": re.compile(r"/profile/([^/?#]+)/post/([^/?#]+)"),
}


def _valid(site: str, ident: str) -> bool:
    if site == "bsky":
        actor, _, rkey = ident.partition("/")
        return bool(_BSKY_ACTOR_RE.fullmatch(actor) and _BSKY_RKEY_RE.fullmatch(rkey))
    pattern = _ID_RE.get(site)
    return bool(pattern and pattern.fullmatch(ident))


def _target(m: LinkMatch) -> tuple[str, str] | None:
    """(site, id) from the canonical key, falling back to the URL. Bluesky ids are "<actor>/<rkey>"."""
    prefix, _, rest = (m.canonical_key or "").partition(":")  # partition: DIDs have colons too
    site = _SITE_ALIASES.get(prefix.lower(), prefix.lower())
    if site not in _URL_RE:
        site = _SITE_ALIASES.get(m.site.id, m.site.id)
        rest = ""
    if site not in _URL_RE:
        return None
    if site == "reddit":
        rest = rest.lower()
    if _valid(site, rest):
        return site, rest
    found = _URL_RE[site].search(m.url or "")
    if found:
        ident = "/".join(unquote(g) for g in found.groups())
        if site == "reddit":
            ident = ident.lower()
        if _valid(site, ident):
            return site, ident
    return None


def _stem(site: str, ident: str) -> str:
    """Filename stem, e.g. "twitter_1234" or "bsky_3l3vgf77uco2g"."""
    short = ident.rpartition("/")[2]
    return f"{site}_{re.sub(r'[^A-Za-z0-9_-]', '', short)[:64] or 'post'}"


# --- downloading --------------------------------------------------------------------------


@dataclass
class _Ctx:
    session: aiohttp.ClientSession
    workdir: Path
    max_bytes: int
    stem: str


@dataclass(frozen=True)
class _Item:
    """One media item. Candidate URLs go best first; the first one that fits max_bytes wins."""

    urls: tuple[str, ...]
    video: bool = False
    headers: tuple[tuple[str, str], ...] = ()


def _item(
    urls: list[Any] | tuple[Any, ...], *, video: bool = False, headers: dict[str, str] | None = None
) -> _Item:
    seen: list[str] = []
    for url in urls:
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.append(url)
    return _Item(tuple(seen), video, tuple((headers or {}).items()))


def _ext_for(content_type: str | None, url: str) -> str | None:
    """File extension for a response, or None if it isn't media. url is where the body really came
    from (after redirects)."""
    mime = (content_type or "").split(";")[0].strip().lower()
    ext = _MIME_EXT.get(mime)
    if ext:
        return ext
    if mime and not mime.startswith(("image/", "video/")) and mime not in _OPAQUE_MIMES:
        return None  # a web page or an API answer, whatever the URL ends in
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in _URL_EXTS else None


_OPAQUE_MIMES = frozenset({"application/octet-stream", "binary/octet-stream"})


def _is_public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_global
    except ValueError:
        return False


class PublicResolver(aiohttp.abc.AbstractResolver):
    """DNS for BB's HTTP sessions: only public internet addresses come back. _public_url checks a URL
    before the request; this checks the address actually connected to, so a host that answers
    public first and private second (DNS rebinding) still can't reach BB's own network."""

    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        found = await self._inner.resolve(host, port, family)
        if ALLOW_PRIVATE_HOSTS:
            return found
        public = [entry for entry in found if _is_public(str(entry["host"]))]
        if not public:
            raise OSError(f"{host} resolves only to non-public addresses")
        return public

    async def close(self) -> None:
        await self._inner.close()


def public_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(resolver=PublicResolver())


async def _public_url(url: str) -> bool:
    """http(s) to a host that resolves only to public internet addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if ALLOW_PRIVATE_HOSTS:
        return True
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return _is_public(host)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    return bool(infos) and all(_is_public(str(info[4][0])) for info in infos)


@asynccontextmanager
async def _get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """GET that follows redirects itself, checking every hop with _public_url."""
    for _ in range(MAX_REDIRECTS + 1):
        if not await _public_url(url):
            raise ValueError(f"refusing non-public URL {urlsplit(url).hostname!r}")
        resp = await session.get(
            url, params=params, headers=headers, timeout=_HTTP_TIMEOUT, allow_redirects=False
        )
        location = resp.headers.get("Location") if resp.status in _REDIRECT_STATUSES else None
        if location:
            resp.release()
            url, params = urljoin(str(resp.url), location), None
            continue
        try:
            yield resp
        finally:
            resp.release()
        return
    raise ValueError(f"too many redirects for {urlsplit(url).hostname!r}")


async def _download_items(ctx: _Ctx, items: list[_Item]) -> list[MediaFile]:
    items = [i for i in items if i.urls][:MAX_FILES]
    results = await asyncio.gather(
        *(_download(ctx, item, n) for n, item in enumerate(items, 1)), return_exceptions=True
    )
    files = []
    for result in results:
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            log.warning("upload: download crashed for %s: %r", ctx.stem, result)
        elif result is not None:
            files.append(result)
    return files


async def _download(ctx: _Ctx, item: _Item, index: int) -> MediaFile | None:
    for url in item.urls:
        try:
            got = await _download_one(ctx, url, dict(item.headers), index)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            log.info("upload: %s failed: %r", url, e)
            continue
        if got is not None:
            return got
    return None


async def _download_one(ctx: _Ctx, url: str, headers: dict[str, str], index: int) -> MediaFile | None:
    headers = {"User-Agent": BROWSER_UA, **headers}
    async with _get(ctx.session, url, headers=headers) as resp:
        if resp.status != 200:
            log.info("upload: %s -> HTTP %s", url, resp.status)
            return None
        if resp.content_length is not None and resp.content_length > ctx.max_bytes:
            log.info("upload: %s is %s bytes, over %s", url, resp.content_length, ctx.max_bytes)
            return None
        ext = _ext_for(resp.headers.get("Content-Type"), str(resp.url))
        if ext is None:
            log.info("upload: %s isn't media (%s)", url, resp.headers.get("Content-Type"))
            return None
        filename = f"{ctx.stem}_{index}.{ext}"
        path = ctx.workdir / filename
        size = 0
        fh = await asyncio.to_thread(path.open, "wb")
        try:
            async for chunk in resp.content.iter_chunked(CHUNK):
                size += len(chunk)
                if size > ctx.max_bytes:
                    break
                fh.write(chunk)
        finally:
            fh.close()
    if size > ctx.max_bytes or size == 0:
        path.unlink(missing_ok=True)
        log.info("upload: %s ran over %s bytes or was empty", url, ctx.max_bytes)
        return None
    return MediaFile(path=path, filename=filename, size=size)


async def _get_json(
    ctx: _Ctx,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> Any:
    """GET and decode JSON. None on HTTP errors, non-200s and non-JSON bodies (e.g. Cloudflare pages)."""
    try:
        async with _get(
            session or ctx.session, url, params=params, headers={"User-Agent": BROWSER_UA, **(headers or {})}
        ) as resp:
            if resp.status != 200:
                log.info("upload: %s -> HTTP %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, OSError, ValueError) as e:
        log.info("upload: %s failed: %r", url, e)
        return None


# --- yt-dlp -------------------------------------------------------------------------------


class _YtdlpLog:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)


async def _ytdlp(ctx: _Ctx, url: str) -> list[MediaFile]:
    cancel = threading.Event()
    job = asyncio.get_running_loop().run_in_executor(
        None, _ytdlp_sync, url, ctx.workdir, ctx.stem, ctx.max_bytes, cancel
    )
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        # Timed out: the thread keeps going until its next progress tick, and may write into workdir
        # after the caller already removed it. Clean up again once it has really stopped.
        job.add_done_callback(lambda done: _cleanup_after(done, ctx.workdir))
        raise
    finally:
        cancel.set()


def _cleanup_after(job: asyncio.Future[Any], workdir: Path) -> None:
    if not job.cancelled() and job.exception() is not None:
        log.info("upload: abandoned yt-dlp run ended with %r", job.exception())
    shutil.rmtree(workdir, ignore_errors=True)


def _ytdlp_options(workdir: Path, stem: str, max_bytes: int, cancel: threading.Event) -> dict[str, Any]:
    def hook(_: dict[str, Any]) -> None:
        if cancel.is_set():
            raise yt_dlp.utils.DownloadCancelled("upload fallback gave up")

    fits = f"[filesize<=?{max_bytes}][filesize_approx<=?{max_bytes}]"
    if shutil.which("ffmpeg"):
        fmt = f"b{fits}/bv*{fits}+ba/b/bv*+ba"
    else:
        fmt = f"b{fits}/b"
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpLog(),
        "paths": {"home": str(workdir), "temp": str(workdir)},
        "outtmpl": {"default": f"{stem}_%(playlist_autonumber|1)s.%(ext)s"},
        "format": fmt,
        # Discord can't play HEVC inline (TikTok's default pick); 720p keeps files small.
        "format_sort": ["vcodec:h264", "res:720", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "max_filesize": max_bytes,
        "playlist_items": f"1-{MAX_FILES}",
        "ignoreerrors": "only_download",
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
        # Site extractors only: the generic one would fetch whatever URL a post points at.
        "allowed_extractors": ["default", "-generic"],
        "overwrites": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [hook],
    }


def _ytdlp_sync(
    url: str, workdir: Path, stem: str, max_bytes: int, cancel: threading.Event
) -> list[MediaFile]:
    try:
        with yt_dlp.YoutubeDL(_ytdlp_options(workdir, stem, max_bytes, cancel)) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.YoutubeDLError as e:
        log.info("upload: yt-dlp gave nothing for %s: %s", url, e)
        return []
    files = []
    for path in _ytdlp_paths(info)[:MAX_FILES]:
        size = path.stat().st_size
        if 0 < size <= max_bytes:  # max_filesize isn't enforced for HLS/DASH or merged output
            files.append(MediaFile(path=path, filename=path.name, size=size))
        else:
            log.info("upload: yt-dlp file %s is %s bytes, over %s", path.name, size, max_bytes)
            path.unlink(missing_ok=True)
    return files


def _ytdlp_paths(info: Any) -> list[Path]:
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    found: list[Path] = []
    for entry in entries if entries is not None else [info]:
        if not isinstance(entry, dict):
            continue
        for download in entry.get("requested_downloads") or []:
            filepath = download.get("filepath") if isinstance(download, dict) else None
            if filepath and Path(filepath).is_file() and Path(filepath) not in found:
                found.append(Path(filepath))
    return found


# --- Twitter ------------------------------------------------------------------------------


def _twimg(url: Any) -> list[Any]:
    """Photo candidates: original size first, then "large" in case the original is too big."""
    if not isinstance(url, str):
        return []
    parts = urlsplit(url)
    if parts.hostname != "pbs.twimg.com" or not PurePosixPath(parts.path).suffix:
        return [url]  # "?format=jpg&name=..." style URLs keep their query
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return [f"{base}?name=orig", f"{base}?name=large"]


def _fx_video(entry: dict[str, Any]) -> _Item:
    variants = []
    for v in [*(entry.get("formats") or []), *(entry.get("variants") or [])]:
        if not isinstance(v, dict) or not isinstance(v.get("url"), str):
            continue
        if v.get("container") == "mp4" or v.get("content_type") == "video/mp4":
            variants.append((v.get("bitrate") or 0, v["url"]))
    variants.sort(key=lambda bv: bv[0], reverse=True)  # lower bitrates are fallbacks for size
    return _item([entry.get("url"), *(url for _, url in variants)], video=True)


def _fx_media(tweet: dict[str, Any]) -> list[_Item]:
    media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}
    entries = media.get("all") or [*(media.get("photos") or []), *(media.get("videos") or [])]
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "photo":
            items.append(_item(_twimg(entry.get("url"))))
        elif entry.get("type") in ("video", "gif"):
            items.append(_fx_video(entry))
    return [i for i in items if i.urls]


def _fx_items(data: Any) -> list[_Item] | None:
    """Media from an FxTwitter /status response. None means the API failed (try the next one)."""
    if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(data.get("tweet"), dict):
        return None
    tweet = data["tweet"]
    items = _fx_media(tweet)
    if not items and isinstance(tweet.get("quote"), dict):
        items = _fx_media(tweet["quote"])
    return items


def _vx_items(data: Any) -> list[_Item] | None:
    """Media from a vxTwitter API response. None means the API failed."""
    if not isinstance(data, dict) or not ("tweetID" in data or "media_extended" in data):
        return None
    items = []
    extended = data.get("media_extended")
    if isinstance(extended, list):
        for e in extended:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "image":
                items.append(_item(_twimg(e.get("url"))))
            elif e.get("type") in ("video", "gif"):
                items.append(_item([e.get("url")], video=True))
    else:
        for url in data.get("mediaURLs") or []:
            if isinstance(url, str):
                is_video = ".mp4" in url
                items.append(_item([url] if is_video else _twimg(url), video=is_video))
    items = [i for i in items if i.urls]
    if not items and isinstance(data.get("qrt"), dict):
        items = _vx_items(data["qrt"]) or []
    return items


async def _twitter(ctx: _Ctx, tweet_id: str) -> list[MediaFile]:
    items = _fx_items(await _get_json(ctx, f"{FXTWITTER_API}/status/{tweet_id}"))
    if items is None:
        items = _vx_items(await _get_json(ctx, f"{VXTWITTER_API}/Twitter/status/{tweet_id}"))
    if items is None:
        log.info("upload: both tweet APIs failed for %s; trying yt-dlp", tweet_id)
        return await _ytdlp(ctx, f"https://x.com/i/status/{tweet_id}")
    files = await _download_items(ctx, items)
    if not files and any(i.video for i in items):
        return await _ytdlp(ctx, f"https://x.com/i/status/{tweet_id}")
    return files


# --- Pixiv --------------------------------------------------------------------------------


async def _pixiv(ctx: _Ctx, illust_id: str) -> list[MediaFile]:
    headers = {**PIXIV_HEADERS, "Accept-Language": "en"}
    data = await _get_json(ctx, f"{PIXIV}/ajax/illust/{illust_id}", headers=headers)
    body = data.get("body") if isinstance(data, dict) and not data.get("error") else None
    if not isinstance(body, dict):
        log.info("upload: pixiv %s not found (or R-18 behind login)", illust_id)
        return []
    if body.get("xRestrict"):
        log.info("upload: pixiv %s is R-18; needs a logged-in session", illust_id)
        return []
    pages = [body.get("urls")]
    if int(body.get("pageCount") or 1) > 1:
        more = await _get_json(ctx, f"{PIXIV}/ajax/illust/{illust_id}/pages", headers=headers)
        if isinstance(more, dict) and not more.get("error") and isinstance(more.get("body"), list):
            pages = [p.get("urls") for p in more["body"] if isinstance(p, dict)]
    items = [
        _item([urls.get("original"), urls.get("regular")], headers=PIXIV_HEADERS)
        for urls in pages
        if isinstance(urls, dict)
    ]
    return await _download_items(ctx, items)


# --- Reddit -------------------------------------------------------------------------------


def _reddit_items(post: dict[str, Any]) -> list[_Item]:
    """Images and galleries. Videos and external hosts are left to yt-dlp."""
    src = post
    crossposts = post.get("crosspost_parent_list")
    if not (post.get("is_gallery") or post.get("post_hint") == "image") and crossposts:
        if isinstance(crossposts[0], dict):
            src = crossposts[0]
    if src.get("is_gallery"):
        meta = src.get("media_metadata") or {}
        items = []
        for entry in (src.get("gallery_data") or {}).get("items") or []:
            media_id = entry.get("media_id") if isinstance(entry, dict) else None
            info = meta.get(media_id) if isinstance(media_id, str) else None
            if not isinstance(info, dict) or info.get("status") != "valid" or entry.get("is_deleted"):
                continue
            best = info.get("s") or {}
            if info.get("e") == "Image":
                ext = _MIME_EXT.get(str(info.get("m")), "jpg")
                items.append(_item([f"{I_REDD_IT}/{media_id}.{ext}", best.get("u")]))
            elif info.get("e") == "AnimatedImage":
                items.append(_item([best.get("mp4"), best.get("gif")], video=bool(best.get("mp4"))))
        return [i for i in items if i.urls]
    url = src.get("url_overridden_by_dest") or src.get("url")
    if not isinstance(url, str):
        return []
    ext = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
    if src.get("post_hint") == "image" or ext in _IMAGE_EXTS:
        try:
            preview = src["preview"]["images"][0]["source"]["url"]
        except (KeyError, IndexError, TypeError):
            preview = None
        return [i for i in [_item([url, preview])] if i.urls]
    return []


async def _reddit_post(ctx: _Ctx, post_id: str) -> dict[str, Any] | None:
    # Reddit 403s anonymous JSON requests without a "loid" cookie; old.reddit.com hands one out.
    # A private cookie jar keeps that out of the bot's shared session.
    async with aiohttp.ClientSession(headers={"User-Agent": BROWSER_UA}, connector=public_connector()) as rs:
        try:
            async with rs.get(f"{OLD_REDDIT}/", timeout=_HTTP_TIMEOUT) as resp:
                resp.release()
        except (aiohttp.ClientError, OSError) as e:
            log.info("upload: reddit session warm-up failed: %r", e)
        url = f"{REDDIT}/comments/{post_id}/.json"
        data = await _get_json(ctx, url, params={"raw_json": "1"}, session=rs)
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None
    return post if isinstance(post, dict) else None


async def _reddit(ctx: _Ctx, post_id: str) -> list[MediaFile]:
    post = await _reddit_post(ctx, post_id)
    items = _reddit_items(post) if post else []
    if items:
        return await _download_items(ctx, items)
    return await _ytdlp(ctx, f"https://www.reddit.com/comments/{post_id}/")


# --- Bluesky ------------------------------------------------------------------------------


def _bsky_image(url: Any) -> list[Any]:
    """The CDN serves webp by default; "@jpeg" asks for a JPEG, which every client and source search takes."""
    if not isinstance(url, str) or urlsplit(url).hostname != "cdn.bsky.app":
        return [url]
    if "@" not in url.rsplit("/", 1)[-1]:
        return [f"{url}@jpeg", url]
    return [url]


def _bsky_media(embed: Any, did: str | None) -> tuple[list[_Item], tuple[str, str] | None]:
    """(images, (did, video cid)) from an embed view, looking inside quotes when the post has none."""
    if not isinstance(embed, dict):
        return [], None
    kind = str(embed.get("$type") or "")
    if kind.startswith("app.bsky.embed.recordWithMedia"):
        return _bsky_media(embed.get("media"), did)
    if kind.startswith("app.bsky.embed.images"):
        images = [i.get("fullsize") for i in embed.get("images") or [] if isinstance(i, dict)]
        return [i for i in (_item(_bsky_image(u)) for u in images) if i.urls], None
    if kind.startswith("app.bsky.embed.video"):
        cid = embed.get("cid")
        return [], ((did, cid) if isinstance(cid, str) and did else None)
    if kind.startswith("app.bsky.embed.record"):
        record = embed.get("record")
        if isinstance(record, dict):
            inner_did = (record.get("author") or {}).get("did")
            for inner in record.get("embeds") or []:
                found = _bsky_media(inner, inner_did if isinstance(inner_did, str) else None)
                if found[0] or found[1]:
                    return found
    return [], None


async def _pds_endpoint(ctx: _Ctx, did: str) -> str | None:
    """The author's PDS from their DID document. https only."""
    if did.startswith("did:plc:"):
        doc = await _get_json(ctx, f"{PLC_DIRECTORY}/{did}")
    elif did.startswith("did:web:"):
        host = did.removeprefix("did:web:")
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", host):
            return None
        doc = await _get_json(ctx, f"https://{host}/.well-known/did.json")
    else:
        return None
    services = doc.get("service") if isinstance(doc, dict) else None
    for service in services if isinstance(services, list) else []:
        if not isinstance(service, dict):
            continue
        is_pds = service.get("type") == "AtprotoPersonalDataServer"
        if is_pds or str(service.get("id")).endswith("#atproto_pds"):
            endpoint = service.get("serviceEndpoint")
            if isinstance(endpoint, str) and endpoint.startswith("https://"):
                return endpoint.rstrip("/")
    return None


async def _bluesky(ctx: _Ctx, ident: str) -> list[MediaFile]:
    actor, _, rkey = ident.partition("/")
    data = await _get_json(
        ctx,
        f"{BSKY_APPVIEW}/xrpc/app.bsky.feed.getPostThread",
        params={"uri": f"at://{actor}/app.bsky.feed.post/{rkey}", "depth": "0", "parentHeight": "0"},
    )
    thread = data.get("thread") if isinstance(data, dict) else None
    post = thread.get("post") if isinstance(thread, dict) else None
    if not isinstance(post, dict):
        log.info("upload: bluesky post %s not found", ident)
        return []
    author_did = (post.get("author") or {}).get("did")
    images, video = _bsky_media(post.get("embed"), author_did if isinstance(author_did, str) else None)
    if images:
        return await _download_items(ctx, images)
    if video is None:
        return []
    did, cid = video
    pds = await _pds_endpoint(ctx, did)
    if pds:
        blob = f"{pds}/xrpc/com.atproto.sync.getBlob?{urlencode({'did': did, 'cid': cid})}"
        files = await _download_items(ctx, [_item([blob], video=True)])
        if files:
            return files
    # Original blob unreachable or too big: let yt-dlp pick a smaller HLS rendition.
    return await _ytdlp(ctx, f"https://bsky.app/profile/{author_did or actor}/post/{rkey}")


# --- yt-dlp-only sites --------------------------------------------------------------------


async def _tiktok(ctx: _Ctx, video_id: str) -> list[MediaFile]:
    return await _ytdlp(ctx, f"https://www.tiktok.com/@/video/{video_id}")


async def _instagram(ctx: _Ctx, shortcode: str) -> list[MediaFile]:
    return await _ytdlp(ctx, f"https://www.instagram.com/p/{shortcode}/")


_HANDLERS = {
    "twitter": _twitter,
    "pixiv": _pixiv,
    "reddit": _reddit,
    "tiktok": _tiktok,
    "bsky": _bluesky,
    "instagram": _instagram,
}
