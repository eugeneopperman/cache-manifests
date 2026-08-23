#!/usr/bin/env python3
"""Tests for the parts of the harvester that decide something.

The network is not mocked and not exercised — what is worth pinning here is the
judgement: what counts as relevant, what counts as the same offer twice, what a headline
implies, and above all what this tool is forbidden from touching.

Run: python3 harvester/test_harvest.py
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harvest import (  # noqa: E402
    FEED_PATH, contains_phrase, dedupe_key, guess_institution, guess_value_cents,
    looks_relevant, normalise, parse_rss, rss_day, visible_text,
)

HERE = Path(__file__).resolve().parent


class TestTheLineThisToolMustNotCross(unittest.TestCase):
    """The whole arrangement rests on this: the harvester proposes, a person publishes."""

    def test_candidates_json_is_the_only_thing_this_writes(self):
        """The strongest form of the rule: one write target, and it isn't the feed.

        Asserted against the source rather than by running it, because the failure this
        guards is someone later adding a convenient "and publish it too" line. FEED_PATH
        exists only so we can dedupe against what is already live — if a write ever goes
        through it, `verifiedOn` starts meaning "a script saw a headline" and the app's
        staleness badge becomes a lie.
        """
        source = (HERE / "harvest.py").read_text()
        targets = set(re.findall(r"(\w+)\.write_text\(", source))
        self.assertEqual(targets, {"CANDIDATES_PATH"},
                         f"harvest.py writes to {sorted(targets)}; only CANDIDATES_PATH is allowed")
        self.assertNotIn("FEED_PATH.write", source)
        self.assertNotIn("shutil", source, "no file copying either")

    def test_the_feed_is_read_and_only_read(self):
        source = (HERE / "harvest.py").read_text()
        self.assertIn("feed_path.read_text()", source, "the feed should still be read, for deduping")

    def test_every_candidate_says_it_is_unverified(self):
        source = (HERE / "harvest.py").read_text()
        self.assertIn("Lead only", source)

    def test_the_rss_description_is_never_read(self):
        # The description is the publisher's own summary. Recording that an offer exists
        # is a different act from copying what someone wrote about it, and this is where
        # that distinction is actually enforced.
        entries = parse_rss("""<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>Chase Total Checking $300 Bonus</title>
            <link>https://example.com/a</link>
            <description>Our editors think this is the best offer of the month.</description>
            <pubDate>Mon, 18 Aug 2026 12:00:00 +0000</pubDate></item>
        </channel></rss>""")
        self.assertEqual(len(entries), 1)
        self.assertNotIn("description", entries[0])
        self.assertNotIn("editors", json.dumps(entries[0]))


class TestHeadlineReading(unittest.TestCase):

    def test_it_finds_the_institution(self):
        self.assertEqual(guess_institution("Chase Total Checking $300 Bonus"), "Chase")
        self.assertEqual(guess_institution("New Ally Bank savings promotion"), "Ally")

    def test_longer_names_win(self):
        # "Capital One" must not be shadowed by a shorter entry, and "Bank of America"
        # must not resolve to whatever else happens to match inside it.
        self.assertEqual(guess_institution("Capital One 360 Checking $250"), "Capital One")
        self.assertEqual(guess_institution("Bank of America $200 offer"), "Bank of America")

    def test_no_guess_is_better_than_a_wrong_one(self):
        self.assertIsNone(guess_institution("Best savings rates this week"))

    def test_an_institution_hiding_inside_an_ordinary_word_is_not_a_match(self):
        """The bug this caught on its first live run.

        "Aven Rewards Checking + Rewards Visa: 5% Back On All Purchases" was filed under
        Chase, because "chase" is inside "purchases". Several institution names are
        substrings of common English words — chase, ally, discover, citizens — and each
        one sends a curator to the wrong bank.
        """
        self.assertIsNone(guess_institution("5% Back On All Purchases"))
        self.assertIsNone(guess_institution("Finally, a card that pays"))       # "ally"
        self.assertIsNone(guess_institution("Rediscover your savings rate"))    # "discover"

    def test_the_real_institution_still_matches_beside_those_words(self):
        self.assertEqual(guess_institution("Chase raises cash back on all purchases"), "Chase")
        self.assertEqual(guess_institution("Ally finally raises its rate"), "Ally")

    def test_short_forms_are_recognised(self):
        self.assertEqual(guess_institution("[Targeted] AmEx Offer: Use Apple Pay"), "American Express")
        self.assertEqual(guess_institution("BofA $200 checking bonus"), "Bank of America")

    def test_word_boundaries_allow_punctuation_and_ends_of_string(self):
        self.assertTrue(contains_phrase("chase", "chase"))
        self.assertTrue(contains_phrase("new chase offer", "chase"))
        self.assertFalse(contains_phrase("purchases", "chase"))
        self.assertFalse(contains_phrase("chaser", "chase"))

    def test_it_takes_the_largest_amount_in_the_headline(self):
        # "Earn up to $750 with $15,000 deposited" leads with the payout; "$300 after
        # $500 in deposits" does not. Largest is right more often than first, and it is
        # only ever used to sort the queue.
        self.assertEqual(guess_value_cents("$300 bonus after $500 in deposits"), 50000)
        self.assertEqual(guess_value_cents("Chase Total Checking $300 Bonus"), 30000)
        self.assertEqual(guess_value_cents("Earn $1,000 with a transfer"), 100000)
        self.assertIsNone(guess_value_cents("New checking promotion announced"))

    def test_relevance_matches_on_words_not_substrings(self):
        terms = ["bonus", "checking"]
        self.assertTrue(looks_relevant("Chase Total Checking $300 Bonus", terms))
        self.assertFalse(looks_relevant("Weekly mortgage rate roundup", terms))

    def test_an_empty_match_list_lets_everything_through(self):
        self.assertTrue(looks_relevant("Anything at all", []))


class TestDeduping(unittest.TestCase):

    def test_the_same_offer_phrased_twice_collapses(self):
        a = dedupe_key("Chase", "Chase Total Checking $300 Bonus")
        b = dedupe_key("Chase", "Chase Total Checking Bonus of $300")
        self.assertEqual(a, b)

    def test_different_offers_stay_apart(self):
        # A false merge hides a real offer. This errs toward keeping things separate.
        a = dedupe_key("Chase", "Chase Total Checking $300 Bonus")
        b = dedupe_key("Chase", "Chase Sapphire Preferred 60k points")
        self.assertNotEqual(a, b)

    def test_different_institutions_never_merge(self):
        a = dedupe_key("Chase", "Total Checking $300 Bonus")
        b = dedupe_key("Citi", "Total Checking $300 Bonus")
        self.assertNotEqual(a, b)


class TestPageChangeDetection(unittest.TestCase):

    def test_scripts_and_styles_are_stripped(self):
        text = visible_text("<html><head><style>.a{color:red}</style></head>"
                            "<body><script>var x=1</script><p>Open a checking account</p></body></html>")
        self.assertEqual(text, "Open a checking account")

    def test_volatile_values_are_stripped_so_a_page_does_not_change_every_run(self):
        # Without this an issuer page reports a change on every single fetch and the
        # whole signal is noise.
        one = visible_text("<p>Offer 3f2a91bc44de77a10ffb2c3d Open now 1755900000 12:04</p>")
        two = visible_text("<p>Offer aa11bb22cc33dd44ee55ff66 Open now 1755986400 09:31</p>")
        self.assertEqual(one, two)


class TestFeedParsing(unittest.TestCase):

    def test_it_reads_rss(self):
        entries = parse_rss("""<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>One</title><link>https://example.com/1</link>
            <pubDate>Mon, 18 Aug 2026 12:00:00 +0000</pubDate></item>
            <item><title>Two</title><link>https://example.com/2</link></item>
        </channel></rss>""")
        self.assertEqual([e["title"] for e in entries], ["One", "Two"])

    def test_it_reads_atom_where_the_link_is_an_attribute(self):
        entries = parse_rss("""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>One</title><link href="https://example.com/1"/>
            <published>2026-08-18T12:00:00Z</published></entry></feed>""")
        self.assertEqual(entries[0]["link"], "https://example.com/1")

    def test_an_entry_with_no_link_is_dropped(self):
        entries = parse_rss("""<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>No link here</title></item></channel></rss>""")
        self.assertEqual(entries, [])

    def test_dates_come_back_as_plain_days(self):
        self.assertEqual(rss_day("Mon, 18 Aug 2026 12:00:00 +0000"), "2026-08-18")
        self.assertEqual(rss_day("2026-08-18T12:00:00Z"), "2026-08-18")
        self.assertIsNone(rss_day("sometime last week"))
        self.assertIsNone(rss_day(None))


class TestNormalising(unittest.TestCase):

    def test_it_keeps_dollar_signs_and_drops_the_rest(self):
        self.assertEqual(normalise("Chase — Total  Checking: $300!"), "chase total checking $300")


class TestConfig(unittest.TestCase):

    def test_sources_json_is_valid_and_every_source_is_complete(self):
        config = json.loads((HERE / "sources.json").read_text())
        self.assertTrue(config.get("userAgent", "").startswith("CacheOfferHarvester"))
        self.assertNotIn("Mozilla", config["userAgent"], "the tool must not claim to be a browser")
        for source in config["sources"]:
            for field in ("id", "name", "kind", "url", "enabled"):
                self.assertIn(field, source, f"{source.get('id')} is missing {field}")
            self.assertIn(source["kind"], ("rss", "page"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
