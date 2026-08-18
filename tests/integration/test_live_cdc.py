"""Opt-in live replica-set acceptance test. No Docker is used."""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from pymongo import MongoClient

from mongodb_migrate.config import MigrationOptions
from mongodb_migrate.engine import MigrationEngine

pytestmark = pytest.mark.integration


def test_live_full_copy_and_change_stream_delete(tmp_path):
    if os.getenv("MONGODB_MIGRATE_RUN_INTEGRATION") != "1":
        pytest.skip("set MONGODB_MIGRATE_RUN_INTEGRATION=1 for live acceptance")
    source_uri = os.environ["MONGODB_MIGRATE_IT_SOURCE_URI"]
    target_uri = os.environ["MONGODB_MIGRATE_IT_TARGET_URI"]
    suffix = uuid.uuid4().hex[:10]
    source_db = f"mongodb_migrate_it_source_{suffix}"
    target_db = f"mongodb_migrate_it_target_{suffix}"
    source_client = MongoClient(source_uri)
    target_client = MongoClient(target_uri)
    source = source_client[source_db]["records"]
    source.insert_many(
        [
            {"_id": 1, "value": "before"},
            {"_id": 2, "value": "will-delete"},
        ]
    )

    def mutate_during_copy() -> None:
        time.sleep(0.5)
        source.replace_one({"_id": 1}, {"_id": 1, "value": "after"})
        source.delete_one({"_id": 2})
        source.insert_one({"_id": 3, "value": "inserted"})

    writer = threading.Thread(target=mutate_during_copy)
    writer.start()
    try:
        job_id = MigrationEngine(
            MigrationOptions(
                source_uri=source_uri,
                target_uri=target_uri,
                source_db=source_db,
                target_db=target_db,
                collections="records",
                target_suffix="__shadow",
                cdc_enabled=True,
                cdc_quiet_seconds=2,
                cdc_max_seconds=30,
                verify="full",
                state_db=str(tmp_path / "state.sqlite3"),
                dlq_dir=str(tmp_path / "dlq"),
            )
        ).run()
        assert job_id
        migrated = list(
            target_client[target_db]["records__shadow"].find().sort("_id", 1)
        )
        assert migrated == [
            {"_id": 1, "value": "after"},
            {"_id": 3, "value": "inserted"},
        ]
    finally:
        writer.join(timeout=5)
        source_client.drop_database(source_db)
        target_client.drop_database(target_db)
        source_client.close()
        target_client.close()

