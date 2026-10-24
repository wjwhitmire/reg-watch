"""What the Pages workflow publishes, tested in a browser.

The release fill is applied to the tracked files, the collect workflow's command runs
against the recorded API fixtures, the "Assemble site" step of pages.yml runs as written,
and the result is served under /reg-watch/ and opened in headless Chromium.
Skips when Playwright or Chromium is missing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from pages_site import install_routes, release_tree, serve, watch

sync_api = pytest.importorskip("playwright.sync_api")
NAME = "reg-watch"
LIVE = "https://wjwhitmire.github.io/reg-watch/"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tree = release_tree(tmp_path_factory.mktemp("release"), NAME)
    # The collect workflow's command, run from the release tree with the API swapped for the fixtures.
    code = (
        "import sys, datetime as dt\n"
        f"sys.path[:0] = [{str(tree / 'src')!r}, {str(tree / 'tests')!r}]\n"
        "from conftest import FakeAPI, all_raw_records\n"
        "import reg_watch.collect as C\n"
        "cfg = C.load_config('config.json')\n"
        f"assert cfg.site_url == {LIVE!r}, cfg.site_url\n"
        "res = C.run(cfg, 'data', opener=FakeAPI(all_raw_records()), sleep=lambda s: None, today=dt.date(2026, 10, 23))\n"
        "assert res.total == 63, res.total\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_REPOSITORY"}
    subprocess.run([sys.executable, "-c", code], cwd=tree, check=True, env=env)
    wf = (tree / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    step = re.search(r"- name: Assemble site\n\s+run: \|\n((?:\s{10}.*\n)+)", wf).group(1)
    subprocess.run(step, shell=True, cwd=tree, check=True)
    with serve(tree / "_site", NAME) as url:
        yield url, tree


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        b.close()


def test_feeds_carry_the_live_url(site):
    _, tree = site
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feeds = sorted((tree / "_site" / "data").glob("feed*.xml"))
    assert len(feeds) == 9
    for f in feeds:
        root = ET.parse(f).getroot()
        assert [l.get("href") for l in root.findall("a:link", ns) if l.get("rel") == "self"] == [LIVE + "data/" + f.name]


def test_published_page_reads_live_data(site, browser):
    url, _ = site
    errors: list = []
    ctx = browser.new_context(viewport={"width": 400, "height": 800}, has_touch=True, is_mobile=True)
    install_routes(ctx, url.split("/" + NAME)[0], errors)
    page = ctx.new_page()
    watch(page, errors)
    page.goto(url)
    page.wait_for_function("() => /Updated|Could not|Sample/.test(document.getElementById('data-note').textContent)")
    assert "Updated 2026-10-23" in page.inner_text("#data-note")
    assert page.inner_text("#t-count") == "63"
    page.fill("#f-ref", "2026-10-21")
    page.dispatch_event("#f-ref", "change")
    page.check("#v-closing")
    assert int(page.inner_text("#t-count")) > 0
    feeds = page.locator("#feeds a").evaluate_all("as => as.map(a => a.href)")
    assert len(feeds) == 9 and all(page.request.get(u).status == 200 for u in feeds)
    small = page.evaluate("""() => Array.from(document.querySelectorAll('.doc-links a, .feeds a'))
        .filter(a => { const r = a.getBoundingClientRect(); return r.height < 44 || r.width < 44; }).length""")
    assert small == 0
    assert page.evaluate("() => window.__csp") == []
    assert page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    assert errors == []
    ctx.close()
