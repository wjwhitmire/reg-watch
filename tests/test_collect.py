import datetime as dt
import io
import json
import logging
import os
import urllib.parse
import xml.etree.ElementTree as ET

import pytest

from conftest import FakeAPI, all_raw_records, load_fixture_files
from reg_watch import cli
from reg_watch.collect import (
    FIELDS, CollectError, Config, DEFAULT_AGENCIES, build_url, compute_since, date_chunks,
    get_json, load_config, read_documents, run, urllib_opener,
)
from reg_watch.feeds import ATOM_NS
from reg_watch.schema import SCHEMA_KEYS, validate_document

TODAY = dt.date(2026, 10, 21)
A = f"{{{ATOM_NS}}}"


def cfg(**kw):
    c = Config(agencies=list(DEFAULT_AGENCIES))
    c.request_delay_s = 0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------ fixtures are real and well formed

def test_fixtures_cover_every_default_agency():
    files = load_fixture_files()
    assert set(files) == {"cftc", "sec", "ferc", "epa", "phmsa", "bsee", "blm", "interior"}
    for name, data in files.items():
        assert "web fetch" in data["_provenance"]["method"]
        assert data["_provenance"]["retrieved"] == "2026-10-21"
        for resp in data["responses"]:
            assert resp["request"].startswith("https://www.federalregister.gov/api/v1/documents.json?")
    recs = all_raw_records()
    assert len(recs) == 63
    for r in recs:
        assert r["html_url"].startswith("https://www.federalregister.gov/documents/")
        assert r["pdf_url"].startswith("https://www.govinfo.gov/content/pkg/FR-")
        assert r["document_number"] in r["html_url"] and r["document_number"] in r["pdf_url"]


# ------------------------------------------------------------ full run

def test_full_run_writes_all_outputs(tmp_path, fake_api, no_sleep):
    res = run(cfg(), str(tmp_path), opener=fake_api, sleep=no_sleep, today=TODAY, backfill_days=365)
    names = sorted(os.listdir(tmp_path))
    expected_feeds = ["feed.xml"] + [f"feed-{a.key.lower()}.xml" for a in DEFAULT_AGENCIES]
    assert names == sorted(["documents.json", "latest.json"] + expected_feeds)
    doc = load(tmp_path / "documents.json")
    assert doc["schema_version"] == 1 and doc["updated"] == "2026-10-21"
    assert doc["count"] == len(doc["documents"]) == res.total
    # Every fixture record is reachable by at least one configured agency query.
    assert res.total == 63
    assert all(validate_document(d) for d in doc["documents"])
    latest = load(tmp_path / "latest.json")
    assert latest["count"] == 63  # first run: everything is new
    assert res.stats.skipped == 0


def test_user_agent_and_fields(tmp_path, fake_api, no_sleep):
    run(cfg(repo_url="https://github.com/example/reg-watch"), str(tmp_path), opener=fake_api, sleep=no_sleep, today=TODAY, backfill_days=10)
    for h in fake_api.headers_seen:
        assert h["User-Agent"].startswith("reg-watch/0.1.0 (+https://github.com/example/reg-watch;")
        assert h["Accept"] == "application/json"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(fake_api.calls[0]).query)
    assert q["fields[]"] == list(FIELDS)
    assert q["order"] == ["oldest"]
    assert int(q["per_page"][0]) <= 1000


def test_multi_agency_doc_gets_every_watch_key(tmp_path, fake_api, no_sleep):
    run(cfg(), str(tmp_path), opener=fake_api, sleep=no_sleep, today=TODAY, backfill_days=365)
    docs = {d["document_number"]: d for d in load(tmp_path / "documents.json")["documents"]}
    assert docs["2026-18104"]["watch"] == ["CFTC", "SEC"]  # joint CFTC/SEC Form PF rule
    assert docs["2026-12787"]["watch"] == ["CFTC", "SEC"]  # nine-agency FDTA rule
    assert docs["2026-21182"]["watch"] == ["Interior", "BSEE"]
    assert docs["2026-21476"]["watch"] == ["Interior"]  # BIA notice, child of Interior
    assert docs["2026-21443"]["comments_close_on"] == "2026-12-21"
    assert docs["2026-12787"]["significant"] is True
    assert docs["2026-16111"]["significant"] is False


# ------------------------------------------------------------ pagination

