#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
python_bin="${MONGODB_MIGRATE_PYTHON:-python3}"
"$python_bin" -c 'import sys, tkinter; assert sys.version_info >= (3, 10); assert tuple(map(int, tkinter.Tcl().call("info", "patchlevel").split(".")[:2])) >= (8, 6), "Tk 8.6+ required"'
"$python_bin" -m venv --clear build_venv
build_venv/bin/python -m pip install --upgrade pip
build_venv/bin/python -m pip install '.[dev]'
build_venv/bin/python -m pytest -q
build_venv/bin/pyinstaller --clean --noconfirm mongo_migrate.spec
build_venv/bin/pyinstaller --clean --noconfirm mongo_migrate_app.spec
"dist/MongoDB Migrate.app/Contents/MacOS/MongoDB Migrate" --smoke-test
if [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  scripts/sign_macos.sh
  signing_kind="developer-id-notarized"
else
  echo "WARNING: producing an ad-hoc signed development build, not a commercial release"
  codesign --force --deep --sign - "dist/MongoDB Migrate.app"
  ditto -c -k --keepParent \
    "dist/MongoDB Migrate.app" "dist/MongoDB-Migrate-macOS-arm64.zip"
  signing_kind="ad-hoc"
fi
build_venv/bin/python generate_sbom.py --output dist/SBOM.spdx.json
build_venv/bin/python release_manifest.py \
  --output dist/RELEASE.json \
  --platform macos --architecture arm64 --signing "$signing_kind" \
  dist/mongodb-migrate dist/MongoDB-Migrate-macOS-arm64.zip dist/SBOM.spdx.json
{
  shasum -a 256 dist/mongodb-migrate
  shasum -a 256 dist/MongoDB-Migrate-macOS-arm64.zip
  shasum -a 256 dist/SBOM.spdx.json
} > dist/SHA256SUMS.txt
