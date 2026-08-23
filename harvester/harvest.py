#!/usr/bin/env python3
"""Find offers worth checking. Never publish one.

This fills a review queue. It does not touch feed/v1.json and there is a test that
asserts so, because the whole arrangement rests on one thing: `verifiedOn` in the
published feed means *a person opened the issuer's page and read the terms*. The app
shows that date to users and flips it amber after fourteen days. A script cannot
honestly write it. So this writes candidates.json and stops.

What a candidate is
-------------------
A **lead**: a source said an offer exists, here is the URL, go and look. It carries a
headline, a link, a date, and a guess at the institution and amount pulled from the
headline itself. It deliberately does **not** carry the article body or the RSS
description — those are the publisher's editorial work, and copying them into a
product is a different act from noticing them.

Being a good citizen, in code rather than in a comment
------------------------------------------------------
* robots.txt is fetched and honoured per host at runtime, via `urllib.robotparser`.
  If a site tightens its rules, this stops on its own without anyone editing it.
* `Crawl-delay` is read from robots.txt and slept. Doctor of Credit asks for 600
  seconds; one RSS request per run means that costs nothing.
* The User-Agent identifies the tool and links to the repo. It never claims to be a
  browser. If a source wants to refuse it, it must be able to.

Why Python, in a Swift project
-------------------------------
`Tools/FeedPublisher` is Swift because it runs on a developer's Mac inside the publish
flow and is covered by `verify.sh`. This runs on a Linux CI box in a *different*,
public repo, where a Swift toolchain and a CacheEngine dependency would be pure cost.
The standard library already has robots.txt parsing with crawl-delay support, which is
the one genuinely fiddly part.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SOURCES_PATH = HERE / "sources.json"
CANDIDATES_PATH = HERE / "candidates.json"
FEED_PATH = REPO / "feed" / "v1.json"

TIMEOUT = 30
# A floor under whatever robots.txt says, so a site that sets no Crawl-delay still
# doesn't get hit as fast as the loop can go.
MIN_DELAY = 2.0
# A ceiling, so one polite-but-slow site can't stall a CI run past its timeout. Above
# this we make the request anyway and log that we shortened the wait — visible, rather
# than silently ignoring a site's stated preference.
MAX_DELAY = 30.0


# --------------------------------------------------------------------------------------
# Pure helpers — the parts worth testing, kept free of the network


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. The basis of every comparison."""
    return " ".join(re.sub(r"[^a-z0-9$ ]+", " ", text.lower()).split())


