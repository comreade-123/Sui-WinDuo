# WindowsDuo OLED / I2C 接线诊断（零依赖）
#
# 干嘛用：一边调整接线，一边实时看 I2C 总线上到底有哪些设备。
#         板子上的固件每 3 秒重扫一次总线，设备一出现就会打印。
#
# 用法（在仓库根目录）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\diagnose-oled.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\diagnose-oled.ps1 -Port COM3 -Seconds 60
#
# 判读：
#   OLED(0x3C)=OK  且 MPU6050(0x68)=OK   -> 接线全通，屏幕应立即显示角度界面
#   OLED(0x3C)=MISSING                    -> 只差 OLED：查 VCC/GND 供电、SDA/SCL 是否接反、屏是否为 SH1106
#   devices=0                             -> 整条总线不通：查共地、3V3、SDA/SCL 是否接反

[CmdletBinding()]
param(
  [string]$Port = 'COM3',
  [int]$Seconds = 0
)

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot

Write-Host ''
Write-Host '==== WindowsDuo OLED / I2C 接线诊断 ====' -ForegroundColor Cyan
Write-Host '接线基准（务必逐条核对）：' -ForegroundColor White
Write-Host '  OLED  VCC -> ESP32 3V3      OLED  GND -> ESP32 GND' -ForegroundColor Gray
Write-Host '  OLED  SDA -> ESP32 GPIO23   OLED  SCL -> ESP32 GPIO22' -ForegroundColor Gray
Write-Host '  MPU6050 SDA -> ESP32 GPIO23  MPU6050 SCL -> ESP32 GPIO22（两模块共用总线）' -ForegroundColor Gray
Write-Host '  MPU6050 AD0 -> GND（决定 0x68）  所有 GND 必须共地' -ForegroundColor Gray
Write-Host ''
Write-Host '最容易接错的两点：' -ForegroundColor Yellow
Write-Host '  1) SDA 与 SCL 接反（接反时总线上一个设备都不会出现）' -ForegroundColor Yellow
Write-Host '  2) OLED 模块的 VCC/GND 极性反了（模块不亮、也不应答）' -ForegroundColor Yellow
Write-Host ''

# 端口清单
$ports = [System.IO.Ports.SerialPort]::GetPortNames()
Write-Host ("可用串口: " + ($ports -join ', ')) -ForegroundColor DarkGray
if (-not ($ports -contains $Port)) {
  Write-Host "端口 $Port 不可用，请用 -Port 指定上面列出的端口。" -ForegroundColor Red
  exit 1
}

$sp = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$sp.ReadTimeout = 200
$sp.DtrEnable = $false
$sp.RtsEnable = $false

try { $sp.Open() } catch {
  Write-Host "打开 $Port 失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host '提示：关闭 Arduino IDE 串口监视器或其他占用该端口的程序后重试。' -ForegroundColor Yellow
  exit 1
}

Write-Host ("监听 {0}（板子每 3 秒重扫一次总线）..." -f $Port) -ForegroundColor Green

# 复位一次开发板，保证每次运行都能看到完整的开机扫描结果
Write-Host '复位开发板以捕获完整开机诊断 ...' -ForegroundColor DarkGray
$sp.DtrEnable = $false
Start-Sleep -Milliseconds 200
$sp.RtsEnable = $true
Start-Sleep -Milliseconds 150
$sp.RtsEnable = $false
Start-Sleep -Milliseconds 400

Write-Host '现在可以去动线了，设备上线/掉线会立刻打印。Ctrl+C 退出。' -ForegroundColor Green
Write-Host ''

$enc = New-Object System.Text.UTF8Encoding($false)
$buf = New-Object System.Text.StringBuilder
$sw  = [System.Diagnostics.Stopwatch]::StartNew()
$script:lastLine = ''
$script:lastSummarySec = -1

try {
  while ($true) {
    if ($Seconds -gt 0 -and $sw.Elapsed.TotalSeconds -ge $Seconds) { break }

    # 心跳：即使总线状态没变，也每 10 秒播报一次当前状态，确认工具仍在工作
    $sec = [int]$sw.Elapsed.TotalSeconds
    if ($script:lastLine -ne '' -and ($sec % 10) -eq 0 -and $sec -ne $script:lastSummarySec) {
      $script:lastSummarySec = $sec
      Write-Host ("[{0}] 当前状态: {1}" -f $sw.Elapsed.ToString('mm\:ss'), $script:lastLine) -ForegroundColor DarkCyan
    }

    try {
      $n = $sp.BytesToRead
      if ($n -le 0) { Start-Sleep -Milliseconds 60; continue }
      $b = New-Object byte[] $n
      $r = $sp.Read($b, 0, $n)
      [void]$buf.Append($enc.GetString($b, 0, $r))
      $text = $buf.ToString()
      $i = $text.LastIndexOf("`n")
      if ($i -lt 0) { continue }
      $chunk = $text.Substring(0, $i)
      [void]$buf.Clear(); [void]$buf.Append($text.Substring($i + 1))

      foreach ($line in ($chunk -split "`r?`n")) {
        $t = $line.Trim()
        if ($t.Length -eq 0) { continue }
        if ($t -match 'i2c\.master|s_i2c_synchronous|i2c_master_multi|^E \(\d+\)') { continue }   # 驱动噪音
        if ($t -notmatch '^#') { continue }                                                       # 只看诊断行

        $ts = $sw.Elapsed.ToString('mm\:ss')
        if ($t -match 'wire-check') {
          if ($t -match 'OLED\(0x3C\)=OK') {
            Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Green
          } else {
            Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Yellow
          }
        } elseif ($t -match 'both devices online') {
          Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Green
          Write-Host ''
          Write-Host '>>> 两个设备都在线了！屏幕应显示角度界面。' -ForegroundColor Green
          Write-Host ''
        } elseif ($t -match 'FAILED|no I2C|no ACK') {
          Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Red
        } elseif ($t -match 're-initialized') {
          Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Green
        } else {
          Write-Host ("[{0}] {1}" -f $ts, $t) -ForegroundColor Gray
        }
        $script:lastLine = $t
      }
    } catch { Start-Sleep -Milliseconds 100 }
  }
} finally {
  if ($sp.IsOpen) { $sp.Close() }
  $sp.Dispose()
  Write-Host ''
  Write-Host '==== 诊断结束 ====' -ForegroundColor Cyan
  if ($script:lastLine -match 'OLED\(0x3C\)=OK' -and $script:lastLine -match 'MPU6050\(0x68\)=OK') {
    Write-Host '结论：总线正常，OLED 与 MPU6050 都在线。' -ForegroundColor Green
  } elseif ($script:lastLine -match 'OLED\(0x3C\)=MISSING' -and $script:lastLine -match 'MPU6050\(0x68\)=OK') {
    Write-Host '结论：MPU6050 正常，OLED 未应答。下一步按顺序查：' -ForegroundColor Yellow
    Write-Host '  1) OLED 的 VCC 是否接在 3V3、GND 是否接 GND（用万用表量模块 VCC-GND 应为 3.3V）' -ForegroundColor Yellow
    Write-Host '  2) OLED 的 SDA/SCL 是否与 MPU6050 并到同一条总线、有没有接反' -ForegroundColor Yellow
    Write-Host '  3) 屏幕若是 SH1106（常见 1.3 寸），SSD1306 库驱动不了，需改用 U8g2' -ForegroundColor Yellow
  } elseif ($script:lastLine -match 'devices=0') {
    Write-Host '结论：总线上没有任何设备。优先查共地、3V3 供电、SDA/SCL 是否整条接反。' -ForegroundColor Red
  }
}
