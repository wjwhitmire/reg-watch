"""Generate reference outputs from the Python library.

Writes:
  tests/vectors.json                     filter/search/CSV cases checked against the JS core by web/parity.test.mjs
  tests/fixtures/expected_documents.json the normalized form of the recorded API fixtures (pinned by test_snapshot_matches_fixtures)
  data/ seed (only with --seed-data)     documents.json, latest.json and feeds built from the fixtures

Run: python tests/make_vectors.py [--seed-data]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

from conftest import FakeAPI, all_raw_records  # noqa: E402
from reg_watch.collect import DEFAULT_AGENCIES, Config, _dump, run  # noqa: E402
from reg_watch.filters import add_days, coerce_dataset, csv_cell, filter_documents, is_iso, parse_query, safe_url, to_csv  # noqa: E402
from reg_watch.schema import dedupe, normalize_record  # noqa: E402

REF = "2026-10-21"


def dump(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def normalized_fixtures() -> list[dict]:
    watch = {a.slug: a.key for a in DEFAULT_AGENCIES}
    return dedupe(normalize_record(r, watch) for r in all_raw_records())


def hostile_docs() -> list[dict]:
    base = {
        "agencies": [{"name": "Environmental Protection Agency", "slug": "environmental-protection-agency"}],
        "watch": ["EPA"], "effective_on": None, "html_url": None, "pdf_url": None,
        "regulation_id_numbers": [], "significant": None,
    }
    return [
        dict(base, document_number="2026-90001", title='=HYPERLINK("http://x","click") <script>alert(1)</script>',
             type="Rule", publication_date="2026-10-20", comments_close_on="2026-10-21",
             abstract="+cmd|' /C calc'!A0, \"quoted\"\nnewline", docket_ids=["@SUM(1+1)", "-2+3"]),
        dict(base, document_number="2026-90002", title="Émissions STANDARDS café ß", type="Other",
             publication_date="2026-01-05", comments_close_on="2026-02-30x", abstract=None, docket_ids=[], watch=[]),
    ]


QUERIES = [
    ("all", {}),
    ("watch_epa", {"watch": "EPA"}),
    ("watch_interior", {"watch": "Interior"}),
    ("watch_bsee", {"watch": "BSEE"}),
    ("watch_unknown", {"watch": "NRC"}),
    ("type_proposed", {"type": "Proposed Rule"}),
    ("type_rule_sec", {"type": "Rule", "watch": "SEC"}),
    ("type_presidential", {"type": "Presidential Document"}),
    ("kw_single", {"q": "pinal"}),
    ("kw_case", {"q": "SWAP"}),
    ("kw_two_terms", {"q": "information collection"}),
    ("kw_phrase", {"q": '"special permits"'}),
    ("kw_exclude", {"q": "hazardous -modification"}),
    ("kw_exclude_phrase", {"q": 'agency -"office of management"'}),
    ("kw_docket", {"q": "bsee-2026-0067"}),
    ("kw_rin", {"q": "3038-AF8"}),
    ("kw_docnum", {"q": "C1-2026"}),
    ("kw_agency_name", {"q": "indian affairs"}),
    ("kw_unicode", {"q": "ÉMISSIONS"}),
    ("kw_unicode_lower", {"q": "émissions"}),
    ("kw_markup", {"q": "pm10"}),
    ("kw_entity", {"q": "juaneño"}),
    ("kw_only_minus", {"q": "-"}),
    ("kw_empty_quotes", {"q": '"" -""'}),
    ("kw_whitespace", {"q": "  \t pipeline \n  safety  "}),
    ("kw_unclosed_quote", {"q": '"crypto asset'}),
    ("kw_no_match", {"q": "zzzz-not-present"}),
    ("kw_script", {"q": "<script>"}),
    ("date_from", {"from": "2026-10-19"}),
    ("date_to", {"to": "2026-08-06"}),
    ("date_range", {"from": "2026-08-01", "to": "2026-08-31"}),
    ("date_bad", {"from": "10/19/2026", "to": "nope"}),
    ("date_inverted", {"from": "2026-10-20", "to": "2026-10-01"}),
    ("closing_14", {"closing": True, "ref": REF, "window": 14}),
    ("closing_30_sorted", {"closing": True, "ref": REF, "window": 30, "sort": "closing"}),
    ("closing_0", {"closing": True, "ref": "2026-11-18", "window": 0}),
    ("closing_no_ref", {"closing": True}),
    ("closing_bad_window", {"closing": True, "ref": REF, "window": "x"}),
    ("closing_neg_window", {"closing": True, "ref": REF, "window": -5}),
    ("closing_huge_window", {"closing": True, "ref": REF, "window": 10 ** 6}),
    ("closing_float_window", {"closing": True, "ref": REF, "window": 14.9}),
    ("closing_truthy_not_true", {"closing": 1, "ref": REF}),
    ("sort_closing", {"sort": "closing", "ref": REF}),
    ("combined", {"watch": "EPA", "type": "Proposed Rule", "q": "arizona -fugitive", "from": "2026-10-01", "closing": True, "ref": "2026-11-10", "window": 14}),
]

PARSE = ["", "a", "A b", '"x y" z', '-a -"b c" d', '"unclosed', "- -- -x", "a a A", '  "  spaced   out  "  ', "ÉTÉ Café", 'x"y"z', "-\"\"", 'say "hi"there']

CELLS = [None, "", "plain", "=1+2", "+1", "-1", "@x", "\tx", "\rx", 'a,b', 'say "hi"', "line\nbreak", ["a", "=b"], 42, "'already"]

URLS = ["https://www.federalregister.gov/d/2026-1", "https://www.govinfo.gov/x.pdf", "http://www.federalregister.gov/x",
        "https://www.federalregister.gov.evil.com/", "javascript:alert(1)", 'https://www.federalregister.gov/"x',
        "https://www.federalregister.gov/a b", "https://www.federalregister.gov/\u00e9", "https://govinfo.gov/a?b=c&d=e", None, 5]

ISO = ["2026-10-21", "2026-02-29", "2028-02-29", "2026-13-01", "2026-1-01", "2026-10-21\n", " 2026-10-21", "\u0662026-10-21", "0000-01-01", "", None, 20261021]

DATASETS = [
    ("not_object", [1, 2]),
    ("no_documents", {"documents": "x"}),
    ("mixed", {"updated": "2026-10-21", "agencies": [{"key": "EPA", "label": "Environmental Protection Agency"}, {"key": ""}, "x", {"key": "SEC"}],
               "documents": [
                   {"document_number": "2026-1", "title": "Ok <b>", "publication_date": "2026-10-01", "type": "Rule",
                    "html_url": "javascript:alert(1)", "pdf_url": "https://www.govinfo.gov/a.pdf", "watch": ["EPA", 5],
                    "agencies": [{"name": "EPA", "slug": "environmental-protection-agency"}, {"name": 5}, "x"],
                    "comments_close_on": "2026-02-30", "effective_on": "2026-11-01", "abstract": 7,
                    "docket_ids": ["A", None], "regulation_id_numbers": "x", "significant": "yes", "extra": 1},
                   {"document_number": "2026-1", "title": "Duplicate", "publication_date": "2026-10-01"},
                   {"document_number": "2026-2", "title": "", "publication_date": "2026-10-01"},
                   {"document_number": "2026-3", "title": "Bad date", "publication_date": "2026-10-1"},
                   {"document_number": 4, "title": "x", "publication_date": "2026-10-01"},
                   None,
                   {"document_number": "2026-5", "title": "Memo", "publication_date": "2026-10-02", "type": "Memo", "significant": True},
               ]}),
]

DATES = [("2026-10-21", 0), ("2026-10-21", 14), ("2026-12-25", 7), ("2028-02-28", 1), ("2027-02-28", 1), ("2026-01-31", 30)]


def dataset_vector(name, obj):
    try:
        return {"name": name, "input": obj, "out": coerce_dataset(obj)}
    except ValueError as e:
        return {"name": name, "input": obj, "error": str(e)}


def build_vectors() -> dict:
    docs = normalized_fixtures() + hostile_docs()
    by_num = {d["document_number"]: d for d in docs}
    cases = []
    for name, q in QUERIES:
        res = filter_documents(docs, q)
        csv = to_csv([by_num[n] for n in res["numbers"]])
        cases.append({"name": name, "query": q, "result": res, "csv": csv})
    return {
        "generated_by": "tests/make_vectors.py",
        "documents": docs,
        "cases": cases,
        "parse": [{"q": q, "result": parse_query(q)} for q in PARSE],
        "cells": [{"v": v, "out": csv_cell(v)} for v in CELLS],
        "add_days": [{"a": a, "n": n, "out": add_days(a, n)} for a, n in DATES],
        "urls": [{"v": u, "out": safe_url(u)} for u in URLS],
        "iso": [{"v": v, "out": is_iso(v)} for v in ISO],
        "datasets": [dataset_vector(n, o) for n, o in DATASETS],
    }


def seed_data() -> None:
    """Build data/ from the fixtures. complete_since is cleared so the first live run does not trust it."""
    cfg = Config(agencies=list(DEFAULT_AGENCIES))
    cfg.request_delay_s = 0
    data_dir = os.path.join(ROOT, "data")
    for n in os.listdir(data_dir) if os.path.isdir(data_dir) else []:
        if n.endswith((".json", ".xml")):
            os.remove(os.path.join(data_dir, n))
    run(cfg, data_dir, opener=FakeAPI(all_raw_records()), sleep=lambda s: None, today=dt.date.fromisoformat(REF), backfill_days=365)
    p = os.path.join(data_dir, "documents.json")
    with open(p, encoding="utf-8") as fh:
        top = json.load(fh)
    top["complete_since"] = None  # a seed is a sample, so the first live run must not trust it as complete
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_dump(top))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-data", action="store_true")
    args = ap.parse_args()
    dump(os.path.join(HERE, "fixtures", "expected_documents.json"), normalized_fixtures())
    v = build_vectors()
    dump(os.path.join(HERE, "vectors.json"), v)
    print(f"wrote tests/vectors.json: {len(v['cases'])} cases, {len(v['parse'])} parse, {len(v['cells'])} cells, {len(v['documents'])} documents")
    if args.seed_data:
        seed_data()
        print("wrote data/ seed snapshot")


if __name__ == "__main__":
    main()
