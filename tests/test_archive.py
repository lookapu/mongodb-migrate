from __future__ import annotations

import json
import zipfile

import pytest
from bson import ObjectId

from mongodb_migrate import archive
from mongodb_migrate.archive import (
    BackupOptions,
    RestoreOptions,
    create_backup,
    inspect_backup,
    restore_backup,
    verify_backup,
)


class FakeCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def sort(self, *_args, **_kwargs):
        return self

    def __iter__(self):
        return iter(self.documents)

    def close(self):
        return None


class SourceCollection:
    def __init__(self, documents):
        self.documents = documents

    def list_indexes(self):
        return iter([
            {"name": "_id_", "key": {"_id": 1}, "v": 2},
            {"name": "email_1", "key": {"email": 1}, "unique": True, "v": 2},
        ])

    def with_options(self, **_kwargs):
        return self

    def find(self, *_args, **_kwargs):
        return FakeCursor(self.documents)


class SourceDatabase:
    def __init__(self, documents):
        self.collection = SourceCollection(documents)

    def list_collection_names(self, **_kwargs):
        return ["users"]

    def list_collections(self):
        return iter([{
            "name": "users",
            "type": "collection",
            "options": {"validator": {"email": {"$type": "string"}}},
        }])

    def __getitem__(self, _name):
        return self.collection


class Admin:
    def command(self, name):
        assert name == "ping"
        return {"ok": 1}


class SourceClient:
    def __init__(self, documents):
        self.admin = Admin()
        self.database = SourceDatabase(documents)

    def __getitem__(self, _name):
        return self.database

    def server_info(self):
        return {"version": "8.0.0"}

    def close(self):
        return None


class TargetCollection:
    def __init__(self):
        self.writes = 0
        self.indexes = []

    def bulk_write(self, operations, **_kwargs):
        self.writes += len(operations)

    def create_indexes(self, models):
        self.indexes.extend(models)

    def count_documents(self, _query):
        return self.writes


class TargetDatabase:
    def __init__(self):
        self.collections = {}
        self.created_options = {}

    def list_collection_names(self, **_kwargs):
        return list(self.collections)

    def create_collection(self, name, **options):
        self.collections[name] = TargetCollection()
        self.created_options[name] = options

    def drop_collection(self, name):
        self.collections.pop(name, None)

    def __getitem__(self, name):
        return self.collections.setdefault(name, TargetCollection())


class TargetClient:
    def __init__(self):
        self.admin = Admin()
        self.database = TargetDatabase()

    def __getitem__(self, _name):
        return self.database

    def close(self):
        return None


def test_native_bson_backup_verify_and_restore(tmp_path, monkeypatch):
    documents = [
        {"_id": ObjectId(), "email": "one@example.test"},
        {"_id": ObjectId(), "email": "two@example.test"},
    ]
    source_client = SourceClient(documents)
    monkeypatch.setattr(archive, "MongoClient", lambda *_args, **_kwargs: source_client)
    output = tmp_path / "backup.mmbackup"
    result = create_backup(BackupOptions(
        source_uri="mongodb://user:secret@source:27017",
        source_db="app",
        output=str(output),
    ))
    assert result["documents"] == 2
    assert "secret" not in json.dumps(result["manifest"])
    assert verify_backup(output)["sha256"] == result["sha256"]

    target_client = TargetClient()
    monkeypatch.setattr(archive, "MongoClient", lambda *_args, **_kwargs: target_client)
    restored = restore_backup(RestoreOptions(
        target_uri="mongodb://target",
        target_db="restored",
        input=str(output),
    ))
    assert restored["documents"] == 2
    assert target_client.database.collections["users"].writes == 2
    assert len(target_client.database.collections["users"].indexes) == 1
    assert "validator" in target_client.database.created_options["users"]


def test_backup_verifier_rejects_modified_data(tmp_path, monkeypatch):
    source_client = SourceClient([{"_id": 1, "value": "original"}])
    monkeypatch.setattr(archive, "MongoClient", lambda *_args, **_kwargs: source_client)
    output = tmp_path / "backup.mmbackup"
    create_backup(BackupOptions(
        source_uri="mongodb://source", source_db="app", output=str(output)
    ))
    rewritten = tmp_path / "changed.mmbackup"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(rewritten, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name.startswith("data/"):
                content += b"corruption"
            target.writestr(name, content)
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_backup(rewritten)


def test_encrypted_backup_round_trip(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    source_client = SourceClient([{"_id": 1, "secret": "payload"}])
    monkeypatch.setattr(archive, "MongoClient", lambda *_args, **_kwargs: source_client)
    output = tmp_path / "encrypted.mmbackup"
    result = create_backup(BackupOptions(
        source_uri="mongodb://source",
        source_db="app",
        output=str(output),
        encryption_password="correct horse battery staple",
    ))
    assert result["encrypted"] is True
    assert b"payload" not in output.read_bytes()
    assert inspect_backup(
        output, password="correct horse battery staple"
    )["documents"] == 1
    with pytest.raises(ValueError, match="invalid backup password"):
        inspect_backup(output, password="wrong password value")
