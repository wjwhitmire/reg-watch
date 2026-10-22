"""Embed a data snapshot into web/app.html (<script id="snapshot" type="application/json">).

The published demo page cannot fetch at runtime, so it carries a frozen copy of real data.
Run: python web/embed_snapshot.py [path/to/documents.json]   (default: data/documents.json)
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAX_BYTES = 3 * 1024 * 1024
SNAP_RE = re.compile(r'(<script id="snapshot" type="application/json">)(.*?)(</script>)', re.S)


def script_safe_json(obj) -> str:
    """JSON that cannot close the <script> element or open an HTML comment."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def embed(app_html: str, data: dict) -> str:
    snap = {k: data.get(k) for k in ("schema_version", "updated", "retention_days", "agencies", "count", "documents")}
    body = script_safe_json(snap)
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise SystemExit(f"snapshot is {len(body) // 1024} KB; keep it under {MAX_BYTES // 1024} KB (pass a smaller documents.json)")
    if not SNAP_RE.search(app_html):
        raise SystemExit('app.html: <script id="snapshot" type="application/json"> block not found')
    return SNAP_RE.sub(lambda m: m.group(1) + body + m.group(3), app_html, count=1)


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "documents.json")
    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    path = os.path.join(HERE, "app.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    out = embed(html, data)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"embedded {len(data.get('documents', []))} documents (updated {data.get('updated')}) into web/app.html")


if __name__ == "__main__":
    main()
