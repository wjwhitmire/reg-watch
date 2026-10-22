# Reg Watch

A free, self-hosting tracker of US federal rulemaking for the agencies you choose. Every day a GitHub Action pulls new rules, proposed rules, notices and presidential documents from the official Federal Register API, commits them to this repository, and publishes a searchable page and Atom feeds. No servers, no keys, no subscription.

**Live page:** https://wjwhitmire.github.io/reg-watch/
**Repository:** https://github.com/wjwhitmire/reg-watch

Default agencies: CFTC, SEC, FERC, EPA, PHMSA, BSEE, BLM and the Department of the Interior. Change them in `config.json`.

## What you get

- `data/documents.json`: every watched document from the last 365 days, one JSON object per line, newest first.
- `data/latest.json`: only the documents that were new in the most recent run.
- `data/feed.xml`: Atom feed of everything; `data/feed-<key>.xml`: one feed per agency (for example `feed-epa.xml`).
- The page (`docs/index.html` on GitHub Pages): filter by agency, document type, keywords and publication date range; a "comments closing in the next 14 days" view; copy the current results as CSV. Every item links to its federalregister.gov page and the official govinfo.gov PDF.
- `web/app.html`: the same page as a self-contained standalone page with a frozen snapshot of real data embedded. It says "Snapshot as of <date>. The GitHub version updates daily."

## Turn it on (one time, about five minutes)

1. Create an empty public repository on GitHub (for example `reg-watch`) and push this code to its `main` branch.
2. Replace the placeholders. In `config.json` and this README, set `https://github.com/wjwhitmire/reg-watch` to `https://github.com/<you>/reg-watch` and `https://wjwhitmire.github.io/reg-watch/` to `https://<you>.github.io/reg-watch/`. The repo URL goes into the API User-Agent; the live URL goes into the Atom self links.
3. **Settings > Actions > General > Workflow permissions:** select "Read and write permissions" and save. Only the `collect` job asks for `contents: write`; every other workflow stays read-only.
4. **Settings > Pages > Build and deployment > Source:** select "GitHub Actions".
5. **Actions tab:** enable workflows if GitHub asks. Open "collect", click "Run workflow", and enter `365` in `backfill_days` for a full first fill (or leave it blank for the last 30 days). The run commits `data/` and then deploys Pages.
6. Open the live URL. From then on the collector runs daily at 11:17 UTC and redeploys only when something changed.

The repository ships with a small seed in `data/` built from the recorded API responses in `tests/fixtures/`. It is marked as a sample (`complete_since: null`), so the first live run fetches a full window instead of trusting it.

## Quick start (local)

```bash
pip install -e .[dev]
pytest -q

reg-watch collect                         # incremental: since the last run, minus a 3 day overlap
reg-watch collect --backfill-days 365     # full year
reg-watch collect --since 2026-10-01 --dry-run
reg-watch query --watch EPA --type "Proposed Rule" --closing
reg-watch query -q 'pipeline -"special permits"' --from 2026-09-01 --csv > hits.csv
```

Python:

```python
from reg_watch.collect import load_config, run
res = run(load_config("config.json"), "data")
print(len(res.new), "new documents")
```

## Configuration (`config.json`)

| key | default | meaning |
|---|---|---|
| `agencies` | the eight above | list of `{key, slug, label}`. `slug` is the agency's slug on federalregister.gov (`federalregister.gov/agencies/<slug>`). `key` names the feed file and filter. |
| `per_page` | 100 | results per API request (API maximum 1000) |
| `retention_days` | 365 | rolling window kept in `documents.json` |
| `first_run_lookback_days` | 30 | window for a run with no complete store |
| `overlap_days` | 3 | days re-fetched before the newest stored date |
| `chunk_days` | 31 | each query covers at most this many days, so no query needs many pages |
| `max_pages_per_query` | 40 | safety stop; a warning is logged if reached |
| `max_retries`, `backoff_base_s`, `backoff_cap_s` | 5, 2, 120 | retry policy for HTTP 429, 5xx and network errors |
| `request_delay_s` | 1 | pause between requests |
| `feed_entries` | 200 | newest entries per Atom feed |

