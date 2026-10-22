"""Offline test harness: a fake Federal Register API backed by the recorded fixtures."""

from __future__ import annotations

import glob
import json
import os
import urllib.parse

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def load_fixture_files() -> dict:
    out = {}
    for path in sorted(glob.glob(os.path.join(FIXTURES, "fr_*.json"))):
        with open(path, encoding="utf-8") as fh:
            out[os.path.basename(path)[3:-5]] = json.load(fh)
    return out


def all_raw_records() -> list[dict]:
    """Every recorded API result, deduplicated by document_number (first copy wins)."""
    seen = {}
    for data in load_fixture_files().values():
        for resp in data["responses"]:
            for r in resp["results"]:
                seen.setdefault(r["document_number"], r)
    return list(seen.values())


class FakeAPI:
    """Answers GET /api/v1/documents.json like the real API for the parameters the collector sends.

    Supports conditions[agencies][], conditions[publication_date][gte|lte], per_page, page,
    order=oldest|newest and fields[]. Records can be scripted to fail with given statuses.
    """

    def __init__(self, records, per_page_cap=1000):
        self.records = list(records)
        self.calls: list[str] = []
        self.headers_seen: list[dict] = []
        self.script: list = []  # queued (status, headers, body) responses returned before normal ones
        self.per_page_cap = per_page_cap
        self.drop_fields_in_next = True

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        self.headers_seen.append(dict(headers))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "www.federalregister.gov", url
        assert parts.path in ("/api/v1/documents.json", "/api/v1/documents"), parts.path
        q = urllib.parse.parse_qs(parts.query)
        slugs = q.get("conditions[agencies][]", [])
        gte = q.get("conditions[publication_date][gte]", [""])[0]
        lte = q.get("conditions[publication_date][lte]", ["9999-12-31"])[0]
        per_page = min(int(q.get("per_page", ["20"])[0]), self.per_page_cap)
        page = int(q.get("page", ["1"])[0])
        order = q.get("order", ["relevance"])[0]
        fields = q.get("fields[]")
        hits = [r for r in self.records
                if isinstance(r, dict)
                and (not slugs or any(isinstance(a, dict) and a.get("slug") in slugs for a in (r.get("agencies") or [])))
                and gte <= str(r.get("publication_date") or "") <= lte]
        hits.sort(key=lambda r: (str(r.get("publication_date")), str(r.get("document_number"))), reverse=(order == "newest"))
        total_pages = max(1, -(-len(hits) // per_page)) if hits else None
        chunk = hits[(page - 1) * per_page: page * per_page]
        if fields:
            chunk = [{k: r.get(k) for k in fields} for r in chunk]
        nxt = None
        if total_pages and page < total_pages:
            nq = [(k, v) for k, vs in q.items() for v in vs if k != "page" and not (self.drop_fields_in_next and k == "fields[]")]
            nq.append(("page", str(page + 1)))
            nxt = "https://www.federalregister.gov/api/v1/documents?" + urllib.parse.urlencode(nq)
        body = {"count": len(hits), "total_pages": total_pages, "next_page_url": nxt, "results": chunk}
        if not hits:
            body = {"count": 0, "total_pages": None, "next_page_url": None, "results": []}
        return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()


@pytest.fixture
def raw_records():
    return all_raw_records()


@pytest.fixture
def fake_api(raw_records):
    return FakeAPI(raw_records)


@pytest.fixture
def no_sleep():
    waits = []

    def sleep(s):
        waits.append(s)

    sleep.waits = waits
    return sleep
