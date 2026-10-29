"""Shop and quest resets: when the heads-up and the "has reset" post are due. No network, no Discord."""

from __future__ import annotations

from datetime import datetime

from bb import resets
from bb.news import DAY, JST, SOON, START


def jst(*args: int) -> float:
    return datetime(*args, tzinfo=JST).timestamp()


NOV_1 = jst(2026, 11, 1)  # Mana Prism shop and craftable grails
NOV_1_EVENING = jst(2026, 11, 1, 18)  # Hellish Ordeal, and its shop (November is a shop month)
MIDNIGHT = "Mana Prism shop, Craftable grails"


def midnight(notices):
    """Only the 00:00 resets' posts: the 18:00 ones' windows overlap them."""
    return [n for n in notices if MIDNIGHT in n.text]


def texts(notices):
    return [n.text.split("\n")[0] for n in notices]


def test_a_day_before_then_when_it_happens():
    assert midnight(resets.due(NOV_1 - DAY - 60, set())) == []
    (soon,) = midnight(resets.due(NOV_1 - DAY + 60, set()))
    assert soon.text == (
        f"**Resets in a day** · {MIDNIGHT}\n-# Resets <t:{int(NOV_1)}:f> (<t:{int(NOV_1)}:R>)"
    )
    assert set(soon.marks) == {("reset:mana:2026-11-01T00", SOON), ("reset:grails:2026-11-01T00", SOON)}
    done = set(soon.marks)
    assert midnight(resets.due(NOV_1 - 60, done)) == []
    (now,) = midnight(resets.due(NOV_1 + 60, done))
    assert now.text == f"**Just reset** · {MIDNIGHT}"
    done |= set(now.marks)
    assert midnight(resets.due(NOV_1 + 3600, done)) == []


def test_a_reset_is_only_news_for_a_few_hours():
    # BB was offline over the reset, or the news channel was set the day after: no late "has reset"
    assert f"**Just reset** · {MIDNIGHT}" in texts(resets.due(NOV_1 + 5 * 3600, set()))
    assert midnight(resets.due(NOV_1 + 7 * 3600, set())) == []


def test_missing_the_heads_up_still_posts_the_reset_once():
    (now,) = midnight(resets.due(NOV_1 + 60, set()))
    assert ("reset:mana:2026-11-01T00", SOON) in now.marks  # its heads-up is old news now
    assert ("reset:mana:2026-11-01T00", START) in now.marks


def test_the_hellish_ordeal_shop_resets_every_three_months():
    both = "**Just reset** · Hellish Ordeal, Hellish Ordeal shop"
    assert texts(resets.due(NOV_1_EVENING + 60, set())) == [both]
    assert texts(resets.due(jst(2026, 12, 1, 18) + 60, set())) == ["**Just reset** · Hellish Ordeal"]
    shop = next(r for r in resets.RESETS if r.slug == "ordeal_shop")
    months = {datetime.fromtimestamp(t, JST).month
              for m in range(1, 13) for t in resets.occurrences(shop, jst(2026, m, 10))}
    assert sorted(months) == [2, 5, 8, 11]


def test_the_rare_prism_shop_resets_on_the_15th():
    (soon,) = resets.due(jst(2026, 11, 14, 1), set())
    assert soon.text.startswith("**Resets in a day** · Rare Prism shop\n")


def test_a_year_end_reset_is_found_from_december():
    (soon,) = midnight(resets.due(jst(2026, 12, 31, 1), set()))
    assert f"<t:{int(jst(2027, 1, 1))}:R>" in soon.text
