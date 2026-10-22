"""Atom 1.0 feeds (RFC 4287) built with xml.etree, so every value is XML-escaped by the serializer."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .collect import Config

ATOM_NS = "http://www.w3.org/2005/Atom"
FR_SHORT = "https://www.federalregister.gov/d/"
# Characters XML 1.0 cannot carry. Stored documents are normally clean already; this guards hand-edited stores.
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _x(s: str) -> str:
    return _XML_BAD.sub("", s)


def _rfc3339(date: str) -> str:
    return f"{date}T00:00:00Z"


def entry_id(doc: dict) -> str:
    """Stable entry id. The Federal Register short link is permanent per document number."""
    return FR_SHORT + doc["document_number"]


def entry_summary(doc: dict) -> str:
    parts = [doc["type"]]
    names = [a["name"] for a in doc["agencies"] if a.get("name")]
    if names:
        parts.append("; ".join(names))
    parts.append(f"Published {doc['publication_date']}")
    if doc.get("comments_close_on"):
        parts.append(f"Comments close {doc['comments_close_on']}")
    if doc.get("effective_on"):
        parts.append(f"Effective {doc['effective_on']}")
    if doc.get("docket_ids"):
        parts.append("Docket " + ", ".join(doc["docket_ids"][:5]))
    head = ". ".join(parts) + "."
    return head + ("\n\n" + doc["abstract"] if doc.get("abstract") else "")


def build_feed(docs: list[dict], title: str, feed_id: str, updated: str, self_href: str | None,
               site_href: str | None, max_entries: int) -> bytes:
    ET.register_namespace("", ATOM_NS)
    q = lambda t: f"{{{ATOM_NS}}}{t}"  # noqa: E731
    feed = ET.Element(q("feed"))
    ET.SubElement(feed, q("title")).text = _x(title)
    ET.SubElement(feed, q("id")).text = feed_id
    ET.SubElement(feed, q("updated")).text = _rfc3339(updated)
    author = ET.SubElement(feed, q("author"))
    ET.SubElement(author, q("name")).text = "Federal Register (via Reg Watch)"
    ET.SubElement(feed, q("generator")).text = "Reg Watch"
    ET.SubElement(feed, q("rights")).text = "Federal Register content is a work of the US government and is in the public domain."
    if self_href:
        ET.SubElement(feed, q("link"), rel="self", href=self_href, type="application/atom+xml")
    if site_href:
        ET.SubElement(feed, q("link"), rel="alternate", href=site_href, type="text/html")
    for d in docs[:max_entries]:
        e = ET.SubElement(feed, q("entry"))
        ET.SubElement(e, q("title")).text = _x(d["title"])
        ET.SubElement(e, q("id")).text = entry_id(d)
        ET.SubElement(e, q("updated")).text = _rfc3339(d["publication_date"])
        ET.SubElement(e, q("published")).text = _rfc3339(d["publication_date"])
        # RFC 4287 4.1.1: an entry without content needs an alternate link. The short link always works.
        ET.SubElement(e, q("link"), rel="alternate", href=d.get("html_url") or entry_id(d), type="text/html")
        if d.get("pdf_url"):
            ET.SubElement(e, q("link"), rel="enclosure", href=d["pdf_url"], type="application/pdf")
        ET.SubElement(e, q("category"), term=d["type"])
        for w in d.get("watch") or []:
            ET.SubElement(e, q("category"), term=w)
        ET.SubElement(e, q("summary"), type="text").text = _x(entry_summary(d))
    ET.indent(feed, space=" ")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="utf-8", xml_declaration=False) + b"\n"


def _is_url(s: str) -> bool:
    return s.startswith("https://") and "{{" not in s


def write_feeds(data_dir: str, docs: list[dict], cfg: "Config", updated: str) -> list[str]:
    """Write feed.xml and feed-<key>.xml. Returns the file names that changed."""
    base = cfg.site_url.rstrip("/") if _is_url(cfg.site_url) else None
    data_base = f"{base}/data" if base else None
    changed = []
    targets = [("feed.xml", "Reg Watch: all watched agencies", "urn:reg-watch:feed:all", docs)]
    for a in cfg.agencies:
        subset = [d for d in docs if a.key in (d.get("watch") or [])]
        targets.append((f"feed-{a.key.lower()}.xml", f"Reg Watch: {a.label}", f"urn:reg-watch:feed:{a.key.lower()}", subset))
    for name, title, fid, subset in targets:
        body = build_feed(subset, title, fid, updated,
                          f"{data_base}/{name}" if data_base else None, f"{base}/" if base else None, cfg.feed_entries)
        path = os.path.join(data_dir, name)
        old = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                old = fh.read()
        if old != body:
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
            changed.append(name)
    return changed
