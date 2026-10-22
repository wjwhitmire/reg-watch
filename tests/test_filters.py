import json
import os

import pytest

from reg_watch.filters import add_days, closing_soon, csv_cell, days_between, filter_documents, fold, parse_query, to_csv
from reg_watch.schema import (
    MalformedRecord, clean_date, clean_text, dedupe, merge_records, normalize_record, normalize_type, safe_url,
    validate_document,
)

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(scope="module")
def docs():
    with open(os.path.join(HERE, "fixtures", "expected_documents.json"), encoding="utf-8") as fh:
        return json.load(fh)


def nums(res):
    return set(res["numbers"])


# ------------------------------------------------------------ normalization

def test_clean_text_strips_gpo_markup_and_controls():
    assert clean_text("PM<INF>10</INF> and SO<INF>2</INF>", 100) == "PM10 and SO2"
    assert clean_text("Juane[ntilde]o Band", 100) == "Juaneño Band"
    assert clean_text("10 ([mu]m)", 100) == "10 (µm)"
    assert clean_text("Set[hyphen]aside", 100) == "Set-aside"
    assert clean_text("[unknownentity] stays", 100) == "[unknownentity] stays"
    assert clean_text("a\x00b\x1fc​d", 100) == "abc​d"
    assert clean_text("  a \n\t b  ", 100) == "a b"
    assert clean_text("x" * 50, 10) == "xxxxxxx..."
    assert clean_text("   ", 10) is None
    assert clean_text(5, 10) is None
    # Non-GPO angle brackets are text, kept as text (the page renders with textContent).
    assert clean_text("<script>alert(1)</script>", 100) == "<script>alert(1)</script>"


def test_clean_date():
    assert clean_date("2026-10-21") == "2026-10-21"
    for bad in ("2026-02-30", "2026-13-01", "26-10-21", "2026-10-21T00:00", None, 20261021, "1800-01-01"):
        assert clean_date(bad) is None


def test_safe_url():
    ok = "https://www.federalregister.gov/documents/2026/10/19/2026-21377/x"
    assert safe_url(ok) == ok
    assert safe_url("https://www.govinfo.gov/content/pkg/FR-2026-10-19/pdf/2026-21377.pdf")
    for bad in ("javascript:alert(1)", "http://www.federalregister.gov/x", "https://www.federalregister.gov.evil.com/x",
                "https://evil.com/https://www.federalregister.gov/", 'https://www.federalregister.gov/x"onmouseover=1',
                "https://www.federalregister.gov/a b", "//www.federalregister.gov/x", None, 7):
        assert safe_url(bad) is None, bad


def test_normalize_type():
    assert normalize_type("Proposed Rule") == "Proposed Rule"
    assert normalize_type("PRORULE") == "Proposed Rule"
    assert normalize_type("presdocu") == "Presidential Document"
    assert normalize_type("Uncategorized Document") == "Other"
    assert normalize_type(None) == "Other"


def test_normalize_record_minimal_and_watch():
    raw = {"document_number": "2026-00001", "title": "T", "publication_date": "2026-10-01",
           "agencies": [{"name": "A", "slug": "interior-department"}, {"raw_name": "B BUREAU", "slug": "land-management-bureau"},
                        {"name": "A", "slug": "interior-department"}, "junk", {"slug": "BAD SLUG"}]}
    d = normalize_record(raw, {"interior-department": "Interior", "land-management-bureau": "BLM"}, "BLM")
    assert d["watch"] == ["Interior", "BLM"]
    assert d["agencies"] == [{"name": "A", "slug": "interior-department"}, {"name": "B BUREAU", "slug": "land-management-bureau"}]
    assert d["type"] == "Other" and d["abstract"] is None and d["docket_ids"] == [] and d["significant"] is None
    assert validate_document(d)


def test_normalize_record_rejects():
    with pytest.raises(MalformedRecord, match="not a JSON object"):
        normalize_record([1])
    with pytest.raises(MalformedRecord, match="document_number"):
        normalize_record({"document_number": "../../etc", "title": "x", "publication_date": "2026-01-01"})
    with pytest.raises(MalformedRecord, match="missing title"):
        normalize_record({"document_number": "2026-1", "title": None, "publication_date": "2026-01-01"})


def test_docket_list_cleaning():
    raw = {"document_number": "2026-00002", "title": "T", "publication_date": "2026-10-01",
           "docket_ids": ["A", "A", 5, None, " B ", "x" * 400] + ["n%d" % i for i in range(100)]}
    d = normalize_record(raw)
    assert d["docket_ids"][:3] == ["A", "B", "x" * 297 + "..."]
    assert len(d["docket_ids"]) <= 50


def test_significant_only_bool():
    for v, want in ((True, True), (False, False), ("true", None), (1, None), (None, None)):
        d = normalize_record({"document_number": "2026-00003", "title": "T", "publication_date": "2026-10-01", "significant": v})
        assert d["significant"] is want


def test_merge_and_dedupe_order():
    a = normalize_record({"document_number": "2026-1", "title": "Old", "publication_date": "2026-10-01",
                          "comments_close_on": "2026-11-01"}, query_key="EPA")
    b = normalize_record({"document_number": "2026-1", "title": "New", "publication_date": "2026-10-01"}, query_key="SEC")
    m = merge_records(a, b)
    assert m["title"] == "New" and m["comments_close_on"] == "2026-11-01" and m["watch"] == ["EPA", "SEC"]
    c = normalize_record({"document_number": "2026-2", "title": "C", "publication_date": "2026-10-05"})
    out = dedupe([a, c, b])
    assert [d["document_number"] for d in out] == ["2026-2", "2026-1"]