#: Institutions worth recognising in a headline, each with the spellings that appear in
#: the wild. Deliberately a short list: a guess that is often absent is honest, and a
#: guess that is often wrong sends a curator to the wrong page.
INSTITUTIONS: list[tuple[str, list[str]]] = [
    ("Bank of America", ["bank of america", "bofa"]),
    ("Capital One", ["capital one", "capitalone"]),
    ("Wells Fargo", ["wells fargo"]),
    ("American Express", ["american express", "amex"]),
    ("U.S. Bank", ["u s bank", "us bank", "usbank"]),
    ("TD Bank", ["td bank"]),
    ("Navy Federal", ["navy federal", "nfcu"]),
    ("Fifth Third", ["fifth third"]),
    ("Chase", ["chase"]),
    ("Citi", ["citi", "citibank"]),
    ("Discover", ["discover"]),
    ("PNC", ["pnc"]),
    ("Citizens", ["citizens"]),
    ("SoFi", ["sofi"]),
    ("Ally", ["ally"]),
    ("Barclays", ["barclays"]),
    ("HSBC", ["hsbc"]),
    ("Truist", ["truist"]),
    ("Huntington", ["huntington"]),
    ("Schwab", ["schwab"]),
    ("Fidelity", ["fidelity"]),
    ("Robinhood", ["robinhood"]),
    ("Webull", ["webull"]),
]


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word containment, not substring.

    **This function exists because of one wrong answer.** With plain `in`, "Chase" matched
    inside "pur*chase*s" and a headline about Aven Rewards Checking was filed under Chase —
    which sends a curator to the wrong bank's website. Substring matching on institution
    names is wrong the moment a common English word contains one, and several do: chase,
    ally, discover, citizens.
    """
    pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def guess_institution(headline: str) -> str | None:
    hay = normalise(headline)
    # Longest alias first, so "Bank of America" wins over a bare "america" and
    # "Capital One" is never shadowed.
    aliases = [(alias, canonical) for canonical, names in INSTITUTIONS for alias in names]
    for alias, canonical in sorted(aliases, key=lambda pair: len(pair[0]), reverse=True):
        if contains_phrase(hay, alias):
            return canonical
    return None


def guess_value_cents(headline: str) -> int | None:
    """The largest dollar figure in the headline.

    Largest rather than first, because "$300 bonus after $500 in deposits" leads with the
    payout but "Earn up to $750 with $15,000" does not. A curator corrects this; it exists
    to sort the queue, never to be published.
    """
    amounts = []
    for raw in re.findall(r"\$\s?([0-9][0-9,]*)", headline):
        try:
            amounts.append(int(raw.replace(",", "")))
        except ValueError:
            continue
    return max(amounts) * 100 if amounts else None


def looks_relevant(headline: str, terms: list[str]) -> bool:
    if not terms:
        return True
    hay = normalise(headline)
    return any(contains_phrase(hay, normalise(t)) for t in terms)


def candidate_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def dedupe_key(institution: str | None, headline: str) -> str:
    """What makes two leads the same offer.

    Institution plus the distinctive words of the headline. Crude on purpose — this only
    has to stop the same post reappearing every Monday and stop a lead being raised for
    something already in the feed. A missed duplicate costs a curator ten seconds; a
    false merge hides a real offer, so this errs toward keeping things apart.
    """
    words = [w for w in normalise(headline).split() if len(w) > 3 and not w.startswith("$")]
    return f"{normalise(institution or '')}|{' '.join(sorted(set(words))[:6])}"


def visible_text(html: str) -> str:
    """Rough text extraction, for change-detecting a page we cannot parse properly.

    Scripts and styles out, tags out, whitespace collapsed. Then the volatile parts that
    change on every single fetch — session ids, nonces, timestamps, cache-busting query
    strings — are stripped, or an issuer page would report a change every run and the
    whole signal would be noise.
    """
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;|&#\d+;", " ", text)
    text = re.sub(r"\b[0-9a-f]{16,}\b", " ", text, flags=re.I)      # ids, nonces, hashes
    text = re.sub(r"\b\d{10,13}\b", " ", text)                       # epoch timestamps
    text = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", text)          # clock times
    return " ".join(text.split())


def parse_rss(xml_text: str) -> list[dict]:
    """Title, link and date from RSS 2.0 or Atom.

    **The description is deliberately not read.** It is the publisher's summary — their
    words about the offer — and this tool records that an offer exists, not what someone
    else wrote about it.
    """
    items: list[dict] = []
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"

    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = published = None
        for child in item:
            ctag = child.tag.split("}")[-1]
            if ctag == "title" and child.text:
                title = child.text.strip()
            elif ctag == "link":
                link = (child.get("href") or child.text or "").strip() or link
            elif ctag in ("pubDate", "published", "updated") and child.text and not published:
                published = child.text.strip()
        if title and link:
            items.append({"title": title, "link": link, "published": published})
    return items


def rss_day(published: str | None) -> str | None:
    """The entry's date as `YYYY-MM-DD`, or None if it can't be read."""
    if not published:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(published.replace("Z", "+0000"), fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(published).date().isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# The network


class Fetcher:
    """One GET at a time, per host, at whatever pace that host asked for."""

    def __init__(self, user_agent: str, log: list[str]):
        self.user_agent = user_agent
        self.log = log
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}

    def _robots_for(self, host_url: str):
        """Fetch and parse a host's robots.txt **with our own User-Agent**.

        Not `RobotFileParser.read()`, and the difference is not cosmetic. `read()` fetches
        using urllib's default agent, which Doctor of Credit's edge answers with a 403 —
        and a 403 on robots.txt makes the parser disallow the entire site. So the polite
        thing (asking first) silently blocked us from the one source that actually permits
        us, and the log line read as though DoC had refused. Fetching robots.txt as
        ourselves is both more honest and the only way to get the real answer.
        """
        host = urlparse(host_url).netloc
        if host in self._robots:
            return self._robots[host]

        parser = urllib.robotparser.RobotFileParser()
        url = f"{urlparse(host_url).scheme}://{host}/robots.txt"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8", errors="replace")
            parser.parse(text.splitlines())
        except urllib.error.HTTPError as error:
            if error.code == 404:
                # No robots.txt is the conventional "everything is allowed". Recorded
                # rather than assumed, so the log says which rule we're following.
                self.log.append(f"{host}: no robots.txt (404) — treating as allow-all")
                parser.parse([])
            else:
                # 401 and 403 are a refusal, and anything else we cannot read is treated
                # as one too. Failing closed is the only safe direction here.
                self.log.append(f"{host}: robots.txt returned {error.code} — treating as disallow")
                self._robots[host] = None
                return None
        except Exception as error:                        # noqa: BLE001
            self.log.append(f"{host}: robots.txt unreadable ({type(error).__name__}) — treating as disallow")
            self._robots[host] = None
            return None

        self._robots[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        if parser is None:
            return False
        return parser.can_fetch(self.user_agent, url)

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        parser = self._robots.get(host)
        delay = MIN_DELAY
        if parser is not None:
            stated = parser.crawl_delay(self.user_agent)
            if stated:
                delay = float(stated)
        if delay > MAX_DELAY:
            self.log.append(
                f"{host} asks for {delay:.0f}s between requests; waiting {MAX_DELAY:.0f}s "
                f"(one request this run, so the rate is still well under what it asked for)"
            )
            delay = MAX_DELAY
        last = self._last_hit.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)

    def get(self, url: str) -> str | None:
        if not self.allowed(url):
            self.log.append(f"robots.txt disallows {url} — skipped")
            return None
        self._wait(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8",
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
        except Exception as error:                        # noqa: BLE001
            self.log.append(f"{url} — {type(error).__name__}: {error}")
            return None
        finally:
            self._last_hit[urlparse(url).netloc] = time.monotonic()
        return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------------------
# Harvest


def published_keys(feed_path: Path) -> set[str]:
    """Dedupe keys for offers already in the feed, so we don't raise what we have."""
    if not feed_path.exists():
        return set()
    try:
        feed = json.loads(feed_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        dedupe_key(offer.get("institution"), f"{offer.get('institution','')} {offer.get('title','')}")
        for offer in feed.get("offers", [])
    }