def test_pagination_follows_next_page_url(tmp_path, raw_records, no_sleep):
    api = FakeAPI(raw_records)
    res = run(cfg(per_page=2), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY, backfill_days=365)
    assert res.total == 63
    paged = [u for u in api.calls if "page=" in u]
    assert paged, "expected follow-up page requests"
    # The collector re-adds the field list when the API's next_page_url drops it.
    for u in paged:
        assert "fields%5B%5D=comments_close_on" in u or "fields[]=comments_close_on" in urllib.parse.unquote(u)
    docs = {d["document_number"]: d for d in load(tmp_path / "documents.json")["documents"]}
    assert docs["2026-21377"]["comments_close_on"] == "2026-11-18"


def test_pagination_cap_logs_and_writes_nothing(tmp_path, raw_records, no_sleep, caplog):
    # Review fix: a truncated query used to be written and marked complete, so the gap was never refilled.
    api = FakeAPI(raw_records)
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        with pytest.raises(CollectError, match="incomplete results"):
            run(cfg(per_page=1, max_pages_per_query=2, chunk_days=366), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY, backfill_days=365)
    assert "max_pages_per_query" in caplog.text
    assert os.listdir(tmp_path) == []


def test_silent_api_page_limit_is_detected(tmp_path, raw_records, no_sleep, caplog):
    # Review fix: the live API serves at most 50 pages and then omits next_page_url without an error
    # (verified 2026-10-21: count=291, total_pages=50 at per_page=2, no next_page_url on page 50).
    class CappedAPI(FakeAPI):
        def __call__(self, url, headers, timeout):
            status, h, body = super().__call__(url, headers, timeout)
            data = json.loads(body)
            if "page=2" in url:
                data["next_page_url"] = None
            return status, h, json.dumps(data).encode()
    api = CappedAPI(raw_records)
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        with pytest.raises(CollectError, match="incomplete results"):
            run(cfg(per_page=2, chunk_days=366), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY, backfill_days=365)
    assert "API reported" in caplog.text
    assert os.listdir(tmp_path) == []


def test_failure_in_last_agency_after_others_succeed_writes_nothing(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=30)
    before = {n: (tmp_path / n).read_bytes() for n in os.listdir(tmp_path)}

    class FailLast(FakeAPI):
        def __call__(self, url, headers, timeout):
            if "interior-department" in url:
                return 503, {}, b""
            return super().__call__(url, headers, timeout)
    with pytest.raises(CollectError):
        run(cfg(max_retries=1), str(tmp_path), opener=FailLast(raw_records), sleep=no_sleep, today=TODAY + dt.timedelta(days=1))
    assert {n: (tmp_path / n).read_bytes() for n in os.listdir(tmp_path)} == before


def test_next_page_url_outside_api_is_ignored(no_sleep, caplog):
    def opener(url, headers, timeout):
        body = {"results": [], "next_page_url": "https://evil.example/steal"}
        return 200, {}, json.dumps(body).encode()
    from reg_watch.collect import RunStats, fetch_agency
    c = cfg()
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        out = fetch_agency(c.agencies[0], "2026-10-01", "2026-10-21", c, opener, no_sleep, {}, RunStats())
    assert out == []
    assert "ignoring next_page_url" in caplog.text


