from __future__ import annotations

import argparse
import json

from .store import MigrationStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect MongoDB migration jobs")
    parser.add_argument("--state-db", default=".mongodb-migrate.sqlite3")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    store = MigrationStore(args.state_db)
    try:
        data = store.report(args.job_id) if args.job_id else store.list_jobs(args.limit)
    finally:
        store.close()
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    else:
        print(payload)
    return 0

