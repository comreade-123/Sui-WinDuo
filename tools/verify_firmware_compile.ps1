<#
.SYNOPSIS
    Offline compile pre-check for the WindowsDuo_EthanMaven firmware sketch.

.DESCRIPTION
    Arduino IDE cannot compile this sketch without the third-party libraries
    (Adafruit SSD1306 / GFX / MPU6050 / ArduinoJson). When those libraries are
    not installed and there is no network access, this script still performs a
    REAL compiler check:

      * real ESP32 Arduino core sources  (packages\esp32\hardware\esp32\<ver>)
      * real ESP-IDF headers             (packages\esp32\tools\esp32-libs\<ver>)
      * real xtensa-esp-elf-g++ compiler (packages\esp32\tools\esp-x32\<ver>)
      * minimal but interface-faithful stub headers (tools\verify_stubs)
      * -fsyntax-only, so no linking is required

    What it proves   : the sketch parses under the real ESP32 toolchain, has no
                       syntax/type errors, and calls the third-party APIs with
                       signatures consistent with the stub declarations.
    What it does NOT : prove the official libraries behave identically, that the
                       sketch links, or that it runs correctly on hardware.
    Always finish with a real Arduino IDE build before flashing.

    NOTE: output is intentionally ASCII/English only, because Windows
    PowerShell 5.1 decodes BOM-less UTF-8 files as ANSI and would garble
    non-ASCII diagnostics. Chinese documentation lives in
    tools/README_verify.md.

.PARAMETER Sketch
    Path to the .ino file. Defaults to
    firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino

.PARAMETER KeepResponseFile
    Keep the generated gcc response file for troubleshooting.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware_compile.ps1
#>
[CmdletBinding()]
param(
  [string]$Sketch = "",
  [switch]$KeepResponseFile
)

# 'Continue' is required: the compiler writes diagnostics to stderr, and with
# $ErrorActionPreference = 'Stop' PowerShell would abort on the first stderr line.
$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Info { param([string]$m) Write-Host ("[INFO] " + $m) }
function Write-Ok   { param([string]$m) Write-Host ("[PASS] " + $m) -ForegroundColor Green }
function Write-Bad  { param([string]$m) Write-Host ("[FAIL] " + $m) -ForegroundColor Red }
function Write-Skip { param([string]$m) Write-Host ("[SKIP] " + $m) -ForegroundColor Yellow }

if ([string]::IsNullOrWhiteSpace($Sketch)) {
  $Sketch = Join-Path $repoRoot 'firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino'
}
if (-not (Test-Path $Sketch)) {
  Write-Bad ("sketch not found: " + $Sketch)
  exit 2
}

# ---------- 1. locate the esp32 core ----------
$dataDir  = Join-Path $env:LOCALAPPDATA 'Arduino15'
$coreRoot = Join-Path $dataDir 'packages\esp32\hardware\esp32'
if (-not (Test-Path $coreRoot)) {
  Write-Skip ("esp32 core not installed at " + $coreRoot)
  exit 3
}
$coreVersionDir = Get-ChildItem $coreRoot -Directory |
  Sort-Object { [version]($_.Name) } -Descending | Select-Object -First 1
$core = $coreVersionDir.FullName
Write-Info ("esp32 core version: " + $coreVersionDir.Name)

# ---------- 2. locate the xtensa toolchain ----------
$toolRoot = Join-Path $dataDir 'packages\esp32\tools\esp-x32'
$gpp = Get-ChildItem $toolRoot -Recurse -Filter 'xtensa-esp-elf-g++.exe' -ErrorAction SilentlyContinue |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $gpp) {
  Write-Skip 'xtensa-esp-elf-g++ not found, skipping compile pre-check'
  exit 3
}
Write-Info ("compiler: " + $gpp)

# ---------- 3. locate ESP-IDF headers via the core's own flags files ----------
# Arduino IDE compiles with:  -iprefix <sdk>/include/  @<sdk>/flags/includes
#   @<sdk>/flags/defines   -I<sdk>/<memory_type>/include
# Reusing those files reproduces the official include set exactly (and avoids the
# libstdc++/newlib confusion caused by hand-rolled -I lists).
$libsRoot = Join-Path $dataDir 'packages\esp32\tools\esp32-libs'
$libsVersionDir = Get-ChildItem $libsRoot -Directory -ErrorAction SilentlyContinue |
  Sort-Object { [version]($_.Name) } -Descending | Select-Object -First 1