def test_validate_document_rejects_drift(docs):
    d = dict(docs[0])
    assert validate_document(d)
    assert not validate_document({**d, "extra": 1})
    e = dict(d); del e["title"]
    assert not validate_document(e)
    assert not validate_document({**d, "type": "Memo"})
    assert not validate_document({**d, "watch": "EPA"})
    assert not validate_document({**d, "significant": "yes"})
    assert not validate_document({**d, "comments_close_on": "soon"})


# ------------------------------------------------------------ filtering

def test_fold_is_ascii_only():
    assert fold("ABC xyz Éß") == "abc xyz Éß"


def test_parse_query():
    assert parse_query('pipeline -"special permits" "Gas  Act" -notice') == {
        "include": ["pipeline", "gas act"], "exclude": ["special permits", "notice"]}
    assert parse_query("-") == {"include": ["-"], "exclude": []}
    assert parse_query(None) == {"include": [], "exclude": []}
    assert parse_query('"unclosed phrase') == {"include": ['"unclosed', "phrase"], "exclude": []}


def test_filter_by_watch_type_keyword(docs):
    assert len(filter_documents(docs, {})["numbers"]) == 63
    epa = filter_documents(docs, {"watch": "EPA"})
    assert epa["count"] == 10
    assert nums(filter_documents(docs, {"watch": "EPA", "type": "Rule"})) == {"2026-21268", "2026-17579"}
    assert nums(filter_documents(docs, {"q": "pm10"})) == {"2026-21289", "2026-21288", "2026-21287"}
    assert nums(filter_documents(docs, {"q": "swap -casino"})) == {"2026-21377"}
    assert nums(filter_documents(docs, {"q": "BSEE-2026-0067"})) == {"2026-21182"}
    assert nums(filter_documents(docs, {"q": "juaneño"})) == {"2026-21476"}
    assert filter_documents(docs, {"q": "special permits"})["count"] == 6


def test_filter_dates_inclusive(docs):
    r = filter_documents(docs, {"from": "2026-10-21", "to": "2026-10-21"})
    assert r["count"] > 0
    assert all(d["publication_date"] == "2026-10-21" for d in docs if d["document_number"] in r["numbers"])
    assert filter_documents(docs, {"from": "2026-10-21", "to": "2026-10-20"})["count"] == 0
    assert filter_documents(docs, {"from": "garbage"})["count"] == 63


def test_closing_soon_view(docs):
    r = filter_documents(docs, {"closing": True, "ref": "2026-10-21", "window": 14, "sort": "closing"})
    # Comment deadlines from 2026-10-21 through 2026-11-04 inclusive.
    assert r["numbers"] == ["2026-19293", "2026-21307"]
    close = {d["document_number"]: d["comments_close_on"] for d in docs}
    assert [close[n] for n in r["numbers"]] == ["2026-10-22", "2026-11-03"]
    r30 = filter_documents(docs, {"closing": True, "ref": "2026-10-21", "window": 30, "sort": "closing"})
    dates = [close[n] for n in r30["numbers"]]
    assert dates == sorted(dates) and dates[-1] <= "2026-11-20"
    assert filter_documents(docs, {"closing": True})["count"] == 0  # no reference date, no closing view
    assert r30["closing_count"] == r30["count"]


def test_sort_closing_puts_undated_last(docs):
    r = filter_documents(docs, {"sort": "closing"})
    close = {d["document_number"]: d["comments_close_on"] for d in docs}
    seq = [close[n] for n in r["numbers"]]
    first_none = seq.index(None)
    assert all(v is None for v in seq[first_none:])
    assert seq[:first_none] == sorted(seq[:first_none])


def test_facets(docs):
    r = filter_documents(docs, {"watch": "SEC"})
    assert r["by_watch"]["SEC"] == r["count"] == 15
    assert r["by_watch"]["CFTC"] == 2
    assert sum(r["by_type"].values()) == r["count"]


def test_date_helpers():
    assert days_between("2026-10-21", "2026-11-04") == 14
    assert add_days("2028-02-28", 1) == "2028-02-29"
    assert closing_soon({"comments_close_on": "2026-11-04"}, "2026-10-21", 14)
    assert not closing_soon({"comments_close_on": "2026-11-05"}, "2026-10-21", 14)
    assert not closing_soon({"comments_close_on": "2026-10-20"}, "2026-10-21", 14)
    assert not closing_soon({"comments_close_on": None}, "2026-10-21", 14)


# ------------------------------------------------------------ CSV

def test_csv_formula_injection_and_quoting():
    assert csv_cell("=1+2") == "'=1+2"
    assert csv_cell("+1") == "'+1" and csv_cell("-1") == "'-1" and csv_cell("@x") == "'@x"
    assert csv_cell("\tx") == "'\tx"
    assert csv_cell('a,"b"') == '"a,""b"""'
    assert csv_cell(["a", "b"]) == "a; b"
    assert csv_cell(None) == ""
    assert csv_cell("=a,b") == "\"'=a,b\""


def test_to_csv(docs):
    text = to_csv(docs[:2])
    lines = text.strip().split("\n")
    assert lines[0] == "document_number,publication_date,type,watch,title,comments_close_on,effective_on,docket_ids,html_url"
    assert len(lines) == 3
