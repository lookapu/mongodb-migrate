"""Standalone logical BSON backup, restore and data-exchange engines."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from bson import BSON, json_util
from pymongo import ASCENDING, IndexModel, MongoClient, ReplaceOne
from pymongo.read_concern import ReadConcern

from .config import select_collections

ProgressHook = Callable[[dict[str, Any]], None]
ARCHIVE_FORMAT = "mongodb-migrate-backup"
ARCHIVE_VERSION = 1
ENCRYPTED_MAGIC = b"MMBAKENC1"
MAX_BSON_SIZE = 16 * 1024 * 1024


class ArchiveCancelled(RuntimeError):
    """Raised at a safe document-batch boundary after cancellation."""


@dataclass(frozen=True)
class BackupOptions:
    source_uri: str
    source_db: str
    output: str
    collections: str = "*"
    exclude: str = "system.*"
    query: dict[str, Any] | None = None
    compression_level: int = 6
    encryption_password: str = ""
    app_name: str = "mongodb-migrate-backup"

    def validate(self) -> None:
        if not self.source_uri or not self.source_db:
            raise ValueError("source_uri and source_db are required")
        if not self.output:
            raise ValueError("backup output is required")
        if not 0 <= self.compression_level <= 9:
            raise ValueError("compression_level must be between 0 and 9")
        if self.encryption_password and len(self.encryption_password) < 12:
            raise ValueError("encryption password must contain at least 12 characters")


@dataclass(frozen=True)
class RestoreOptions:
    target_uri: str
    target_db: str
    input: str
    encryption_password: str = ""
    conflict: str = "fail"  # fail | drop | merge
    batch_size: int = 1000
    restore_indexes: bool = True
    verify_checksums: bool = True
    app_name: str = "mongodb-migrate-restore"

    def validate(self) -> None:
        if not self.target_uri or not self.target_db:
            raise ValueError("target_uri and target_db are required")
        if not self.input:
            raise ValueError("backup input is required")
        if self.conflict not in {"fail", "drop", "merge"}:
            raise ValueError("restore conflict must be fail, drop, or merge")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")


@dataclass(frozen=True)
class ExportOptions:
    source_uri: str
    source_db: str
    collection: str
    output: str
    format: str = "jsonl"  # jsonl | csv
    fields: tuple[str, ...] = ()
    query: dict[str, Any] | None = None

    def validate(self) -> None:
        if not self.source_uri or not self.source_db or not self.collection:
            raise ValueError("source endpoint, database and collection are required")
        if self.format not in {"jsonl", "csv"}:
            raise ValueError("export format must be jsonl or csv")
        if self.format == "csv" and not self.fields:
            raise ValueError("CSV export requires an explicit field list")


@dataclass(frozen=True)
class ImportOptions:
    target_uri: str
    target_db: str
    collection: str
    input: str
    format: str = "jsonl"  # jsonl | csv
    conflict: str = "merge"  # fail | drop | merge
    batch_size: int = 1000

    def validate(self) -> None:
        if not self.target_uri or not self.target_db or not self.collection:
            raise ValueError("target endpoint, database and collection are required")
        if self.format not in {"jsonl", "csv"}:
            raise ValueError("import format must be jsonl or csv")
        if self.conflict not in {"fail", "drop", "merge"}:
            raise ValueError("import conflict must be fail, drop, or merge")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")


def _emit(hook: ProgressHook | None, kind: str, **payload: Any) -> None:
    if hook:
        hook({"type": kind, "at": time.time(), **payload})


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel and cancel.is_set():
        raise ArchiveCancelled("operation cancelled by operator")


def _safe_key(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")


def _safe_endpoint(uri: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(uri)
    hosts = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme or "mongodb", hosts, "", "", ""))


def _query_digest(query: dict[str, Any] | None) -> str:
    encoded = json_util.dumps(
        query or {}, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _supported_collection_options(raw: dict[str, Any]) -> dict[str, Any]:
    supported = {
        "capped", "collation", "size", "max", "validator",
        "validationLevel", "validationAction", "timeseries",
        "expireAfterSeconds", "clusteredIndex", "changeStreamPreAndPostImages",
    }
    return {key: value for key, value in raw.items() if key in supported}


def _index_spec(index: dict[str, Any]) -> dict[str, Any]:
    spec = dict(index)
    spec["key"] = list(spec.get("key", {}).items())
    for field in ("v", "ns", "background", "buildUUID"):
        spec.pop(field, None)
    return spec


def create_backup(
    options: BackupOptions,
    *,
    hook: ProgressHook | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Create an atomic, checksummed logical BSON archive."""
    options.validate()
    output = Path(options.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
    plain_output = temp_output
    encrypt_after = bool(options.encryption_password)
    if encrypt_after:
        plain_output = temp_output.with_suffix(temp_output.suffix + ".zip")
    client = MongoClient(options.source_uri, appname=options.app_name, retryReads=True)
    try:
        client.admin.command("ping")
        database = client[options.source_db]
        raw_names = database.list_collection_names(authorizedCollections=True)
        names = select_collections(raw_names, options.collections, options.exclude)
        metadata_by_name = {
            row["name"]: row
            for row in database.list_collections()
            if row.get("type") in {"collection", "timeseries"}
        }
        names = [name for name in names if name in metadata_by_name]
        if not names:
            raise RuntimeError("no source collections matched")
        manifest: dict[str, Any] = {
            "format": ARCHIVE_FORMAT,
            "format_version": ARCHIVE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "endpoint": _safe_endpoint(options.source_uri),
                "database": options.source_db,
                "server_version": client.server_info().get("version", ""),
            },
            "consistency": "majority-read-per-collection-cursor",
            "query_sha256": _query_digest(options.query),
            "encrypted": encrypt_after,
            "collections": [],
        }
        compression = zipfile.ZIP_STORED if options.compression_level == 0 else zipfile.ZIP_DEFLATED
        _emit(hook, "backup_started", output=str(output), collections=len(names))
        with zipfile.ZipFile(
            plain_output,
            "w",
            compression=compression,
            compresslevel=options.compression_level or None,
            allowZip64=True,
        ) as archive:
            for name in names:
                _check_cancel(cancel)
                key = _safe_key(name)
                data_path = f"data/{key}.bson"
                metadata_path = f"metadata/{key}.json"
                raw_meta = metadata_by_name[name]
                metadata = {
                    "name": name,
                    "options": _supported_collection_options(raw_meta.get("options") or {}),
                    "indexes": [
                        _index_spec(dict(index))
                        for index in database[name].list_indexes()
                    ],
                }
                metadata_bytes = json_util.dumps(metadata, indent=2).encode()
                archive.writestr(metadata_path, metadata_bytes)
                digest = hashlib.sha256()
                count = 0
                size = 0
                collection = database[name].with_options(
                    read_concern=ReadConcern("majority")
                )
                cursor = collection.find(
                    options.query or {}, no_cursor_timeout=True, batch_size=1000
                ).sort("_id", ASCENDING)
                try:
                    with archive.open(data_path, "w", force_zip64=True) as target:
                        for document in cursor:
                            if count % 500 == 0:
                                _check_cancel(cancel)
                            raw = BSON.encode(document)
                            target.write(raw)
                            digest.update(raw)
                            count += 1
                            size += len(raw)
                            if count % 1000 == 0:
                                _emit(
                                    hook, "backup_progress", collection=name,
                                    documents=count, bytes=size,
                                )
                finally:
                    cursor.close()
                row = {
                    "name": name,
                    "data_path": data_path,
                    "metadata_path": metadata_path,
                    "documents": count,
                    "bson_bytes": size,
                    "data_sha256": digest.hexdigest(),
                    "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
                }
                manifest["collections"].append(row)
                _emit(hook, "collection_backed_up", **row)
            core = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
            manifest["manifest_sha256"] = hashlib.sha256(core).hexdigest()
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            )
        if encrypt_after:
            encrypt_file(plain_output, temp_output, options.encryption_password)
            plain_output.unlink(missing_ok=True)
        os.replace(temp_output, output)
        # A backup is not declared ready until every persisted segment has
        # been read back and verified from the final atomic output path.
        result = verify_backup(output, password=options.encryption_password)
        _emit(hook, "backup_completed", **result)
        return result
    except Exception:
        temp_output.unlink(missing_ok=True)
        if plain_output != temp_output:
            plain_output.unlink(missing_ok=True)
        raise
    finally:
        client.close()


