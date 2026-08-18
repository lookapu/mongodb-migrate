from __future__ import annotations

import json
import sys

import generate_sbom


def test_sbom_contains_runtime_dependency_graph_only(tmp_path, monkeypatch):
    output = tmp_path / "sbom.json"
    monkeypatch.setattr(sys, "argv", ["generate_sbom.py", "--output", str(output)])

    assert generate_sbom.main() == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    package_names = {package["name"].lower() for package in payload["packages"]}
    assert {"mongodb migrate", "pymongo", "dnspython", "cryptography", "tqdm"} <= package_names
    assert {"pytest", "ruff", "pyinstaller"}.isdisjoint(package_names)
    relationships = {
        (item["spdxElementId"], item["relatedSpdxElement"])
        for item in payload["relationships"]
    }
    assert ("SPDXRef-Package-MongoDB-Migrate", "SPDXRef-Package-pymongo") in relationships
    assert ("SPDXRef-Package-pymongo", "SPDXRef-Package-dnspython") in relationships
