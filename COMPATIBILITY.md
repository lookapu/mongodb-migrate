# Compatibility and acceptance policy

## Designed range

- Python runtime: 3.9–3.12 for CLI; packaged GUI uses Python 3.12.
- PyMongo: 4.6–4.x.
- MongoDB source and target: 4.4–8.x API families.
- Topologies: standalone for offline/watermark migration; replica set or sharded
  cluster required for Change Streams CDC.
- Native `.mmbackup`: product-owned logical BSON format version 1, Zip64 capable;
  restore compatibility is governed by BSON, collection option and target server support.
- Backup consistency: majority-committed reads per collection cursor, not a single
  point-in-time snapshot across all collections or shards.
- Encrypted backup: AES-256-GCM through the bundled `cryptography` runtime.

“Designed range” is not the same as a certified combination.

## Certification gate

A release may only be labelled certified after the opt-in live suite passes for
each claimed source/target combination with:

- TLS and authentication enabled;
- full copy, restart/resume and index recreation;
- insert/update/delete CDC and resume-token recovery;
- primary stepdown and transient network interruption;
- count, sample and full verification;
- cutover failure and rollback rehearsal;
- at least one production-representative data volume and document-size profile.
- unencrypted/encrypted backup, intentional corruption rejection and restore rehearsal;
- time-series/capped/validator/index metadata restore for every claimed server family.

Run the live suite without Docker:

```bash
export MONGODB_MIGRATE_RUN_INTEGRATION=1
export MONGODB_MIGRATE_IT_SOURCE_URI='mongodb://.../?replicaSet=source-rs'
export MONGODB_MIGRATE_IT_TARGET_URI='mongodb://.../?replicaSet=target-rs'
pytest -m integration -v
```

The suite creates uniquely named temporary databases and removes only those
databases in its cleanup phase. Use dedicated acceptance clusters.