def _open_plain_archive(path: Path, password: str) -> tuple[Path, Callable[[], None]]:
    with path.open("rb") as handle:
        encrypted = handle.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
    if not encrypted:
        return path, lambda: None
    if not password:
        raise ValueError("this backup is encrypted; provide the encryption password")
    with tempfile.NamedTemporaryFile(
        prefix="mongodb-migrate-", suffix=".zip", delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        decrypt_file(path, temporary, password)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary, lambda: temporary.unlink(missing_ok=True)


def inspect_backup(path: str | Path, *, password: str = "") -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    plain, cleanup = _open_plain_archive(source, password)
    try:
        with zipfile.ZipFile(plain) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != ARCHIVE_FORMAT:
                raise ValueError("unsupported backup format")
        with source.open("rb") as handle:
            encrypted = handle.read(len(ENCRYPTED_MAGIC)) == ENCRYPTED_MAGIC
        return {
            "path": str(source),
            "size": source.stat().st_size,
            "sha256": file_sha256(source),
            "encrypted": encrypted,
            "collections": len(manifest.get("collections", [])),
            "documents": sum(
                int(item.get("documents", 0)) for item in manifest.get("collections", [])
            ),
            "created_at": manifest.get("created_at", ""),
            "source_database": manifest.get("source", {}).get("database", ""),
            "manifest": manifest,
        }
    finally:
        cleanup()


def verify_backup(
    path: str | Path,
    *,
    password: str = "",
    hook: ProgressHook | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    plain, cleanup = _open_plain_archive(source, password)
    try:
        with zipfile.ZipFile(plain) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != ARCHIVE_FORMAT:
                raise ValueError("unsupported backup format")
            core = dict(manifest)
            expected_manifest = core.pop("manifest_sha256", "")
            actual_manifest = hashlib.sha256(
                json.dumps(core, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if expected_manifest != actual_manifest:
                raise ValueError("backup manifest checksum mismatch")
            for item in manifest.get("collections", []):
                _check_cancel(cancel)
                for path_key, checksum_key in (
                    ("data_path", "data_sha256"),
                    ("metadata_path", "metadata_sha256"),
                ):
                    digest = hashlib.sha256()
                    with archive.open(item[path_key]) as member:
                        while chunk := member.read(1024 * 1024):
                            digest.update(chunk)
                    if digest.hexdigest() != item[checksum_key]:
                        raise ValueError(
                            f"backup checksum mismatch: {item['name']} {path_key}"
                        )
                _emit(hook, "backup_verified", collection=item["name"])
        return inspect_backup(source, password=password)
    finally:
        cleanup()


def _bson_documents(handle: Any) -> Iterator[dict[str, Any]]:
    while True:
        prefix = handle.read(4)
        if not prefix:
            return
        if len(prefix) != 4:
            raise ValueError("truncated BSON document length")
        length = struct.unpack("<i", prefix)[0]
        if length < 5 or length > MAX_BSON_SIZE:
            raise ValueError(f"invalid BSON document length: {length}")
        remainder = handle.read(length - 4)
        if len(remainder) != length - 4:
            raise ValueError("truncated BSON document")
        yield BSON(prefix + remainder).decode()


def restore_backup(
    options: RestoreOptions,
    *,
    hook: ProgressHook | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Validate an archive before any write, then restore documents and indexes."""
    options.validate()
    source = Path(options.input).expanduser().resolve()
    if options.verify_checksums:
        verify_backup(source, password=options.encryption_password, hook=hook, cancel=cancel)
    plain, cleanup = _open_plain_archive(source, options.encryption_password)
    client = MongoClient(options.target_uri, appname=options.app_name, retryWrites=True)
    restored_documents = 0
    try:
        client.admin.command("ping")
        database = client[options.target_db]
        existing = set(database.list_collection_names(authorizedCollections=True))
        with zipfile.ZipFile(plain) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            archive_names = {item["name"] for item in manifest["collections"]}
            conflicts = sorted(archive_names & existing)
            if options.conflict == "fail" and conflicts:
                sample = ", ".join(conflicts[:5])
                suffix = "…" if len(conflicts) > 5 else ""
                raise RuntimeError(
                    f"target collections already exist: {sample}{suffix}"
                )
            _emit(
                hook, "restore_started", collections=len(manifest["collections"]),
                target_database=options.target_db,
            )
            for item in manifest["collections"]:
                _check_cancel(cancel)
                name = item["name"]
                metadata = json_util.loads(archive.read(item["metadata_path"]))
                if name in existing and options.conflict == "drop":
                    database.drop_collection(name)
                    existing.remove(name)
                if name not in existing:
                    database.create_collection(name, **(metadata.get("options") or {}))
                    existing.add(name)
                collection = database[name]
                batch: list[ReplaceOne] = []
                count = 0
                with archive.open(item["data_path"]) as member:
                    for document in _bson_documents(member):
                        batch.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
                        if len(batch) >= options.batch_size:
                            _check_cancel(cancel)
                            collection.bulk_write(batch, ordered=False)
                            count += len(batch)
                            restored_documents += len(batch)
                            batch.clear()
                            _emit(
                                hook, "restore_progress", collection=name,
                                documents=count,
                            )
                    if batch:
                        collection.bulk_write(batch, ordered=False)
                        count += len(batch)
                        restored_documents += len(batch)
                if options.restore_indexes:
                    models = []
                    for raw_index in metadata.get("indexes", []):
                        spec = dict(raw_index)
                        if spec.get("name") == "_id_":
                            continue
                        keys = [tuple(pair) for pair in spec.pop("key")]
                        models.append(IndexModel(keys, **spec))
                    if models:
                        collection.create_indexes(models)
                if options.conflict != "merge":
                    target_count = collection.count_documents({})
                    expected_count = int(item.get("documents", 0))
                    if target_count != expected_count:
                        raise RuntimeError(
                            f"restore count verification failed for {name}: "
                            f"expected={expected_count}, target={target_count}"
                        )
                _emit(hook, "collection_restored", collection=name, documents=count)
        result = {
            "path": str(source),
            "target_database": options.target_db,
            "documents": restored_documents,
            "collections": len(manifest["collections"]),
        }
        _emit(hook, "restore_completed", **result)
        return result
    finally:
        cleanup()
        client.close()


def export_data(
    options: ExportOptions,
    *,
    hook: ProgressHook | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    options.validate()
    output = Path(options.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
    client = MongoClient(options.source_uri, appname="mongodb-migrate-export", retryReads=True)
    count = 0
    try:
        client.admin.command("ping")
        cursor = client[options.source_db][options.collection].find(
            options.query or {}, batch_size=1000
        ).sort("_id", ASCENDING)
        try:
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                if options.format == "jsonl":
                    for document in cursor:
                        if count % 500 == 0:
                            _check_cancel(cancel)
                        handle.write(json_util.dumps(document) + "\n")
                        count += 1
                        if count % 1000 == 0:
                            _emit(hook, "export_progress", documents=count)
                else:
                    writer = csv.DictWriter(handle, fieldnames=list(options.fields))
                    writer.writeheader()
                    for document in cursor:
                        if count % 500 == 0:
                            _check_cancel(cancel)
                        writer.writerow({field: _lookup(document, field) for field in options.fields})
                        count += 1
        finally:
            cursor.close()
        os.replace(temporary, output)
        result = {
            "path": str(output), "format": options.format,
            "documents": count, "size": output.stat().st_size,
            "sha256": file_sha256(output),
        }
        _emit(hook, "export_completed", **result)
        return result
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        client.close()


def _lookup(document: dict[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return ""
        value = value[part]
    if isinstance(value, (dict, list)):
        return json_util.dumps(value)
    return value


def import_data(
    options: ImportOptions,
    *,
    hook: ProgressHook | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    options.validate()
    source = Path(options.input).expanduser().resolve()
    client = MongoClient(options.target_uri, appname="mongodb-migrate-import", retryWrites=True)
    count = 0
    try:
        client.admin.command("ping")
        database = client[options.target_db]
        existing = options.collection in database.list_collection_names()
        if existing and options.conflict == "fail":
            raise RuntimeError(f"target collection already exists: {options.collection}")
        if existing and options.conflict == "drop":
            database.drop_collection(options.collection)
        collection = database[options.collection]
        batch: list[ReplaceOne] = []
        with source.open("r", encoding="utf-8", newline="") as handle:
            documents = _import_documents(handle, options.format)
            for document in documents:
                if "_id" not in document:
                    raise ValueError("every imported document must contain _id")
                batch.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
                if len(batch) >= options.batch_size:
                    _check_cancel(cancel)
                    collection.bulk_write(batch, ordered=False)
                    count += len(batch)
                    batch.clear()
                    _emit(hook, "import_progress", documents=count)
            if batch:
                collection.bulk_write(batch, ordered=False)
                count += len(batch)
        result = {
            "path": str(source), "format": options.format,
            "documents": count, "collection": options.collection,
        }
        _emit(hook, "import_completed", **result)
        return result
    finally:
        client.close()


def _import_documents(handle: TextIO, format_name: str) -> Iterator[dict[str, Any]]:
    if format_name == "jsonl":
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json_util.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"JSONL line {line_no} is not an object")
            yield value
        return
    for row in csv.DictReader(handle):
        yield dict(row)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _encryption_key(password: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:
        raise RuntimeError(
            "encrypted backups require the bundled cryptography package"
        ) from exc
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000
    ).derive(password.encode())


def encrypt_file(source: Path, target: Path, password: str) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _encryption_key(password, salt)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with source.open("rb") as raw, target.open("wb") as encrypted:
        encrypted.write(ENCRYPTED_MAGIC + salt + nonce)
        while chunk := raw.read(1024 * 1024):
            encrypted.write(encryptor.update(chunk))
        encrypted.write(encryptor.finalize())
        encrypted.write(encryptor.tag)


def decrypt_file(source: Path, target: Path, password: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    total = source.stat().st_size
    header_size = len(ENCRYPTED_MAGIC) + 16 + 12
    if total < header_size + 16:
        raise ValueError("encrypted backup is truncated")
    with source.open("rb") as encrypted:
        if encrypted.read(len(ENCRYPTED_MAGIC)) != ENCRYPTED_MAGIC:
            raise ValueError("not an encrypted MongoDB Migrate backup")
        salt = encrypted.read(16)
        nonce = encrypted.read(12)
        encrypted.seek(-16, os.SEEK_END)
        tag = encrypted.read(16)
        encrypted.seek(header_size)
        remaining = total - header_size - 16
        decryptor = Cipher(
            algorithms.AES(_encryption_key(password, salt)), modes.GCM(nonce, tag)
        ).decryptor()
        try:
            with target.open("wb") as plain:
                while remaining:
                    chunk = encrypted.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup is truncated")
                    remaining -= len(chunk)
                    plain.write(decryptor.update(chunk))
                plain.write(decryptor.finalize())
        except InvalidTag as exc:
            target.unlink(missing_ok=True)
            raise ValueError("invalid backup password or corrupted encrypted file") from exc
