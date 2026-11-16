from __future__ import annotations

import io

import discord
from PIL import Image

from bb import tweetcard, views
from bb.links.model import LinkMatch
from bb.links.sites import SITES

# FxTwitter's answer for a real tweet (2026-11): 17 CD spines meant to sit side by side. Trimmed to the
# fields BB reads.
SPINES = {
    "url": "https://x.com/yeunrlsd/status/2122017520756429270",
    "id": "2122017520756429270",
    "text": "Ye, \n\n2004 - 2026",
    "author": {"screen_name": "yeunrlsd", "name": "yeunrlsd"},
    "replies": 58,
    "retweets": 2370,
    "likes": 13836,
    "media": {
        "all": [
            {"type": "photo", "url": f"https://pbs.twimg.com/media/S{n}.jpg?name=orig", "width": 52,
             "height": 1195}
            for n in range(17)
        ]
    },
}

MATCH = LinkMatch(
    site=SITES["twitter"], url="https://x.com/yeunrlsd/status/2122017520756429270?s=20",
    canonical_key="twitter:2122017520756429270", parts={"id": "2122017520756429270", "user": "yeunrlsd"},
)


def media(*sizes, video=False):
    return tuple(
        tweetcard.Media(f"https://pbs.twimg.com/media/{n}.jpg", video, w, h) for n, (w, h) in enumerate(sizes)
    )


def test_parse_real_tweet():
    tweet = tweetcard.parse(SPINES)
    assert tweet is not None
    assert (tweet.user, tweet.name, tweet.text) == ("yeunrlsd", "yeunrlsd", "Ye, \n\n2004 - 2026")
    assert tweet.stats == "💬 58  🔁 2,370  ❤️ 13,836"
    assert tweet.url == "https://x.com/yeunrlsd/status/2122017520756429270"
    assert len(tweet.media) == 17 and tweetcard.wants_card(tweet)


def test_parse_needs_author_and_media():
    assert tweetcard.parse(None) is None
    assert tweetcard.parse({**SPINES, "media": {"all": []}}) is None
    assert tweetcard.parse({**SPINES, "author": {}}) is None


def test_strip_row():
    assert tweetcard.is_strip_row(media(*[(52, 1195)] * 17))
    assert tweetcard.is_strip_row(media((100, 1200), (110, 1180), (90, 1200)))  # about one height
    assert not tweetcard.is_strip_row(media(*[(52, 1195)] * 2))  # two sit side by side anyway
    assert not tweetcard.is_strip_row(media(*[(1080, 2340)] * 3))  # phone screenshots aren't strips
    # A real tweet (2026-11): strips with one wider panel where the drawing "slips off the screen".
    assert tweetcard.is_strip_row(media(*[(122, 1920)] * 7, (971, 1920), *[(122, 1920)] * 3))
    assert not tweetcard.is_strip_row(media((52, 1195), (900, 1195), (900, 1195)))  # mostly not strips
    assert not tweetcard.is_strip_row(media((52, 1195), (52, 1195), (52, 600)))  # heights differ
    assert not tweetcard.is_strip_row(media(*[(52, 1195)] * 3, video=True))
    assert not tweetcard.is_strip_row(media(*[(300, 1000)] * 12))  # joined, 3.6 times wider than tall


def test_wants_card():
    def tweet(items):
        return tweetcard.Tweet("a", None, "", "", "https://x.com/a/status/1", items)

    assert not tweetcard.wants_card(tweet(media(*[(1200, 800)] * 4)))
    assert tweetcard.wants_card(tweet(media(*[(1200, 800)] * 5)))
    assert tweetcard.wants_card(tweet(media(*[(52, 1195)] * 3)))


def _jpeg(width: int, height: int, colour: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "JPEG")
    return out.getvalue()


def test_stitch_puts_pictures_side_by_side_with_see_through_gaps():
    row = Image.open(io.BytesIO(tweetcard.stitch([_jpeg(52, 1195, "red"), _jpeg(52, 1195, "blue")] * 2)))
    assert row.size == (4 * 52 + 3 * 6, 1195) and row.format == "WEBP"  # a 6px gap at 1195 tall
    red, gap, blue = (row.convert("RGBA").getpixel((x, 600)) for x in (26, 55, 84))
    assert red[0] > 200 and red[3] == 255
    assert gap[3] == 0
    assert blue[2] > 200 and blue[3] == 255


def test_stitch_scales_to_the_shortest_and_caps_the_height():
    row = Image.open(io.BytesIO(tweetcard.stitch([_jpeg(100, 1000, "red"), _jpeg(200, 2000, "blue")])))
    assert row.size == (200 + 5, 1000)
    tall = Image.open(io.BytesIO(tweetcard.stitch([_jpeg(300, 3000, "red"), _jpeg(300, 3000, "blue")])))
    assert tall.size == (400 + 10, 2000)


