param(
    [string]$GuiPath = "dist\MongoDB-Migrate-GUI.exe",
    [string]$CliPath = "dist\mongodb-migrate.exe"
)

$ErrorActionPreference = "Stop"
if (-not $env:WINDOWS_SIGN_PFX) {
    throw "Set WINDOWS_SIGN_PFX to the code-signing PFX path."
}
if (-not $env:WINDOWS_SIGN_PASSWORD) {
    throw "Set WINDOWS_SIGN_PASSWORD to the PFX password."
}

$signtool = (Get-Command signtool.exe -ErrorAction Stop).Source
foreach ($file in @($GuiPath, $CliPath)) {
    & $signtool sign /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" `
        /f $env:WINDOWS_SIGN_PFX /p $env:WINDOWS_SIGN_PASSWORD $file
    if ($LASTEXITCODE -ne 0) {
        throw "Signing failed: $file"
    }
    & $signtool verify /pa /all $file
    if ($LASTEXITCODE -ne 0) {
        throw "Signature verification failed: $file"
    }
}

