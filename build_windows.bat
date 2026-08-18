@echo off
setlocal
cd /d "%~dp0"

echo [1/10] Creating isolated Python 3.12 environment...
py -3.12 -m venv --clear build_venv
if errorlevel 1 exit /b 1

echo [2/10] Installing dependencies...
build_venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b 1
build_venv\Scripts\python.exe -m pip install ".[dev]"
if errorlevel 1 exit /b 1

echo [3/10] Running static checks...
build_venv\Scripts\ruff.exe check .
if errorlevel 1 exit /b 1

echo [4/10] Running tests...
build_venv\Scripts\python.exe -m pytest -q
if errorlevel 1 exit /b 1

echo [5/10] Building CLI executable...
build_venv\Scripts\pyinstaller.exe --clean --noconfirm mongo_migrate.spec
if errorlevel 1 exit /b 1

echo [6/10] Building standalone GUI executable...
build_venv\Scripts\pyinstaller.exe --clean --noconfirm mongo_migrate_windows_gui.spec
if errorlevel 1 exit /b 1
dist\MongoDB-Migrate-GUI.exe --smoke-test
if errorlevel 1 exit /b 1

echo [7/10] Applying Authenticode signature when configured...
if defined WINDOWS_SIGN_PFX (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\sign_windows.ps1
  if errorlevel 1 exit /b 1
) else (
  echo WARNING: producing an unsigned development build, not a commercial release
)

echo [8/10] Generating SPDX SBOM...
build_venv\Scripts\python.exe generate_sbom.py --output dist\SBOM.spdx.json
if errorlevel 1 exit /b 1

echo [9/10] Creating release archive...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\mongodb-migrate.exe','dist\MongoDB-Migrate-GUI.exe','dist\SBOM.spdx.json','LICENSE' -DestinationPath 'dist\MongoDB-Migrate-windows-x64.zip' -Force"
if errorlevel 1 exit /b 1

set "SIGNING_KIND=unsigned"
if defined WINDOWS_SIGN_PFX set "SIGNING_KIND=authenticode"
build_venv\Scripts\python.exe release_manifest.py --output dist\RELEASE.json --platform windows --architecture x64 --signing "%SIGNING_KIND%" dist\mongodb-migrate.exe dist\MongoDB-Migrate-GUI.exe dist\MongoDB-Migrate-windows-x64.zip dist\SBOM.spdx.json
if errorlevel 1 exit /b 1

echo [10/10] Writing SHA-256 checksums...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$files = @('dist\mongodb-migrate.exe', 'dist\MongoDB-Migrate-GUI.exe', 'dist\MongoDB-Migrate-windows-x64.zip', 'dist\SBOM.spdx.json'); $lines = foreach ($file in $files) { $hash = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower(); $name = Split-Path $file -Leaf; '{0}  {1}' -f $hash, $name }; $lines | Set-Content -Encoding ascii 'dist\SHA256SUMS-windows.txt'"
if errorlevel 1 exit /b 1

echo.
echo Build completed:
echo   dist\mongodb-migrate.exe
echo   dist\MongoDB-Migrate-GUI.exe
echo   dist\MongoDB-Migrate-windows-x64.zip
echo   dist\SHA256SUMS-windows.txt
echo   dist\SBOM.spdx.json
echo   dist\RELEASE.json
exit /b 0