The config is JSON so the collector stays standard library only.

## How it works

**Collection.** For each agency the collector requests

```
GET https://www.federalregister.gov/api/v1/documents.json
    ?per_page=100&order=oldest
    &conditions[agencies][]=<slug>
    &conditions[publication_date][gte]=<since>&conditions[publication_date][lte]=<today>
    &fields[]=document_number&fields[]=title&... (13 fields)
```

and follows `next_page_url` until it is empty. The fields list is re-added to each next page because the API's `next_page_url` does not always carry it. Only URLs under `https://www.federalregister.gov/api/v1/` are ever requested.

**Incremental window.** `since` = newest stored `publication_date` minus `overlap_days`, if the store is known complete (`complete_since` set by a previous run). Otherwise `today - first_run_lookback_days`. Never earlier than `today - retention_days`. The overlap catches late indexing and corrections; dedupe makes it harmless.

**Dedupe and merge.** By `document_number`. A newer copy wins field by field, except that an empty value never erases a known one, and the `watch` lists are unioned (a joint CFTC and SEC rule appears under both).

**Atomic runs.** If any agency fails after retries, or any query comes back incomplete, the run raises and writes nothing. The API serves at most 50 pages per query and then stops listing a next page without an error, so the collector compares what it received with the `count` the API reported and treats a shortfall as a failure. Files are written to a temporary name and renamed. A day with no new documents produces byte-identical files, so there is no commit.

**Politeness.** One scheduled run per day, 100-row pages, one-second spacing, a User-Agent of the form `reg-watch/0.1.0 (+<repo url>; daily Federal Register digest, one run per day)`, exponential backoff (2, 4, 8, ... seconds, capped at 120) that honors `Retry-After`, and no retry on other 4xx errors.

## Data schema

`documents.json`:

```json
{"schema_version":1, "source":"...", "updated":"2026-10-21", "retention_days":365,
 "complete_since":"2025-10-21", "agencies":[{"key":"EPA","slug":"environmental-protection-agency","label":"...","feed":"feed-epa.xml"}],
 "count":1234, "documents":[ ... ]}
```

Each document has exactly these keys, in this order. Keys are never renamed or removed; new ones would only be added with a `schema_version` bump.

| key | type | notes |
|---|---|---|
| `document_number` | string | Federal Register document number, the dedupe key |
| `title` | string | plain text |
| `type` | string | `Rule`, `Proposed Rule`, `Notice`, `Presidential Document`, or `Other` |
| `agencies` | list of `{name, slug}` | every agency on the document, as the API lists them |
| `watch` | list of strings | configured agency keys this document matched |
| `publication_date` | `YYYY-MM-DD` | |
| `comments_close_on` | `YYYY-MM-DD` or null | API field; see limits |
| `effective_on` | `YYYY-MM-DD` or null | |
| `abstract` | string or null | GPO markup removed, at most 2,000 characters |
| `html_url`, `pdf_url` | string or null | only https links on federalregister.gov or govinfo.gov survive |
| `docket_ids`, `regulation_id_numbers` | list of strings | at most 50 each |
| `significant` | true, false or null | API field (EO 12866 significance where the API has it) |

`latest.json` has `schema_version`, `updated`, `window_start`, `count` and `documents` (same document shape).

## Search and filter rules

These are the same in Python (`reg_watch.filters`) and in the page (`<script id="core">`), checked by `web/parity.test.mjs` against vectors generated by Python.

