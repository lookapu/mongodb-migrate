# Commercial release gates

## 2.0 All-in-One controls

- Native BSON backup/restore without a system Python or Database Tools dependency.
- Atomic output, mandatory read-back verification and per-member SHA-256 evidence.
- Optional authenticated AES-256-GCM encryption with non-persisted passphrases.
- Restore verifies the complete archive before the first target write.
- Backup asset inventory records hashes, verification time and retention metadata.
- JSONL/CSV is explicitly labeled data exchange rather than backup.
- The product states that logical per-collection reads are not cluster-wide PITR.

## 1.3.0 implemented product controls

- Single-source product metadata plus macOS and Windows version resources.
- Immutable plan preview, SHA-256 binding and explicit production approval.
- Job and target-collection leases preventing competing writers.
- Resumable Change Streams CDC, idempotent batches and DLQ failure isolation.
- Target connection, WiredTiger cache and disk-pressure runtime guards.
- Full-content verification fingerprints and structured audit evidence.
- Rotating logs, GUI crash reports and data-minimized diagnostic bundles.
- Signing/notarization hooks, SPDX 2.3 SBOM, checksums and release manifests.

These controls make the codebase release-ready; the checklist below still
requires evidence for each exact signed artifact. Commercial status is not
claimed merely from local tests or an ad-hoc signed build.

A build is a commercial candidate only when every applicable gate is recorded
as passed for the exact commit and artifact hash.

- [ ] Unit/static checks pass on Python 3.9 and 3.12.
- [ ] Live compatibility matrix passes on dedicated authenticated TLS clusters.
- [ ] CDC insert/update/delete, resume token and oplog-expiry behavior are tested.
- [ ] Primary stepdown, network interruption, disk pressure and process-kill
      fault injection are tested.
- [ ] Performance envelope and capacity recommendations are published.
- [ ] SPDX SBOM, dependency audit and CodeQL are clean or exceptions approved.
- [ ] macOS artifact has Developer ID signature, notarization and stapling.
- [ ] Windows artifacts have Authenticode signature and timestamp.
- [ ] Release hashes are published through a trusted channel.
- [ ] Backup, cutover and rollback runbooks have been rehearsed.
- [ ] Data handling, support window, vulnerability response and EOL policies are
      approved by the product owner.

Ad-hoc signatures, local unit tests or an unexecuted compatibility design do not
meet this gate.
