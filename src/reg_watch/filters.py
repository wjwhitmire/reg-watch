"""Filter, search and export logic shared with the web page.

This module is the reference implementation. The JavaScript core in web/app.html
(<script id="core">) mirrors it function for function, and web/parity.test.mjs
checks the two against tests/vectors.json. Keep them in step.

Matching rules (stated in the README and the page):
- Case folding is ASCII only (A-Z to a-z), so Python and every browser agree exactly.
- Keyword query: whitespace-separated terms, "quoted phrases" kept together, a leading
  minus excludes (-notice, -"special permits"). Every include term must appear and no
  exclude term may appear, as a substring of title, abstract, document number, docket ids,
  RINs and agency names.
- Dates are ISO strings (YYYY-MM-DD); range bounds are inclusive.
- "Closing soon": comments_close_on from the reference date through reference date + window days.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

_WS = " \t\n\r\f\v"
_TERM_RE = re.compile(r'(-?)"([^"]*)"|([^ \t\n\r\f\v]+)')
_WS_RE = re.compile(r"[ \t\n\r\f\v]+")
_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DOCNUM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{2,39}")
TYPES_ALL = ("Rule", "Proposed Rule", "Notice", "Presidential Document", "Other")
DOC_KEYS = ("document_number", "title", "type", "agencies", "watch", "publication_date", "comments_close_on",
            "effective_on", "abstract", "html_url", "pdf_url", "docket_ids", "regulation_id_numbers", "significant")
URL_PREFIXES = ("https://www.federalregister.gov/", "https://federalregister.gov/",
                "https://www.govinfo.gov/", "https://govinfo.gov/")
MAX_DOCS = 100000
CSV_COLUMNS = ("document_number", "publication_date", "type", "watch", "title",
               "comments_close_on", "effective_on", "docket_ids", "html_url")


def _squash(s: str) -> str:
    """Collapse ASCII whitespace runs to one space and trim (ASCII only, to match JS exactly)."""
    return _WS_RE.sub(" ", s).strip(_WS)


def fold(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def parse_query(q: Any) -> dict:
    """Split a keyword query into include and exclude terms (folded, deduplicated, in order)."""
    inc: list[str] = []
    exc: list[str] = []
    if not isinstance(q, str):
        return {"include": inc, "exclude": exc}
    for m in _TERM_RE.finditer(q):
        if m.group(3) is not None:
            raw = m.group(3)
            neg = raw.startswith("-") and len(raw) > 1
            term = raw[1:] if neg else raw
        else:
            neg = m.group(1) == "-"
            term = m.group(2)
        term = _squash(fold(term))
        if not term:
            continue
        target = exc if neg else inc
        if term not in target:
            target.append(term)
    return {"include": inc, "exclude": exc}


def haystack(doc: dict) -> str:
    parts = [doc.get("title") or "", doc.get("abstract") or "", doc.get("document_number") or ""]
    parts += doc.get("docket_ids") or []
    parts += doc.get("regulation_id_numbers") or []
    parts += [a.get("name") or "" for a in doc.get("agencies") or []]
    return fold(" ".join(_squash(p) for p in parts))


def is_iso(s: Any) -> bool:
    """True for a real calendar date written YYYY-MM-DD (ASCII digits, year 0001 to 9999)."""
    if not isinstance(s, str) or not _ISO_RE.fullmatch(s):
        return False
    try:
        dt.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def days_between(a: str, b: str) -> int:
    """Whole days from a to b (b - a)."""
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def add_days(a: str, n: int) -> str:
    """ISO date a plus n days, clamped to 0001-01-01 .. 9999-12-31."""
    try:
        return (dt.date.fromisoformat(a) + dt.timedelta(days=n)).isoformat()
    except OverflowError:
        return "9999-12-31" if n > 0 else "0001-01-01"


def _window(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        return 14
    return max(0, min(365, int(v)))


def closing_soon(doc: dict, ref: str, window: int) -> bool:
    c = doc.get("comments_close_on")
    return is_iso(c) and ref <= c <= add_days(ref, window)


def filter_documents(docs: list, query: dict) -> dict:
    """Apply the page's filters. Returns matching document numbers in display order plus facet counts.

    query keys (all optional): watch (agency key, "" for all), type ("" for all), q (keywords),
    from, to (ISO dates, inclusive), closing (bool), ref (ISO reference date, required when
    closing is true or sort is "closing"), window (days, default 14), sort ("newest" or "closing").
    """
    watch = query.get("watch") or ""
    typ = query.get("type") or ""
    terms = parse_query(query.get("q") or "")
    d_from = query.get("from") if is_iso(query.get("from")) else ""
    d_to = query.get("to") if is_iso(query.get("to")) else ""
    closing = query.get("closing") is True
    ref = query.get("ref") if is_iso(query.get("ref")) else ""
    window = _window(query.get("window", 14))
    sort = "closing" if query.get("sort") == "closing" else "newest"

    hits = []
    for d in docs:
        if watch and watch not in (d.get("watch") or []):
            continue
        if typ and d.get("type") != typ:
            continue
        pub = d.get("publication_date") or ""
        if d_from and pub < d_from:
            continue
        if d_to and pub > d_to:
            continue
        if closing and not (ref and closing_soon(d, ref, window)):
            continue
        if terms["include"] or terms["exclude"]:
            h = haystack(d)
            if any(t not in h for t in terms["include"]):
                continue
            if any(t in h for t in terms["exclude"]):
                continue
        hits.append(d)

    def newest_key(d):
        return (d.get("publication_date") or "", d.get("document_number") or "")

    hits.sort(key=newest_key, reverse=True)
    if sort == "closing":
        # Stable sort on top of newest-first: earliest deadline first, documents without one last.
        hits.sort(key=lambda d: (0, d["comments_close_on"]) if is_iso(d.get("comments_close_on")) else (1, ""))

    by_type: dict[str, int] = {}
    by_watch: dict[str, int] = {}
    closing_count = 0
    for d in hits:
        by_type[d.get("type")] = by_type.get(d.get("type"), 0) + 1
        for w in d.get("watch") or []:
            by_watch[w] = by_watch.get(w, 0) + 1
        if ref and closing_soon(d, ref, window):
            closing_count += 1
    return {
        "numbers": [d["document_number"] for d in hits],
        "count": len(hits),
        "by_type": by_type,
        "by_watch": by_watch,
        "closing_count": closing_count,
    }


def csv_cell(v: Any) -> str:
    """One CSV cell. Formula-injection guard: a leading = + - @ tab or CR gets a single-quote prefix."""
    if v is None:
        s = ""
    elif isinstance(v, list):
        s = "; ".join(str(x) for x in v)
    else:
        s = str(v)
    if s and s[0] in "=+-@\t\r":
        s = "'" + s
    if any(c in s for c in ',"\n\r'):
        s = '"' + s.replace('"', '""') + '"'
    return s


def to_csv(docs: list) -> str:
    lines = [",".join(CSV_COLUMNS)]
    for d in docs:
        lines.append(",".join(csv_cell(d.get(c)) for c in CSV_COLUMNS))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ loading data in the page

def safe_url(v: Any) -> str | None:
    """Same rule as schema.safe_url: https on federalregister.gov or govinfo.gov, printable ASCII, no quotes or brackets."""
    if not isinstance(v, str) or len(v) > 2000:
        return None
    if any(not ("!" <= c <= "~") or c in "<>\"'`\\" for c in v):
        return None
    return v if any(v.startswith(p) for p in URL_PREFIXES) else None


def _str_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _str_list(v: Any) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def coerce_doc(d: Any) -> dict | None:
    """Defensive read of one stored document, as the page does it. None means the document is dropped.

    The page never trusts data/documents.json blindly: links must pass safe_url, dates must be real
    ISO dates, and every text field must be a string. Unknown types become "Other"; extra keys are ignored.
    """
    if not isinstance(d, dict):
        return None
    num, title, pub = d.get("document_number"), d.get("title"), d.get("publication_date")
    if not isinstance(num, str) or not _DOCNUM_RE.fullmatch(num) or not isinstance(title, str) or not title or not is_iso(pub):
        return None
    typ = d.get("type")
    agencies = []
    if isinstance(d.get("agencies"), list):
        for a in d["agencies"]:
            if isinstance(a, dict):
                agencies.append({"name": a["name"] if isinstance(a.get("name"), str) else "",
                                 "slug": a["slug"] if isinstance(a.get("slug"), str) else None})
    sig = d.get("significant")
    return {
        "document_number": num,
        "title": title,
        "type": typ if isinstance(typ, str) and typ in TYPES_ALL else "Other",
        "agencies": agencies,
        "watch": _str_list(d.get("watch")),
        "publication_date": pub,
        "comments_close_on": d["comments_close_on"] if is_iso(d.get("comments_close_on")) else None,
        "effective_on": d["effective_on"] if is_iso(d.get("effective_on")) else None,
        "abstract": _str_or_none(d.get("abstract")),
        "html_url": safe_url(d.get("html_url")),
        "pdf_url": safe_url(d.get("pdf_url")),
        "docket_ids": _str_list(d.get("docket_ids")),
        "regulation_id_numbers": _str_list(d.get("regulation_id_numbers")),
        "significant": sig if isinstance(sig, bool) else None,
    }


def coerce_dataset(obj: Any) -> dict:
    """Read a documents.json object. Returns docs (deduplicated, first copy wins), dropped count,
    updated date, and agency list. Raises ValueError with a plain message when the shape is wrong."""
    if not isinstance(obj, dict) or not isinstance(obj.get("documents"), list):
        raise ValueError("Data file is not a Reg Watch documents.json (no 'documents' list).")
    raw = obj["documents"]
    if len(raw) > MAX_DOCS:
        raise ValueError(f"Data file has {len(raw)} documents; the page reads at most {MAX_DOCS}.")
    docs, seen, dropped = [], set(), 0
    for d in raw:
        c = coerce_doc(d)
        if c is None or c["document_number"] in seen:
            dropped += 1
            continue
        seen.add(c["document_number"])
        docs.append(c)
    agencies = []
    if isinstance(obj.get("agencies"), list):
        for a in obj["agencies"]:
            if isinstance(a, dict) and isinstance(a.get("key"), str) and a["key"]:
                agencies.append({"key": a["key"], "label": a["label"] if isinstance(a.get("label"), str) else a["key"]})
    return {
        "docs": docs,
        "dropped": dropped,
        "updated": obj["updated"] if is_iso(obj.get("updated")) else None,
        "agencies": agencies,
    }