- **Keywords:** whitespace-separated terms; `"quoted phrase"` stays together; a leading minus excludes (`-notice`, `-"special permits"`). Every include term must appear and no exclude term may appear, as a substring of the title, abstract, document number, docket ids, RINs and agency names. Case folding is ASCII only (A to Z), so both languages agree exactly.
- **Dates:** publication date, inclusive bounds. Invalid dates are ignored.
- **Closing soon:** `as_of <= comments_close_on <= as_of + window` (default window 14 days, clamped to 0 to 365). The page's "As of" date defaults to today on your device, or to the snapshot date in the standalone version.
- **Sort:** newest first (publication date, then document number, both descending), or comment deadline first with undated documents last.
- **CSV export:** cells starting with `= + - @`, tab or carriage return get a leading `'` to stop spreadsheet formula execution.

## Limits

- **Comment deadlines are incomplete.** `comments_close_on` is set by the Federal Register's indexing. Some notices state a deadline only in the text, some reopen or extend a period in a later document, and some meeting notices have none. Treat the closing view as a prompt to read the document, not as a docket calendar.
- **Agency matching follows the API.** A query for a department returns its sub-agencies: Interior includes BSEE, BLM, BOEM, BIA, Fish and Wildlife and more. The `watch` list shows which configured keys matched. Remove `Interior` from the config if you only want specific bureaus. Adding an agency restarts coverage at `first_run_lookback_days`; run the collect workflow with `backfill_days` set to 365 to fill the year for it.
- **Publication lag.** The API indexes each day's issue on the morning of publication. The 11:17 UTC run usually sees that day's documents; the 3 day overlap picks up anything indexed later.
- **Volume.** SEC and FERC publish many notices per day (SRO filings, combined notices). A full 365 day backfill for the default agencies is a few hundred requests and several MB of JSON. Stay well under GitHub's 100 MB file limit by narrowing agencies or retention if you add many.
- **Search** is substring matching without stemming, synonyms or accent folding.
- **Not legal advice.** This is an index of public documents. Read the official document before acting.

## Validation

- `tests/fixtures/fr_*.json`: real responses from the Federal Register API for all eight default agencies, 63 documents retrieved 2026-10-21: list-endpoint responses (titles, abstracts, agencies, URLs, `count`, `total_pages`, `next_page_url` exactly as returned), plus the five fields the list endpoint omits by default (`comments_close_on`, `effective_on`, `docket_ids`, `regulation_id_numbers`, `significant`) from the document detail endpoint for the same document numbers. The provenance is recorded in each file. No document is invented.
- The collector is tested fully offline: a fake API built from the fixtures implements agency, date, paging, ordering and field filtering, and the tests inject it as the HTTP opener. Covered: pagination (including a `next_page_url` that drops the fields list, and one pointing off-site), dedupe across agencies and runs, field-wise merge, incremental since-last-run, retention, quiet days producing no diff, 429 and 5xx backoff with `Retry-After`, give-up without writing, malformed records skipped with a log line, Atom feeds parsed back with `xml.etree` (required elements, unique ids, self links), hostile text in feeds, and a pinned snapshot of the normalized schema.
- `web/parity.test.mjs` runs the page's filter, search, CSV, date and loader functions against the Python vectors with exact equality, cross-checks date arithmetic against JavaScript `Date` over 30 years, and renders a hostile document through the UI code with a fake DOM that throws on any `innerHTML` write.

```bash
python tests/make_vectors.py      # regenerate vectors and the pinned schema snapshot
node web/parity.test.mjs
python web/embed_snapshot.py      # refresh the standalone snapshot from data/documents.json
python web/build_pages.py         # regenerate docs/index.html
```

## GitHub Pages layout

The Pages workflow publishes `docs/index.html` as the site root and copies `data/*.json` and `data/*.xml` beside it, so the page reads `data/documents.json` from its own origin. The collect workflow deploys Pages after each commit that changed data (a push made with the Actions token does not trigger other workflows on its own). If the data file cannot be read, the page falls back to its embedded snapshot and says so.

## License

MIT. Copyright 2026 Will Whitmire. Federal Register content is a work of the US government and is in the public domain.
