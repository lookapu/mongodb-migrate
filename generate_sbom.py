#!/usr/bin/env python3
"""Generate an offline SPDX 2.3 JSON software bill of materials."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import time
import uuid
from collections import deque
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from mongodb_migrate.product_info import PRODUCT_NAME, PRODUCT_VERSION


def _spdx_id(name: str) -> str:
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]+", "-", name)


def _runtime_requirements(
    distribution: importlib.metadata.Distribution,
) -> list[Requirement]:
    requirements: list[Requirement] = []
    for raw_requirement in distribution.requires or []:
        requirement = Requirement(raw_requirement)
        # Optional dependency groups are build/test features, not dependencies of
        # the frozen default product. Normal environment markers still evaluate
        # for the host used to create this platform-specific SBOM.
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        requirements.append(requirement)
    return requirements


def _declared_license(distribution: importlib.metadata.Distribution) -> str:
    return distribution.metadata.get("License-Expression") or "NOASSERTION"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    product_id = "SPDXRef-Package-MongoDB-Migrate"
    packages = [{
        "SPDXID": product_id,
        "name": PRODUCT_NAME,
        "versionInfo": PRODUCT_VERSION,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "MIT",
    }]
    relationships = []
    relationship_keys: set[tuple[str, str]] = set()
    seen: set[str] = set()
    root_distribution = importlib.metadata.distribution("mongodb-migrate")
    queue = deque(
        (product_id, requirement.name)
        for requirement in _runtime_requirements(root_distribution)
    )
    while queue:
        parent_id, requested_name = queue.popleft()
        distribution = importlib.metadata.distribution(requested_name)
        name = distribution.metadata.get("Name") or requested_name
        canonical_name = canonicalize_name(name)
        package_id = _spdx_id(canonical_name)
        relationship_key = (parent_id, package_id)
        if relationship_key not in relationship_keys:
            relationship_keys.add(relationship_key)
            relationships.append({
                "spdxElementId": parent_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_id,
            })
        if canonical_name in seen:
            continue
        seen.add(canonical_name)
        packages.append({
            "SPDXID": package_id,
            "name": name,
            "versionInfo": distribution.version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": _declared_license(distribution),
        })
        queue.extend(
            (package_id, requirement.name)
            for requirement in _runtime_requirements(distribution)
        )
    payload = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{PRODUCT_NAME}-{PRODUCT_VERSION}",
        "documentNamespace": (
            f"https://spdx.org/spdxdocs/mongodb-migrate-{PRODUCT_VERSION}-{uuid.uuid4()}"
        ),
        "creationInfo": {
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "creators": ["Tool: mongodb-migrate-generate-sbom"],
        },
        "documentDescribes": [product_id],
        "packages": packages,
        "relationships": relationships,
    }
    Path(args.output).write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
