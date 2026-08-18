from __future__ import annotations

import json
import zipfile

from mongodb_migrate.config import MigrationOptions
from mongodb_migrate.diagnostics import create_diagnostic_bundle
from mongodb_migrate.engine import MigrationEngine
from mongodb_migrate.store import MigrationStore


def options(tmp_path, **changes):
    values = {
        "source_uri": "mongodb://user:source-secret@source:27017",
        "target_uri": "mongodb://user:target-secret@target:27017",
        "source_db": "source_db",
        "target_db": "target_db",
        "query": {"tenant_secret": "private"},
        "state_db": str(tmp_path / "state.sqlite3"),
        "report_dir": str(tmp_path / "reports"),
    }
    values.update(changes)
    return MigrationOptions(**values)


def test_execution_plan_is_deterministic_and_hides_query(tmp_path):
    engine = object.__new__(MigrationEngine)
    engine.options = options(tmp_path)
    engine.job_id = "job-1"
    first = engine.build_execution_plan(
        ["users", "orders"],
        "mongodb://source/source_db",
        "mongodb://target/target_db",
    )
    second = engine.build_execution_plan(
        ["orders", "users"],
        "mongodb://source/source_db",
        "mongodb://target/target_db",
    )
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["approval_code"] == first["plan_sha256"][:8].upper()
    assert "private" not in json.dumps(first)


def test_diagnostic_bundle_redacts_credentials_query_and_payload(tmp_path):
    opts = options(tmp_path)
    store = MigrationStore(opts.state_db)
    job = store.create_or_resume(
        "mongodb://source/source_db",
        "mongodb://target/target_db",
        opts.durable_dict(),
        "job-1",
    )
    store.add_collection(job, "users", "users__migrating")
    store.event(
        job,
        "ERROR",
        "sample",
        "mongodb://user:password@source:27017 failed",
        payload={"document": {"email": "person@example.test"}},
    )
    store.close()
    output = create_diagnostic_bundle(
        opts.state_db, job, tmp_path / "diagnostics.zip"
    )
    with zipfile.ZipFile(output) as archive:
        report = archive.read("audit_report.redacted.json").decode()
    assert "source-secret" not in report
    assert "target-secret" not in report
    assert "tenant_secret" not in report
    assert "person@example.test" not in report
    assert "password@" not in report