if (-not $libsVersionDir) {
  Write-Skip 'esp32-libs not found, skipping compile pre-check'
  exit 3
}
$sdk = $libsVersionDir.FullName
$sdkInclude = Join-Path $sdk 'include'
$sdkIncludesList = Join-Path $sdk 'flags\includes'
$sdkDefinesList = Join-Path $sdk 'flags\defines'
if (-not (Test-Path $sdkInclude) -or -not (Test-Path $sdkIncludesList)) {
  Write-Skip 'esp32-libs flags/includes not found, skipping compile pre-check'
  exit 3
}

# sdkconfig.h ships per flash mode; dio_qspi matches the default Arduino ESP32 build
$memoryType = 'dio_qspi'
if (-not (Test-Path (Join-Path $sdk ($memoryType + '\include\sdkconfig.h')))) {
  $memoryType = 'qio_qspi'
}
$memoryInclude = Join-Path $sdk ($memoryType + '\include')
if (-not (Test-Path (Join-Path $memoryInclude 'sdkconfig.h'))) {
  Write-Skip 'sdkconfig.h not found in esp32-libs, skipping compile pre-check'
  exit 3
}
Write-Info ("sdk flags: " + $sdkIncludesList)
Write-Info ("memory type include: " + $memoryInclude)

# ---------- 4. assemble the compile command ----------
$stub = Join-Path $PSScriptRoot 'verify_stubs'
$includeDirs = @(
  $stub,
  (Join-Path $core 'cores\esp32'),
  (Join-Path $core 'variants\esp32'),
  (Join-Path $core 'libraries\Wire\src'),
  $memoryInclude     # -I (sdkconfig.h latest wins)
)

$responseFile = Join-Path $env:TEMP ("winduo_verify_{0}.rsp" -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('-fsyntax-only')
$lines.Add('-std=gnu++17')
$lines.Add('-DESP32=ESP32')
$lines.Add('-DARDUINO=10819')
$lines.Add('-DARDUINO_ARCH_ESP32')
$lines.Add('-DARDUINO_ESP32_DEV')
$lines.Add('-DARDUINO_USB_CDC_ON_BOOT=0')
if (Test-Path $sdkDefinesList) {
  $lines.Add('@' + ($sdkDefinesList -replace '\\', '/'))
}
$lines.Add('-iprefix')
$lines.Add(($sdkInclude -replace '\\', '/') + '/')
$lines.Add('@' + ($sdkIncludesList -replace '\\', '/'))
foreach ($dir in $includeDirs) {
  # gcc strips double quotes inside a response file, and backslashes are escapes:
  # forward slashes keep space-containing paths intact.
  $lines.Add(('-I' + ($dir -replace '\\', '/')))
}
$lines.Add('-x')
$lines.Add('c++')
$lines.Add(($Sketch -replace '\\', '/'))
Set-Content -LiteralPath $responseFile -Value $lines -Encoding ascii

Write-Info ("include directories: " + $includeDirs.Count)

# ---------- 5. run the compiler ----------
$output = & $gpp ('@' + $responseFile) 2>&1
if (-not $KeepResponseFile) {
  Remove-Item -LiteralPath $responseFile -Force -ErrorAction SilentlyContinue
} else {
  Write-Info ("response file kept at " + $responseFile)
}

# ---------- 6. verdict ----------
$text = ($output | Out-String)
$errors = $output | Where-Object { $_ -match ':\s*(error|fatal error):' }
$warns  = $output | Where-Object { $_ -match ':\s*warning:' }

if ($errors.Count -gt 0) {
  Write-Host ''
  Write-Host ("===== compile errors (" + $errors.Count + ") =====") -ForegroundColor Red
  $errors | Select-Object -First 40 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
  Write-Bad 'offline compile pre-check did not pass'
  exit 1
}

Write-Host ''
Write-Ok 'offline compile pre-check passed: real xtensa compiler reported no error'
if ($warns.Count -gt 0) {
  Write-Host ("[WARN] " + $warns.Count + " warning(s):") -ForegroundColor Yellow
  $warns | Select-Object -First 20 | ForEach-Object { Write-Host ("  " + $_) -ForegroundColor Yellow }
}
Write-Info 'stub headers were used: syntax/type/API-shape only, not official library semantics'
Write-Info 'run a real Arduino IDE build before flashing the board'
exit 0
