"""Command line: `reg-watch collect` (the daily job) and `reg-watch query` (search the stored data).

Examples:
    reg-watch collect --config config.json --data data
    reg-watch collect --backfill-days 365
    reg-watch query --data data --watch EPA --type "Proposed Rule" --closing
    reg-watch query --data data -q 'pipeline -"special permits"' --csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

from . import __version__
from .collect import CollectError, load_config, read_documents, run
from .filters import filter_documents, to_csv


def _date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a date in YYYY-MM-DD form") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reg-watch", description="Track Federal Register rulemaking for chosen agencies.")
    p.add_argument("--version", action="version", version=f"reg-watch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="fetch new documents and rewrite data/*.json and Atom feeds")
    c.add_argument("--config", default="config.json", help="config file (JSON). Default: config.json; defaults are used if it is missing")
    c.add_argument("--data", default="data", help="output directory. Default: data")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--since", type=_date, help="start date (YYYY-MM-DD), overriding the incremental start")
    g.add_argument("--backfill-days", type=int, help="fetch this many days back (max retention_days)")
    c.add_argument("--today", type=_date, help=argparse.SUPPRESS)
    c.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    c.add_argument("-v", "--verbose", action="store_true")

    q = sub.add_parser("query", help="filter stored documents (offline)")
    q.add_argument("--data", default="data")
    q.add_argument("--watch", default="", help="agency key from config, e.g. EPA")
    q.add_argument("--type", default="", help='Rule, "Proposed Rule", Notice or "Presidential Document"')
    q.add_argument("-q", "--query", default="", help='keywords; "quoted phrase"; -exclude')
    q.add_argument("--from", dest="date_from", type=_date)
    q.add_argument("--to", dest="date_to", type=_date)
    q.add_argument("--closing", action="store_true", help="comment period closes within --window days of --ref")
    q.add_argument("--window", type=int, default=14)
    q.add_argument("--ref", type=_date, help="reference date for --closing. Default: today (UTC)")
    q.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(args, "verbose", False) or args.cmd == "collect" else logging.WARNING,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        if args.cmd == "collect":
            cfg = load_config(args.config if os.path.exists(args.config) else None)
            if args.backfill_days is not None and not 1 <= args.backfill_days <= cfg.retention_days:
                raise CollectError(f"--backfill-days must be from 1 to retention_days ({cfg.retention_days})")
            today = dt.date.fromisoformat(args.today) if args.today else None
            res = run(cfg, args.data, today=today, since=args.since, backfill_days=args.backfill_days, dry_run=args.dry_run)
            print(f"window {res.since}..{res.until}: {len(res.new)} new, {res.total} stored, "
                  f"{res.stats.skipped} skipped, {res.stats.requests} requests; changed: {', '.join(res.changed_files) or 'nothing'}")
            return 0
        docs = read_documents(os.path.join(args.data, "documents.json"))
        ref = args.ref or dt.datetime.now(dt.timezone.utc).date().isoformat()
        res = filter_documents(docs, {"watch": args.watch, "type": args.type, "q": args.query,
                                      "from": args.date_from or "", "to": args.date_to or "",
                                      "closing": args.closing, "ref": ref, "window": args.window,
                                      "sort": "closing" if args.closing else "newest"})
        by_num = {d["document_number"]: d for d in docs}
        hits = [by_num[n] for n in res["numbers"]]
        if args.csv:
            sys.stdout.write(to_csv(hits))
        else:
            for d in hits:
                close = f"  comments close {d['comments_close_on']}" if d["comments_close_on"] else ""
                print(f"{d['publication_date']}  {d['document_number']:<14} {d['type']:<14} {','.join(d['watch'])}{close}")
                print(f"    {d['title'][:150]}")
                if d["html_url"]:
                    print(f"    {d['html_url']}")
            print(f"{res['count']} document(s)", file=sys.stderr)
        return 0
    except (CollectError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
