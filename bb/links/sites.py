"""Site table, URL matching and fixer chains. Owned by track A; signatures are the contract.

Every supported post URL becomes a LinkMatch whose ``canonical_key`` is the same no matter which
domain it came from (x.com, twitter.com, fxtwitter.com, ...). Fixer URLs are rebuilt from the
parsed ``parts``, never by swapping the domain of the input, so tracking junk and odd path shapes
never reach a fixer.

``LinkMatch.parts`` keys (all values are strings):

* every site
    - ``already_fixed``: ``"1"`` when the URL is already on a fixer/mirror domain (fxtwitter.com,
      phixiv.net, ...). Absent otherwise.
    - ``mirror``: ``"1"`` when the host is unknown and the link was recognised by path shape alone
      (meme embedder domains like girlcockx.com, nitter/xcancel, redlib instances). Always comes
      with ``already_fixed``. Only twitter, bluesky, tiktok and reddit have shapes distinctive
      enough for this; see ``_MIRRORS``.
* twitter: ``id`` (status id), ``user`` (screen name; absent for ``/i/status/<id>`` links)
* pixiv: ``id`` (artwork id)
* reddit: ``id`` (base36 post id), ``sub`` (subreddit, when the URL names one),
  ``user`` (when the post lives under ``/user/<name>/``), ``comment`` (comment id for comment
  permalinks; the key is still the post's)
* tiktok: ``id`` (video/photo id), ``kind`` (``"video"`` or ``"photo"``), ``user`` (when named)
* bluesky: ``actor`` (handle or DID, lower-cased), ``rkey`` (post record key)
* instagram: ``shortcode`` (case-sensitive), ``kind`` (``"p"`` or ``"reel"``), ``user`` (when named)

``author``/``author_url`` are set only when the URL itself names the poster: twitter and tiktok
users, ``/user/<name>/`` reddit posts, Bluesky handles (not DIDs) and ``instagram.com/<user>/p/...``.

Fixer notes (verified live 2026-10, Discordbot UA, HTTP 200 + og:/twitter: tags):

* FxEmbed fixers (fxtwitter, fixupx, fxbsky) serve only og:title/twitter:card in the HTML and deliver
  media through an ``application/activity+json`` link (Mastodon API), so health checks must not
  require og:image.
* fixupx is a second domain on the FxEmbed backend; vxtwitter is independent, so it comes second.
* phixiv.net and ppxiv.net are the same server (a second domain only busts Discord's embed cache).
* redditez and tiktokez are both EmbedEZ (one backend, correlated failures).
* Dropped as dead or broken: rxddit (502), vxtiktok (shut down after a legal request), tiktxk (its
  video proxy gets 403 from TikTok), kkinstagram/zzinstagram (raw CDN redirect, no meta tags),
  ddinstagram (DNS gone), vxinstagram (401), instagramez (redirects to an ad domain), bsyy/bskye,
  vxbsky (worked, then HTTP 500 on every post within the same hour).
  They are still recognised below so their links get the right canonical key.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Sequence
from urllib.parse import parse_qs, urlsplit

from bb.links.model import Fixer, LinkMatch, Site

# Fixer id -> function building the path (with leading "/") from LinkMatch.parts.
_PathBuilder = Callable[[dict[str, str]], str]


def _twitter_path(p: dict[str, str]) -> str:
    return f"/{p.get('user') or 'i'}/status/{p['id']}"


def _pixiv_path(p: dict[str, str]) -> str:
    return f"/artworks/{p['id']}"


def _reddit_path(p: dict[str, str]) -> str:
    # EmbedEZ needs the long form; "/comments/<id>" works everywhere when there's no subreddit.
    if p.get("sub"):
        return f"/r/{p['sub']}/comments/{p['id']}/"
    return f"/comments/{p['id']}/"


def _tiktok_path(p: dict[str, str]) -> str:
    # "@i" is accepted by every TikTok fixer when the URL didn't name the user.
    return f"/@{p.get('user') or 'i'}/{p.get('kind', 'video')}/{p['id']}"


def _bsky_path(p: dict[str, str]) -> str:
    return f"/profile/{p['actor']}/post/{p['rkey']}"


def _instagram_path(p: dict[str, str]) -> str:
    return f"/{p.get('kind', 'p')}/{p['shortcode']}/"


_PATHS: dict[str, _PathBuilder] = {
    "twitter": _twitter_path,
    "pixiv": _pixiv_path,
    "reddit": _reddit_path,
    "tiktok": _tiktok_path,
    "bluesky": _bsky_path,
    "instagram": _instagram_path,
}

SITES: dict[str, Site] = {
    "twitter": Site(
        id="twitter",
        name="Twitter",
        label="Tweet",
        fixers=(
            Fixer(
                "fxtwitter", "FxTwitter", "fxtwitter.com", translatable=True, direct_domain="d.fxtwitter.com"
            ),
            Fixer(
                "vxtwitter", "vxTwitter", "vxtwitter.com", translatable=True, direct_domain="d.vxtwitter.com"
            ),
            Fixer("fixupx", "FixupX", "fixupx.com", translatable=True, direct_domain="d.fixupx.com"),
        ),
        test_url="https://x.com/BarackObama/status/266031293945503744",
    ),
    "pixiv": Site(
        id="pixiv",
        name="Pixiv",
        label="Pixiv",
        fixers=(
            Fixer("phixiv", "phixiv", "phixiv.net"),
            Fixer("ppxiv", "ppxiv", "ppxiv.net"),
        ),
        test_url="https://www.pixiv.net/artworks/1580459",
    ),
    "reddit": Site(
        id="reddit",
        name="Reddit",
        label="Reddit",
        fixers=(
            Fixer("vxreddit", "vxreddit", "vxreddit.com"),
            Fixer("redditez", "RedditEZ", "redditez.com"),
        ),
        test_url="https://www.reddit.com/r/pics/comments/haucpf/ive_found_a_few_funny_memories_during_lockdown/",
    ),
    "tiktok": Site(
        id="tiktok",
        name="TikTok",
        label="TikTok",
        fixers=(
            Fixer("fxtiktok", "fxTikTok", "tnktok.com"),
            Fixer("tfxktok", "tfxktok", "tfxktok.com"),
            Fixer("tiktokez", "TikTokEZ", "tiktokez.com"),
        ),
        test_url="https://www.tiktok.com/@discord/video/7437673559146827054",
    ),
    "bluesky": Site(
        id="bluesky",
        name="Bluesky",
        label="Bluesky",
        fixers=(
            Fixer("fxbsky", "FxBluesky", "fxbsky.app"),
            Fixer("xbsky", "xbsky", "xbsky.app"),
            # Last: shows only the avatar for the new 10-photo gallery posts.
            Fixer("bskx", "VixBluesky", "bskx.app"),
        ),
        test_url="https://bsky.app/profile/bsky.app/post/3m4j4iy4oek2d",
    ),
    "instagram": Site(
        id="instagram",
        name="Instagram",
        label="Instagram",
        fixers=(
            Fixer("oginstagram", "OGInstagram", "oginstagram.com"),
            Fixer("fxig", "fxig", "fxig.seria.moe"),
            Fixer("inso", "ins.so", "ins.so"),
            # Last: its og:image is a relative URL.
            Fixer("uuinstagram", "uuinstagram", "uuinstagram.com"),
        ),
        test_url="https://www.instagram.com/p/BsOGulcndj-/",
    ),
}

# Original domains. Subdomains (www., mobile., old., m., ...) match too.
_SOURCE_DOMAINS: dict[str, tuple[str, ...]] = {
    "twitter": ("twitter.com", "x.com"),
    "pixiv": ("pixiv.net",),
    "reddit": ("reddit.com",),
    "tiktok": ("tiktok.com",),
    "bluesky": ("bsky.app",),
    "instagram": ("instagram.com", "instagr.am"),
}

# Fixer and mirror domains that aren't in a chain but should still match (already_fixed).
_EXTRA_FIXED_DOMAINS: dict[str, tuple[str, ...]] = {
    "twitter": ("fixvx.com", "twittpr.com", "xfixup.com", "twitterez.com"),
    "pixiv": ("vxpixiv.net",),
    "reddit": ("rxddit.com", "rxyddit.com"),
    "tiktok": ("vxtiktok.com", "tiktxk.com", "kktiktok.com"),
    "bluesky": ("vxbsky.app", "bskyx.app", "bsyy.app", "bskye.app", "cbsky.app"),
    "instagram": (
        "ddinstagram.com",
        "kkinstagram.com",
        "zzinstagram.com",
        "eeinstagram.com",
        "vxinstagram.com",
        "instagramez.com",
        "toinstagram.com",
    ),
}

_FIXED_DOMAINS: dict[str, tuple[str, ...]] = {
    sid: tuple(dict.fromkeys((*(f.domain for f in site.fixers), *_EXTRA_FIXED_DOMAINS.get(sid, ()))))
    for sid, site in SITES.items()
}

_FIXERS: dict[str, tuple[Site, Fixer]] = {f.id: (s, f) for s in SITES.values() for f in s.fixers}

# --- path patterns -------------------------------------------------------------------------------

_LANG = r"(?:/[a-z]{2}(?:[-_][a-z]{2,4})?)"  # FxEmbed-style translation suffix, or pixiv's /en/

_TWITTER = re.compile(
    r"/(?:(?P<user>\w{1,15})|i/web)/status(?:es)?/(?P<id>\d{1,20})"
    rf"(?:/(?:photos?|videos?)/\d+)?{_LANG}?/?",
    re.I,
)
_PIXIV = re.compile(rf"{_LANG}?/(?:artworks|i)/(?P<id>\d+)(?:/\d+)?/?", re.I)
_PIXIV_LEGACY = re.compile(r"/member_illust\.php", re.I)
_REDDIT = (
    re.compile(
        r"/r/(?P<sub>\w+)/comments/(?P<id>[a-z0-9]{2,10})(?:/[^/]*(?:/(?P<comment>[a-z0-9]{2,10}))?)?/?",
        re.I,
    ),
    re.compile(
        r"/(?:u|user)/(?P<user>[\w-]{3,20})/comments/(?P<id>[a-z0-9]{2,10})"
        r"(?:/[^/]*(?:/(?P<comment>[a-z0-9]{2,10}))?)?/?",
        re.I,
    ),
    re.compile(r"/(?:comments|gallery)/(?P<id>[a-z0-9]{2,10})(?:/.*)?", re.I),
)
_REDDIT_SHORT = re.compile(r"/(?P<id>[a-z0-9]{5,10})/?", re.I)  # redd.it/<id>, vxreddit.com/<id>
_TIKTOK = (
    re.compile(r"/@(?P<user>[\w.-]*)/(?P<kind>video|photo)/(?P<id>\d{8,25})/?", re.I),
    re.compile(r"/(?:v|video|embed(?:/v2)?|share/video)/(?P<id>\d{8,25})(?:\.html)?/?", re.I),
)
_BSKY = re.compile(r"/profile/(?P<actor>[^/]+)/post/(?P<rkey>[a-z0-9]{2,20})(?:/.*)?", re.I)
_INSTAGRAM = re.compile(
    r"(?:/(?P<user>[\w.]{1,30}))?/(?P<kind>p|reels?|tv)/(?P<shortcode>[\w-]{5,40})(?:/.*)?",
)

# Strict path shapes for hosts we've never heard of (meme embedders, nitter, redlib, ...). Each one is
# a subset of the matching site parser above and distinctive enough not to hit other software:
# Mastodon uses /@user/<id>, /users/<u>/statuses/<id> and /web/statuses/<id> (excluded below), Lemmy
# uses /post/<id>, Misskey /notes/<id>. Pixiv and Instagram shapes are too generic and stay out.
_MIRRORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "twitter",
        re.compile(
            r"/(?!web/)(?:[a-z0-9_]{1,15}|i/web)/status(?:es)?/\d{17,19}"
            r"(?:/(?:photo|video)/\d{1,2})?(?:/[a-z]{2}(?:[-_][a-z]{2,4})?)?/?",
            re.I,
        ),
    ),
    (
        "bluesky",
        re.compile(
            r"/profile/(?:did:plc:[a-z2-7]{24}|did:web:[a-z0-9.-]+\.[a-z]{2,}"
            r"|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9])"
            r"/post/[a-z2-7]{13}/?",
            re.I,
        ),
    ),
    ("tiktok", re.compile(r"/@[\w.-]{0,24}/(?:video|photo)/\d{15,25}/?", re.I)),
    (
        "reddit",
        re.compile(r"/r/\w{2,21}/comments/[a-z0-9]{3,10}(?:/[^/]*(?:/[a-z0-9]{2,10})?)?/?", re.I),
    ),
)


TWITTER_EPOCH_MS = 1288834974657


def plausible_tweet_id(tweet_id: str, now_ts: float | None = None) -> bool:
    """For hosts we've never heard of: a real tweet id is a snowflake whose timestamp is in the past.
    Order, build and ticket numbers that happen to sit under /status/ usually aren't."""
    try:
        ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
    except ValueError:
        return False
    return ms <= ((time.time() if now_ts is None else now_ts) + 86400) * 1000


