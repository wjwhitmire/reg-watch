# Testing

This file lists every function of Reg Watch, how each was tested, and the result. The point of this pass was to test what the Pages workflow actually publishes:

1. The tracked files were copied and the release placeholders filled (`https://wjwhitmire.github.io/reg-watch/` to `https://wjwhitmire.github.io/reg-watch/`, `https://github.com/wjwhitmire/reg-watch` to the repository URL).
2. The collect workflow's command (`reg-watch collect --config config.json --data data`, with and without `--backfill-days`) ran against the recorded Federal Register responses in `tests/fixtures/`, with the HTTP layer swapped for the fixture-backed fake API.
3. The "Assemble site" step of `pages.yml` ran exactly as written.
4. The result was served under `/reg-watch/` and opened in headless Chromium at 1280 px (light and dark) and 400 px with touch (light and dark). The shipped seed site and `web/app.html` from `file://` were tested too.

## Function inventory

| # | Area | Function | How it was tested | Result |
|---|---|---|---|---|
| 1 | Workflow | collect: full backfill | Exit 0, 63 documents, `complete_since` set, data changed so the pages job runs | Pass |
| 2 | Workflow | collect: quiet day and the next day | Byte-identical files, no commit, no deploy | Pass |
| 3 | Workflow | First run with no backfill input | Fills the full 365-day window by itself (new) | Pass |
| 4 | Workflow | pages.yml assemble step | `_site` has `index.html`, 3 JSON files, 9 Atom feeds, `.nojekyll` | Pass |
| 5 | Feeds | 9 Atom feeds | Parse with `xml.etree`; self links are the live URL plus `data/feed*.xml`; entry ids unique | Pass |
| 6 | Collector | API User-Agent | Carries the filled repository URL | Pass |
| 7 | Collector | Repo and site URLs when the placeholders are unfilled inside Actions | Worked out from `GITHUB_REPOSITORY`, including `<user>.github.io` repos; bad values ignored (new) | Pass |
| 8 | Page | Live data | "Live data", "Updated 2026-10-23 ... 63 documents" | Pass |
| 9 | Page | Content-Security-Policy with script hashes | No violations after the placeholder fill | Pass |
| 10 | Page | Light and dark, 400 px | No sideways scroll; feed and document links at least 44 px on touch after the fix below | Pass after fix |
| 11 | Filters | Agency (menu counts), document type, date range | Counts equal an independent count from `documents.json` | Pass |
| 12 | Filters | Keywords with a quoted phrase and exclusion | `pipeline -"special permit"` equals an independent substring count | Pass |
| 13 | Filters | Hostile query text | No match, no error | Pass |
| 14 | Views | Comments closing view, As of date, window (14, 60, negative) | Counts equal an independent count | Pass |
| 15 | Sort | Newest first, comment deadline first | First item has the soonest deadline | Pass |
| 16 | Results | Document cards, abstract toggle, links | Only federalregister.gov and govinfo.gov links, new tab, `noopener` | Pass |
| 17 | Results | Show more | Hidden with 63 documents (page size 100) | Pass |
| 18 | Export | Copy results as CSV; fallback without clipboard | Header and 63 rows | Pass |
| 19 | Feeds panel | 9 links | All return 200 under the site's own `data/` | Pass |
| 20 | Fallback | `documents.json` 404 or not JSON | Error names the file; embedded snapshot shown | Pass |
| 21 | Fallback | Hostile data file | Malformed record skipped and counted; HTML title shown as text; `javascript:` link dropped | Pass |
| 22 | Seed | Site as shipped, before the first collect | Labelled sample data, asks to run collect | Pass |
| 23 | file:// | `web/app.html` | "Snapshot as of 2026-10-21", 63 documents | Pass |
| 24 | CLI | `query` with `--watch --type --closing --ref --window`, `-q --from --csv` | Pass | Pass |
| 25 | CLI | Errors: bad date, `--backfill-days 0`, `--since` in the future, config not JSON; `--version` | Clear messages | Pass |
| 26 | Library | Collector, schema, filters, feeds | `pytest` with the fake API, and `web/parity.test.mjs` | Pass |

## Bugs fixed in this pass

| Bug | Severity | Fix |
|---|---|---|
| Feed links and document links were 23 px tall (some 27 px wide) on touch screens | Low | 44 x 44 px minimum on coarse pointers |
| README setup step 2 quoted the placeholders, so the release fill turned it into a sentence that told you to replace your own URL | Low | Step rewritten; the URLs are now worked out automatically |
| Fixture provenance and a test variable used words the repository keeps out of tracked files | Low | Reworded; a test now checks tracked files for them |

## Decisions the app now makes for you

| Decision point | Before | After |
|---|---|---|
| `backfill_days` on the first run | You had to enter 365 or get only 30 days | First run fills the full year by itself (`first_run_lookback_days` 365); the input is only for re-fetching |
| Re-fill after adding an agency | Run the workflow with 365 by hand | The next run re-fetches the full window by itself |
| `repo_url` and `site_url` | Edit `config.json` and the README by hand | Taken from the repository the workflow runs in when still placeholders |
| As of date, closing window | Already defaulted to today and 14 days | No change |

## How to rerun

```
pip install -e .[dev] playwright
python -m playwright install chromium
pytest -q                                   # includes tests/test_published_site.py
python tests/make_vectors.py && node web/parity.test.mjs
python web/build_pages.py
```

Test counts: 73 Python tests before this pass, 80 after, plus the parity script.

## Known limits

- The live Federal Register API was not reachable from the test machine; the collector ran against the recorded responses in `tests/fixtures/`.
- See the README for the limits of comment deadlines and agency matching.
