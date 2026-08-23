# The offer harvester

Finds offers worth checking. **Never publishes one.**

```sh
python3 harvester/harvest.py        # fetch sources, update candidates.json
python3 harvester/test_harvest.py   # 25 tests, no network
```

Runs weekly in CI (`.github/workflows/harvest.yml`, Mondays 08:00 UTC) and opens a pull
request when there is something new. Also runnable by hand from the Actions tab.

## The line it does not cross

`verifiedOn` in `feed/v1.json` means **a person opened the issuer's page and read the
terms**. The app shows that date on every offer card and turns it amber after fourteen
days, so users read it as a promise that someone checked.

A script cannot honestly write it. So the harvester stops at `candidates.json`, and a
test asserts that the only file it writes is that one. If that test ever needs changing,
the thing being changed is the feed's credibility, not a build detail.

```
cron → harvest → dedupe → candidates.json → [ a person ] → feed-publish → CDN
                                             ↑
                                    opens the issuer's page,
                                    reads the actual terms,
                                    stamps verifiedOn
```

## What a candidate is

A **lead**: a source said an offer exists, here is the link, go and look.

```json
{
  "id": "b3c1d2…",
  "source": "doctorofcredit",
  "sourceURL": "https://www.doctorofcredit.com/wells-fargo-500-checking-bonus/",
  "headline": "Wells Fargo $500 Checking Bonus",
  "publishedOn": "2026-08-21",
  "seenOn": "2026-08-23",
  "guessedInstitution": "Wells Fargo",
  "guessedValueCents": 50000,
  "status": "new",
  "note": "Lead only — nobody has read these terms…"
}
```

The guesses come from the headline and exist to sort the queue. They are never published.
`guessedInstitution` is `null` when the name isn't recognised, which is deliberate — no
guess is better than a wrong one, and a wrong one sends you to the wrong bank's website.

**It does not store the article body or the RSS description.** Recording that an offer
exists is a different act from copying what a publisher wrote about it. `parse_rss` reads
title, link and date and nothing else, and there is a test for that.

## Reviewing

1. Open the `sourceURL` to see what the lead is about.
2. Go to the **issuer's own page** and read the terms there.
3. Publish through the normal flow (`scripts/publish-manifests.sh` in the app repo),
   which stamps `verifiedOn` with today.
4. Reject the rest: move the `id` into the `dismissed` array so it is never raised again.

## Sources

`sources.json`. Data, not code — switch one off with `"enabled": false` and commit.

| id | kind | Notes |
|---|---|---|
| `doctorofcredit` | rss | robots.txt permits; `Crawl-delay: 600`. The best signal by a distance. |
| `nerdwallet` | rss | Their own published feed, not a scrape of the comparison tables. robots.txt does not disallow it, but **their Terms of Use govern reuse** — leads only, and switch it off if they object. |
| `chase-checking` | page | Change detection only. |
| `capitalone-checking` | page | Change detection only. |

`page` sources hash the visible text and report *that it changed*, never that an offer
exists. Issuer pages are JS-rendered and session-targeted, so expect noise; volatile
values (session ids, timestamps, clock times) are stripped before hashing or every run
would report a change.

Deliberately absent: **bankrewards.io**. Their robots.txt carries
`Content-Signal: ai-train=no, use=reference`, an express reservation of rights under EU
DSM Article 4, and `Disallow: /` for every named AI crawler. That is a licensing
conversation, and PRD §16.3 already plans for it.

## Being a good citizen, in code

* **robots.txt is fetched and honoured per host at runtime.** If a source tightens its
  rules, this stops on its own with no edit.
* **It is fetched with our own User-Agent**, which is not a nicety. `RobotFileParser.read()`
  uses urllib's default agent; Doctor of Credit's edge answers that with a 403, and a 403
  on robots.txt makes the parser disallow the whole site. The first live run reported DoC
  as refusing us when it had done nothing of the kind.
* **`Crawl-delay` is read and slept**, capped at 30s per run so one polite-but-slow site
  cannot stall CI. One request per source per week is far under any stated rate; the cap
  is logged when it applies rather than applied silently.
* **The User-Agent identifies the tool and links here.** It never claims to be a browser.
  A source that wants to refuse it must be able to.
* **Unreadable robots.txt is treated as a refusal**, not as permission. A 404 is
  allow-all, per convention, and says so in the log.
