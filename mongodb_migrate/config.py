from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SYSTEM_COLLECTION_PREFIXES = ("system.",)


@dataclass(frozen=True)
class MigrationOptions:
    source_uri: str
    target_uri: str
    source_db: str
    target_db: str
    collections: str = "*"
    exclude: str = "system.*"
    target_suffix: str = "__migrating"
    query: dict[str, Any] | None = None
    batch_size: int = 1000
    batch_bytes: int = 12 * 1024 * 1024
    workers: int = 2
    docs_per_second: float = 0
    max_retries: int = 6
    retry_backoff: float = 0.5
    incremental_field: str = ""
    incremental_rounds: int = 0
    incremental_overlap_seconds: float = 120
    incremental_interval: float = 3
    convergence_rounds: int = 2
    cdc_enabled: bool = False
    cdc_quiet_seconds: float = 5
    cdc_max_seconds: float = 600
    verify: str = "count"
    sample_size: int = 200
    copy_indexes: bool = True
    cutover: bool = False
    dry_run: bool = False
    conflict: str = "fail"
    state_db: str = ".mongodb-migrate.sqlite3"
    dlq_dir: str = "dlq"
    job_id: str = ""
    lease_ttl: int = 60
    report_dir: str = "reports"
    production_safe_mode: bool = False
    continuous_writes: bool = False
    runtime_guard: bool = True
    max_cache_percent: float = 85
    max_connections_percent: float = 85
    min_disk_free_percent: float = 10
    safety_pause_timeout: float = 300
    approval_token: str = ""
    plan_only: bool = False

    def validate(self) -> None:
        if not self.source_db or not self.target_db:
            raise ValueError("source_db and target_db are required")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not 1024 <= self.batch_bytes <= 15 * 1024 * 1024:
            raise ValueError("batch_bytes must be between 1 KiB and 15 MiB")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.docs_per_second < 0:
            raise ValueError("docs_per_second cannot be negative")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.retry_backoff <= 0:
            raise ValueError("retry_backoff must be > 0")
        if self.verify not in {"none", "count", "sample", "full"}:
            raise ValueError("verify must be none, count, sample, or full")
        if self.sample_size < 1:
            raise ValueError("sample_size must be >= 1")
        if self.conflict not in {"fail", "resume"}:
            raise ValueError("conflict must be fail or resume")
        if self.incremental_rounds < 0:
            raise ValueError("incremental_rounds cannot be negative")
        if self.incremental_rounds and not self.incremental_field:
            raise ValueError("incremental_field is required when incremental_rounds > 0")
        if self.incremental_overlap_seconds < 0:
            raise ValueError("incremental_overlap_seconds cannot be negative")
        if self.incremental_interval < 0:
            raise ValueError("incremental_interval cannot be negative")
        if self.convergence_rounds < 1:
            raise ValueError("convergence_rounds must be >= 1")
        if self.cdc_enabled and self.incremental_rounds:
            raise ValueError("CDC and watermark incremental sync cannot be enabled together")
        if self.cdc_quiet_seconds <= 0:
            raise ValueError("cdc_quiet_seconds must be > 0")
        if self.cdc_max_seconds <= self.cdc_quiet_seconds:
            raise ValueError("cdc_max_seconds must be greater than cdc_quiet_seconds")
        if self.lease_ttl < 5:
            raise ValueError("lease_ttl must be >= 5 seconds")
        if not 1 <= self.max_cache_percent <= 100:
            raise ValueError("max_cache_percent must be between 1 and 100")
        if not 1 <= self.max_connections_percent <= 100:
            raise ValueError("max_connections_percent must be between 1 and 100")
        if not 0 <= self.min_disk_free_percent < 100:
            raise ValueError("min_disk_free_percent must be between 0 and 100")
        if self.safety_pause_timeout < 0:
            raise ValueError("safety_pause_timeout cannot be negative")
        if self.production_safe_mode:
            if self.conflict != "fail":
                raise ValueError("production safe mode forbids resume conflict policy")
            if self.verify != "full":
                raise ValueError("production safe mode requires full verification")
            if self.cutover and self.continuous_writes:
                raise ValueError(
                    "production safe mode forbids cutover while continuous writes are declared"
                )
            if self.continuous_writes and not self.cdc_enabled:
                raise ValueError(
                    "continuous writes require Change Streams in production safe mode"
                )
        if self.cutover and not self.target_suffix:
            raise ValueError("cutover requires a non-empty target_suffix")
        if self.cutover and self.dry_run:
            raise ValueError("cutover and dry_run cannot be used together")
        if (
            self.source_uri == self.target_uri
            and self.source_db == self.target_db
            and not self.target_suffix
        ):
            raise ValueError("same-database migration requires target_suffix")

    def durable_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("job_id", None)
        # Credentials can be embedded in MongoDB URIs. They never belong in
        # the local SQLite audit database.
        data.pop("source_uri", None)
        data.pop("target_uri", None)
        # This is operator intent after a crash, not part of data semantics.
        data.pop("conflict", None)
        # Approval is bound to a generated plan and must not become durable job
        # semantics or appear in reports.
        data.pop("approval_token", None)
        data.pop("plan_only", None)
        return data

    @property
    def state_path(self) -> Path:
        return Path(self.state_db).expanduser()

    @property
    def dlq_path(self) -> Path:
        return Path(self.dlq_dir).expanduser()

    @property
    def report_path(self) -> Path:
        return Path(self.report_dir).expanduser()


def csv_patterns(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def select_collections(names: list[str], include: str, exclude: str) -> list[str]:
    includes = csv_patterns(include) or ["*"]
    excludes = csv_patterns(exclude)
    selected = []
    for name in names:
        if name.startswith(SYSTEM_COLLECTION_PREFIXES):
            continue
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in includes):
            continue
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in excludes):
            continue
        selected.append(name)
    return sorted(selected)
