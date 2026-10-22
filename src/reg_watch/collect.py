"""Collect new Federal Register documents for the configured agencies.

Standard library only. The HTTP layer is a single injectable callable so the
whole pipeline runs offline in tests:

    opener(url: str, headers: dict, timeout: float) -> (status: int, headers: dict, body: bytes)

A non-2xx status is returned, not raised. Network failures raise OSError.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from . import __version__
from .feeds import write_feeds
from .schema import MalformedRecord, dedupe, normalize_record, validate_document

log = logging.getLogger("reg_watch")

API_BASE = "https://www.federalregister.gov/api/v1/documents.json"
API_PREFIX = "https://www.federalregister.gov/api/v1/"
FIELDS = (
    "document_number", "title", "type", "agencies", "publication_date",
    "comments_close_on", "effective_on", "abstract", "html_url", "pdf_url",
    "docket_ids", "regulation_id_numbers", "significant",
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
SCHEMA_VERSION = 1

Opener = Callable[[str, dict, float], tuple]


class CollectError(RuntimeError):
    """The run cannot complete. Nothing is written when this is raised."""


@dataclass
class AgencyConfig:
    key: str
    slug: str
    label: str


@dataclass
class Config:
    agencies: list[AgencyConfig]
    repo_url: str = "https://github.com/wjwhitmire/reg-watch"
    site_url: str = "https://wjwhitmire.github.io/reg-watch/"
    per_page: int = 100
    retention_days: int = 365
    first_run_lookback_days: int = 30
    overlap_days: int = 3
    chunk_days: int = 31
    max_pages_per_query: int = 40
    max_retries: int = 5
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 120.0
    request_delay_s: float = 1.0
    timeout_s: float = 30.0
    feed_entries: int = 200

    @property
    def user_agent(self) -> str:
        return f"reg-watch/{__version__} (+{self.repo_url}; daily Federal Register digest, one run per day)"


DEFAULT_AGENCIES = [
    AgencyConfig("CFTC", "commodity-futures-trading-commission", "Commodity Futures Trading Commission"),
    AgencyConfig("SEC", "securities-and-exchange-commission", "Securities and Exchange Commission"),
    AgencyConfig("FERC", "federal-energy-regulatory-commission", "Federal Energy Regulatory Commission"),
    AgencyConfig("EPA", "environmental-protection-agency", "Environmental Protection Agency"),
    AgencyConfig("PHMSA", "pipeline-and-hazardous-materials-safety-administration", "Pipeline and Hazardous Materials Safety Administration"),
    AgencyConfig("BSEE", "safety-and-environmental-enforcement-bureau", "Bureau of Safety and Environmental Enforcement"),
    AgencyConfig("BLM", "land-management-bureau", "Bureau of Land Management"),
    AgencyConfig("Interior", "interior-department", "Department of the Interior"),
]

_INT_LIMITS = {
    "per_page": (1, 1000), "retention_days": (1, 3650), "first_run_lookback_days": (1, 3650),
    "overlap_days": (0, 30), "chunk_days": (1, 366), "max_pages_per_query": (1, 200),
    "max_retries": (0, 10), "feed_entries": (1, 1000),
}
_FLOAT_LIMITS = {"backoff_base_s": (0, 600), "backoff_cap_s": (0, 3600), "request_delay_s": (0, 60), "timeout_s": (1, 300)}


def load_config(path: str | None) -> Config:
    """Read config.json. Missing file or missing keys fall back to defaults."""
    raw: dict = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            try:
                raw = json.load(fh)
            except json.JSONDecodeError as e:
                raise CollectError(f"{path}: not valid JSON ({e.msg} at line {e.lineno}). Fix the file or delete it to use defaults.") from None
        if not isinstance(raw, dict):
            raise CollectError(f"{path}: top level must be a JSON object")
    agencies = DEFAULT_AGENCIES
    if "agencies" in raw:
        agencies = []
        if not isinstance(raw["agencies"], list) or not raw["agencies"]:
            raise CollectError("config: 'agencies' must be a non-empty list")
        seen = set()
        for i, a in enumerate(raw["agencies"]):
            if not isinstance(a, dict) or not all(isinstance(a.get(k), str) and a.get(k) for k in ("key", "slug")):
                raise CollectError(f"config: agencies[{i}] needs string 'key' and 'slug' (slug as in federalregister.gov/agencies/<slug>)")
            slug = a["slug"]
            if not all((c.isascii() and c.isalnum()) or c == "-" for c in slug) or slug != slug.lower():
                raise CollectError(f"config: agencies[{i}].slug {slug!r} must be lowercase letters, digits and hyphens")
            key = a["key"]
            if not all((c.isascii() and c.isalnum()) or c in "-_" for c in key) or len(key) > 40:
                raise CollectError(f"config: agencies[{i}].key {key!r} must be 1 to 40 ASCII letters, digits, '-' or '_'")
            if key.lower() in seen:
                raise CollectError(f"config: duplicate agency key {key!r}")
            seen.add(key.lower())
            label = str(a.get("label") or key)
            if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in label):
                raise CollectError(f"config: agencies[{i}].label contains control characters; use plain text")
            agencies.append(AgencyConfig(key, slug, label))
    cfg = Config(agencies=agencies)
    for k in ("repo_url", "site_url"):
        if k in raw:
            if not isinstance(raw[k], str):
                raise CollectError(f"config: {k} must be a string")
            if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in raw[k]):
                raise CollectError(f"config: {k} contains control characters; use a plain https URL")
            setattr(cfg, k, raw[k])
    for k, (lo, hi) in _INT_LIMITS.items():
        if k in raw:
            v = raw[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise CollectError(f"config: {k} must be an integer from {lo} to {hi}")
            setattr(cfg, k, v)
    for k, (lo, hi) in _FLOAT_LIMITS.items():
        if k in raw:
            v = raw[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise CollectError(f"config: {k} must be a number from {lo} to {hi}")
            setattr(cfg, k, float(v))
    return cfg


# ---------------------------------------------------------------- HTTP

class _ApiOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only if it stays under the API prefix."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(API_PREFIX):
            raise CollectError(f"API redirected outside {API_PREFIX}: {newurl[:120]}. Not following.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_URL_OPENER = urllib.request.build_opener(_ApiOnlyRedirects)


def urllib_opener(url: str, headers: dict, timeout: float) -> tuple:
    """Default opener. Only https URLs on the Federal Register API are allowed, redirects included."""
    if not url.startswith(API_PREFIX):
        raise CollectError(f"refusing to fetch non-API URL: {url[:120]}")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _URL_OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 (https only, checked above)
            return resp.status, dict(resp.headers.items()), resp.read(20 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(64 * 1024)
        except Exception:  # pragma: no cover
            pass
        return e.code, dict(e.headers.items()) if e.headers else {}, body


def _retry_after(headers: dict) -> float | None:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return max(0.0, float(str(v).strip()))
            except ValueError:
                return None
    return None


def get_json(url: str, cfg: Config, opener: Opener, sleep: Callable[[float], None]) -> Any:
    """GET with retries on 429/5xx and network errors. Exponential backoff, honoring Retry-After."""
    headers = {"User-Agent": cfg.user_agent, "Accept": "application/json"}
    attempt = 0
    while True:
        try:
            status, resp_headers, body = opener(url, headers, cfg.timeout_s)
        except OSError as e:
            status, resp_headers, body = None, {}, b""
            err = f"network error: {e}"
        else:
            err = f"HTTP {status}"
        if status is not None and 200 <= status < 300:
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise CollectError(f"API returned invalid JSON for {url}: {e}") from None
        retryable = status is None or status in RETRY_STATUSES
        if not retryable:
            raise CollectError(f"{err} for {url}. Not retrying (only 429, 5xx and network errors are retried).")
        if attempt >= cfg.max_retries:
            raise CollectError(f"{err} for {url} after {attempt + 1} attempts. Giving up; nothing was written.")
        wait = min(cfg.backoff_cap_s, cfg.backoff_base_s * (2 ** attempt))
        ra = _retry_after(resp_headers)
        if ra is not None:
            wait = min(cfg.backoff_cap_s, max(wait, ra))
        log.warning("%s, retry %d/%d in %.1fs", err, attempt + 1, cfg.max_retries, wait)
        sleep(wait)
        attempt += 1


def build_url(slug: str, since: str, until: str, per_page: int) -> str:
    params: list[tuple[str, str]] = [("per_page", str(per_page)), ("order", "oldest")]
    params.append(("conditions[agencies][]", slug))
    params.append(("conditions[publication_date][gte]", since))
    params.append(("conditions[publication_date][lte]", until))
    params += [("fields[]", f) for f in FIELDS]
    return API_BASE + "?" + urllib.parse.urlencode(params)


def _next_url(next_page_url: Any) -> str | None:
    """Accept only API URLs; ensure the field list survives into the next page."""
    if not isinstance(next_page_url, str) or not next_page_url:
        return None
    if not next_page_url.startswith(API_PREFIX):
        log.warning("ignoring next_page_url outside the API: %s", next_page_url[:120])
        return None
    parts = urllib.parse.urlsplit(next_page_url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "fields[]" for k, _ in q):
        q += [("fields[]", f) for f in FIELDS]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


@dataclass
class RunStats:
    requests: int = 0
    fetched: int = 0
    skipped: int = 0
    truncated_queries: list = field(default_factory=list)


def fetch_agency(agency: AgencyConfig, since: str, until: str, cfg: Config, opener: Opener,
                 sleep: Callable[[float], None], watch_map: dict, stats: RunStats) -> list[dict]:
    """All documents for one agency in [since, until], split into date chunks and paged."""
    out: list[dict] = []
    for c_since, c_until in date_chunks(since, until, cfg.chunk_days):
        url: str | None = build_url(agency.slug, c_since, c_until, cfg.per_page)
        pages = 0
        reported: int | None = None
        received = 0
        while url:
            if stats.requests:
                sleep(cfg.request_delay_s)
            data = get_json(url, cfg, opener, sleep)
            stats.requests += 1
            pages += 1
            if not isinstance(data, dict):
                raise CollectError(f"API response for {agency.key} is not a JSON object")
            results = data.get("results") or []
            if not isinstance(results, list):
                raise CollectError(f"API response for {agency.key}: 'results' is not a list")
            if pages == 1 and isinstance(data.get("count"), int) and not isinstance(data.get("count"), bool):
                reported = data["count"]
            received += len(results)
            for raw in results:
                try:
                    out.append(normalize_record(raw, watch_map, agency.key))
                    stats.fetched += 1
                except MalformedRecord as e:
                    stats.skipped += 1
                    log.warning("skipping malformed record (%s): %s", agency.key, e)
            url = _next_url(data.get("next_page_url"))
            if url and pages >= cfg.max_pages_per_query:
                log.warning("%s %s..%s: stopped at max_pages_per_query=%d; lower chunk_days or raise the limit",
                            agency.key, c_since, c_until, cfg.max_pages_per_query)
                stats.truncated_queries.append(f"{agency.key} {c_since}..{c_until}")
                url = None
                reported = None  # already reported as truncated
        # The API stops paging after 50 pages and drops next_page_url without an error, so compare
        # what was received with the count it reported on the first page.
        if reported is not None and received < reported:
            log.warning("%s %s..%s: API reported %d documents but served %d; lower chunk_days or raise per_page",
                        agency.key, c_since, c_until, reported, received)
            stats.truncated_queries.append(f"{agency.key} {c_since}..{c_until}")
    return out


# ---------------------------------------------------------------- dates

def iso(d: dt.date) -> str:
    return d.isoformat()


def parse_iso(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def date_chunks(since: str, until: str, chunk_days: int) -> list[tuple[str, str]]:
    a, b = parse_iso(since), parse_iso(until)
    if a > b:
        return []
    out = []
    while a <= b:
        end = min(b, a + dt.timedelta(days=chunk_days - 1))
        out.append((iso(a), iso(end)))
        a = end + dt.timedelta(days=1)
    return out


def compute_since(existing: list[dict], today: dt.date, cfg: Config, explicit: str | None, backfill_days: int | None,
                  complete_since: str | None = "") -> str:
    """Incremental window start.

    Explicit --since wins, then --backfill-days. Otherwise, if the store is known complete
    (complete_since is set, meaning a previous collector run filled it): the newest stored
    publication_date minus overlap_days (re-fetching a few days catches corrections and
    late-indexed documents; dedupe makes the overlap harmless). If the store is empty or was
    seeded by hand (no complete_since): today minus first_run_lookback_days.
    The start is never earlier than the retention window.
    """
    floor = today - dt.timedelta(days=cfg.retention_days)
    if explicit:
        start = parse_iso(explicit)
    elif backfill_days:
        start = today - dt.timedelta(days=backfill_days)
    elif existing and complete_since:
        newest = max(parse_iso(d["publication_date"]) for d in existing)
        start = newest - dt.timedelta(days=cfg.overlap_days)
    else:
        start = today - dt.timedelta(days=cfg.first_run_lookback_days)
    return iso(max(start, floor))


# ---------------------------------------------------------------- storage

def read_store_meta(path: str) -> dict:
    """Top-level fields of documents.json other than the documents (empty dict if absent or unreadable)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != "documents"}


