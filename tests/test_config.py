import pytest

from mongodb_migrate.config import MigrationOptions, select_collections


def make_options(**overrides):
    values = {
        "source_uri": "mongodb://source",
        "target_uri": "mongodb://target",
        "source_db": "app",
        "target_db": "app",
    }
    values.update(overrides)
    return MigrationOptions(**values)


def test_collection_selection_uses_globs_and_exclusions():
    names = ["users", "orders_2025", "orders_tmp", "system.profile", "audit"]
    assert select_collections(names, "users,orders_*", "*_tmp") == [
        "orders_2025",
        "users",
    ]


def test_incremental_sync_requires_watermark_field():
    with pytest.raises(ValueError, match="incremental_field"):
        make_options(incremental_rounds=2).validate()


def test_cutover_requires_shadow_suffix():
    with pytest.raises(ValueError, match="target_suffix"):
        make_options(cutover=True, target_suffix="").validate()


def test_durable_options_do_not_contain_credentials():
    options = make_options(
        source_uri="mongodb://alice:secret@source",
        target_uri="mongodb://bob:secret@target",
    )
    durable = options.durable_dict()
    assert "source_uri" not in durable
    assert "target_uri" not in durable
    assert "secret" not in str(durable)


def test_resume_conflict_policy_is_not_part_of_data_identity():
    assert make_options(conflict="fail").durable_dict() == make_options(
        conflict="resume"
    ).durable_dict()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_retries", -1, "max_retries"),
        ("retry_backoff", 0, "retry_backoff"),
        ("sample_size", 0, "sample_size"),
        ("convergence_rounds", 0, "convergence_rounds"),
        ("lease_ttl", 4, "lease_ttl"),
    ],
)
def test_runtime_safety_limits(field, value, message):
    with pytest.raises(ValueError, match=message):
        make_options(**{field: value}).validate()


def test_change_stream_and_watermark_are_mutually_exclusive():
    with pytest.raises(ValueError, match="cannot be enabled together"):
        make_options(
            cdc_enabled=True,
            incremental_field="updated_at",
            incremental_rounds=2,
        ).validate()


def test_change_stream_max_time_must_exceed_quiet_window():
    with pytest.raises(ValueError, match="cdc_max_seconds"):
        make_options(cdc_enabled=True, cdc_quiet_seconds=10, cdc_max_seconds=10).validate()


def test_production_safe_mode_enforces_full_verification():
    with pytest.raises(ValueError, match="full verification"):
        make_options(production_safe_mode=True, verify="sample").validate()


def test_production_continuous_writes_require_cdc():
    with pytest.raises(ValueError, match="Change Streams"):
        make_options(
            production_safe_mode=True,
            continuous_writes=True,
            verify="full",
        ).validate()