def _host_in(host: str, domains: Collection[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _site_for_host(host: str) -> tuple[str, bool] | None:
    """(site id, already_fixed) for a host, or None."""
    for sid, domains in _FIXED_DOMAINS.items():
        if _host_in(host, domains):
            return sid, True
    for sid, domains in _SOURCE_DOMAINS.items():
        if _host_in(host, domains):
            return sid, False
    if host == "redd.it":
        return "reddit", False
    return None


def _clean(groups: dict[str, str | None]) -> dict[str, str]:
    return {k: v for k, v in groups.items() if v}


def _parse_twitter(path: str, _query: str, _fixed: bool) -> dict[str, str] | None:
    m = _TWITTER.fullmatch(path)
    if not m:
        return None
    parts = _clean(m.groupdict())
    if parts.get("user", "").lower() == "i":
        del parts["user"]
    return parts


def _parse_pixiv(path: str, query: str, _fixed: bool) -> dict[str, str] | None:
    m = _PIXIV.fullmatch(path)
    if m:
        return {"id": m["id"]}
    if _PIXIV_LEGACY.fullmatch(path):
        ids = parse_qs(query).get("illust_id")
        if ids and ids[0].isdigit():
            return {"id": ids[0]}
    return None


def _parse_reddit(path: str, _query: str, short_ok: bool) -> dict[str, str] | None:
    for pattern in _REDDIT:
        m = pattern.fullmatch(path)
        if m:
            parts = _clean(m.groupdict())
            parts["id"] = parts["id"].lower()
            return parts
    if short_ok:
        m = _REDDIT_SHORT.fullmatch(path)
        if m:
            return {"id": m["id"].lower()}
    return None


def _parse_tiktok(path: str, _query: str, _fixed: bool) -> dict[str, str] | None:
    for pattern in _TIKTOK:
        m = pattern.fullmatch(path)
        if m:
            parts = _clean(m.groupdict())
            parts["kind"] = parts.get("kind", "video").lower()
            if parts.get("user", "").lower() == "i":  # placeholder used on fixer links
                del parts["user"]
            return parts
    return None


def _parse_bsky(path: str, _query: str, _fixed: bool) -> dict[str, str] | None:
    m = _BSKY.fullmatch(path)
    if not m:
        return None
    return {"actor": m["actor"].lower(), "rkey": m["rkey"]}


_INSTAGRAM_NOT_USERS = {"share", "stories", "explore", "accounts", "direct"}


def _parse_instagram(path: str, _query: str, _fixed: bool) -> dict[str, str] | None:
    m = _INSTAGRAM.fullmatch(path)
    if not m:
        return None
    parts = _clean(m.groupdict())
    if parts.get("user", "").lower() in _INSTAGRAM_NOT_USERS:
        return None
    parts["kind"] = "reel" if parts["kind"].startswith("reel") else "p"
    return parts


_PARSERS: dict[str, Callable[[str, str, bool], dict[str, str] | None]] = {
    "twitter": _parse_twitter,
    "pixiv": _parse_pixiv,
    "reddit": _parse_reddit,
    "tiktok": _parse_tiktok,
    "bluesky": _parse_bsky,
    "instagram": _parse_instagram,
}


def _key(site_id: str, parts: dict[str, str]) -> str:
    if site_id == "bluesky":
        return f"bsky:{parts['actor']}/{parts['rkey']}"
    if site_id == "instagram":
        return f"instagram:{parts['shortcode']}"
    return f"{site_id}:{parts['id']}"


def _author(site_id: str, parts: dict[str, str]) -> tuple[str | None, str | None]:
    user = parts.get("user")
    if site_id == "twitter" and user:
        return user, f"https://x.com/{user}"
    if site_id == "tiktok" and user:
        return user, f"https://www.tiktok.com/@{user}"
    if site_id == "reddit" and user:
        return user, f"https://www.reddit.com/user/{user}/"
    if site_id == "instagram" and user:
        return user, f"https://www.instagram.com/{user}/"
    if site_id == "bluesky" and not parts["actor"].startswith("did:"):
        return parts["actor"], f"https://bsky.app/profile/{parts['actor']}"
    return None, None


def match(url: str, spoiler: bool = False) -> LinkMatch | None:
    """Return a LinkMatch if url is a supported post (any known domain, including fixer domains)."""
    try:
        split = urlsplit(url.strip())
        host = (split.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if split.scheme.lower() not in ("http", "https") or not host:
        return None
    path = re.sub(r"/{2,}", "/", split.path) or "/"
    found = _site_for_host(host)
    mirror = found is None
    if mirror:
        site_id = next((sid for sid, shape in _MIRRORS if shape.fullmatch(path)), None)
        if site_id is None:
            return None
        fixed = True
    else:
        site_id, fixed = found
    short_ok = (host == "redd.it" or fixed) and not mirror
    parts = _PARSERS[site_id](path, split.query, short_ok)
    if parts is None:
        return None
    if mirror and site_id == "twitter" and not plausible_tweet_id(parts.get("id", "")):
        return None
    if fixed:
        parts["already_fixed"] = "1"
    if mirror:
        parts["mirror"] = "1"
    author, author_url = _author(site_id, parts)
    return LinkMatch(
        site=SITES[site_id],
        url=url,
        canonical_key=_key(site_id, parts),
        author=author,
        author_url=author_url,
        spoiler=spoiler,
        parts=parts,
    )


def chain(site_id: str, down: Collection[str] = ()) -> list[Fixer]:
    """Fixers to try for a site: healthy ones in configured order, then the ones in `down`."""
    site = SITES.get(site_id)
    if site is None:
        return []
    healthy = [f for f in site.fixers if f.id not in down]
    unhealthy = [f for f in site.fixers if f.id in down]
    return healthy + unhealthy


_LANG_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z]{2,4})?")


def fixed_url(m: LinkMatch, fixer: Fixer, lang: str | None = None) -> str:
    """The fixer URL for this post. lang adds the translation suffix if the fixer supports it."""
    url = f"https://{fixer.domain}{_PATHS[m.site.id](m.parts)}"
    if lang and fixer.translatable:
        code = lang.strip().lower().replace("_", "-")
        if _LANG_CODE.fullmatch(code):
            url = f"{url.rstrip('/')}/{code}"
    return url


def media_links(
    m: LinkMatch, fixer: Fixer, media: Sequence[str] = (), lang: str | None = None
) -> list[tuple[str, str]]:
    """(kind, fixer URL) for each link a repost needs. Normally one. A tweet mixing photos and videos gets
    one per item: Discord folds same-URL previews into one image gallery, where a video can't play, so
    each item gets its own URL ("/photo/1", "/video/2"; every fixer picks the item by position). The
    first is the full card with the tweet's text; the rest are the bare media (the fixer's direct
    domain), so the text doesn't show twice."""
    base = fixed_url(m, fixer).rstrip("/")
    if not media:
        return [("", fixed_url(m, fixer, lang=lang))]
    direct = base
    if fixer.direct_domain:
        direct = base.replace(f"https://{fixer.domain}", f"https://{fixer.direct_domain}", 1)
    links = []
    for index, kind in enumerate(media[:4], 1):
        item = f"/{'photo' if kind == 'photo' else 'video'}/{index}"
        if index > 1:
            links.append((kind, direct + item))  # media only: no text, so no translation either
            continue
        url = base + item
        if lang and fixer.translatable:
            code = lang.strip().lower().replace("_", "-")
            if _LANG_CODE.fullmatch(code):
                url = f"{url}/{code}"
        links.append((kind, url))
    return links


def get_fixer(fixer_id: str) -> Fixer | None:
    found = _FIXERS.get(fixer_id)
    return found[1] if found else None


def site_of_fixer(fixer_id: str) -> Site | None:
    found = _FIXERS.get(fixer_id)
    return found[0] if found else None


# Where "Open on X" (and friends) points: the real site, never a fixer or meme mirror.
_CANONICAL_HOSTS: dict[str, str] = {
    "twitter": "x.com",
    "pixiv": "www.pixiv.net",
    "reddit": "www.reddit.com",
    "tiktok": "www.tiktok.com",
    "bluesky": "bsky.app",
    "instagram": "www.instagram.com",
}


def source_url(m: LinkMatch) -> str:
    """The post on its real site. The URL as posted, unless it was a fixer or meme-mirror link."""
    if m.parts.get("already_fixed") != "1":
        return m.url
    try:
        return f"https://{_CANONICAL_HOSTS[m.site.id]}{_PATHS[m.site.id](m.parts)}"
    except KeyError:  # a match without the parts to rebuild it: the posted URL still works
        return m.url
