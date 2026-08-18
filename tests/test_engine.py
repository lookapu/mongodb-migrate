from datetime import datetime, timezone

from bson import BSON

from mongodb_migrate.config import MigrationOptions
from mongodb_migrate.engine import (
    MigrationEngine,
    _safe_endpoint,
    _subtract_overlap,
    batches,
    merge_query,
    stable_digest,
)


def test_batches_respect_count():
    documents = [{"_id": i, "payload": "x"} for i in range(5)]
    result = list(batches(documents, max_count=2, max_bytes=1024 * 1024))
    assert [len(item) for item in result] == [2, 2, 1]


def test_batches_respect_encoded_size():
    documents = [{"_id": i, "payload": "x" * 100} for i in range(3)]
    one_size = len(BSON.encode(documents[0]))
    result = list(batches(documents, max_count=10, max_bytes=one_size + 10))
    assert [len(item) for item in result] == [1, 1, 1]


def test_merge_query_preserves_both_filters():
    assert merge_query({"tenant": 7}, {"_id": {"$gt": 10}}) == {
        "$and": [{"tenant": 7}, {"_id": {"$gt": 10}}]
    }


def test_datetime_overlap():
    value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert (_subtract_overlap(value, 30) - value).total_seconds() == -30


def test_endpoint_redacts_credentials():
    endpoint = _safe_endpoint(
        "mongodb://alice:very-secret@mongo.internal:27017/?replicaSet=rs0", "app"
    )
    assert endpoint == "mongodb://mongo.internal:27017/app?replicaSet=rs0"
    assert "alice" not in endpoint
    assert "secret" not in endpoint


def test_digest_ignores_document_key_order():
    assert stable_digest({"_id": 1, "nested": {"b": 2, "a": 1}}) == stable_digest(
        {"nested": {"a": 1, "b": 2}, "_id": 1}
    )


class FakeSource:
    def __init__(self, document=None):
        self.document = document
        self.queries = []

    def find_one(self, query):
        self.queries.append(query)
        return self.document


def cdc_engine(query=None):
    engine = object.__new__(MigrationEngine)
    engine.options = MigrationOptions(
        source_uri="mongodb://source",
        target_uri="mongodb://target",
        source_db="app",
        target_db="app",
        query=query,
    )
    engine.written = []
    engine.deleted = []
    engine._write_batch = lambda target, name, docs: engine.written.extend(docs)
    engine._delete_with_retry = lambda target, document_id: engine.deleted.append(
        document_id
    )
    return engine


def test_cdc_applies_insert_full_document():
    engine = cdc_engine()
    document = {"_id": 7, "name": "new"}
    engine._apply_change_event(
        FakeSource(), object(), "users",
        {
            "operationType": "insert",
            "documentKey": {"_id": 7},
            "fullDocument": document,
        },
    )
    assert engine.written == [document]
    assert engine.deleted == []


def test_cdc_delete_removes_target_document():
    engine = cdc_engine()
    engine._apply_change_event(
        FakeSource(), object(), "users",
        {"operationType": "delete", "documentKey": {"_id": 9}},
    )
    assert engine.deleted == [9]


def test_cdc_filtered_update_deletes_document_that_left_query():
    engine = cdc_engine({"active": True})
    source = FakeSource(document=None)
    engine._apply_change_event(
        source, object(), "users",
        {
            "operationType": "update",
            "documentKey": {"_id": 11},
            "fullDocument": {"_id": 11, "active": False},
        },
    )
    assert engine.deleted == [11]
    assert source.queries == [
        {"$and": [{"active": True}, {"_id": 11}]}
    ]
