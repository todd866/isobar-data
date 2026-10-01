"""Run one archive pass and print a JSON report."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from isobar_data.config import ConfigError, load_config
from isobar_data.notams import import_notams
from isobar_data.notac import DEFAULT_LOCATIONS, NotacError, fetch_notams
from isobar_data.retain import ensure_root, retain
from isobar_data.scheduler import run_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill an Isobar data directory and exit.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "retain", "import-notams", "fetch-notams"))
    parser.add_argument("--data-dir", default=str(Path.home() / "Data" / "isobar"))
    parser.add_argument("--config", default="")
    parser.add_argument("--now", default="", help="UTC instant, yyyy-mm-ddTHH:MM:SSZ")
    parser.add_argument("--file", default="", help="NOTAM plaintext or canonical JSON file")
    parser.add_argument("--source", default="", help="source label stored in the published product")
    parser.add_argument("--locations", default=",".join(DEFAULT_LOCATIONS), help="NOTAC A-field locations, comma-separated ICAO codes")
    parser.add_argument("--hours", type=int, default=72, help="NOTAC forecast window, 1–168 hours")
    parser.add_argument("--max-requests", type=int, default=150, help="NOTAC request cap, 1–200 (including detail lookups)")
    parser.add_argument("--token-stdin", action="store_true", help="read NOTAC key from a pipe instead of a hidden terminal prompt")
    parser.add_argument("--dry-run", action="store_true", help="fetch and validate NOTAC data without publishing it (uses API credits)")
    parser.add_argument("--profile", choices=("default", "public-web"), default="default",
                        help="source profile for run (default or the public-web export subset)")
    args = parser.parse_args(argv)
    root = Path(args.data_dir)
    if args.command == "fetch-notams":
        try:
            if args.token_stdin:
                token = sys.stdin.readline(256).strip()
            elif sys.stdin.isatty():
                token = getpass.getpass("NOTAC API key: ")
            else:
                raise ValueError("use --token-stdin or run in a terminal for a hidden API-key prompt")
            moment = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
            result = fetch_notams(root, token, locations=tuple(code.strip().upper() for code in args.locations.split(",")),
                                  now=moment, hours=args.hours, max_requests=args.max_requests, publish=not args.dry_run)
            print(json.dumps(result, indent=2))
            return 0
        except (NotacError, OSError, ValueError, RuntimeError, EOFError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            token = ""
    if args.command == "import-notams":
        if not args.file or not args.source:
            parser.error("import-notams requires --file and --source")
        try:
            print(json.dumps(import_notams(Path(args.file), args.source, root), indent=2))
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        config = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    if args.command == "retain":
        ensure_root(root)
        moment = now or datetime.now(timezone.utc)
        summary = retain(root, moment)
        print(json.dumps({"removed": len(summary["removed"]), "obs_rows": summary["obs_rows"]}))
        return 0
    report = run_once(root, config, now, profile=args.profile)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
