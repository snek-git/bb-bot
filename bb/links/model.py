"""Shared link types. Track A fills the site table in sites.py using these."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fixer:
    """A third-party (or self-hosted) service that serves embed-friendly pages for a site."""

    id: str  # stable id used in the DB and health checks, e.g. "fxtwitter"
    name: str  # shown in the repost line, e.g. "FxTwitter"
    domain: str  # e.g. "fxtwitter.com"
    translatable: bool = False  # supports the "/<lang>" translation suffix


@dataclass(frozen=True)
class Site:
    id: str  # "twitter", "pixiv", "reddit", "tiktok", "bluesky", "instagram"
    name: str  # human name for settings and roundups, e.g. "Twitter"
    label: str  # first link in the repost line, e.g. "Tweet"
    fixers: tuple[Fixer, ...]  # configured fallback order
    test_url: str  # a stable public post used by health checks


@dataclass
class LinkMatch:
    site: Site
    url: str  # the original URL (after short-link expansion)
    canonical_key: str  # same post -> same key regardless of domain, e.g. "twitter:1234"
    author: str | None = None  # shown as "@author" when the URL names one
    author_url: str | None = None
    spoiler: bool = False
    parts: dict[str, str] = field(default_factory=dict)  # site-specific pieces (ids, handles) for rewriting
