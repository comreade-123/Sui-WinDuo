<#
.SYNOPSIS
    WindowsDuo_EthanMaven 固件离线静态检查 (无第三方依赖, Windows PowerShell 5.1 可用)

.DESCRIPTION
    本机没有 ESP32 实物、没有 Adafruit/ArduinoJson 库、网络受限, 因此本脚本只做
    **静态与逻辑校验**: 结构、库包含、引脚/地址/周期常量、括号配平、JSON 字段与
    类型、已知缺陷的回归断言、UTF-8 编码。逐项输出 PASS/FAIL, 有 FAIL 时退出码为 1。

    它不能替代真机验证 —— 见 tools/README_verify.md 的"证明边界"。

.PARAMETER FirmwarePath
    .ino 路径。默认取仓库内 firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1 -FirmwarePath firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino
#>

[CmdletBinding()]
param(
    [string]$FirmwarePath = ""
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 结果收集
# ---------------------------------------------------------------------------
$script:Results = New-Object System.Collections.ArrayList

function Add-Check {
    param(
        [string]$Group,
        [string]$Name,
        [bool]$Ok,
        [string]$Detail = ""
    )
    [void]$script:Results.Add([pscustomobject]@{
        Group  = $Group
        Name   = $Name
        Ok     = $Ok
        Detail = $Detail
    })
}

function Add-Info {
    param([string]$Group, [string]$Name, [string]$Detail)
    # 纯信息项, 不计入 PASS/FAIL
    [void]$script:Results.Add([pscustomobject]@{
        Group  = $Group
        Name   = $Name
        Ok     = $null
        Detail = $Detail
    })
}

# ---------------------------------------------------------------------------
# 解析辅助: 去掉注释 (保留字符串字面量), 以便在"纯代码"上做正则断言
# ---------------------------------------------------------------------------
function Remove-SourceComments {
    param([string]$Text)
    $sb = New-Object System.Text.StringBuilder
    $n = $Text.Length
    $i = 0
    $inString = $false
    while ($i -lt $n) {
        $c = $Text[$i]
        if ($inString) {
            [void]$sb.Append($c)
            if ($c -eq '\' -and ($i + 1) -lt $n) {
                [void]$sb.Append($Text[$i + 1])
                $i += 2
                continue
            }
            if ($c -eq '"') { $inString = $false }
            $i++
            continue
        }
        if ($c -eq '"') {
            $inString = $true
            [void]$sb.Append($c)
            $i++
            continue
        }
        if ($c -eq '/' -and ($i + 1) -lt $n -and $Text[$i + 1] -eq '/') {
            while ($i -lt $n -and $Text[$i] -ne "`n") { $i++ }
            continue
        }
        if ($c -eq '/' -and ($i + 1) -lt $n -and $Text[$i + 1] -eq '*') {
            $i += 2
            while (($i + 1) -lt $n -and -not ($Text[$i] -eq '*' -and $Text[$i + 1] -eq '/')) { $i++ }
            $i += 2
            continue
        }
        [void]$sb.Append($c)
        $i++
    }
    return $sb.ToString()
}

# 括号配平: 跳过字符串/字符字面量与注释
function Test-BracketBalance {
    param([string]$Text)
    $depthParen = 0
    $depthBrace = 0
    $depthBracket = 0
    $minParen = 0
    $n = $Text.Length
    $i = 0
    $inString = $false
    $inChar = $false
    while ($i -lt $n) {
        $c = $Text[$i]
        if ($inString) {
            if ($c -eq '\' -and ($i + 1) -lt $n) { $i += 2; continue }
            if ($c -eq '"') { $inString = $false }
            $i++
            continue
        }
        if ($inChar) {
            if ($c -eq '\' -and ($i + 1) -lt $n) { $i += 2; continue }
            if ($c -eq "'") { $inChar = $false }
            $i++
            continue
        }
        if ($c -eq '"') { $inString = $true; $i++; continue }
        if ($c -eq "'") { $inChar = $true; $i++; continue }
        if ($c -eq '/' -and ($i + 1) -lt $n -and $Text[$i + 1] -eq '/') {
            while ($i -lt $n -and $Text[$i] -ne "`n") { $i++ }
            continue
        }
        if ($c -eq '/' -and ($i + 1) -lt $n -and $Text[$i + 1] -eq '*') {
            $i += 2
            while (($i + 1) -lt $n -and -not ($Text[$i] -eq '*' -and $Text[$i + 1] -eq '/')) { $i++ }
            $i += 2
            continue
        }
        switch ($c) {
            '(' { $depthParen++ }
            ')' { $depthParen--; if ($depthParen -lt $minParen) { $minParen = $depthParen } }
            '{' { $depthBrace++ }
            '}' { $depthBrace-- }
            '[' { $depthBracket++ }
            ']' { $depthBracket-- }
        }
        $i++
    }
    return [pscustomobject]@{
        Paren   = $depthParen
        Brace   = $depthBrace
        Bracket = $depthBracket
    }
}

function Get-CodeMatchCount {
    param([string]$Code, [string]$Pattern)
    return ([regex]::Matches($Code, $Pattern)).Count
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
Write-Output "=============================================================================="
Write-Output "WindowsDuo_EthanMaven 固件离线静态检查"
Write-Output "PowerShell: $($PSVersionTable.PSVersion)  (Table: $($PSVersionTable.PSEdition))"
Write-Output "=============================================================================="

if ([string]::IsNullOrWhiteSpace($FirmwarePath)) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $FirmwarePath = Join-Path $repoRoot 'firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino'
}
Write-Output "目标文件: $FirmwarePath"
Write-Output ""

$group = 'A. 文件与编码'
if (-not (Test-Path -LiteralPath $FirmwarePath -PathType Leaf)) {
    Add-Check -Group $group -Name '固件文件存在' -Ok $false -Detail "未找到: $FirmwarePath"
    Write-Output "[FAIL] 固件文件不存在: $FirmwarePath"
    Write-Output "       请检查 lead 是否已落地 firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino"
    Write-Output ""
    Write-Output "结果: 0 PASS / 1 FAIL  (无法继续, 缺少目标文件)"
    exit 1
}
Add-Check -Group $group -Name '固件文件存在' -Ok $true -Detail $FirmwarePath

$bytes = [System.IO.File]::ReadAllBytes($FirmwarePath)
$sizeKb = [math]::Round($bytes.Length / 1024.0, 1)
Add-Check -Group $group -Name '文件非空' -Ok ($bytes.Length -gt 1024) -Detail "$sizeKb KB"

$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
Add-Check -Group $group -Name 'UTF-8 无 BOM' -Ok (-not $hasBom) -Detail $(if ($hasBom) { '检测到 BOM' } else { '无 BOM' })

$utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
$source = $null
try {
    $source = $utf8Strict.GetString($bytes)
    Add-Check -Group $group -Name '可按严格 UTF-8 解码' -Ok $true -Detail "字符数 $($source.Length)"
}
catch {
    Add-Check -Group $group -Name '可按严格 UTF-8 解码' -Ok $false -Detail $_.Exception.Message
    Write-Output "[FAIL] 文件不是合法 UTF-8, 中文注释会乱码。"
    Write-Output ""
}

if ($null -eq $source) { exit 1 }

# 乱码启发式: 经典 UTF-8 被按 GBK 解码后的连续字节
$mojibake = [regex]::Matches($source, '[\uFFFD]|Ã[\u0080-\u00BF]|â€|å[\u0080-\u00BF]{2}')
Add-Check -Group $group -Name '中文无乱码迹象' -Ok ($mojibake.Count -eq 0) -Detail "可疑序列 $($mojibake.Count) 处"

$lineCount = ($source -split "`n").Count
Add-Info -Group $group -Name '行数' -Detail $lineCount

$code = Remove-SourceComments $source

# ---------------------------------------------------------------------------
$group = 'B. 结构'
$setupDefs = Get-CodeMatchCount $code '(?m)^\s*void\s+setup\s*\(\s*\)\s*\{'
$loopDefs = Get-CodeMatchCount $code '(?m)^\s*void\s+loop\s*\(\s*\)\s*\{'
Add-Check -Group $group -Name '恰好一个 setup()' -Ok ($setupDefs -eq 1) -Detail "找到 $setupDefs 个"
Add-Check -Group $group -Name '恰好一个 loop()' -Ok ($loopDefs -eq 1) -Detail "找到 $loopDefs 个"

$delayCalls = Get-CodeMatchCount $code '\bdelay\s*\('
$delayMicro = Get-CodeMatchCount $code '\bdelayMicroseconds\s*\('
Add-Check -Group $group -Name '无 delay() 阻塞调用' -Ok ($delayCalls -eq 0) -Detail "delay( 出现 $delayCalls 次"
Add-Check -Group $group -Name '无 delayMicroseconds()' -Ok ($delayMicro -eq 0) -Detail "出现 $delayMicro 次"

$whileTrue = Get-CodeMatchCount $code 'while\s*\(\s*(true|1)\s*\)'
Add-Check -Group $group -Name '无 while(true) 死循环' -Ok ($whileTrue -eq 0) -Detail "出现 $whileTrue 次"

$millisCalls = Get-CodeMatchCount $code '\bmillis\s*\(\s*\)'
Add-Check -Group $group -Name '时间基准用 millis()' -Ok ($millisCalls -ge 4) -Detail "millis() 出现 $millisCalls 次"

$balance = Test-BracketBalance $source
Add-Check -Group $group -Name '圆括号配平' -Ok ($balance.Paren -eq 0) -Detail "净值 $($balance.Paren)"
Add-Check -Group $group -Name '花括号配平' -Ok ($balance.Brace -eq 0) -Detail "净值 $($balance.Brace)"
Add-Check -Group $group -Name '方括号配平' -Ok ($balance.Bracket -eq 0) -Detail "净值 $($balance.Bracket)"

# ---------------------------------------------------------------------------
$group = 'C. 库包含'
$includes = @(
    @{ File = 'Wire.h';               Pattern = '#include\s*[<"]Wire\.h[>"]' },
    @{ File = 'Adafruit_GFX.h';       Pattern = '#include\s*[<"]Adafruit_GFX\.h[>"]' },
    @{ File = 'Adafruit_SSD1306.h';   Pattern = '#include\s*[<"]Adafruit_SSD1306\.h[>"]' },
    @{ File = 'Adafruit_MPU6050.h';   Pattern = '#include\s*[<"]Adafruit_MPU6050\.h[>"]' },
    @{ File = 'ArduinoJson.h';        Pattern = '#include\s*[<"]ArduinoJson\.h[>"]' }
)
foreach ($inc in $includes) {
    $c = Get-CodeMatchCount $code $inc.Pattern
    Add-Check -Group $group -Name "包含 $($inc.File)" -Ok ($c -ge 1) -Detail "找到 $c 处"
}

# ---------------------------------------------------------------------------
$group = 'D. 引脚 / 地址 / 周期常量'
$constChecks = @(
    @{ Name = 'PIN_I2C_SDA = 23';        Pattern = 'PIN_I2C_SDA\s*=\s*23\s*;' },
    @{ Name = 'PIN_I2C_SCL = 22';        Pattern = 'PIN_I2C_SCL\s*=\s*22\s*;' },
    @{ Name = 'PIN_BUTTON = 5';          Pattern = 'PIN_BUTTON\s*=\s*5\s*;' },
    @{ Name = 'MPU6050_ADDR = 0x68';     Pattern = 'MPU6050_ADDR\s*=\s*0x68\s*;' },
    @{ Name = 'SSD1306_ADDR = 0x3C';     Pattern = 'SSD1306_ADDR\s*=\s*0x3C\s*;' },
    @{ Name = 'SERIAL_BAUD = 115200';    Pattern = 'SERIAL_BAUD\s*=\s*115200\s*;' },
    @{ Name = 'OLED_UPDATE_INTERVAL = 200'; Pattern = 'OLED_UPDATE_INTERVAL\s*=\s*200\s*;' },
    @{ Name = 'SERIAL_SEND_INTERVAL = 50';  Pattern = 'SERIAL_SEND_INTERVAL\s*=\s*50\s*;' },
    @{ Name = 'COMP_FILTER_ALPHA = 0.98';   Pattern = 'COMP_FILTER_ALPHA\s*=\s*0\.98' },
    @{ Name = 'ANGLE_MIN_DEG = 0.0';        Pattern = 'ANGLE_MIN_DEG\s*=\s*0\.0f?\s*;' },
    @{ Name = 'ANGLE_MAX_DEG = 180.0';      Pattern = 'ANGLE_MAX_DEG\s*=\s*180\.0f?\s*;' },
    @{ Name = 'BUTTON_LONG_MS = 800';       Pattern = 'BUTTON_LONG_MS\s*=\s*800\s*;' },
    @{ Name = 'BUTTON_SHORTMIN_MS = 50';    Pattern = 'BUTTON_SHORTMIN_MS\s*=\s*50\s*;' },
    @{ Name = 'BUTTON_DEBOUNCE_MS = 30';    Pattern = 'BUTTON_DEBOUNCE_MS\s*=\s*30\s*;' },
    @{ Name = 'CALIB_WINDOW_MS = 3000';     Pattern = 'CALIB_WINDOW_MS\s*=\s*3000\s*;' }
)
foreach ($cc in $constChecks) {
    $c = Get-CodeMatchCount $code $cc.Pattern
    Add-Check -Group $group -Name $cc.Name -Ok ($c -ge 1) -Detail "命中 $c 处"
}

$pullup = Get-CodeMatchCount $code 'pinMode\s*\(\s*PIN_BUTTON\s*,\s*INPUT_PULLUP\s*\)'
Add-Check -Group $group -Name '按键使用 INPUT_PULLUP' -Ok ($pullup -ge 1) -Detail "命中 $pullup 处"

$wireBegin = Get-CodeMatchCount $code 'Wire\.begin\s*\(\s*PIN_I2C_SDA\s*,\s*PIN_I2C_SCL\s*\)'
Add-Check -Group $group -Name 'Wire.begin(SDA, SCL) 显式指定引脚' -Ok ($wireBegin -ge 1) -Detail "命中 $wireBegin 处"

$oledInit = Get-CodeMatchCount $code 'oled\.begin\s*\(\s*SSD1306_SWITCHCAPVCC\s*,\s*(OLED_ADDR|SSD1306_ADDR)\s*\)'
Add-Check -Group $group -Name 'OLED 用 0x3C 初始化' -Ok ($oledInit -ge 1) -Detail "命中 $oledInit 处"

$mpuInit = Get-CodeMatchCount $code 'mpu\.begin\s*\(\s*MPU6050_ADDR'
Add-Check -Group $group -Name 'MPU6050 用 0x68 初始化' -Ok ($mpuInit -ge 1) -Detail "命中 $mpuInit 处"

# ---------------------------------------------------------------------------
$group = 'E. 调度 (非阻塞节流)'
$pairs = @(
    # 非阻塞节流两种等价写法都接受: now-last >= PERIOD  或  now-last < PERIOD (提前 return)
    @{ Name = 'OLED 刷新用 millis 节流';   Pattern = 'now\s*-\s*\w+\s*(>=|<)\s*OLED_UPDATE_INTERVAL' },
    @{ Name = '串口发送用 millis 节流';    Pattern = 'now\s*-\s*\w+\s*>=\s*SERIAL_SEND_INTERVAL' },
    @{ Name = 'IMU 采样用 millis 节流';    Pattern = 'now\s*-\s*\w+\s*<\s*5' },
    @{ Name = '按键消抖用 millis 节流';    Pattern = 'now\s*-\s*\w+\s*>=\s*BUTTON_DEBOUNCE_MS' },
    @{ Name = '长按判定用 millis 节流';    Pattern = 'now\s*-\s*\w+\s*>=\s*BUTTON_LONG_MS' },
    @{ Name = '校准窗口用 millis 节流';    Pattern = 'now\s*-\s*\w+\s*>=\s*CALIB_WINDOW_MS' }
)
foreach ($p in $pairs) {
    $c = Get-CodeMatchCount $code $p.Pattern
    Add-Check -Group $group -Name $p.Name -Ok ($c -ge 1) -Detail "命中 $c 处"
}

# ---------------------------------------------------------------------------
$group = 'F. JSON 输出'
$jsonOk = $true
$jsonDetail = ''
$serializeCount = Get-CodeMatchCount $code 'serializeJson\s*\('
Add-Check -Group $group -Name '走 ArduinoJson serializeJson' -Ok ($serializeCount -ge 1) -Detail "命中 $serializeCount 处"

$strConcat = Get-CodeMatchCount $code 'serialized\s*\(\s*String\s*\('
Add-Check -Group $group -Name '回归: 不得再用 serialized(String(...' -Ok ($strConcat -eq 0) `
    -Detail "出现 $strConcat 次 (0 = 已修复为数值写入)"

$angleStrAssign = Get-CodeMatchCount $code 'doc\s*\[\s*"angle"\s*\]\s*=\s*serialized\s*\('
Add-Check -Group $group -Name '回归: doc["angle"] 不得是 serialized(...)' -Ok ($angleStrAssign -eq 0) `
    -Detail "出现 $angleStrAssign 次"

$angleNumAssign = Get-CodeMatchCount $code 'doc\s*\[\s*"angle"\s*\]\s*=\s*\(?\s*(float|double)\s*\)?\s*'
Add-Check -Group $group -Name 'doc["angle"] 为数值赋值' -Ok ($angleNumAssign -ge 1) -Detail "命中 $angleNumAssign 处"

# 抽取 sendJsonLine 函数体, 检查键
$jsonFnMatch = [regex]::Match($source, '(?s)static\s+void\s+sendJsonLine\s*\(\s*\)\s*\{(.*?)\n\}')
if ($jsonFnMatch.Success) {
    $jsonBody = $jsonFnMatch.Groups[1].Value
    foreach ($key in @('angle', 'status', 'mode', 'author')) {
        $kc = ([regex]::Matches($jsonBody, "doc\s*\[\s*`"$key`"\s*\]")).Count
        Add-Check -Group $group -Name "JSON 含字段 $key" -Ok ($kc -ge 1) -Detail "命中 $kc 处"
    }
    $debugKeys = @('gyro', 'bias', 'base', 'sp', 'lp')
    $foundDebug = @()
    foreach ($key in $debugKeys) {
        if (([regex]::Matches($jsonBody, "doc\s*\[\s*`"$key`"\s*\]")).Count -ge 1) { $foundDebug += $key }
    }
    Add-Check -Group $group -Name 'debug 字段齐备 (gyro/bias/base/sp/lp)' `
        -Ok ($foundDebug.Count -eq 5) -Detail "找到: $($foundDebug -join ',')"
    $prog = ([regex]::Matches($jsonBody, 'doc\s*\[\s*"progress"\s*\]')).Count
    Add-Check -Group $group -Name '校准中输出 progress' -Ok ($prog -ge 1) -Detail "命中 $prog 处"
    $authorStr = ([regex]::Matches($jsonBody, '"EthanMaven"|TEXT_AUTHOR')).Count
    Add-Check -Group $group -Name 'author 值为 EthanMaven' -Ok ($authorStr -ge 1) -Detail "命中 $authorStr 处"
} else {
    Add-Check -Group $group -Name '能定位 sendJsonLine()' -Ok $false -Detail '未找到函数定义'
}
$newlineWrite = Get-CodeMatchCount $code "Serial\.write\s*\(\s*'\S*'?\s*\)"
Add-Check -Group $group -Name 'JSON 行以换行结尾' -Ok ($newlineWrite -ge 1) `
    -Detail "Serial.write('...') 命中 $newlineWrite 处"

# ---------------------------------------------------------------------------
$group = 'G. 缺陷回归断言'
$latchDecl = Get-CodeMatchCount $code 'static\s+bool\s+btnLongLatched'
$latchSet = Get-CodeMatchCount $code 'btnLongLatched\s*=\s*true'
$latchClear = Get-CodeMatchCount $code 'btnLongLatched\s*=\s*false'
Add-Check -Group $group -Name 'btnLongLatched 声明存在' -Ok ($latchDecl -ge 1) -Detail "命中 $latchDecl 处"
Add-Check -Group $group -Name 'btnLongLatched 置位存在' -Ok ($latchSet -ge 1) -Detail "命中 $latchSet 处"
Add-Check -Group $group -Name 'btnLongLatched 清除存在' -Ok ($latchClear -ge 1) -Detail "命中 $latchClear 处"
$latchTotal = $latchDecl + $latchSet + $latchClear
Add-Check -Group $group -Name 'btnLongLatched 出现 >= 3 处 (声明+置位+清除)' -Ok ($latchTotal -ge 3) `
    -Detail "合计 $latchTotal 处"

$longGuard = Get-CodeMatchCount $code 'BUTTON_LONG_MS\s*&&\s*!btnLongLatched|!btnLongLatched\s*&&\s*now\s*-\s*\w+\s*>=\s*BUTTON_LONG_MS'
Add-Check -Group $group -Name '长按分支带 !btnLongLatched 条件' -Ok ($longGuard -ge 1) -Detail "命中 $longGuard 处"

$shortGuard = Get-CodeMatchCount $code '!btnLongLatched\s*&&\s*heldMs'
Add-Check -Group $group -Name '短按分支带 !btnLongLatched 条件 (防长按误判为短按)' -Ok ($shortGuard -ge 1) `
    -Detail "命中 $shortGuard 处"

$quantized = Get-CodeMatchCount $code '/\s*10\.0\s*\)'
Add-Check -Group $group -Name '角度量化写法 (整数/10.0) 存在' -Ok ($quantized -ge 1) -Detail "命中 $quantized 处"

# ---------------------------------------------------------------------------
$group = 'H. 校准与滤波常量'
$calibGuardVar = Get-CodeMatchCount $code 'CALIB_VAR_MAX'
$calibGuardGyro = Get-CodeMatchCount $code 'CALIB_GYRO_MAX_DPS'
Add-Check -Group $group -Name '校准有方差静止判定' -Ok ($calibGuardVar -ge 1) -Detail "命中 $calibGuardVar 处"
Add-Check -Group $group -Name '校准有角速度静止判定' -Ok ($calibGuardGyro -ge 1) -Detail "命中 $calibGuardGyro 处"
$calibReject = Get-CodeMatchCount $code 'finishCalibration\s*\(\s*false\s*\)'
Add-Check -Group $group -Name '校准被拒绝时不清零零偏 (finishCalibration(false))' -Ok ($calibReject -ge 1) `
    -Detail "命中 $calibReject 处"
$clamp = Get-CodeMatchCount $code 'clampFloat\s*\('
Add-Check -Group $group -Name '角度限幅使用 clampFloat' -Ok ($clamp -ge 1) -Detail "命中 $clamp 处"
$wrap = Get-CodeMatchCount $code 'wrapDeg180\s*\('
Add-Check -Group $group -Name '相对角做 180 度归一化' -Ok ($wrap -ge 1) -Detail "命中 $wrap 处"

$group = 'I. 模式名'
foreach ($m in @('default', 'calibrate', 'debug')) {
    $c = Get-CodeMatchCount $code "`"$m`""
    Add-Check -Group $group -Name "模式名 $m" -Ok ($c -ge 1) -Detail "命中 $c 处"
}

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
Write-Output ""
foreach ($grp in ($script:Results | Select-Object -ExpandProperty Group -Unique)) {
    Write-Output "--- $grp ---"
    foreach ($r in ($script:Results | Where-Object { $_.Group -eq $grp })) {
        if ($null -eq $r.Ok) {
            Write-Output ("  [INFO] {0,-52} {1}" -f $r.Name, $r.Detail)
        }
        elseif ($r.Ok) {
            Write-Output ("  [PASS] {0,-52} {1}" -f $r.Name, $r.Detail)
        }
        else {
            Write-Output ("  [FAIL] {0,-52} {1}" -f $r.Name, $r.Detail)
        }
    }
}

$checked = @($script:Results | Where-Object { $null -ne $_.Ok })
$failed = @($checked | Where-Object { -not $_.Ok })
$passed = $checked.Count - $failed.Count

Write-Output ""
Write-Output "=============================================================================="
Write-Output "汇总: $passed PASS / $($failed.Count) FAIL  (共 $($checked.Count) 项断言)"
if ($failed.Count -gt 0) {
    Write-Output "失败项:"
    foreach ($f in $failed) {
        Write-Output ("  - [{0}] {1}  ({2})" -f $f.Group, $f.Name, $f.Detail)
    }
}
else {
    Write-Output "全部静态检查通过"
}
Write-Output "=============================================================================="

if ($failed.Count -gt 0) { exit 1 }
exit 0
