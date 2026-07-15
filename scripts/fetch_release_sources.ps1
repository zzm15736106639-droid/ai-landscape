$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$TargetDir = Join-Path $Root "release-resources"
New-Item -ItemType Directory -Force $TargetDir | Out-Null
$Target = Join-Path $TargetDir "ffmpeg-8.1.2.tar.xz"
$Expected = "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
if (-not (Test-Path -LiteralPath $Target) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToLowerInvariant() -ne $Expected) {
    Invoke-WebRequest -UseBasicParsing -Uri "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz" -OutFile $Target
}
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToLowerInvariant()
if ($Actual -ne $Expected) { throw "FFmpeg source SHA256 mismatch: $Actual" }
Write-Host $Target
