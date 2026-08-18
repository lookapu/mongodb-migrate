from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import MigrationOptions
from .engine import MigrationEngine, PlanApprovalRequired
from .product_info import PRODUCT_VERSION

ALL_IN_ONE_COMMANDS = {"backup", "restore", "verify", "inspect", "export", "import", "list"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mongodb-migrate",
        description="All-in-one MongoDB migration, backup, restore and data exchange",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=PRODUCT_VERSION)
    source_uri = os.getenv("MONGODB_MIGRATE_SOURCE_URI", "")
    target_uri = os.getenv("MONGODB_MIGRATE_TARGET_URI", "")
    p.add_argument(
        "--source-uri",
        default=source_uri,
        required=not source_uri,
        help="or set MONGODB_MIGRATE_SOURCE_URI",
    )
    p.add_argument(
        "--target-uri",
        default=target_uri,
        required=not target_uri,
        help="or set MONGODB_MIGRATE_TARGET_URI",
    )
    p.add_argument("--source-db", required=True)
    p.add_argument("--target-db", required=True)
    p.add_argument("--collections", default="*", help="comma-separated glob patterns")
    p.add_argument("--exclude", default="system.*", help="comma-separated glob patterns")
    p.add_argument("--target-suffix", default="__migrating")
    p.add_argument("--query", default="", help="MongoDB Extended JSON filter")
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--batch-bytes", type=int, default=12 * 1024 * 1024)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--docs-per-second", type=float, default=0)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--retry-backoff", type=float, default=0.5)
    p.add_argument("--incremental-field", default="")
    p.add_argument("--incremental-rounds", type=int, default=0)
    p.add_argument("--incremental-overlap-seconds", type=float, default=120)
    p.add_argument("--incremental-interval", type=float, default=3)
    p.add_argument("--convergence-rounds", type=int, default=2)
    p.add_argument(
        "--change-stream",
        action="store_true",
        help="capture inserts, updates and deletes with a resumable Change Stream",
    )
    p.add_argument("--cdc-quiet-seconds", type=float, default=5)
    p.add_argument("--cdc-max-seconds", type=float, default=600)
    p.add_argument("--verify", choices=("none", "count", "sample", "full"), default="count")
    p.add_argument("--sample-size", type=int, default=200)
    p.add_argument("--no-copy-indexes", action="store_true")
    p.add_argument("--cutover", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--conflict", choices=("fail", "resume"), default="fail")
    p.add_argument("--state-db", default=".mongodb-migrate.sqlite3")
    p.add_argument("--dlq-dir", default="dlq")
    p.add_argument("--job-id", default="", help="resume a durable job")
    p.add_argument("--lease-ttl", type=int, default=60)
    p.add_argument("--report-dir", default="reports")
    p.add_argument(
        "--production-safe-mode",
        action="store_true",
        help="require full verification, immutable plan approval and strict runtime guards",
    )
    p.add_argument(
        "--continuous-writes",
        action="store_true",
        help="declare that source writes continue; safe mode then requires Change Streams",
    )
    p.add_argument("--no-runtime-guard", action="store_true")
    p.add_argument("--max-cache-percent", type=float, default=85)
    p.add_argument("--max-connections-percent", type=float, default=85)
    p.add_argument("--min-disk-free-percent", type=float, default=10)
    p.add_argument("--safety-pause-timeout", type=float, default=300)
    p.add_argument("--approval-token", default="")
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="write a credential-free execution plan without changing MongoDB",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def options_from_args(args: argparse.Namespace) -> MigrationOptions:
    query = None
    if args.query:
        from bson import json_util

        query = json_util.loads(args.query)
        if not isinstance(query, dict):
            raise ValueError("--query must decode to a JSON object")
    return MigrationOptions(
        source_uri=args.source_uri,
        target_uri=args.target_uri,
        source_db=args.source_db,
        target_db=args.target_db,
        collections=args.collections,
        exclude=args.exclude,
        target_suffix=args.target_suffix,
        query=query,
        batch_size=args.batch_size,
        batch_bytes=args.batch_bytes,
        workers=args.workers,
        docs_per_second=args.docs_per_second,
        max_retries=args.max_retries,
        retry_backoff=args.retry_backoff,
        incremental_field=args.incremental_field,
        incremental_rounds=args.incremental_rounds,
        incremental_overlap_seconds=args.incremental_overlap_seconds,
        incremental_interval=args.incremental_interval,
        convergence_rounds=args.convergence_rounds,
        cdc_enabled=args.change_stream,
        cdc_quiet_seconds=args.cdc_quiet_seconds,
        cdc_max_seconds=args.cdc_max_seconds,
        verify=args.verify,
        sample_size=args.sample_size,
        copy_indexes=not args.no_copy_indexes,
        cutover=args.cutover,
        dry_run=args.dry_run,
        conflict=args.conflict,
        state_db=args.state_db,
        dlq_dir=args.dlq_dir,
        job_id=args.job_id,
        lease_ttl=args.lease_ttl,
        report_dir=args.report_dir,
        production_safe_mode=args.production_safe_mode,
        continuous_writes=args.continuous_writes,
        runtime_guard=not args.no_runtime_guard,
        max_cache_percent=args.max_cache_percent,
        max_connections_percent=args.max_connections_percent,
        min_disk_free_percent=args.min_disk_free_percent,
        safety_pause_timeout=args.safety_pause_timeout,
        approval_token=args.approval_token,
        plan_only=args.plan_only,
    )


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    first_command = next(
        (item for item in effective_argv if item in ALL_IN_ONE_COMMANDS), ""
    )
    if first_command in ALL_IN_ONE_COMMANDS:
        from .backup_cli import main as backup_main

        return backup_main(effective_argv)
    args = parser().parse_args(effective_argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    try:
        job_id = MigrationEngine(options_from_args(args)).run()
        print(json.dumps({
            "status": "planned" if args.plan_only else "completed",
            "job_id": job_id,
        }))
        return 0
    except PlanApprovalRequired as exc:
        print(json.dumps({
            "status": "approval_required",
            "job_id": exc.plan["job_id"],
            "approval_code": exc.plan["approval_code"],
            "plan": str(exc.path),
        }))
        return 2
    except KeyboardInterrupt:
        logging.getLogger("mongodb_migrate").warning("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary maps failures to exit status
        logging.getLogger("mongodb_migrate").error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
