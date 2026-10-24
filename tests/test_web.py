"""Static checks on the web page and its build scripts (the security contract in SECURITY.md)."""

import base64
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))

import build_pages  # noqa: E402
import embed_snapshot  # noqa: E402


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


APP = read("web", "app.html")


def scripts(html):
    return dict(re.findall(r'<script id="([a-z]+)"[^>]*>(.*?)</script>', html, flags=re.S))


def test_standalone_contract_head():
    lines = APP.split("\n")
    assert lines[0] == "<title>Reg Watch</title>"
    assert lines[1].startswith('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Old+Standard+TT')
    assert lines[2] == "<style>"
    for tag in (r"<!doctype", r"<html[\s>]", r"<head[\s>]", r"<body[\s>]"):
        assert not re.search(tag, APP, flags=re.I), tag


def test_no_network_or_dangerous_apis_in_app():
    s = scripts(APP)
    assert set(s) == {"snapshot", "core", "ui"}
    code = s["core"] + s["ui"]
    for bad in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "eval(", "new Function",
                "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "importScripts", "window.open",
                "alert(", "confirm(", "prompt(", "window.print", "localStorage", "sessionStorage", "indexedDB"):
        assert bad not in code, bad
    assert not re.search(r"\son[a-z]+\s*=", re.sub(r"<script.*?</script>", "", APP, flags=re.S)), "inline handler"
    assert "<a download" not in APP
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", re.sub(r'<script id="snapshot".*?</script>', "", APP, flags=re.S)))
    allowed = ("https://fonts.googleapis.com/", "https://www.federalregister.gov/", "https://federalregister.gov/",
               "https://www.govinfo.gov/", "https://govinfo.gov/")
    assert all(u.startswith(allowed) for u in urls), urls


def test_core_is_pure():
    core = scripts(APP)["core"]
    for bad in ("document.", "window.", "navigator.", "setTimeout", "Date(", "globalThis"):
        assert bad not in core, bad


def test_controls_have_labels():
    ids = re.findall(r'<(?:input|select|textarea)[^>]*\bid="([^"]+)"', APP)
    assert ids
    for i in ids:
        assert f'for="{i}"' in APP or i.startswith("v-"), i  # radios are wrapped in their <label for=...>
    for i in ("v-all", "v-closing"):
        assert f'<label for="{i}">' in APP


def test_snapshot_embedded_and_labeled():
    snap = json.loads(scripts(APP)["snapshot"])
    assert snap["updated"] == "2026-10-21"
    assert len(snap["documents"]) == 63
    assert "Snapshot as of" in APP and "The GitHub version updates daily." in APP


def test_embed_snapshot_cannot_break_out():
    data = {"updated": "2026-10-21", "documents": [{"document_number": "2026-1", "title": "</script><script>alert(1)</script><!--"}]}
    out = embed_snapshot.embed(APP, data)
    body = re.search(r'<script id="snapshot" type="application/json">(.*?)</script>', out, flags=re.S).group(1)
    assert "</script" not in body and "<!--" not in body
    assert json.loads(body)["documents"][0]["title"] == "</script><script>alert(1)</script><!--"
    assert out.count('<script id="ui">') == 1


def test_pages_build_matches_and_has_csp():
    built = build_pages.build()
    assert built == read("docs", "index.html"), "docs/index.html is stale: run python web/build_pages.py"
    assert built.startswith("<!doctype html>")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', built).group(1)
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    s = scripts(built)
    assert set(s) == {"snapshot", "loader", "core", "ui"}
    for name in ("loader", "core", "ui"):
        h = "'sha256-" + base64.b64encode(hashlib.sha256(s[name].encode()).digest()).decode() + "'"
        assert h in csp, name
    # The only fetch anywhere is the same-origin, relative data file.
    assert re.findall(r"fetch\(([^,]+),", built) == ['"data/documents.json"']


def test_workflow_permissions():
    ci = read(".github", "workflows", "ci.yml")
    assert "permissions:\n  contents: read" in ci and "contents: write" not in ci
    col = read(".github", "workflows", "collect.yml")
    assert "permissions:\n  contents: read" in col  # workflow default is read-only
    jobs = col.split("\njobs:\n", 1)[1]
    collect_job = jobs.split("\n  pages:\n")[0]
    assert "contents: write" in collect_job and jobs.count("contents: write") == 1
    minute, hour = re.search(r'cron: "(\d+) (\d+) \* \* \*"', col).groups()
    assert minute not in ("0", "00", "30") and 0 <= int(hour) <= 23
    assert "41898282+github-actions[bot]@users.noreply.github.com" in col
    pages = read(".github", "workflows", "pages.yml")
    assert "pages: write" in pages and "contents: write" not in pages


def test_backfill_input_is_validated_before_the_shell():
    # Review: workflow input reaches the shell only through an env var, after a digits-only check.
    import subprocess
    col = read(".github", "workflows", "collect.yml")
    assert "${{ github.event.inputs.backfill_days }}" not in col.split("env:", 1)[1].split("run: |", 1)[1]
    case = re.search(r'case "\$BACKFILL" in (.*?)\) echo', col).group(1)
    script = 'case "$BACKFILL" in ' + case + ') echo reject;; *) echo accept;; esac'
    for v, want in [("365", "accept"), ("1", "accept"), ("0", "reject"), ("1;id", "reject"), ("$(id)", "reject"),
                    ("-5", "reject"), ("3 65", "reject"), ("100000", "reject"), ("007", "reject")]:
        out = subprocess.run(["sh", "-c", script], env={"BACKFILL": v}, capture_output=True, text=True).stdout.strip()
        assert out == want, (v, out)


def test_checkouts_do_not_persist_credentials_in_read_only_jobs():
    for name in ("ci.yml", "pages.yml"):
        wf = read(".github", "workflows", name)
        assert wf.count("actions/checkout@") == wf.count("persist-credentials: false"), name


def test_no_em_dash_anywhere():
    bad = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache") and not d.endswith(".egg-info")]
        for f in files:
            p = os.path.join(base, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    if chr(0x2014) in fh.read():
                        bad.append(os.path.relpath(p, ROOT))
            except UnicodeDecodeError:
                pass
    assert bad == []


def test_no_tooling_words_in_tracked_files():
    import subprocess

    import pytest
    # Words kept out of the published repo. Stored reversed so this file does not contain them.
    words = [w[::-1] for w in ("edualc", "ciporhtna", "xobdnas")]
    pattern = re.compile(r"\b(" + "|".join(words) + r")\b", re.I)
    out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    bad = []
    for rel in out.stdout.split("\n"):
        p = os.path.join(ROOT, rel)
        if not rel or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                if pattern.search(fh.read()):
                    bad.append(rel)
        except UnicodeDecodeError:
            pass
    assert bad == []
