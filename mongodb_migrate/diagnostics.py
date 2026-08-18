"""Create a credential-free, data-minimized support bundle."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from .product_info import PRODUCT_NAME, PRODUCT_VERSION
from .store import MigrationStore

SENSITIVE_KEYS = {
    "password", "pass", "secret", "token", "credentials", "query",
    "source_uri", "target_uri", "uri", "document", "payload_json",
}


def _safe_uri(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        if not parsed.scheme:
            return value
        return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:  # noqa: BLE001
        return "<redacted-uri>"


def redact(value: Any, key: str = "") -> Any:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in SENSITIVE_KEYS or any(
        marker in normalized for marker in ("password", "secret", "credential", "token")
    ):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str) and normalized == "options_json":
        try:
            return json.dumps(redact(json.loads(value)), ensure_ascii=False)
        except ValueError:
            return "<redacted-options>"
    if isinstance(value, str) and ("uri" in normalized or normalized.endswith("endpoint")):
        return _safe_uri(value)
    if isinstance(value, str):
        return re.sub(
            r"(mongodb(?:\+srv)?://)[^/@\s]+@",
            r"\1<redacted>@",
            value,
            flags=re.IGNORECASE,
        )
    return value


def create_diagnostic_bundle(
    state_db: str | Path,
    job_id: str,
    output: str | Path,
    *,
    config_path: str | Path | None = None,
) -> Path:
    """Export audit metadata only; never include SQLite, credentials or DLQ data."""
    output_path = Path(output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    store = MigrationStore(state_db)
    try:
        report = redact(store.report(job_id))
    finally:
        store.close()
    files: dict[str, bytes] = {
        "system.json": json.dumps({
            "product": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "created_at": time.time(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
        }, ensure_ascii=False, indent=2).encode(),
        "audit_report.redacted.json": json.dumps(
            report, ensure_ascii=False, indent=2
        ).encode(),
        "README.txt": (
            b"This data-minimized support bundle excludes credentials, source documents, "
            b"DLQ records, query filters and the SQLite state database.\n"
        ),
    }
    if config_path and Path(config_path).expanduser().exists():
        try:
            config = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
            files["gui_config.redacted.json"] = json.dumps(
                redact(config), ensure_ascii=False, indent=2
            ).encode()
        except (OSError, ValueError) as exc:
            files["config_error.txt"] = str(exc).encode()
    manifest = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(files.items())
    }
    files["MANIFEST.sha256.json"] = json.dumps(
        manifest, indent=2, sort_keys=True
    ).encode()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    return output_path
