from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bson import BSON, json_util
from pymongo import ASCENDING, DESCENDING, DeleteOne, MongoClient, ReplaceOne
from pymongo.collection import Collection
from pymongo.errors import (
    AutoReconnect,
    BulkWriteError,
    ConnectionFailure,
    ExecutionTimeout,
    NetworkTimeout,
    NotPrimaryError,
    PyMongoError,
)

from .config import MigrationOptions, select_collections
from .store import MigrationStore

log = logging.getLogger("mongodb_migrate")
RETRYABLE_CODES = {
    6, 7, 50, 64, 89, 91, 189, 9001, 10107, 11600, 11601, 11602, 13435, 13436,
}
RETRYABLE_EXCEPTIONS = (
    AutoReconnect, ConnectionFailure, ExecutionTimeout, NetworkTimeout, NotPrimaryError,
)


class MigrationCancelled(RuntimeError):
    """Raised at a safe batch boundary after an operator cancellation."""


class PlanApprovalRequired(RuntimeError):
    """Raised before external writes when a production plan is not approved."""

    def __init__(self, plan: dict[str, Any], path: Path):
        self.plan = plan
        self.path = path
        super().__init__(
            f"production plan approval required: enter code "
            f"{plan['approval_code']} from {path}"
        )


class RateLimiter:
    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0
        self.next_at = 0.0
        self.lock = threading.Lock()

    def take(self, amount: int) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + amount * self.interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)


class DeadLetterQueue:
    def __init__(self, directory: Path, job_id: str):
        self.path = directory / f"{job_id}.jsonl"
        self.lock = threading.Lock()

    def write(self, collection: str, document: dict[str, Any], error: Exception) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "collection": collection,
            "error": str(error),
            "document": document,
        }
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json_util.dumps(record) + "\n")
            handle.flush()


def merge_query(base: dict[str, Any] | None, extra: dict[str, Any]) -> dict[str, Any]:
    if not base:
        return extra
    if not extra:
        return base
    return {"$and": [base, extra]}