def read_documents(path: str) -> list[dict]:
    """Stored documents. A missing file is an empty store; invalid entries are dropped with a log line."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            raise CollectError(f"{path} is not valid JSON ({e.msg}); restore it from git history") from None
    docs = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(docs, list):
        raise CollectError(f"{path}: expected an object with a 'documents' list")
    good = []
    for d in docs:
        if validate_document(d):
            good.append(d)
        else:
            log.warning("dropping invalid stored document: %s", str(d)[:100])
    return good


def _dump(obj: Any) -> str:
    """Compact JSON with one document per line: small on disk, readable diffs in git."""
    head = {k: v for k, v in obj.items() if k != "documents"}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"{json.dumps(k)}:{json.dumps(v, ensure_ascii=False, separators=(',', ':'))},")
    docs = obj.get("documents", [])
    lines.append('"documents":[')
    for i, d in enumerate(docs):
        lines.append(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + ("," if i < len(docs) - 1 else ""))
    lines.append("]}")
    return "\n".join(lines) + "\n"


def _write_if_changed(path: str, text: str) -> bool:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def agencies_meta(cfg: Config) -> list[dict]:
    return [{"key": a.key, "slug": a.slug, "label": a.label, "feed": f"feed-{a.key.lower()}.xml"} for a in cfg.agencies]


def write_outputs(data_dir: str, docs: list[dict], new_docs: list[dict], cfg: Config, run_date: str,
                  since: str, prev_updated: str | None, changed_docs: bool, complete_since: str) -> list[str]:
    """Write documents.json, latest.json and the Atom feeds. Returns the paths that changed.

    'updated' moves only when the document set changes, so a quiet day produces no diff and no commit.
    """
    os.makedirs(data_dir, exist_ok=True)
    updated = run_date if (changed_docs or not prev_updated) else prev_updated
    changed = []
    main = {
        "schema_version": SCHEMA_VERSION,
        "source": "Federal Register API v1, https://www.federalregister.gov/developers/documentation/api/v1",
        "updated": updated,
        "retention_days": cfg.retention_days,
        "complete_since": complete_since,
        "agencies": agencies_meta(cfg),
        "count": len(docs),
        "documents": docs,
    }
    if _write_if_changed(os.path.join(data_dir, "documents.json"), _dump(main)):
        changed.append("documents.json")
    latest = {
        "schema_version": SCHEMA_VERSION,
        "updated": updated,
        "window_start": since,
        "count": len(new_docs),
        "documents": new_docs,
    }
    if changed_docs or not os.path.exists(os.path.join(data_dir, "latest.json")):
        if _write_if_changed(os.path.join(data_dir, "latest.json"), _dump(latest)):
            changed.append("latest.json")
    changed += write_feeds(data_dir, docs, cfg, updated)
    return changed


# ---------------------------------------------------------------- run

@dataclass
class RunResult:
    since: str
    until: str
    total: int
    new: list
    changed_files: list
    stats: RunStats


def run(cfg: Config, data_dir: str, *, opener: Opener = urllib_opener, sleep: Callable[[float], None] = time.sleep,
        today: dt.date | None = None, since: str | None = None, backfill_days: int | None = None,
        dry_run: bool = False) -> RunResult:
    """One collection run. All agencies must succeed or nothing is written."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    doc_path = os.path.join(data_dir, "documents.json")
    existing = read_documents(doc_path)
    meta = read_store_meta(doc_path)
    prev_complete = meta.get("complete_since") if isinstance(meta.get("complete_since"), str) else ""
    prev_slugs = {a.get("slug") for a in meta.get("agencies") or [] if isinstance(a, dict)}
    added = [a.key for a in cfg.agencies if prev_slugs and a.slug not in prev_slugs]
    if prev_complete and added:
        # The store says nothing about agencies it never watched, so its coverage claim no longer holds.
        log.warning("agencies added since the last run (%s): starting a fresh window; run with --backfill-days "
                    "%d to fill the full year", ", ".join(added), cfg.retention_days)
        prev_complete = ""
    start = compute_since(existing, today, cfg, since, backfill_days, prev_complete)
    until = iso(today)
    if start > until:
        raise CollectError(f"start date {start} is after today ({until}); pass a --since date on or before today")
    watch_map = {a.slug: a.key for a in cfg.agencies}
    stats = RunStats()
    fetched: list[dict] = []
    for agency in cfg.agencies:
        log.info("fetching %s (%s) %s..%s", agency.key, agency.slug, start, until)
        fetched += fetch_agency(agency, start, until, cfg, opener, sleep, watch_map, stats)
    if stats.truncated_queries:
        # Writing a partial result would mark the window complete and the gap would never be refilled.
        raise CollectError("incomplete results for " + "; ".join(stats.truncated_queries)
                           + ". Lower chunk_days or raise per_page / max_pages_per_query in config.json. Nothing was written.")

    floor = iso(today - dt.timedelta(days=cfg.retention_days))
    old_nums = {d["document_number"] for d in existing}
    merged = [d for d in dedupe(existing + fetched) if d["publication_date"] >= floor]
    new_docs = [d for d in merged if d["document_number"] not in old_nums]
    # Coverage: the store holds every matching document published from max(complete_since,
    # today - retention_days) through today. A run whose window overlaps the stored data extends the
    # existing coverage; a run after a gap (or over a hand-seeded store) restarts coverage at its own start.
    # complete_since is not advanced with the retention floor, so a quiet day writes no diff.
    if prev_complete and existing and start <= max(d["publication_date"] for d in existing):
        complete = min(prev_complete, start)
    else:
        complete = start
    changed_docs = merged != sorted(existing, key=lambda d: (d["publication_date"], d["document_number"]), reverse=True)
    changed_docs = changed_docs or complete != prev_complete
    changed: list[str] = []
    if not dry_run:
        prev_updated = meta.get("updated") if isinstance(meta.get("updated"), str) else None
        changed = write_outputs(data_dir, merged, new_docs, cfg, until, start, prev_updated, changed_docs, complete)
    log.info("requests=%d fetched=%d skipped=%d new=%d total=%d changed=%s",
             stats.requests, stats.fetched, stats.skipped, len(new_docs), len(merged), ",".join(changed) or "none")
    return RunResult(start, until, len(merged), new_docs, changed, stats)
