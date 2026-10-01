# WindowsDuo 串口实时监视器（纯 PowerShell，零依赖，不需要 pyserial）
#
# 用途：一边改接线，一边实时看到 I2C 总线扫描结果。
#       固件每 2 秒重扫一次总线，设备出现/消失会立刻打印：
#         # [wire-check] devices=2  OLED(0x3C)=OK  MPU6050(0x68)=OK
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\monitor.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\monitor.ps1 -Port COM3
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\monitor.ps1 -NoReset   # 不复位，仅监听
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\monitor.ps1 -Seconds 120
#
# 说明：默认先做一次 DTR/RTS 复位，以便完整看到开机诊断（I2C 扫描、OLED/MPU 初始化结果）。
#       按 Ctrl+C 退出。

[CmdletBinding()]
param(
  [string]$Port    = 'COM3',
  [int]$Baud       = 115200,
  [int]$Seconds    = 0,          # 0 = 一直监视，直到 Ctrl+C
  [switch]$NoReset,
  [switch]$ShowJson              # 加上此开关才逐行显示 JSON（默认只显示 # 诊断行与状态变化）
)

$ErrorActionPreference = 'Continue'

if (-not [System.IO.Ports.SerialPort]::GetPortNames() -contains $Port) {
  Write-Host "端口 $Port 不存在。当前可用端口：" -ForegroundColor Red
  [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object { Write-Host "  $_" }
  exit 1
}

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.ReadTimeout = 200
$sp.DtrEnable   = $false
$sp.RtsEnable   = $false

try {
  $sp.Open()
} catch {
  Write-Host "打开 $Port 失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host "提示：先关闭 Arduino IDE 的串口监视器 / 其他占用该端口的程序。" -ForegroundColor Yellow
  exit 1
}

Write-Host ("=== 监视 {0} @ {1} ===" -f $Port, $Baud) -ForegroundColor Cyan
if (-not $NoReset) {
  Write-Host '正在复位开发板（DTR/RTS），以捕获完整开机诊断 ...' -ForegroundColor DarkGray
  $sp.DtrEnable = $false
  Start-Sleep -Milliseconds 200
  $sp.RtsEnable = $true      # EN 拉低 = 复位
  Start-Sleep -Milliseconds 150
  $sp.RtsEnable = $false     # 释放 = 重启
}
Write-Host '（Ctrl+C 退出）' -ForegroundColor DarkGray
Write-Host ''

$enc     = New-Object System.Text.UTF8Encoding($false)
$buffer  = New-Object System.Text.StringBuilder
$started = [System.Diagnostics.Stopwatch]::StartNew()
$jsonCount = 0
$noiseCount = 0
$lastStatus = ''

try {
  while ($true) {
    if ($Seconds -gt 0 -and $started.Elapsed.TotalSeconds -ge $Seconds) { break }

    try {
      $n = $sp.BytesToRead
      if ($n -gt 0) {
        $bytes = New-Object byte[] $n
        $read  = $sp.Read($bytes, 0, $n)
        [void]$buffer.Append($enc.GetString($bytes, 0, $read))

        $text = $buffer.ToString()
        $idx  = $text.LastIndexOf("`n")
        if ($idx -ge 0) {
          $complete = $text.Substring(0, $idx)
          [void]$buffer.Clear()
          [void]$buffer.Append($text.Substring($idx + 1))

          foreach ($line in ($complete -split "`r?`n")) {
            $t = $line.Trim()
            if ($t.Length -eq 0) { continue }

            # 过滤 ESP32 核心的 I2C 驱动噪音日志（排障扫描不存在的地址时必然产生）
            if ($t -match 'i2c\.master|s_i2c_synchronous_transaction|i2c_master_multi_buffer_transmit|^E \(\d+\)') {
              $noiseCount++
              continue
            }

            if ($t.StartsWith('#')) {
              # 诊断/状态行：高亮显示
              if ($t -match 'wire-check|FAIL|no I2C|peripherals') {
                Write-Host $t -ForegroundColor Yellow
              } elseif ($t -match 'devices=|found 0x|booting|OLED|MPU6050') {
                Write-Host $t -ForegroundColor Green
              } else {
                Write-Host $t -ForegroundColor Gray
              }
            }
            elseif ($t.StartsWith('{')) {
              $jsonCount++
              if ($ShowJson) {
                Write-Host ("[{0}] {1}" -f $started.Elapsed.ToString('mm\:ss'), $t) -ForegroundColor DarkCyan
              } else {
                # 只在 status 变化时提示一次，避免刷屏
                if ($t -match '"status":"([^"]+)"') {
                  $st = $Matches[1]
                  if ($st -ne $lastStatus) {
                    $lastStatus = $st
                    Write-Host ("[{0}] 状态 -> {1}" -f $started.Elapsed.ToString('mm\:ss'), $st) -ForegroundColor Magenta
                  }
                }
              }
            }
            else {
              Write-Host $t -ForegroundColor DarkGray
            }
          }
        }
      } else {
        Start-Sleep -Milliseconds 50
      }
    } catch {
      Start-Sleep -Milliseconds 100
    }
  }
} finally {
  if ($sp.IsOpen) { $sp.Close() }
  $sp.Dispose()
  Write-Host ''
  Write-Host ("=== 结束：共收到 {0} 帧 JSON（已过滤 {1} 行 I2C 驱动噪音）===" -f $jsonCount, $noiseCount) -ForegroundColor Cyan
}
