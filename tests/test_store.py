import pytest

from mongodb_migrate.store import MigrationStore


def test_job_checkpoint_and_report(tmp_path):
    store = MigrationStore(tmp_path / "state.sqlite3")
    try:
        job_id = store.create_or_resume("source/app", "target/app", {"batch": 10}, "job-1")
        store.acquire(job_id, "owner", 60)
        store.add_collection(job_id, "users", "users__migrating")
        store.checkpoint(job_id, "users", "last_id", {"compound": 42})
        store.update_collection(job_id, "users", status="completed", copied_docs=3)
        store.event(job_id, "INFO", "done", "finished", "users")
        store.finish(job_id, "completed")

        assert store.get_checkpoint(job_id, "users", "last_id") == {"compound": 42}
        report = store.report(job_id)
        assert report["job"]["status"] == "completed"
        assert report["collections"][0]["copied_docs"] == 3
        assert report["events"][0]["type"] == "done"
    finally:
        store.close()


def test_live_lease_blocks_second_owner(tmp_path):
    store = MigrationStore(tmp_path / "state.sqlite3")
    try:
        job_id = store.create_or_resume("source", "target", {}, "job-1")
        store.acquire(job_id, "owner-a", 60)
        with pytest.raises(RuntimeError, match="already running"):
            store.acquire(job_id, "owner-b", 60)
    finally:
        store.close()


def test_resume_rejects_changed_options(tmp_path):
    store = MigrationStore(tmp_path / "state.sqlite3")
    try:
        store.create_or_resume("source", "target", {"batch": 10}, "job-1")
        with pytest.raises(ValueError, match="different"):
            store.create_or_resume("source", "target", {"batch": 20}, "job-1")
    finally:
        store.close()


def test_target_collection_lease_blocks_different_job(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = MigrationStore(path)
    second = MigrationStore(path)
    try:
        first_job = first.create_or_resume("source/a", "target/b", {}, "job-a")
        second_job = second.create_or_resume("source/c", "target/b", {}, "job-b")
        first.add_collection(first_job, "users", "users__migrating")
        second.add_collection(second_job, "users", "users__migrating")
        first.acquire_target_leases(
            first_job, "owner-a", "mongodb://target/b", "b", 60
        )
        with pytest.raises(RuntimeError, match="leased by job job-a"):
            second.acquire_target_leases(
                second_job, "owner-b", "mongodb://target/b", "b", 60
            )
        first.finish(first_job, "completed")
        second.acquire_target_leases(
            second_job, "owner-b", "mongodb://target/b", "b", 60
        )
    finally:
        first.close()
        second.close()


def test_backup_asset_catalog_and_retention(tmp_path):
    store = MigrationStore(tmp_path / "state.sqlite3")
    archive = tmp_path / "daily.mmbackup"
    archive.write_bytes(b"backup")
    try:
        asset_id = store.register_asset(
            kind="backup",
            path=archive,
            collections=3,
            documents=42,
            size_bytes=6,
            sha256="abc",
            encrypted=True,
            retention_days=7,
        )
        assets = store.list_assets()
        assert assets[0]["id"] == asset_id
        assert assets[0]["documents"] == 42
        assert assets[0]["encrypted"] == 1
        assert store.expired_assets(now=assets[0]["retention_until"] + 1)[0]["id"] == asset_id
        store.forget_asset(asset_id)
        assert not store.list_assets()
        assert archive.exists()
    finally:
        store.close()