def _galleries(view: discord.ui.LayoutView) -> list[list[str]]:
    (container,) = [child for child in view.children if isinstance(child, discord.ui.Container)]
    return [
        [item.media.url for item in child.items]
        for child in container.children
        if isinstance(child, discord.ui.MediaGallery)
    ]


def test_pages_of_splits_evenly():
    def sizes(n):
        return [len(page) for page in tweetcard.pages_of([str(i) for i in range(n)])]

    assert sizes(3) == [3] and sizes(10) == [10]
    assert sizes(11) == [6, 5] and sizes(17) == [9, 8] and sizes(20) == [10, 10]
    assert sizes(21) == [7, 7, 7]


def _page_ids(view: discord.ui.LayoutView) -> list[str]:
    return [cid for cid in views.component_custom_ids(view.children) if cid.startswith("bb:pg:")]


def test_card_shows_one_page_with_page_buttons():
    tweet = tweetcard.parse(SPINES)
    assert tweet is not None
    first = tweetcard.build_card(tweet, MATCH, buttons=[])
    assert _galleries(first) == [[f"https://pbs.twimg.com/media/S{n}.jpg?name=orig" for n in range(9)]]
    assert _page_ids(first) == ["bb:pg:p:0", "bb:pg:at", "bb:pg:n:1"]
    second = tweetcard.build_card(tweet, MATCH, buttons=[], page=1)
    assert _galleries(second) == [[f"https://pbs.twimg.com/media/S{n}.jpg?name=orig" for n in range(9, 17)]]
    assert _galleries(tweetcard.build_card(tweet, MATCH, buttons=[], page=7)) == _galleries(second)  # clamped


def test_card_with_ten_or_fewer_has_no_page_buttons():
    tweet = tweetcard.parse({**SPINES, "media": {"all": SPINES["media"]["all"][:10]}})
    assert tweet is not None
    view = tweetcard.build_card(tweet, MATCH, buttons=[])
    assert [len(g) for g in _galleries(view)] == [10] and _page_ids(view) == []


def test_card_with_a_joined_row_shows_only_that():
    tweet = tweetcard.parse(SPINES)
    assert tweet is not None
    view = tweetcard.build_card(tweet, MATCH, buttons=[], cover=tweetcard.Cover(b"x", "row.webp"), quip="hi")
    assert _galleries(view) == [["attachment://row.webp"]]
    assert isinstance(view.children[0], discord.ui.TextDisplay) and view.children[0].content == "-# hi"


# FxTwitter's answer for a real FGO post quoting another (2026-11), fetched with /en and trimmed.
QUOTING = {
    "url": "https://x.com/fgoproject/status/2122268632776642909",
    "text": "【カルデア広報局より】\n★5(SSR)ダイナマイト利休を紹介！",
    "translation": {"text": "Introducing ★5 (SSR) Dynamite Rikyu!", "source_lang": "ja", "target_lang": "en"},
    "author": {"screen_name": "fgoproject", "name": "【公式】Fate/Grand Order"},
    "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/A.png", "width": 1200, "height": 675}]},
    "quote": {
        "url": "https://x.com/fgoproject/status/2122253808327860593",
        "text": "ピックアップ召喚を開催！\n#FGO",
        "translation": {"text": "The pickup summon is on!\n#FGO", "source_lang": "ja", "target_lang": "en"},
        "author": {"screen_name": "fgoproject", "name": "【公式】Fate/Grand Order"},
    },
}


def test_card_shows_the_quoted_post():
    tweet = tweetcard.parse(QUOTING)
    assert tweet is not None and tweet.quote is not None and tweet.translated_from is None
    text = tweetcard.card_text(tweet)
    assert text.endswith(
        "> **[【公式】Fate/Grand Order (@fgoproject)](https://x.com/fgoproject/status/2122253808327860593)**\n"
        "> ピックアップ召喚を開催！\n"
        "> \\#FGO"
    )


def test_translated_card_translates_the_tweet_and_its_quote():
    tweet = tweetcard.parse(QUOTING, translated=True)
    assert tweet is not None and tweet.translated_from == "ja"
    text = tweetcard.card_text(tweet)
    assert "Introducing ★5 (SSR) Dynamite Rikyu!\n-# Translated from ja\n> **" in text
    assert text.endswith("> The pickup summon is on!\n> \\#FGO")


def test_translated_without_a_translation_keeps_the_original():
    tweet = tweetcard.parse({**QUOTING, "translation": None}, translated=True)
    assert tweet is not None and tweet.translated_from is None
    assert tweet.text.startswith("【カルデア広報局より】")


def test_card_text_escapes_markdown():
    tweet = tweetcard.Tweet("a_b", "A [b]", "*bold* claim", "", "https://x.com/a_b/status/1", ())
    assert tweetcard.card_text(tweet) == "**[A \\[b\\] (@a_b)](https://x.com/a_b)**\n\\*bold\\* claim"
