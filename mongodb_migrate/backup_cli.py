"""All-in-one backup, restore and data-exchange command line interface."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from bson import json_util

from .archive import (
    BackupOptions,
    ExportOptions,
    ImportOptions,
    RestoreOptions,
    create_backup,
    export_data,
    import_data,
    inspect_backup,
    restore_backup,
    verify_backup,
)
from .product_info import PRODUCT_VERSION
from .store import MigrationStore

PASSWORD_ENV = "MONGODB_MIGRATE_BACKUP_PASSWORD"


def _query(value: str) -> dict | None:
    if not value:
        return None
    decoded = json_util.loads(value)
    if not isinstance(decoded, dict):
        raise argparse.ArgumentTypeError("query must be an Extended JSON object")
    return decoded


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="mongodb-migrate-backup",
        description="Standalone BSON backup, restore and data exchange",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    root.add_argument("--version", action="version", version=PRODUCT_VERSION)
    root.add_argument("--state-db", default=".mongodb-migrate.sqlite3")
    root.add_argument("-v", "--verbose", action="count", default=0)
    commands = root.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a checksummed BSON archive")
    backup.add_argument(
        "--source-uri", default=os.getenv("MONGODB_MIGRATE_SOURCE_URI", ""), required=False
    )
    backup.add_argument("--db", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--collections", default="*")
    backup.add_argument("--exclude", default="system.*")
    backup.add_argument("--query", type=_query, default=None)
    backup.add_argument("--compression-level", type=int, default=6)
    backup.add_argument(
        "--encrypt", action="store_true",
        help=f"encrypt with AES-256-GCM using password from {PASSWORD_ENV}",
    )
    backup.add_argument("--retention-days", type=int, default=0)

    restore = commands.add_parser("restore", help="verify and restore a BSON archive")
    restore.add_argument(
        "--target-uri", default=os.getenv("MONGODB_MIGRATE_TARGET_URI", ""), required=False
    )
    restore.add_argument("--db", required=True)
    restore.add_argument("--input", required=True)
    restore.add_argument("--conflict", choices=("fail", "drop", "merge"), default="fail")
    restore.add_argument("--batch-size", type=int, default=1000)
    restore.add_argument("--no-indexes", action="store_true")
    restore.add_argument("--no-verify", action="store_true")

    verify = commands.add_parser("verify", help="verify every archive checksum")
    verify.add_argument("--input", required=True)

    inspect = commands.add_parser("inspect", help="show archive metadata")
    inspect.add_argument("--input", required=True)

    export = commands.add_parser("export", help="export Extended JSONL or CSV")
    export.add_argument(
        "--source-uri", default=os.getenv("MONGODB_MIGRATE_SOURCE_URI", ""), required=False
    )
    export.add_argument("--db", required=True)
    export.add_argument("--collection", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    export.add_argument("--fields", default="")
    export.add_argument("--query", type=_query, default=None)
    export.add_argument("--retention-days", type=int, default=0)

    import_command = commands.add_parser("import", help="import JSONL or CSV")
    import_command.add_argument(
        "--target-uri", default=os.getenv("MONGODB_MIGRATE_TARGET_URI", ""), required=False
    )
    import_command.add_argument("--db", required=True)
    import_command.add_argument("--collection", required=True)
    import_command.add_argument("--input", required=True)
    import_command.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    import_command.add_argument(
        "--conflict", choices=("fail", "drop", "merge"), default="merge"
    )
    import_command.add_argument("--batch-size", type=int, default=1000)

    commands.add_parser("list", help="list cataloged backup and export assets")
    return root


def _password(required: bool = False) -> str:
    value = os.getenv(PASSWORD_ENV, "")
    if required and not value:
        raise ValueError(f"set {PASSWORD_ENV} for encrypted backup operations")
    return value


def _require(value: str, label: str) -> str:
    if not value:
        raise ValueError(f"{label} is required or must be provided by its environment variable")
    return value


def _register(store: MigrationStore, kind: str, result: dict, args: argparse.Namespace) -> None:
    manifest = result.get("manifest") or {}
    source = manifest.get("source") or {}
    store.register_asset(
        kind=kind,
        path=result["path"],
        source_endpoint=source.get("endpoint", ""),
        source_db=source.get("database", ""),
        collections=int(result.get("collections", 0)),
        documents=int(result.get("documents", 0)),
        size_bytes=int(result.get("size", 0)),
        sha256=str(result.get("sha256", "")),
        encrypted=bool(result.get("encrypted", False)),
        verified=kind == "backup",
        retention_days=max(0, int(getattr(args, "retention_days", 0))),
        metadata={
            "format": result.get("format", ARCHIVE_LABEL if kind == "backup" else ""),
            "created_at": result.get("created_at", ""),
        },
    )


ARCHIVE_LABEL = "mongodb-migrate-backup"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    password = _password(False)
    try:
        store = MigrationStore(args.state_db)
        try:
            if args.command == "backup":
                result = create_backup(BackupOptions(
                    source_uri=_require(args.source_uri, "--source-uri"),
                    source_db=args.db,
                    output=args.output,
                    collections=args.collections,
                    exclude=args.exclude,
                    query=args.query,
                    compression_level=args.compression_level,
                    encryption_password=_password(args.encrypt) if args.encrypt else "",
                ))
                _register(store, "backup", result, args)
            elif args.command == "restore":
                result = restore_backup(RestoreOptions(
                    target_uri=_require(args.target_uri, "--target-uri"),
                    target_db=args.db,
                    input=args.input,
                    encryption_password=password,
                    conflict=args.conflict,
                    batch_size=args.batch_size,
                    restore_indexes=not args.no_indexes,
                    verify_checksums=not args.no_verify,
                ))
            elif args.command == "verify":
                result = verify_backup(args.input, password=password)
                try:
                    store.mark_asset_verified(args.input)
                except KeyError:
                    _register(store, "backup", result, args)
            elif args.command == "inspect":
                result = inspect_backup(args.input, password=password)
            elif args.command == "export":
                result = export_data(ExportOptions(
                    source_uri=_require(args.source_uri, "--source-uri"),
                    source_db=args.db,
                    collection=args.collection,
                    output=args.output,
                    format=args.format,
                    fields=tuple(field.strip() for field in args.fields.split(",") if field.strip()),
                    query=args.query,
                ))
                _register(store, "export", result, args)
            elif args.command == "import":
                result = import_data(ImportOptions(
                    target_uri=_require(args.target_uri, "--target-uri"),
                    target_db=args.db,
                    collection=args.collection,
                    input=args.input,
                    format=args.format,
                    conflict=args.conflict,
                    batch_size=args.batch_size,
                ))
            else:
                result = {"assets": store.list_assets()}
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - process boundary
        logging.getLogger("mongodb_migrate").error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
