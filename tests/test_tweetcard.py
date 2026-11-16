from __future__ import annotations

import io

import discord
from PIL import Image

from bb import tweetcard
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


def test_card_shows_every_picture_ten_to_a_gallery():
    tweet = tweetcard.parse(SPINES)
    assert tweet is not None
    view = tweetcard.build_card(tweet, MATCH, buttons=[])
    galleries = _galleries(view)
    assert [len(g) for g in galleries] == [10, 7] and galleries[0][0] == "https://pbs.twimg.com/media/S0.jpg?name=orig"


def test_card_with_a_joined_row_shows_only_that():
    tweet = tweetcard.parse(SPINES)
    assert tweet is not None
    view = tweetcard.build_card(tweet, MATCH, buttons=[], cover=tweetcard.Cover(b"x", "row.webp"), quip="hi")
    assert _galleries(view) == [["attachment://row.webp"]]
    assert isinstance(view.children[0], discord.ui.TextDisplay) and view.children[0].content == "-# hi"


def test_card_text_escapes_markdown():
    tweet = tweetcard.Tweet("a_b", "A [b]", "*bold* claim", "", "https://x.com/a_b/status/1", ())
    assert tweetcard.card_text(tweet) == "**[A \\[b\\] (@a_b)](https://x.com/a_b)**\n\\*bold\\* claim"