def harvest(sources: dict, existing: dict, fetcher: Fetcher, today: str, log: list[str]) -> dict:
    known_ids = {c["id"] for c in existing.get("candidates", [])}
    dismissed = set(existing.get("dismissed", []))
    page_hashes = dict(existing.get("pageHashes", {}))
    already = published_keys(FEED_PATH)
    seen_keys = {dedupe_key(c.get("guessedInstitution"), c["headline"])
                 for c in existing.get("candidates", [])}

    fresh: list[dict] = []

    for source in sources.get("sources", []):
        if not source.get("enabled", True):
            log.append(f"{source['id']}: disabled, skipped")
            continue

        if source["kind"] == "rss":
            body = fetcher.get(source["url"])
            if body is None:
                continue
            try:
                entries = parse_rss(body)
            except ET.ParseError as error:
                log.append(f"{source['id']}: could not parse feed ({error})")
                continue

            kept = 0
            for entry in entries:
                if not looks_relevant(entry["title"], source.get("match", [])):
                    continue
                cid = candidate_id(entry["link"])
                if cid in known_ids or cid in dismissed:
                    continue
                institution = guess_institution(entry["title"])
                key = dedupe_key(institution, entry["title"])
                if key in already or key in seen_keys:
                    continue
                seen_keys.add(key)
                fresh.append({
                    "id": cid,
                    "source": source["id"],
                    "sourceURL": entry["link"],
                    "headline": entry["title"],
                    "publishedOn": rss_day(entry["published"]),
                    "seenOn": today,
                    "guessedInstitution": institution,
                    "guessedValueCents": guess_value_cents(entry["title"]),
                    "status": "new",
                    "note": "Lead only — nobody has read these terms. Open the issuer's own page before publishing.",
                })
                kept += 1
            log.append(f"{source['id']}: {len(entries)} entries, {kept} new candidates")

        elif source["kind"] == "page":
            body = fetcher.get(source["url"])
            if body is None:
                continue
            digest = hashlib.sha256(visible_text(body).encode("utf-8")).hexdigest()
            previous = page_hashes.get(source["id"])
            page_hashes[source["id"]] = digest
            if previous is None:
                log.append(f"{source['id']}: first look, recorded a baseline")
                continue
            if previous == digest:
                log.append(f"{source['id']}: unchanged")
                continue
            cid = candidate_id(f"{source['url']}#{digest}")
            if cid in dismissed:
                continue
            fresh.append({
                "id": cid,
                "source": source["id"],
                "sourceURL": source["url"],
                "headline": f"{source['name']} — page changed",
                "publishedOn": None,
                "seenOn": today,
                "guessedInstitution": guess_institution(source["name"]),
                "guessedValueCents": None,
                "status": "new",
                "note": "The page's visible text changed. That is not an offer — it may be a layout tweak. Open it and see.",
            })
            log.append(f"{source['id']}: page changed")

        else:
            log.append(f"{source['id']}: unknown kind '{source['kind']}', skipped")

    return {
        "version": 1,
        "generatedOn": today,
        "candidates": existing.get("candidates", []) + fresh,
        "dismissed": sorted(dismissed),
        "pageHashes": page_hashes,
    }


def main() -> int:
    sources = json.loads(SOURCES_PATH.read_text())
    existing = json.loads(CANDIDATES_PATH.read_text()) if CANDIDATES_PATH.exists() else {}
    today = datetime.now(timezone.utc).date().isoformat()

    log: list[str] = []
    fetcher = Fetcher(sources.get("userAgent", "CacheOfferHarvester/1"), log)
    result = harvest(sources, existing, fetcher, today, log)

    before = len(existing.get("candidates", []))
    after = len(result["candidates"])

    CANDIDATES_PATH.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")

    print("Offer harvest —", today)
    for line in log:
        print("  ", line)
    print(f"\n{after - before} new candidate(s); {after} awaiting review.")
    if after > before:
        print("\nNothing here has been verified. Each one needs the issuer's own page opened")
        print("and its terms read before it can go anywhere near feed/v1.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
