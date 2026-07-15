param(
    [switch]$SkipFfmpeg,
    [switch]$SkipFonts
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Downloads = Join-Path $Root ".downloads"
New-Item -ItemType Directory -Force $Downloads | Out-Null

function Get-VerifiedFile {
    param([string]$Uri, [string]$Target, [string]$Sha256)
    if (Test-Path -LiteralPath $Target) {
        $current = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToLowerInvariant()
        if ($current -eq $Sha256.ToLowerInvariant()) { return }
        Remove-Item -LiteralPath $Target -Force
    }
    Write-Host "Downloading $Uri"
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Target
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $Target -Force
        throw "SHA256 mismatch for $Target. Expected $Sha256, got $actual"
    }
}

if (-not $SkipFonts) {
    $FontDir = Join-Path $Root "assets\fonts"
    New-Item -ItemType Directory -Force $FontDir | Out-Null
    Get-VerifiedFile `
        "https://raw.githubusercontent.com/adobe-fonts/source-han-sans/2.005R/OTF/SimplifiedChinese/SourceHanSansSC-Heavy.otf" `
        (Join-Path $FontDir "SourceHanSansSC-Heavy.otf") `
        "6374b11bc4c2cd4bd7be1a1d64cf5047906c8a6a025c64e023c6792e50ba985e"
    Get-VerifiedFile `
        "https://raw.githubusercontent.com/adobe-fonts/source-han-serif/2.003R/OTF/SimplifiedChinese/SourceHanSerifSC-Heavy.otf" `
        (Join-Path $FontDir "SourceHanSerifSC-Heavy.otf") `
        "d033af54f96530476faed924ab5d5e9e6ef0833495670fd57bab9a7758398048"
}

if (-not $SkipFfmpeg) {
    $Archive = Join-Path $Downloads "ffmpeg-8.1.2-essentials_build.zip"
    Get-VerifiedFile `
        "https://github.com/GyanD/codexffmpeg/releases/download/8.1.2/ffmpeg-8.1.2-essentials_build.zip" `
        $Archive `
        "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
    $Extracted = Join-Path $Downloads "ffmpeg-8.1.2"
    if (Test-Path -LiteralPath $Extracted) { Remove-Item -LiteralPath $Extracted -Recurse -Force }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted
    $PackageRoot = Get-ChildItem -LiteralPath $Extracted -Directory | Select-Object -First 1
    if (-not $PackageRoot) { throw "FFmpeg archive layout is invalid" }
    $BinDir = Join-Path $Root "vendor\ffmpeg\bin"
    New-Item -ItemType Directory -Force $BinDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $PackageRoot.FullName "bin\ffmpeg.exe") -Destination $BinDir -Force
    Copy-Item -LiteralPath (Join-Path $PackageRoot.FullName "bin\ffprobe.exe") -Destination $BinDir -Force
}

Write-Host "Third-party resources are ready."
