from __future__ import annotations

import pytest

from bb import instagram, views
from bb.links import sites

# OGInstagram's notes for two real posts (2026-11), trimmed to the fields BB reads.
HORSE = {
    "attachment": [{"mediaType": "video/mp4", "url": "https://oginstagram.com/offload/DdpIbz_sQWm/1?sig=x"}],
    "attributedTo": "https://oginstagram.com/alpha_horse_rescue",
    "content": (
        "<p><b>❤️ 18,409  \U0001f4ac 70</b></p><p>Literally no one even KNOWS how hard it is to be "
        "three years old and unemployed.  <a href=\"https://www.instagram.com/explore/search/keyword/?q=%23"
        "unemployed\">#unemployed</a> <a href=\"https://www.instagram.com/explore/search/keyword/?q=%23baby\">"
        "#baby</a> equestrian sleepy relax growingup Summer vibes fypシ pet nj</p>"
    ),
}
EGG = {
    "attachment": [{"mediaType": "image/jpeg", "url": "https://oginstagram.com/offload/BsOGulcndj-/1?sig=y"}],
    "attributedTo": "https://oginstagram.com/world_record_egg",
    "content": (
        "<p><b>❤️ 60,249,526  \U0001f4ac 3,766,767</b></p><p>Let’s set a world record together "
        "&amp; get the most liked post on Instagram.<br>We got this \U0001f64c<br><br>"
        "<a href=\"https://www.instagram.com/explore/search/keyword/?q=%23LikeTheEgg\">#LikeTheEgg</a> "
        "<a href=\"https://www.instagram.com/explore/search/keyword/?q=%23EggGang\">#EggGang</a></p>"
    ),
}


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        # Keto-style: the block after the caption goes, plain words in it too
        ("and unemployed.  #unemployed #baby equestrian sleepy", "and unemployed."),
        ("We got this 🙌\n\n#LikeTheEgg #EggGang", "We got this 🙌"),
        ("new drop! #art #oc", "new drop!"),
        ("so fun #summer #beach", "so fun"),  # hashtags to the very end
        ("off to #paris today", "off to #paris today"),  # a hashtag inside a sentence stays
        ("#only #tags", ""),
        ("no tags at all", "no tags at all"),
    ],
)
def test_strip_hashtags(caption, expected):
    assert instagram.strip_hashtags(caption) == expected


def test_parse_note_video_post():
    post = instagram.parse_note(HORSE, name="NANDAWULA FLORENCE")
    assert post is not None
    assert (post.user, post.name) == ("alpha_horse_rescue", "NANDAWULA FLORENCE")
    assert post.stats == "❤️ 18,409  💬 70"
    assert post.caption == "Literally no one even KNOWS how hard it is to be three years old and unemployed."
    assert [m.video for m in post.media] == [True] and not post.has_image


def test_parse_note_keeps_line_breaks_and_unescapes():
    post = instagram.parse_note(EGG)
    assert post is not None and post.name is None and post.has_image
    assert post.caption == (
        "Let’s set a world record together & get the most liked post on Instagram.\nWe got this 🙌"
    )


def test_parse_note_without_media_or_author_is_no_card():
    assert instagram.parse_note({**EGG, "attachment": []}) is None
    page = [{"mediaType": "text/html", "url": "https://x"}]
    assert instagram.parse_note({**EGG, "attachment": page}) is None
    assert instagram.parse_note({**EGG, "attributedTo": ""}) is None


def test_caption_paragraphs_and_no_stats():
    stats, caption = instagram.split_content("<p>first</p><p>second</p>")
    assert (stats, caption) == ("", "first\n\nsecond")


def _container(view):
    (container,) = [c for c in view.children if type(c).__name__ == "Container"]
    return container.children


def test_card_layout():
    m = sites.match("https://www.instagram.com/reel/DdpIbz_sQWm/?igsh=x")
    post = instagram.parse_note(HORSE, name="Some Name [x]")
    buttons = views.repost_buttons(origin_author_id=1, has_image=True)
    view = instagram.build_card(post, m, buttons=buttons, quip="nice")
    assert view.children[0].content == "-# nice"
    text, gallery, row = _container(view)
    assert text.content.splitlines() == [
        r"**[Some Name \[x\] (@alpha_horse_rescue)](https://www.instagram.com/alpha_horse_rescue/)**",
        "Literally no one even KNOWS how hard it is to be three years old and unemployed.",
        "-# ❤️ 18,409  💬 70",
    ]
    assert [item.media.url for item in gallery.items] == [HORSE["attachment"][0]["url"]]
    link, find = row.children
    assert (link.label, link.url) == ("Open on Instagram", "https://www.instagram.com/reel/DdpIbz_sQWm/")
    assert find.custom_id == views.FIND_SOURCE_ID


def test_card_escapes_markdown_and_spoilers():
    m = sites.match("https://www.instagram.com/p/BsOGulcndj-/", spoiler=True)
    post = instagram.parse_note({**EGG, "content": "<p>*bold* claims</p>"})
    text, gallery, _row = _container(instagram.build_card(post, m, buttons=[]))
    assert text.content.splitlines()[1] == r"\*bold\* claims"
    assert all(item.spoiler for item in gallery.items)
