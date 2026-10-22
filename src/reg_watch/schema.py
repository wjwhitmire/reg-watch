"""Normalize Federal Register API records into the stable Reg Watch schema.

The schema is a contract with the web page, the Atom feeds and anyone who
consumes data/documents.json. Keys never change meaning; new keys are only
ever added, never renamed or removed.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Iterable

# Order is the order keys are written in documents.json.
SCHEMA_KEYS = (
    "document_number",
    "title",
    "type",
    "agencies",
    "watch",
    "publication_date",
    "comments_close_on",
    "effective_on",
    "abstract",
    "html_url",
    "pdf_url",
    "docket_ids",
    "regulation_id_numbers",
    "significant",
)

# API "type" display values, and the short codes the API uses for conditions[type][].
TYPES = ("Rule", "Proposed Rule", "Notice", "Presidential Document")
OTHER_TYPE = "Other"

ALLOWED_URL_PREFIXES = (
    "https://www.federalregister.gov/",
    "https://federalregister.gov/",
    "https://www.govinfo.gov/",
    "https://govinfo.gov/",
)

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DOCNUM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{2,39}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
# XML 1.0 forbids most C0 controls; strip them (and C1, and lone surrogates) everywhere.
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]")
_WS_RE = re.compile(r"\s+")
# GPO markup that appears inside abstracts: <INF>10</INF> (subscript), <SU>2</SU> (superscript), <E T="03">x</E> (emphasis).
_GPO_TAG_RE = re.compile(r"</?(?:INF|SU|E|FR|LI|B|I)(?:\s[^<>]{0,40})?>", re.IGNORECASE)
# GPO bracketed character entities, e.g. Juane[ntilde]o. Unknown names are left as written.
_GPO_ENTITIES = {
    "ntilde": "ñ", "Ntilde": "Ñ", "eacute": "é", "Eacute": "É",
    "aacute": "á", "iacute": "í", "oacute": "ó", "uacute": "ú",
    "uuml": "ü", "ouml": "ö", "auml": "ä", "ccedil": "ç",
    "egrave": "è", "agrave": "à",
    "mu": "µ", "deg": "°", "hyphen": "-", "middot": "·",
    "sect": "§", "para": "¶", "plusmn": "±", "times": "×",
    "ge": "≥", "le": "≤", "reg": "®", "trade": "™", "copy": "©",
}
_GPO_ENTITY_RE = re.compile(r"\[(" + "|".join(sorted(_GPO_ENTITIES, key=len, reverse=True)) + r")\]")

MAX_TITLE = 1000
MAX_ABSTRACT = 2000
MAX_LIST = 50
MAX_ITEM = 300


class MalformedRecord(ValueError):
    """A record from the API that cannot be normalized. The message says why."""


def clean_text(value: Any, limit: int) -> str | None:
    """Plain text: strip control characters and GPO markup, collapse whitespace, cap length."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = _CTRL_RE.sub("", value)
    s = _GPO_TAG_RE.sub("", s)
    s = _GPO_ENTITY_RE.sub(lambda m: _GPO_ENTITIES[m.group(1)], s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return None
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def clean_date(value: Any) -> str | None:
    """Return an ISO YYYY-MM-DD string, or None if absent or not a valid calendar date."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    y, m, d = (int(p) for p in value.split("-"))
    if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2999):
        return None
    try:
        _dt.date(y, m, d)
    except ValueError:
        return None
    return value


def safe_url(value: Any) -> str | None:
    """Only https links to federalregister.gov or govinfo.gov survive. Anything else is dropped."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    # Printable ASCII only (the API percent-encodes), and no characters that could break out of an attribute.
    if any(not ("!" <= c <= "~") or c in "<>\"'`\\" for c in value):
        return None
    for prefix in ALLOWED_URL_PREFIXES:
        if value.startswith(prefix):
            return value
    return None


def clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:MAX_LIST]:
        s = clean_text(item, MAX_ITEM)
        if s and s not in out:
            out.append(s)
    return out


def normalize_type(value: Any) -> str:
    if isinstance(value, str):
        v = value.strip()
        if v in TYPES:
            return v
        upper = v.upper()
        short = {"RULE": "Rule", "PRORULE": "Proposed Rule", "NOTICE": "Notice", "PRESDOCU": "Presidential Document"}
        if upper in short:
            return short[upper]
    return OTHER_TYPE


def normalize_agencies(value: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(value, list):
        return out
    seen = set()
    for a in value[:MAX_LIST]:
        if not isinstance(a, dict):
            continue
        name = clean_text(a.get("name"), MAX_ITEM) or clean_text(a.get("raw_name"), MAX_ITEM)
        slug = a.get("slug")
        slug = slug if isinstance(slug, str) and _SLUG_RE.fullmatch(slug) else None
        if not name and not slug:
            continue
        key = slug or name
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name or slug, "slug": slug})
    return out


