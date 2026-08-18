#!/usr/bin/env bash
set -euo pipefail

: "${MACOS_SIGN_IDENTITY:?Set MACOS_SIGN_IDENTITY to a Developer ID Application identity}"
: "${APPLE_ID:?Set APPLE_ID for notarization}"
: "${APPLE_TEAM_ID:?Set APPLE_TEAM_ID for notarization}"
: "${APPLE_APP_PASSWORD:?Set APPLE_APP_PASSWORD to an app-specific password}"

app_path="${1:-dist/MongoDB Migrate.app}"
zip_path="${2:-dist/MongoDB-Migrate-macOS-arm64.zip}"

codesign --force --deep --options runtime --timestamp \
  --entitlements macos.entitlements \
  --sign "$MACOS_SIGN_IDENTITY" "$app_path"
codesign --verify --deep --strict --verbose=2 "$app_path"
ditto -c -k --keepParent "$app_path" "$zip_path"
xcrun notarytool submit "$zip_path" \
  --apple-id "$APPLE_ID" \
  --team-id "$APPLE_TEAM_ID" \
  --password "$APPLE_APP_PASSWORD" \
  --wait
xcrun stapler staple "$app_path"
xcrun stapler validate "$app_path"
# Recreate the deliverable after stapling so the distributed app contains the
# offline notarization ticket, rather than only the pre-staple submitted copy.
ditto -c -k --keepParent "$app_path" "$zip_path"