def batches(documents: Iterable[dict[str, Any]], max_count: int, max_bytes: int):
    batch: list[dict[str, Any]] = []
    size = 0
    for document in documents:
        document_size = len(BSON.encode(document))
        if batch and (len(batch) >= max_count or size + document_size > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(document)
        size += document_size
    if batch:
        yield batch


def stable_digest(document: dict[str, Any]) -> str:
    canonical = json_util.dumps(
        document, json_options=json_util.CANONICAL_JSON_OPTIONS, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MigrationEngine:
    def __init__(self, options: MigrationOptions):
        options.validate()
        self.options = options
        self.store = MigrationStore(options.state_path)
        self.owner_id = uuid.uuid4().hex
        self.source = MongoClient(
            options.source_uri, appname="mongodb-migrate-source", retryReads=True
        )
        self.target = MongoClient(
            options.target_uri, appname="mongodb-migrate-target", retryWrites=True
        )
        self.source_db = self.source[options.source_db]
        self.target_db = self.target[options.target_db]
        self.job_id = ""
        self.rate = RateLimiter(options.docs_per_second)
        self.dlq: DeadLetterQueue | None = None
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self._last_guard_check = 0.0

    def cancel(self) -> None:
        self.cancelled.set()

    def close(self) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        self.source.close()
        self.target.close()
        self.store.close()

    def preflight(self) -> list[str]:
        self.source.admin.command("ping")
        self.target.admin.command("ping")
        source_hello = self.source.admin.command("hello")
        source_info = self.source.server_info()
        target_info = self.target.server_info()
        if self.options.cutover and (
            self.options.source_uri == self.options.target_uri
            and self.options.source_db == self.options.target_db
        ):
            raise RuntimeError("cutover in the source database would replace the source collection")
        if self.options.cdc_enabled and not (
            source_hello.get("setName") or source_hello.get("msg") == "isdbgrid"
        ):
            raise RuntimeError(
                "Change Streams require a replica set or sharded cluster source"
            )
        raw_names = self.source_db.list_collection_names(authorizedCollections=True)
        names = select_collections(raw_names, self.options.collections, self.options.exclude)
        if not names:
            raise RuntimeError("no source collections matched")
        views = {
            item["name"]
            for item in self.source_db.list_collections()
            if item.get("type") == "view"
        }
        names = [name for name in names if name not in views]
        if not names:
            raise RuntimeError("only views matched; views are intentionally not migrated")
        existing = set(self.target_db.list_collection_names(authorizedCollections=True))
        for name in names:
            target_name = name + self.options.target_suffix
            if (
                self.options.production_safe_mode
                and target_name in existing
                and not self.options.job_id
            ):
                raise RuntimeError(
                    f"production safe mode refuses existing target collection {target_name!r}"
                )
        log.info(
            "preflight OK: source=%s target=%s collections=%d",
            source_info.get("version"), target_info.get("version"), len(names),
        )
        return names

    def run(self) -> str:
        try:
            names = self.preflight()
            endpoint_source = _safe_endpoint(self.options.source_uri, self.options.source_db)
            endpoint_target = _safe_endpoint(self.options.target_uri, self.options.target_db)
            self.job_id = self.store.create_or_resume(
                endpoint_source,
                endpoint_target,
                self.options.durable_dict(),
                self.options.job_id,
            )
            self.dlq = DeadLetterQueue(self.options.dlq_path, self.job_id)
            for name in names:
                self.store.add_collection(self.job_id, name, name + self.options.target_suffix)
            plan = self.build_execution_plan(names, endpoint_source, endpoint_target)
            plan_path = self._write_execution_plan(plan)
            self.store.event(
                self.job_id,
                "INFO",
                "execution_plan",
                f"execution plan written: {plan_path}",
                payload={
                    "plan_sha256": plan["plan_sha256"],
                    "approval_code": plan["approval_code"],
                },
            )
            if self.options.plan_only:
                self.store.finish(self.job_id, "planned")
                return self.job_id
            if (
                self.options.production_safe_mode
                and self.options.approval_token.strip().upper()
                != plan["approval_code"]
            ):
                self.store.finish(self.job_id, "planned")
                raise PlanApprovalRequired(plan, plan_path)
            self.store.acquire(self.job_id, self.owner_id, self.options.lease_ttl)
            self.store.acquire_target_leases(
                self.job_id,
                self.owner_id,
                endpoint_target,
                self.options.target_db,
                self.options.lease_ttl,
            )
            self._start_heartbeat()
            self.store.event(
                self.job_id, "INFO", "job_started",
                f"migration started for {len(names)} collections",
                payload={"collections": names},
            )
            failures: list[str] = []
            with ThreadPoolExecutor(
                max_workers=min(self.options.workers, len(names)),
                thread_name_prefix="mongo-migrate",
            ) as pool:
                futures = {pool.submit(self._migrate_collection, name): name for name in names}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(f"{name}: {exc}")
                        log.exception("collection %s failed", name)
            if self.cancelled.is_set():
                self.store.finish(self.job_id, "cancelled", "cancelled by operator")
                self.store.event(
                    self.job_id, "WARNING", "job_cancelled", "cancelled by operator"
                )
                raise MigrationCancelled("migration cancelled by operator")
            if failures:
                error = "; ".join(failures)
                self.store.finish(self.job_id, "failed", error)
                raise RuntimeError(error)
            self.store.finish(self.job_id, "completed")
            self.store.event(self.job_id, "INFO", "job_completed", "migration completed")
            return self.job_id
        except (MigrationCancelled, PlanApprovalRequired):
            raise
        except Exception as exc:
            if self.job_id:
                try:
                    self.store.finish(self.job_id, "failed", str(exc))
                except Exception:
                    log.exception("failed to persist terminal job state")
            raise
        finally:
            self.close()

    def build_execution_plan(
        self, names: list[str], source_endpoint: str, target_endpoint: str
    ) -> dict[str, Any]:
        """Build a deterministic, credential-free plan bound to this exact job."""
        query_json = json_util.dumps(
            self.options.query or {},
            json_options=json_util.CANONICAL_JSON_OPTIONS,
            sort_keys=True,
        )
        core = {
            "schema_version": 1,
            "job_id": self.job_id,
            "source": {"endpoint": source_endpoint, "database": self.options.source_db},
            "target": {"endpoint": target_endpoint, "database": self.options.target_db},
            "collections": [
                {"source": name, "target": name + self.options.target_suffix}
                for name in sorted(names)
            ],
            "query_sha256": hashlib.sha256(query_json.encode()).hexdigest(),
            "safety": {
                "production_safe_mode": self.options.production_safe_mode,
                "continuous_writes": self.options.continuous_writes,
                "runtime_guard": self.options.runtime_guard,
                "verify": self.options.verify,
                "copy_indexes": self.options.copy_indexes,
                "cutover": self.options.cutover,
                "cdc_enabled": self.options.cdc_enabled,
                "workers": self.options.workers,
                "docs_per_second": self.options.docs_per_second,
            },
        }
        canonical = json.dumps(
            core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        return {
            **core,
            "plan_sha256": digest,
            "approval_code": digest[:8].upper(),
            "created_at": time.time(),
        }

    def _write_execution_plan(self, plan: dict[str, Any]) -> Path:
        self.options.report_path.mkdir(parents=True, exist_ok=True)
        path = self.options.report_path / f"{self.job_id}.plan.json"
        path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def _start_heartbeat(self) -> None:
        def heartbeat() -> None:
            interval = max(1, self.options.lease_ttl // 3)
            while not self._stop_heartbeat.wait(interval):
                try:
                    self.store.heartbeat(self.job_id, self.owner_id, self.options.lease_ttl)
                except Exception:
                    log.exception("job heartbeat failed")
                    return

        self._heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def _migrate_collection(self, name: str) -> None:
        task = self.store.collection(self.job_id, name)
        if task and task["status"] == "completed":
            log.info("skip completed collection %s", name)
            return
        target_name = name + self.options.target_suffix
        self.store.update_collection(self.job_id, name, status="running", error="")
        self.store.event(
            self.job_id, "INFO", "collection_started", "collection migration started", name
        )
        try:
            if self.options.dry_run:
                source_count = self.source_db[name].count_documents(self.options.query or {})
                self.store.update_collection(
                    self.job_id, name, status="completed",
                    source_docs=source_count, target_docs=0, verified=0,
                )
                return
            if self._cutover_already_finished(name, target_name):
                source_count, target_count = self._verify(name, name)
                self.store.update_collection(
                    self.job_id,
                    name,
                    status="completed",
                    source_docs=source_count,
                    target_docs=target_count,
                    verified=int(self.options.verify != "none"),
                )
                return
            cdc_start = self._prepare_cdc_start(name)
            self._prepare_target(name, target_name)
            baseline_source_count = self.source_db[name].count_documents(
                self.options.query or {}
            )
            self.store.update_collection(
                self.job_id, name, source_docs=baseline_source_count
            )
            copied = self._full_copy(name, target_name)
            # Index builds can take minutes. Complete them before the final
            # CDC drain so writes during index creation are still replayed.
            if self.options.copy_indexes:
                self._copy_indexes(name, target_name)
            if self.options.cdc_enabled:
                rounds = self._change_stream_sync(name, target_name, cdc_start)
            else:
                rounds = self._incremental_sync(name, target_name)
            source_count, target_count = self._verify(name, target_name)
            if self.options.cutover:
                self._cutover(name, target_name)
            self.store.update_collection(
                self.job_id, name, status="completed", copied_docs=copied,
                source_docs=source_count, target_docs=target_count,
                verified=int(self.options.verify != "none"), sync_rounds=rounds,
            )
            self.store.event(
                self.job_id, "INFO", "collection_completed",
                f"copied={copied} source={source_count} target={target_count}", name,
            )
        except Exception as exc:
            self.store.update_collection(
                self.job_id, name, status="failed", error=str(exc)
            )
            self.store.event(
                self.job_id, "ERROR", "collection_failed", str(exc), name
            )
            raise

    def _prepare_cdc_start(self, source_name: str) -> Any:
        if not self.options.cdc_enabled:
            return None
        existing = self.store.get_checkpoint(
            self.job_id, source_name, "cdc_start_time"
        )
        if existing is not None:
            return existing
        hello = self.source.admin.command("hello")
        start_time = hello.get("operationTime") or (
            hello.get("$clusterTime") or {}
        ).get("clusterTime")
        if start_time is None:
            raise RuntimeError("source did not return a Change Stream cluster time")
        self.store.checkpoint(
            self.job_id, source_name, "cdc_start_time", start_time
        )
        return start_time

    def _prepare_target(self, source_name: str, target_name: str) -> None:
        existing = set(self.target_db.list_collection_names())
        checkpoint = self.store.get_checkpoint(self.job_id, source_name, "last_id")
        if target_name in existing:
            if self.options.conflict == "resume" or checkpoint is not None:
                return
            raise RuntimeError(
                f"target collection {target_name!r} exists; use --conflict resume "
                "only if it belongs to this migration"
            )
        source_meta = next(
            self.source_db.list_collections(filter={"name": source_name}), None
        )
        raw_options = dict((source_meta or {}).get("options") or {})
        supported = {
            "capped", "collation", "size", "max", "validator",
            "validationLevel", "validationAction",
            "timeseries", "expireAfterSeconds", "clusteredIndex",
            "changeStreamPreAndPostImages",
        }
        create_options = {k: v for k, v in raw_options.items() if k in supported}
        self.target_db.create_collection(target_name, **create_options)

    def _full_copy(self, source_name: str, target_name: str) -> int:
        source = self.source_db[source_name]
        target = self.target_db[target_name]
        last_id = self.store.get_checkpoint(self.job_id, source_name, "last_id")
        query = self.options.query or {}
        if last_id is not None:
            query = merge_query(query, {"_id": {"$gt": last_id}})
        cursor = source.find(
            query, no_cursor_timeout=True, batch_size=self.options.batch_size
        ).sort("_id", ASCENDING)
        copied = int((self.store.collection(self.job_id, source_name) or {}).get("copied_docs", 0))
        try:
            for batch in batches(cursor, self.options.batch_size, self.options.batch_bytes):
                if self.cancelled.is_set():
                    raise MigrationCancelled("migration cancelled by operator")
                self._write_batch(target, source_name, batch)
                copied += len(batch)
                self.store.checkpoint(self.job_id, source_name, "last_id", batch[-1]["_id"])
                self.store.update_collection(
                    self.job_id, source_name, copied_docs=copied
                )
        finally:
            cursor.close()
        return copied

    def _write_batch(
        self, target: Collection, source_name: str, batch: list[dict[str, Any]]
    ) -> None:
        self._runtime_safety_guard()
        self.rate.take(len(batch))
        operations = [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in batch]
        last_error: Exception | None = None
        for attempt in range(self.options.max_retries + 1):
            if self.cancelled.is_set():
                raise MigrationCancelled("migration cancelled by operator")
            try:
                target.bulk_write(operations, ordered=False)
                return
            except BulkWriteError as exc:
                errors = exc.details.get("writeErrors", [])
                retryable = errors and all(err.get("code") in RETRYABLE_CODES for err in errors)
                last_error = exc
                if not retryable:
                    for item in errors:
                        index = item.get("index", -1)
                        if 0 <= index < len(batch) and self.dlq:
                            self.dlq.write(source_name, batch[index], exc)
                    raise
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
            except PyMongoError:
                raise
            if attempt >= self.options.max_retries:
                break
            time.sleep(self.options.retry_backoff * (2**attempt))
        if self.dlq:
            for document in batch:
                self.dlq.write(source_name, document, last_error or RuntimeError("write failed"))
        raise RuntimeError(
            f"batch write failed after {self.options.max_retries + 1} attempts: {last_error}"
        )

    def _incremental_sync(self, source_name: str, target_name: str) -> int:
        if not self.options.incremental_rounds:
            return 0
        source = self.source_db[source_name]
        target = self.target_db[target_name]
        field = self.options.incremental_field
        watermark = self.store.get_checkpoint(self.job_id, source_name, "watermark")
        stable = 0
        previous_count: int | None = None
        for round_no in range(1, self.options.incremental_rounds + 1):
            if self.cancelled.is_set():
                raise MigrationCancelled("migration cancelled by operator")
            newest = source.find_one(
                merge_query(self.options.query, {field: {"$exists": True}}),
                projection={field: 1},
                sort=[(field, DESCENDING)],
            )
            if not newest:
                break
            upper = newest[field]
            lower = _subtract_overlap(watermark, self.options.incremental_overlap_seconds)
            bounds: dict[str, Any] = {"$lte": upper}
            if lower is not None:
                bounds["$gte"] = lower
            query = merge_query(self.options.query, {field: bounds})
            cursor = source.find(query, batch_size=self.options.batch_size).sort(
                [(field, ASCENDING), ("_id", ASCENDING)]
            )
            for batch in batches(cursor, self.options.batch_size, self.options.batch_bytes):
                self._write_batch(target, source_name, batch)
            watermark = upper
            self.store.checkpoint(self.job_id, source_name, "watermark", watermark)
            source_count = source.count_documents(self.options.query or {})
            target_count = target.count_documents({})
            stable = (
                stable + 1
                if source_count == target_count
                and (previous_count is None or previous_count == source_count)
                else 0
            )
            previous_count = source_count
            self.store.update_collection(self.job_id, source_name, sync_rounds=round_no)
            if stable >= self.options.convergence_rounds:
                return round_no
            if round_no < self.options.incremental_rounds:
                time.sleep(self.options.incremental_interval)
        return self.options.incremental_rounds

    def _change_stream_sync(
        self, source_name: str, target_name: str, start_time: Any
    ) -> int:
        """Drain a resumable collection Change Stream after the baseline copy."""
        source = self.source_db[source_name]
        target = self.target_db[target_name]
        resume_token = self.store.get_checkpoint(
            self.job_id, source_name, "cdc_resume_token"
        )
        watch_options: dict[str, Any] = {
            "full_document": "updateLookup",
            "max_await_time_ms": 1000,
        }
        if resume_token is not None:
            watch_options["resume_after"] = resume_token
        else:
            watch_options["start_at_operation_time"] = start_time
        started = time.monotonic()
        quiet_since: float | None = None
        applied = 0
        self.store.event(
            self.job_id,
            "INFO",
            "cdc_started",
            "Change Stream catch-up started",
            source_name,
        )
        with source.watch(**watch_options) as stream:
            while True:
                if self.cancelled.is_set():
                    raise MigrationCancelled("migration cancelled by operator")
                now = time.monotonic()
                if now - started >= self.options.cdc_max_seconds:
                    raise RuntimeError(
                        "Change Stream did not converge before cdc_max_seconds"
                    )
                event = stream.try_next()
                if event is None:
                    quiet_since = quiet_since or time.monotonic()
                    if (
                        time.monotonic() - quiet_since
                        >= self.options.cdc_quiet_seconds
                    ):
                        break
                    continue
                quiet_since = None
                self._apply_change_event(source, target, source_name, event)
                applied += 1
                token = event.get("_id") or stream.resume_token
                if token is not None:
                    self.store.checkpoint(
                        self.job_id,
                        source_name,
                        "cdc_resume_token",
                        token,
                    )
        self.store.event(
            self.job_id,
            "INFO",
            "cdc_converged",
            f"Change Stream converged after {applied} events",
            source_name,
            {"applied_events": applied},
        )
        return applied

    def _apply_change_event(
        self,
        source: Collection,
        target: Collection,
        source_name: str,
        event: dict[str, Any],
    ) -> None:
        operation = event.get("operationType")
        key = (event.get("documentKey") or {}).get("_id")
        if operation in {"insert", "replace", "update"}:
            document = event.get("fullDocument")
            # Re-read against the migration query. This also handles an update
            # that makes a document leave the filtered data set.
            if key is not None and (self.options.query or document is None):
                document = source.find_one(
                    merge_query(self.options.query, {"_id": key})
                )
            if document is None:
                self._delete_with_retry(target, key)
            else:
                self._write_batch(target, source_name, [document])
            return
        if operation == "delete":
            self._delete_with_retry(target, key)
            return
        if operation in {"drop", "rename", "dropDatabase", "invalidate"}:
            raise RuntimeError(
                f"source namespace changed during CDC: operationType={operation}"
            )

    def _delete_with_retry(self, target: Collection, document_id: Any) -> None:
        if document_id is None:
            raise RuntimeError("Change Stream delete event has no documentKey._id")
        last_error: Exception | None = None
        self._runtime_safety_guard()
        for attempt in range(self.options.max_retries + 1):
            try:
                target.bulk_write(
                    [DeleteOne({"_id": document_id})], ordered=True
                )
                return
            except RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
            if attempt >= self.options.max_retries:
                break
            time.sleep(self.options.retry_backoff * (2**attempt))
        raise RuntimeError(f"CDC delete failed after retries: {last_error}")

    def _verify(self, source_name: str, target_name: str) -> tuple[int, int]:
        source = self.source_db[source_name]
        target = self.target_db[target_name]
        source_count = source.count_documents(self.options.query or {})
        target_count = target.count_documents({})
        if self.options.verify == "none":
            return source_count, target_count
        if source_count != target_count:
            raise RuntimeError(
                f"count verification failed: source={source_count}, target={target_count}"
            )
        if self.options.verify in {"sample", "full"}:
            limit = 0 if self.options.verify == "full" else self.options.sample_size
            cursor = source.find(self.options.query or {}).sort("_id", ASCENDING)
            if limit:
                cursor = cursor.limit(limit)
            checked = 0
            mismatches = 0
            source_hash = hashlib.sha256()
            target_hash = hashlib.sha256()
            for document in cursor:
                target_document = target.find_one({"_id": document["_id"]})
                source_item = stable_digest(document)
                target_item = stable_digest(target_document) if target_document else ""
                source_hash.update((source_item + "\n").encode())
                target_hash.update((target_item + "\n").encode())
                checked += 1
                if target_document is None or source_item != target_item:
                    mismatches += 1
                    self.store.update_collection(
                        self.job_id,
                        source_name,
                        sampled_docs=checked,
                        sample_mismatches=mismatches,
                        source_digest=source_hash.hexdigest(),
                        target_digest=target_hash.hexdigest(),
                    )
                    raise RuntimeError(f"content verification failed at _id={document['_id']!r}")
            self.store.update_collection(
                self.job_id,
                source_name,
                sampled_docs=checked,
                sample_mismatches=mismatches,
                source_digest=source_hash.hexdigest(),
                target_digest=target_hash.hexdigest(),
            )
            self.store.event(
                self.job_id, "INFO", "content_verified",
                f"verified {checked} documents", source_name,
                {
                    "sampled_docs": checked,
                    "sample_mismatches": mismatches,
                    "source_digest": source_hash.hexdigest(),
                    "target_digest": target_hash.hexdigest(),
                },
            )
        return source_count, target_count

    def _runtime_safety_guard(self, force: bool = False) -> None:
        """Pause at safe write boundaries while the target is under pressure."""
        if not self.options.runtime_guard:
            return
        now = time.monotonic()
        if not force and now - self._last_guard_check < 5:
            return
        self._last_guard_check = now
        deadline = now + self.options.safety_pause_timeout
        while True:
            if self.cancelled.is_set():
                raise MigrationCancelled("migration cancelled by operator")
            reasons: list[str] = []
            try:
                status = self.target.admin.command("serverStatus")
                connections = status.get("connections", {})
                current = float(connections.get("current", 0) or 0)
                available = float(connections.get("available", 0) or 0)
                if current + available > 0:
                    used = current * 100 / (current + available)
                    if used >= self.options.max_connections_percent:
                        reasons.append(f"connections={used:.1f}%")
                cache = (
                    status.get("wiredTiger", {})
                    .get("cache", {})
                )
                maximum = float(
                    cache.get("maximum bytes configured", 0) or 0
                )
                cached = float(cache.get("bytes currently in the cache", 0) or 0)
                if maximum > 0:
                    used = cached * 100 / maximum
                    if used >= self.options.max_cache_percent:
                        reasons.append(f"WiredTiger cache={used:.1f}%")
                db_stats = self.target_db.command("dbStats")
                total = float(db_stats.get("fsTotalSize", 0) or 0)
                used_size = float(db_stats.get("fsUsedSize", 0) or 0)
                if total > 0:
                    free = max(0.0, (total - used_size) * 100 / total)
                    if free <= self.options.min_disk_free_percent:
                        reasons.append(f"disk free={free:.1f}%")
            except Exception as exc:  # noqa: BLE001
                if self.options.production_safe_mode:
                    reasons.append(f"target resource metrics unavailable: {exc}")
                else:
                    log.warning("runtime resource guard unavailable: %s", exc)
                    return
            if not reasons:
                return
            message = "; ".join(reasons)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"runtime safety guard timed out: {message}")
            self.store.event(
                self.job_id, "WARNING", "runtime_safety_pause",
                message,
            )
            log.warning("runtime safety pause; retrying in 5 seconds: %s", message)
            if self.cancelled.wait(5):
                raise MigrationCancelled("migration cancelled by operator")

    def _copy_indexes(self, source_name: str, target_name: str) -> None:
        models = []
        from pymongo import IndexModel

        for index in self.source_db[source_name].list_indexes():
            spec = dict(index)
            if spec.get("name") == "_id_":
                continue
            keys = list(spec.pop("key").items())
            for field in ("v", "ns", "background", "buildUUID"):
                spec.pop(field, None)
            models.append(IndexModel(keys, **spec))
        if models:
            self._runtime_safety_guard(force=True)
            self.target_db[target_name].create_indexes(models)

    def _cutover(self, final_name: str, staging_name: str) -> None:
        self._runtime_safety_guard(force=True)
        existing = set(self.target_db.list_collection_names())
        backup = f"{final_name}__backup_{int(time.time())}"
        moved_old = False
        self.store.checkpoint(self.job_id, final_name, "cutover_started", True)
        try:
            if final_name in existing:
                self.target_db[final_name].rename(backup, dropTarget=False)
                moved_old = True
            self.target_db[staging_name].rename(final_name, dropTarget=False)
            self.store.event(
                self.job_id, "INFO", "cutover",
                f"{staging_name} renamed to {final_name}; backup={backup if moved_old else 'none'}",
                final_name,
            )
            self.store.checkpoint(self.job_id, final_name, "cutover_done", True)
        except Exception:
            if moved_old and final_name not in self.target_db.list_collection_names():
                self.target_db[backup].rename(final_name, dropTarget=False)
            raise

    def _cutover_already_finished(self, final_name: str, staging_name: str) -> bool:
        """Recover a crash after renameCollection but before task completion."""
        if not self.options.cutover:
            return False
        started = self.store.get_checkpoint(
            self.job_id, final_name, "cutover_started"
        )
        names = set(self.target_db.list_collection_names())
        return bool(started and staging_name not in names and final_name in names)


def _subtract_overlap(value: Any, seconds: float) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value - timedelta(seconds=seconds)
    if isinstance(value, (int, float)):
        return value - seconds
    raise TypeError("incremental field must contain BSON date or numeric values")


def _safe_endpoint(uri: str, database: str) -> str:
    """Persist an endpoint fingerprint, never URI credentials."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(uri)
        hosts = parsed.netloc.rsplit("@", 1)[-1]
        scheme = parsed.scheme or "mongodb"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{hosts}/{database}{query}"
    except ValueError:
        return f"sha256:{hashlib.sha256(uri.encode()).hexdigest()[:16]}/{database}"
