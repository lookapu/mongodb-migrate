#!/usr/bin/env python3
"""Generate a machine-readable release artifact manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

from mongodb_migrate.product_info import (
    PRODUCT_CHANNEL,
    PRODUCT_NAME,
    PRODUCT_VERSION,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--signing", required=True)
    parser.add_argument("artifacts", nargs="+")
    args = parser.parse_args()
    artifacts = []
    for raw in args.artifacts:
        path = Path(raw)
        content = path.read_bytes()
        artifacts.append({
            "name": path.name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    payload = {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "channel": PRODUCT_CHANNEL,
        "platform": args.platform,
        "architecture": args.architecture,
        "signing": args.signing,
        "build_host": platform.platform(),
        "created_at": time.time(),
        "artifacts": artifacts,
    }
    Path(args.output).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
