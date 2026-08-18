# Security Policy

## Supported versions

Only the latest minor release receives security fixes until a formal LTS policy
is published.

## Reporting

Do not open a public issue for a suspected vulnerability. Use the repository's
private GitHub Security Advisory flow and include:

- affected version and operating system;
- reproduction steps without production credentials or customer data;
- expected impact;
- suggested mitigation, if known.

Connection URIs, database samples, DLQ records and SQLite state files may contain
sensitive operational metadata and must never be attached to a public report.

## Product security properties

- GUI connection URIs remain memory-only and are excluded from saved settings.
- SQLite state stores redacted endpoint identities rather than URI credentials.
- Target writes use shadow collections by default.
- Fatal write errors fail the job and are not silently ignored.
- Native backups are atomically published only after complete read-back verification.
- Optional backup encryption uses PBKDF2-SHA256 and authenticated AES-256-GCM.
- Backup passwords remain memory-only and are excluded from settings, SQLite and manifests.
- Restore verifies all archive segments before the first target write by default.
- Release pipelines generate an SBOM and run dependency and CodeQL scans.