def normalize_record(raw: Any, watch_map: dict[str, str] | None = None, query_key: str | None = None) -> dict:
    """Map one API result to the Reg Watch schema.

    watch_map: agency slug -> configured key (e.g. "commodity-futures-trading-commission" -> "CFTC").
    query_key: the configured key whose query returned this record. It is always added to "watch",
    because the API matches child agencies (a BSEE notice is returned for the Interior query).

    Raises MalformedRecord when the record lacks a usable document_number, title or publication_date.
    """
    if not isinstance(raw, dict):
        raise MalformedRecord("record is not a JSON object")
    num = raw.get("document_number")
    if not isinstance(num, str) or not _DOCNUM_RE.fullmatch(num):
        raise MalformedRecord(f"bad or missing document_number: {str(num)[:60]!r}")
    title = clean_text(raw.get("title"), MAX_TITLE)
    if not title:
        raise MalformedRecord(f"{num}: missing title")
    pub = clean_date(raw.get("publication_date"))
    if not pub:
        raise MalformedRecord(f"{num}: bad or missing publication_date: {str(raw.get('publication_date'))[:30]!r}")

    agencies = normalize_agencies(raw.get("agencies"))
    watch: list[str] = []
    if watch_map:
        for a in agencies:
            k = watch_map.get(a["slug"] or "")
            if k and k not in watch:
                watch.append(k)
    if query_key and query_key not in watch:
        watch.append(query_key)

    sig = raw.get("significant")
    return {
        "document_number": num,
        "title": title,
        "type": normalize_type(raw.get("type")),
        "agencies": agencies,
        "watch": watch,
        "publication_date": pub,
        "comments_close_on": clean_date(raw.get("comments_close_on")),
        "effective_on": clean_date(raw.get("effective_on")),
        "abstract": clean_text(raw.get("abstract"), MAX_ABSTRACT),
        "html_url": safe_url(raw.get("html_url")),
        "pdf_url": safe_url(raw.get("pdf_url")),
        "docket_ids": clean_str_list(raw.get("docket_ids")),
        "regulation_id_numbers": clean_str_list(raw.get("regulation_id_numbers")),
        "significant": sig if isinstance(sig, bool) else None,
    }


def merge_records(old: dict, new: dict) -> dict:
    """Combine two normalized copies of the same document. The newer copy wins field by field,
    except that an empty new value never erases a known old one, and watch lists are unioned."""
    out = dict(new)
    for k in SCHEMA_KEYS:
        if out.get(k) in (None, [], "") and old.get(k) not in (None, [], ""):
            out[k] = old[k]
    watch = list(old.get("watch") or [])
    for w in new.get("watch") or []:
        if w not in watch:
            watch.append(w)
    out["watch"] = watch
    return {k: out.get(k) for k in SCHEMA_KEYS}


def validate_document(doc: Any) -> bool:
    """True if doc is a dict with exactly the schema keys and the right value types."""
    if not isinstance(doc, dict) or tuple(doc.keys()) != SCHEMA_KEYS:
        return False
    if not isinstance(doc["document_number"], str) or not isinstance(doc["title"], str):
        return False
    if doc["type"] not in TYPES + (OTHER_TYPE,):
        return False
    if clean_date(doc["publication_date"]) is None:
        return False
    for k in ("comments_close_on", "effective_on"):
        if doc[k] is not None and clean_date(doc[k]) is None:
            return False
    for k in ("abstract", "html_url", "pdf_url"):
        if doc[k] is not None and not isinstance(doc[k], str):
            return False
    for k in ("watch", "docket_ids", "regulation_id_numbers"):
        if not isinstance(doc[k], list) or not all(isinstance(x, str) for x in doc[k]):
            return False
    if not isinstance(doc["agencies"], list):
        return False
    for a in doc["agencies"]:
        if not isinstance(a, dict) or set(a) != {"name", "slug"}:
            return False
    if doc["significant"] not in (True, False, None):
        return False
    return True


def sort_key_newest(doc: dict) -> tuple:
    return (doc["publication_date"], doc["document_number"])


def dedupe(docs: Iterable[dict]) -> list[dict]:
    """Deduplicate by document_number; later items merge over earlier ones. Order: newest first."""
    by_num: dict[str, dict] = {}
    for d in docs:
        n = d["document_number"]
        by_num[n] = merge_records(by_num[n], d) if n in by_num else {k: d.get(k) for k in SCHEMA_KEYS}
    return sorted(by_num.values(), key=sort_key_newest, reverse=True)