def test_date_chunks():
    assert date_chunks("2026-01-01", "2026-01-01", 31) == [("2026-01-01", "2026-01-01")]
    assert date_chunks("2026-01-01", "2026-03-01", 31) == [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-03-01")]
    assert date_chunks("2026-02-01", "2026-01-01", 31) == []
    ch = date_chunks("2025-10-21", "2026-10-21", 31)
    assert ch[0][0] == "2025-10-21" and ch[-1][1] == "2026-10-21"
    for (a, b), (c, _) in zip(ch, ch[1:]):
        assert (dt.date.fromisoformat(c) - dt.date.fromisoformat(b)).days == 1


def test_build_url_is_api_only():
    u = build_url("environmental-protection-agency", "2026-10-01", "2026-10-21", 100)
    assert u.startswith("https://www.federalregister.gov/api/v1/documents.json?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
    assert q["conditions[agencies][]"] == ["environmental-protection-agency"]
    assert q["conditions[publication_date][gte]"] == ["2026-10-01"]
    assert q["conditions[publication_date][lte]"] == ["2026-10-21"]


# ------------------------------------------------------------ dedupe

def test_dedupe_across_agencies_and_runs(tmp_path, raw_records, no_sleep):
    dup = [dict(r) for r in raw_records] + [dict(r) for r in raw_records[:10]]
    api = FakeAPI(dup)
    res = run(cfg(), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY, backfill_days=365)
    nums = [d["document_number"] for d in load(tmp_path / "documents.json")["documents"]]
    assert len(nums) == len(set(nums)) == res.total == 63
    # Second identical run: nothing new, no file changes.
    res2 = run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    assert res2.new == [] and res2.changed_files == []


def test_dedupe_merges_newer_fields_without_erasing(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    changed = []
    for r in raw_records:
        r = dict(r)
        if r["document_number"] == "2026-21377":
            r["title"] = r["title"] + " (corrected)"
            r["comments_close_on"] = None  # a later copy missing a field must not erase it
        changed.append(r)
    res = run(cfg(), str(tmp_path), opener=FakeAPI(changed), sleep=no_sleep, today=TODAY, backfill_days=365)
    docs = {d["document_number"]: d for d in load(tmp_path / "documents.json")["documents"]}
    assert docs["2026-21377"]["title"].endswith("(corrected)")
    assert docs["2026-21377"]["comments_close_on"] == "2026-11-18"
    assert res.new == [] and "documents.json" in res.changed_files


# ------------------------------------------------------------ incremental since-last-run

def test_incremental_since_last_run(tmp_path, raw_records, no_sleep):
    early = [r for r in raw_records if r["publication_date"] <= "2026-10-16"]
    run(cfg(), str(tmp_path), opener=FakeAPI(early), sleep=no_sleep, today=dt.date(2026, 10, 16), backfill_days=365)
    first = load(tmp_path / "documents.json")
    assert first["complete_since"] == "2025-10-16"
    api = FakeAPI(raw_records)
    res = run(cfg(), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY)
    # Window starts at newest stored date (2026-10-16) minus overlap (3 days).
    assert res.since == "2026-10-13"
    for u in api.calls:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        assert q["conditions[publication_date][gte]"][0] >= "2026-10-13"
    new_nums = sorted(d["document_number"] for d in res.new)
    expected = sorted(r["document_number"] for r in raw_records if r["publication_date"] > "2026-10-16")
    assert new_nums == expected
    latest = load(tmp_path / "latest.json")
    assert sorted(d["document_number"] for d in latest["documents"]) == expected
    assert load(tmp_path / "documents.json")["complete_since"] == "2025-10-16"


def test_first_run_uses_lookback_and_retention_floor():
    c = cfg(first_run_lookback_days=30, retention_days=365)
    assert compute_since([], TODAY, c, None, None) == "2026-09-21"
    assert compute_since([], TODAY, c, "2020-01-01", None) == "2025-10-21"  # clamped to retention
    assert compute_since([], TODAY, c, None, 7) == "2026-10-14"
    stored = [{"publication_date": "2026-10-10"}]
    assert compute_since(stored, TODAY, c, None, None, "2026-01-01") == "2026-10-07"
    # A seeded store without complete_since is not trusted for incremental starts.
    assert compute_since(stored, TODAY, c, None, None, "") == "2026-09-21"


def test_retention_drops_old_documents(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    later = dt.date(2027, 7, 1)  # 365 days back is 2026-07-01: the June records age out
    run(cfg(), str(tmp_path), opener=FakeAPI([]), sleep=no_sleep, today=later)
    pubs = [d["publication_date"] for d in load(tmp_path / "documents.json")["documents"]]
    assert pubs and min(pubs) >= "2026-07-01"


def test_quiet_day_changes_nothing(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    before = {n: (tmp_path / n).read_bytes() for n in os.listdir(tmp_path)}
    res = run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY + dt.timedelta(days=1))
    after = {n: (tmp_path / n).read_bytes() for n in os.listdir(tmp_path)}
    assert res.changed_files == [] and before == after


# ------------------------------------------------------------ backoff

def test_429_backoff_then_success(no_sleep):
    api = FakeAPI(all_raw_records())
    api.script = [(429, {"Retry-After": "7"}, b""), (503, {}, b""), (502, {}, b"")]
    c = cfg(backoff_base_s=2, backoff_cap_s=120, max_retries=5)
    data = get_json(build_url("environmental-protection-agency", "2026-10-19", "2026-10-21", 10), c, api, no_sleep)
    assert len(data["results"]) == 5
    assert no_sleep.waits == [7.0, 4.0, 8.0]  # Retry-After honored, then 2*2^1, 2*2^2


def test_backoff_gives_up_and_writes_nothing(tmp_path, no_sleep):
    api = FakeAPI(all_raw_records())
    api.script = [(429, {}, b"")] * 10
    with pytest.raises(CollectError, match="after 4 attempts"):
        run(cfg(max_retries=3), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY)
    assert os.listdir(tmp_path) == []
    assert no_sleep.waits == [2.0, 4.0, 8.0]


def test_backoff_cap_and_network_errors(no_sleep):
    api = FakeAPI(all_raw_records())
    api.script = [OSError("connection reset"), (500, {"Retry-After": "9999"}, b"")]
    data = get_json(build_url("land-management-bureau", "2026-08-01", "2026-08-31", 5), cfg(backoff_cap_s=60), api, no_sleep)
    assert data["count"] == 5
    assert no_sleep.waits == [2.0, 60.0]


def test_4xx_is_not_retried(no_sleep):
    api = FakeAPI([])
    api.script = [(400, {}, b'{"errors":["bad"]}')]
    with pytest.raises(CollectError, match="HTTP 400"):
        get_json(build_url("x", "2026-10-01", "2026-10-02", 5), cfg(), api, no_sleep)
    assert no_sleep.waits == []


def test_invalid_json_body(no_sleep):
    api = FakeAPI([])
    api.script = [(200, {}, b"<html>maintenance</html>")]
    with pytest.raises(CollectError, match="invalid JSON"):
        get_json(build_url("x", "2026-10-01", "2026-10-02", 5), cfg(), api, no_sleep)


def test_default_opener_refuses_other_hosts():
    with pytest.raises(CollectError, match="non-API"):
        urllib_opener("https://example.com/", {}, 1)
    with pytest.raises(CollectError, match="non-API"):
        urllib_opener("http://www.federalregister.gov/api/v1/documents.json", {}, 1)


# ------------------------------------------------------------ malformed records

def test_malformed_records_skipped_with_log(tmp_path, raw_records, no_sleep, caplog):
    slug = {"slug": "environmental-protection-agency", "name": "Environmental Protection Agency"}
    bad = [
        {"document_number": None, "title": "x", "publication_date": "2026-10-20", "agencies": [slug]},
        {"document_number": "2026-99001", "title": "", "publication_date": "2026-10-20", "agencies": [slug]},
        {"document_number": "2026-99002", "title": "No date", "publication_date": "2026-10-20T09", "agencies": [slug]},
        {"document_number": "2026-99003", "title": "Bad day", "publication_date": "2026-02-30", "agencies": [slug]},
        {"document_number": "<script>", "title": "x", "publication_date": "2026-10-20", "agencies": [slug]},
    ]
    api = FakeAPI(raw_records + bad)
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        res = run(cfg(), str(tmp_path), opener=api, sleep=no_sleep, today=TODAY, backfill_days=365)
    assert res.stats.skipped == 5
    assert res.total == 63
    assert caplog.text.count("skipping malformed record (EPA)") == 5
    assert "missing title" in caplog.text and "bad or missing publication_date" in caplog.text


def test_non_object_record_skipped(no_sleep, caplog):
    def opener(url, headers, timeout):
        return 200, {}, json.dumps({"results": ["oops", 5, None], "next_page_url": None}).encode()
    from reg_watch.collect import RunStats, fetch_agency
    c = cfg()
    st = RunStats()
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        out = fetch_agency(c.agencies[0], "2026-10-01", "2026-10-21", c, opener, no_sleep, {}, st)
    assert out == [] and st.skipped == 3


def test_results_not_a_list_is_an_error(no_sleep):
    def opener(url, headers, timeout):
        return 200, {}, b'{"results": {"a": 1}}'
    from reg_watch.collect import RunStats, fetch_agency
    c = cfg()
    with pytest.raises(CollectError, match="not a list"):
        fetch_agency(c.agencies[0], "2026-10-01", "2026-10-21", c, opener, no_sleep, {}, RunStats())


def test_corrupt_store_is_an_error(tmp_path):
    (tmp_path / "documents.json").write_text("{not json")
    with pytest.raises(CollectError, match="restore it from git"):
        read_documents(str(tmp_path / "documents.json"))


def test_invalid_stored_docs_dropped(tmp_path, caplog):
    (tmp_path / "documents.json").write_text(json.dumps({"documents": [{"document_number": "x"}]}))
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        assert read_documents(str(tmp_path / "documents.json")) == []
    assert "dropping invalid stored document" in caplog.text


# ------------------------------------------------------------ Atom

def test_atom_feeds_parse_and_are_valid(tmp_path, raw_records, no_sleep):
    run(cfg(site_url="https://example.github.io/reg-watch"), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    docs = load(tmp_path / "documents.json")["documents"]
    for name in os.listdir(tmp_path):
        if not name.endswith(".xml"):
            continue
        root = ET.parse(tmp_path / name).getroot()
        assert root.tag == A + "feed"
        for req in ("title", "id", "updated", "author"):
            assert root.find(A + req) is not None, (name, req)
        assert root.find(A + "updated").text == "2026-10-21T00:00:00Z"
        self_link = [l for l in root.findall(A + "link") if l.get("rel") == "self"]
        assert self_link and self_link[0].get("href") == f"https://example.github.io/reg-watch/data/{name}"
        ids = set()
        for e in root.findall(A + "entry"):
            for req in ("title", "id", "updated", "summary"):
                assert e.find(A + req) is not None and e.find(A + req).text, (name, req)
            assert e.find(A + "id").text.startswith("https://www.federalregister.gov/d/")
            ids.add(e.find(A + "id").text)
            alt = [l for l in e.findall(A + "link") if l.get("rel") == "alternate"]
            assert alt and alt[0].get("href").startswith("https://www.federalregister.gov/documents/")
        assert len(ids) == len(root.findall(A + "entry"))
    allf = ET.parse(tmp_path / "feed.xml").getroot()
    assert len(allf.findall(A + "entry")) == len(docs)
    epa = ET.parse(tmp_path / "feed-epa.xml").getroot()
    assert len(epa.findall(A + "entry")) == sum(1 for d in docs if "EPA" in d["watch"])
    titles = [e.find(A + "title").text for e in allf.findall(A + "entry")]
    assert "Further Definition of “Swap”-Inclusions" in titles


def test_atom_escapes_hostile_text(tmp_path, no_sleep):
    evil = {
        "document_number": "2026-99999",
        "title": 'Rule <script>alert(1)</script> & "quotes" ]]> \x01\x0b',
        "type": "Rule",
        "abstract": "<img src=x onerror=alert(1)> &amp; done",
        "publication_date": "2026-10-20",
        "html_url": 'https://www.federalregister.gov/documents/x"><script>',
        "pdf_url": "javascript:alert(1)",
        "agencies": [{"slug": "environmental-protection-agency", "name": "EPA <b>"}],
    }
    run(cfg(), str(tmp_path), opener=FakeAPI([evil]), sleep=no_sleep, today=TODAY, backfill_days=5)
    raw = (tmp_path / "feed.xml").read_text(encoding="utf-8")
    assert "<script>" not in raw and "<img" not in raw
    root = ET.fromstring(raw.encode())
    e = root.find(A + "entry")
    assert e.find(A + "title").text == 'Rule <script>alert(1)</script> & "quotes" ]]>'
    # Unsafe URL dropped; the alternate link falls back to the permanent short link (RFC 4287 4.1.1).
    alts = [l.get("href") for l in e.findall(A + "link") if l.get("rel") == "alternate"]
    assert alts == ["https://www.federalregister.gov/d/2026-99999"]
    assert all(l.get("rel") != "enclosure" for l in e.findall(A + "link"))
    d = load(tmp_path / "documents.json")["documents"][0]
    assert d["html_url"] is None and d["pdf_url"] is None


def test_feed_strips_xml_illegal_chars_from_stored_docs(tmp_path):
    # Review fix: a hand-edited store with a control character used to make the serializer emit invalid XML.
    from reg_watch.feeds import build_feed
    doc = {"document_number": "2026-1", "title": "bad\x01title\ufffe", "type": "Rule", "agencies": [], "watch": [],
           "publication_date": "2026-10-20", "comments_close_on": None, "effective_on": None, "abstract": "a\x0bb",
           "html_url": None, "pdf_url": None, "docket_ids": [], "regulation_id_numbers": [], "significant": None}
    root = ET.fromstring(build_feed([doc], "t\x02", "urn:x", "2026-10-21", None, None, 10))
    assert root.find(A + "entry").find(A + "title").text == "badtitle"
    assert root.find(A + "title").text == "t"


def test_adding_an_agency_resets_coverage(tmp_path, raw_records, no_sleep, caplog):
    # Review fix: an agency added to config.json inherited the old complete_since, so the page claimed a
    # full year for it after a 3 day incremental fetch.
    few = cfg(agencies=[a for a in DEFAULT_AGENCIES if a.key != "PHMSA"])
    run(few, str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    assert load(tmp_path / "documents.json")["complete_since"] == "2025-10-21"
    with caplog.at_level(logging.WARNING, logger="reg_watch"):
        res = run(cfg(first_run_lookback_days=30), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY)
    assert "agencies added" in caplog.text and "PHMSA" in caplog.text
    assert res.since == "2026-09-21"  # first_run_lookback_days, not newest minus overlap
    assert load(tmp_path / "documents.json")["complete_since"] == "2026-09-21"


def test_since_after_today_is_an_error(tmp_path, no_sleep):
    with pytest.raises(CollectError, match="after today"):
        run(cfg(), str(tmp_path), opener=FakeAPI([]), sleep=no_sleep, today=TODAY, since="2026-12-01")
    assert os.listdir(tmp_path) == []


def test_default_opener_refuses_offsite_redirect():
    from reg_watch.collect import _ApiOnlyRedirects
    import urllib.request
    h = _ApiOnlyRedirects()
    req = urllib.request.Request("https://www.federalregister.gov/api/v1/documents.json")
    with pytest.raises(CollectError, match="redirected outside"):
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://www.federalregister.gov/api/v1/documents?x=1")
    assert ok.full_url.startswith("https://www.federalregister.gov/api/v1/")


def test_feed_without_site_url_has_no_self_link(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=10)
    root = ET.parse(tmp_path / "feed.xml").getroot()
    assert not [l for l in root.findall(A + "link") if l.get("rel") == "self"]


# ------------------------------------------------------------ schema stability

def test_schema_keys_are_frozen():
    assert SCHEMA_KEYS == (
        "document_number", "title", "type", "agencies", "watch", "publication_date",
        "comments_close_on", "effective_on", "abstract", "html_url", "pdf_url",
        "docket_ids", "regulation_id_numbers", "significant",
    )


def test_output_schema_stable(tmp_path, raw_records, no_sleep):
    run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY, backfill_days=365)
    top = load(tmp_path / "documents.json")
    assert list(top) == ["schema_version", "source", "updated", "retention_days", "complete_since", "agencies", "count", "documents"]
    assert list(load(tmp_path / "latest.json")) == ["schema_version", "updated", "window_start", "count", "documents"]
    for d in top["documents"]:
        assert tuple(d) == SCHEMA_KEYS
        assert d["type"] in ("Rule", "Proposed Rule", "Notice", "Presidential Document", "Other")
    for a in top["agencies"]:
        assert list(a) == ["key", "slug", "label", "feed"]
    # Output is deterministic: rerunning from scratch gives byte-identical files.
    other = tmp_path / "again"
    run(cfg(), str(other), opener=FakeAPI(list(reversed(raw_records))), sleep=no_sleep, today=TODAY, backfill_days=365)
    for n in os.listdir(other):
        assert (other / n).read_bytes() == (tmp_path / n).read_bytes(), n


def test_snapshot_matches_fixtures():
    """tests/fixtures/expected_documents.json pins the normalized form of the recorded API data."""
    from reg_watch.schema import dedupe, normalize_record
    watch = {a.slug: a.key for a in DEFAULT_AGENCIES}
    got = []
    for r in all_raw_records():
        doc = normalize_record(r, watch)
        got.append(doc)
    got = dedupe(got)
    path = os.path.join(os.path.dirname(__file__), "fixtures", "expected_documents.json")
    assert got == load(path)


# ------------------------------------------------------------ config and CLI

def test_load_config_defaults_and_validation(tmp_path):
    c = load_config(None)
    assert [a.key for a in c.agencies] == ["CFTC", "SEC", "FERC", "EPA", "PHMSA", "BSEE", "BLM", "Interior"]
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"agencies": [{"key": "NRC", "slug": "nuclear-regulatory-commission"}], "per_page": 50}))
    c = load_config(str(p))
    assert c.agencies[0].label == "NRC" and c.per_page == 50
    for bad, msg in [
        ({"agencies": []}, "non-empty"),
        ({"agencies": [{"key": "X"}]}, "slug"),
        ({"agencies": [{"key": "X", "slug": "Bad Slug"}]}, "lowercase"),
        ({"agencies": [{"key": "X", "slug": "a"}, {"key": "x", "slug": "b"}]}, "duplicate"),
        ({"agencies": [{"key": "a/b", "slug": "a"}]}, "key"),
        ({"per_page": 5000}, "per_page"),
        ({"per_page": True}, "per_page"),
        ({"backoff_base_s": -1}, "backoff_base_s"),
        ([1, 2], "JSON object"),
        # Review fixes: non-ASCII keys made feed files the page would not link; control characters
        # in labels or URLs reached the User-Agent header and the feeds.
        ({"agencies": [{"key": "\u00e9pa", "slug": "a"}]}, "key"),
        ({"agencies": [{"key": "X", "slug": "\u00e9pa"}]}, "lowercase"),
        ({"agencies": [{"key": "X", "slug": "a", "label": "bad\nlabel"}]}, "control characters"),
        ({"repo_url": "https://x\r\nX-Evil: 1"}, "control characters"),
    ]:
        p.write_text(json.dumps(bad))
        with pytest.raises(CollectError, match=msg):
            load_config(str(p))
    p.write_text("{oops")
    with pytest.raises(CollectError, match="not valid JSON"):
        load_config(str(p))


def test_repo_config_matches_defaults():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    c = load_config(os.path.join(root, "config.json"))
    assert [(a.key, a.slug) for a in c.agencies] == [(a.key, a.slug) for a in DEFAULT_AGENCIES]


def test_cli_collect_and_query(tmp_path, raw_records, monkeypatch, capsys):
    import reg_watch.collect as col
    api = FakeAPI(raw_records)
    monkeypatch.setattr(col, "urllib_opener", api)
    monkeypatch.setattr(col.time, "sleep", lambda s: None)
    real_run = col.run

    def patched_run(*a, **kw):
        kw["opener"] = api
        kw["sleep"] = lambda s: None
        return real_run(*a, **kw)

    monkeypatch.setattr(cli, "run", patched_run)
    data = str(tmp_path / "data")
    assert cli.main(["collect", "--config", str(tmp_path / "missing.json"), "--data", data, "--today", "2026-10-21", "--backfill-days", "365"]) == 0
    out = capsys.readouterr().out
    assert "63 new, 63 stored" in out
    assert cli.main(["query", "--data", data, "--watch", "EPA", "--type", "Proposed Rule", "--closing", "--ref", "2026-11-10"]) == 0
    out = capsys.readouterr().out
    assert "2026-21383" in out and "comments close 2026-11-18" in out
    assert cli.main(["query", "--data", data, "-q", "special permits", "--csv"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("document_number,publication_date") and len(out) == 1 + 6
    assert cli.main(["collect", "--data", data, "--backfill-days", "9999"]) == 1
    assert "backfill-days" in capsys.readouterr().err


def test_cli_rejects_bad_dates(capsys):
    with pytest.raises(SystemExit):
        cli.main(["query", "--from", "10/21/2026"])
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_first_run_fills_the_whole_retention_window_by_default(tmp_path, raw_records, no_sleep):
    # No backfill_days to choose: a store with no completed run gets the full year on its own.
    res = run(cfg(), str(tmp_path), opener=FakeAPI(raw_records), sleep=no_sleep, today=TODAY)
    assert res.since == "2025-10-21"
    assert load(tmp_path / "documents.json")["complete_since"] == "2025-10-21"
    assert load_config(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")).first_run_lookback_days == 365


@pytest.mark.parametrize("repo,site", [
    ("Someone/reg-watch", "https://someone.github.io/reg-watch/"),
    ("someone/someone.github.io", "https://someone.github.io/"),
])
def test_placeholder_urls_come_from_the_actions_repository(tmp_path, monkeypatch, repo, site):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"repo_url": "{{" + "REPO_URL}}", "site_url": "{{" + "LIVE_URL}}"}))
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    c = load_config(str(p))
    assert c.repo_url == "https://github.com/" + repo and c.site_url == site
    assert "https://github.com/" + repo in c.user_agent


def test_filled_urls_are_kept_and_bad_repository_values_ignored(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"repo_url": "https://github.com/a/b", "site_url": "https://x.example/"}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "c/d")
    c = load_config(str(p))
    assert c.repo_url == "https://github.com/a/b" and c.site_url == "https://x.example/"
    p.write_text(json.dumps({"site_url": "{{" + "LIVE_URL}}"}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "../evil;rm")
    assert "{{" in load_config(str(p)).site_url
