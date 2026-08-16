# cache-manifests

The data Cache downloads. Two versioned static JSON documents, served straight from this repo by
jsDelivr. There is no server, no API and no account — publishing an offer is a commit, rolling one
back is a revert, and the record of what was live on any given day is this repo's log.

```
feed/v1.json     the deal feed  — curated offers shown in Discover
rules/v1.json    issuer rules   — 5/24 and friends, behind the eligibility engine
```

## The URLs the app fetches

```
https://cdn.jsdelivr.net/gh/eugeneopperman/cache-manifests@main/feed/v1.json
https://cdn.jsdelivr.net/gh/eugeneopperman/cache-manifests@main/rules/v1.json
```

Branch URLs, not tags, so a push is a publish. jsDelivr holds a branch URL for **up to 12 hours**,
which is why the publish script purges it — see below.

## Publish

Always from the app repo, never by editing files here directly:

```sh
cd ../Tracker
./scripts/publish-manifests.sh feed      # or: rules, or: both
```

It validates, bumps the revision, stamps the date, copies the file here, commits, pushes, and
purges the CDN. Editing `feed/v1.json` by hand skips every one of those steps.

## Two rules that matter more than they look

**1. `revision` must increase, every single time.**

Clients ignore any manifest whose revision hasn't advanced. Republishing without a bump uploads
cleanly, parses cleanly, and changes nothing on any device — indistinguishable from a CDN caching
problem, and there is no error anywhere to tell you. The publish script bumps it for you and
refuses to publish a revision that isn't higher than what's live.

**2. A broken manifest is silent.**

The app swallows every fetch and parse failure and keeps the last good copy. That's right for the
app — a curator's mistake must never break somebody's phone — but it means nothing downstream will
ever tell you the file is wrong. `feed-publish` validates by decoding with the app's own types, so
"the app can read this" is proven rather than hoped. Run it. It is the only check there is.

## What the app does with these

- Fetched on launch and on returning to the foreground, throttled to once every 4 hours, plus
  pull-to-refresh in Discover.
- Conditional GET with `If-None-Match`, so an unchanged manifest costs a 304 and a few hundred
  bytes rather than the whole document.
- Cached on device; a bundled copy ships inside the app as the floor. The app is fully functional
  with no network and an empty feed.
- Anonymous. The request carries a URL and an ETag the server itself issued. No identifiers, no
  account, no user data — the App Privacy label reads "Data Not Collected" and has to stay true.

## Editorial rules for the feed

Carried over from `docs/prd/PRD.md` §16.3, and enforced by `feed-publish` where they can be:

- `verifiedOn` is the date the **source** was published, not the date you read it. An offer taken
  from a three-week-old article should render as stale, not as freshly checked.
- No "as high as" or targeted offers. Amex's headline bonuses aren't uniformly available, and
  stating one as a flat fact tells a user they can get something they may not.
- Never predict an approval. "You're at 4 of 5" is a fact; whether a bank approves someone is not
  ours to say. The validator rejects the banned phrasings from `approval-language-v1.json`.
- A short good list beats a long stale one. An empty category is fine; a padded one isn't.
