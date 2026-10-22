"""Wrap web/app.html into docs/index.html as a full HTML document for GitHub Pages.

Differences from app.html (the standalone version, which never touches the network):
- a <script id="loader"> defines window.regWatchLoad(), which reads data/documents.json from the
  same origin. The Pages workflow publishes docs/index.html with data/ next to it, so the page
  shows the latest committed data. If the fetch fails, the page falls back to the embedded snapshot.
- a Content-Security-Policy meta tag: scripts only by SHA-256 hash, connections only to the same
  origin, styles and fonts only from the page and Google Fonts.

Run: python web/build_pages.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LOADER = """<script id="loader">
"use strict";
window.regWatchLoad = function () {
  var MAX = 60 * 1024 * 1024;
  return fetch("data/documents.json", { cache: "no-cache", credentials: "same-origin", redirect: "error" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    })
    .then(function (t) {
      if (t.length > MAX) throw new Error("data file is larger than 60 MB");
      return { data: JSON.parse(t), base: "data/" };
    });
};
</script>
"""


def _hash(script_body: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script_body.encode("utf-8")).digest()).decode() + "'"


def build() -> str:
    with open(os.path.join(HERE, "app.html"), encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"<style>.*?</style>", src, flags=re.S)
    if not m:
        raise SystemExit("app.html: <style> block not found")
    head = src[: m.end()]
    body = src[m.end():].lstrip("\n")
    marker = '<script id="core">'
    if marker not in body:
        raise SystemExit('app.html: <script id="core"> not found')
    body = body.replace(marker, LOADER + marker, 1)
    hashes = [_hash(s) for s in re.findall(r"<script id=\"(?:loader|core|ui)\">(.*?)</script>", body, flags=re.S)]
    if len(hashes) != 3:
        raise SystemExit("expected loader, core and ui scripts")
    csp = ("default-src 'none'; "
           f"script-src {' '.join(hashes)}; "
           "connect-src 'self'; "
           "style-src 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src https://fonts.gstatic.com; "
           "img-src 'self'; base-uri 'none'; form-action 'none'")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="description" content="Reg Watch: daily Federal Register rules, proposed rules and notices for chosen agencies, with comment deadlines.">\n'
        '<link rel="alternate" type="application/atom+xml" title="Reg Watch: all agencies" href="data/feed.xml">\n'
        f"{head}\n</head>\n<body>\n{body}</body>\n</html>\n"
    )


def main() -> None:
    out_dir = os.path.join(ROOT, "docs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build())
    print("wrote docs/index.html")


if __name__ == "__main__":
    main()
