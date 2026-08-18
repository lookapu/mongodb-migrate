# Changelog

## 2.0.0

- Added a standalone native BSON backup format with atomic writes and Zip64.
- Added per-segment SHA-256, manifest binding and mandatory read-back verification.
- Added optional PBKDF2-SHA256 + AES-256-GCM authenticated backup encryption.
- Added collection options/index preservation and pre-write verified restore.
- Added Extended JSONL and explicit-schema CSV import/export workflows.
- Added a durable backup asset catalog with verification and retention metadata.
- Added the fourth GUI workspace, “Backup & Exchange”, with safe restore controls.
- Unified migration, backup, restore, verify, inspect, export, import and list commands.

## 1.3.0

- Added production-safe mode with deterministic plans and approval codes.
- Added cross-job target collection leases and runtime resource guards.
- Persisted full-verification fingerprints, sample counts and mismatch evidence.
- Added rotating GUI logs, crash reports and redacted diagnostic bundles.
- Centralized product metadata and added native platform version resources.
- Added offline SPDX SBOM, release manifests and checksums to platform builds.
- Expanded the GUI with safety controls, threshold help and diagnostics export.

## 1.2.0

- Added resumable Change Streams CDC for insert, update, replace and delete.
- Persisted CDC start cluster time and per-event resume tokens.
- Added CDC topology preflight, quiet-window and maximum catch-up limits.
- Moved index creation before final CDC drain to cover writes during index builds.
- Added GUI and CLI controls for CDC.
- Added opt-in live replica-set acceptance tests without Docker.
- Added CodeQL, dependency audit and CycloneDX SBOM workflow.
- Added Developer ID/notarization and Authenticode signing scripts.
- Added compatibility, security and commercial release gate documentation.
- Added complete standalone Windows GUI/CLI build flow.

## 1.1.0

- Redesigned the GUI with fixed-size tabs, option help, collection search and
  production safety controls.
- Added independent Python 3.12 and Tcl/Tk 9 macOS application packaging.

## 1.0.0

- Initial durable full-copy, watermark sync, verification, DLQ and cutover engine.
