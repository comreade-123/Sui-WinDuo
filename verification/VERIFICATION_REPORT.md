# 独立验证报告 — WindowsDuo_EthanMaven

| 项目 | 内容 |
| --- | --- |
| 验证者 | verify-dev（独立验证工程师，不参与固件/PC 端编码） |
| 验证对象 | `firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino`（956 行）、`tools/*`、`verification/*` |
| 验证时间 | 见各步骤"运行时间"标注（同一会话内顺序执行） |
| 环境 | Windows；Python 3.11.0；**Windows PowerShell 5.1.26100.9444（Desktop）**；无 ESP32 实物 |
| 结论 | **离线可验证项全部通过（71 静态 + 193 仿真 + 22 常量对齐 = 286 条断言，0 FAIL）**；发现并推动修复 **2 个真实固件缺陷**；另有 **1 项设计约束**（加速度计 ±90° 量程）必须实物标定，已列入实物验证清单 |

> 本报告所有数字均为本机**真实运行输出**，未做任何修饰或推测填充。
> 原始 stdout 未长期归档（体积原因），但每条数字都可按第 1 节的命令在同机复现。

---

## 0. 重要声明：本次验证"证明了什么 / 没证明什么"

| | 内容 |
| --- | --- |
| **已证明（无硬件可证）** | 固件的静态结构、常量、非阻塞调度、JSON 接口契约与字段类型、姿态解算的收敛与限幅、校准静止判定的拒绝逻辑、按键短/长按时序、噪声行解析健壮性、2 个缺陷的修复与回归 |
| **未证明（必须实物）** | I2C 地址实际响应、OLED 实际点亮、按键实际电平、真实零漂量级、**屏幕行程与传感器转角的真实比例**、PC 端玻璃效果在 Win10/Win11 的差异、串口端口独占、长时稳定性、温度漂移 |
| **明确不覆盖** | 主机→设备命令路径（`cal`/`mode=`/`status`）、OLED 像素渲染、传感器掉线重连路径、ESP32 栈/堆/看门狗行为 |

实物验证的**完整清单**见 `tools/README_verify.md` 第 3 节（阻断性 8 项 + 建议 8 项）。
下文第 6 节也列出了必须实物确认的条目。

---

## 1. 复现命令一览

```powershell
# 1) 固件静态检查（Windows PowerShell 5.1；有 FAIL 则退出码 1）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1

# 2) 设备行为仿真（15 场景 / 193 断言）
python tools\demo_device.py
python tools\demo_device.py --quick

# 3) 仿真参数 vs 固件常量交叉核对
python tools\demo_device.py --check-firmware firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino

# 4) 长按缺陷最小复现（buggy / fixed 对照）
python tools\repro_longpress.py

# 5) 加速度计量程/单调性分析
python tools\verify_pitch_range.py
```

原始 stdout **不落盘留档**（避免仓库里堆积 >150KB 的生成物）。本报告已逐项引用其中的真实数字，
任何一条都可用上面的命令**在同机复现**；需要完整逐条输出时按下面方式重新生成即可：

```powershell
# 需要完整原始输出时（会写到 verification/，注意别提交）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1 | Tee-Object -FilePath verification\_raw_static.txt
python tools\demo_device.py            | Tee-Object -FilePath verification\_raw_demo_device.txt
python tools\demo_device.py --json-out verification\demo_device_result.json

# 不需要时请清理（这些是生成物，非交付物）
Remove-Item verification\_raw_*.txt, verification\demo_device_result*.json -ErrorAction SilentlyContinue
```

> 建议（`.gitignore` 由 docs-dev 负责，我未修改）：如需长期防误提交，可加入以下规则 ——
> ```
> verification/_raw_*
> verification/demo_device_result*.json
> tools/__pycache__/
> ```
> 或者直接提交前用上面的 `Remove-Item` 清理。**本次交付仓库内只保留
> `VERIFICATION_REPORT.md` 与 `pitch_range.json` 两个文件。**

---

## 2. 逐项结果

### 2.1 固件静态检查 — 71 PASS / 0 FAIL

命令：`powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1`

真实输出结尾：

```
==============================================================================
汇总: 71 PASS / 0 FAIL  (共 71 项断言)
全部静态检查通过
==============================================================================
```

分组结果（实测标题行）：

| 分组 | 结果 | 关键证据 |
| --- | --- | --- |
| A. 文件与编码 | 5 PASS | 34.3 KB / **956 行** / **UTF-8 无 BOM** / 严格 UTF-8 解码成功（30313 字符）/ 乱码启发式 0 处 |
| B. 结构 | 9 PASS | setup() ×1、loop() ×1；**`delay(` 0 次、`delayMicroseconds(` 0 次、`while(true)` 0 次**；`millis()` 5 次；圆/花/方括号净值均为 0 |
| C. 库包含 | 5 PASS | Wire.h / Adafruit_GFX.h / Adafruit_SSD1306.h / Adafruit_MPU6050.h / ArduinoJson.h 各 1 处 |
| D. 引脚/地址/周期 | 19 PASS | SDA=23、SCL=22、BUTTON=5、INPUT_PULLUP、MPU=0x68、SSD1306=0x3C、BAUD=115200、OLED=200ms、SERIAL=50ms、ALPHA=0.98、ANGLE 0/180、BUTTON 30/50/800ms、CALIB=3000ms；`Wire.begin(SDA,SCL)` 显式指定 |
| E. 非阻塞调度 | 6 PASS | OLED/串口/IMU/消抖/长按/校准窗口**全部走 `millis()` 节流** |
| F. JSON 输出 | 12 PASS | `serializeJson` 命中；**`serialized(String(` 出现 0 次**（回归通过）；**`doc["angle"]` 为数值赋值**；angle/status/mode/author 四键齐备；debug 五字段齐备；progress 存在；行尾换行 |
| G. 缺陷回归 | 7 PASS | `btnLongLatched` 声明/置位/清除合计 **4 处（≥3）**；长按与短按分支均带 `!btnLongLatched`；量化写法存在 |
| H. 校准与滤波 | 5 PASS | 方差静止判定命中 2、角速度静止判定命中 2、`finishCalibration(false)`（拒绝时不覆盖零偏）命中 1、`clampFloat` 4、`wrapDeg180` 5 |
| I. 模式名 | 3 PASS | default / calibrate / debug 齐备 |

**边界行为验证**：对不存在的路径运行时脚本给出清晰 FAIL 而非崩溃 ——

```
[FAIL] 固件文件不存在: firmware\DoesNotExist\Missing.ino
       请检查 lead 是否已落地 firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino
结果: 0 PASS / 1 FAIL  (无法继续, 缺少目标文件)
EXIT=1
```

### 2.2 设备行为仿真 — 15 场景 / 193 断言 / 0 FAIL

命令：`python tools\demo_device.py`

真实输出结尾：

```
==============================================================================
汇总: 15 场景 / 193 断言 / 0 FAIL
全部断言通过
```

关键实测数字（非 quick 全量运行）：

| 场景 | 实测输出 |
| --- | --- |
| `static` 静止 30s | 静态标准差 **0.0073°**；30s 漂移估计 **+0.0008°**；越界 0/6800；NaN 0；校准 1 次且被接受（样本 **600**） |
| `close` 合盖到机械极限 | 静止段标准差 0.0258°；无跳变；校准被接受 |
| `open_back` 双向 | 运动段标准差 25.1581°；双向单调比 1.000；两端极差 <1°；越界 0/5500 |
| `full_180` 全 180° 行程 | 输出达到 **179.83**；回到合盖 **0.00**；单调 1.000/1.000；越界 0/8300 |
| `jitter` 15Hz±8° 抖动 | 抖动段 **13.6678°** vs 静止段 **0.0074°**（比值 ≈1847 倍） |
| `jitter_low` 15Hz±45° | 运动段 23.2243°；角度仍夹在 [0,180]，越界 0/3300 |
| `longpress` 长按 1.2s | **长按 1 次**、校准 2 次（boot + long press）；短按 0 |
| `longpress_3s` 长按 3.0s | **长按 1 次**、校准 2 次 |
| `longpress_double` 两次长按 | **长按 2 次**、校准 3 次（其中 2 次被接受，1 次为长按后立即被下一次长按打断）；短按 0 |
| `shortpress` 短按 300ms×3 | **短按 3 次**、长按 0 次；模式轮换 `default→calibrate→debug→default` 符合 |
| `calib_motion` 校准中合盖 | **accepted=False**、`meanAbsGyro=30.00 > 6.0`；零偏保持 **0**、基准未被污染 |
| `drift` 注入 2.0 dps 零漂静置 20s | 30s 漂移估计 **+0.0027°**（静止锚定把积分漂移压住） |
| `drift_recal` 注入零漂后长按重校准 | 测得零偏 **2.0005 dps**（注入 2.0，误差 0.0005）；30s 漂移收敛到 −2.1648° |
| `noise` 噪声注入 | 60 行合法 JSON 全部解析成功，0 失败；13 类噪声行全部安全忽略 |
| `json_schema` 三模式 JSON | 三种模式都出现；`default(ok)` 键集合 = 4 基础键；`debug` 键集合 = 4+5；校准行带 `progress` |

**跨全部场景的通用断言**：角度恒在 [0,180]、无 NaN、模式名与状态名合法、
串口行数与 OLED 帧数落在节流预期内、**每行 JSON 合法且键集合与固件一致**、
**`angle/gyro/bias/base` 均为 float 而非 str**、`angle` 量化到 1 位小数。

**全量累计（15 场景汇总，取自本次 `--json-out` 生成的汇总数据）**：

```
场景数 = 15    断言总数 = 193    total_fail = 0
JSON 总行数 = 4964     总样本数 = 49640
总越界样本 = 0         总 NaN = 0
总校准次数 = 22        按键事件: 短按 6 / 长按 5
```

### 2.3 常量交叉核对 — 22 PASS / 0 FAIL

命令：`python tools\demo_device.py --check-firmware firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino`

```
结果: 22 PASS / 0 FAIL
```

覆盖 `COMP_FILTER_ALPHA=0.98`、`OLED_UPDATE_INTERVAL=200`、`SERIAL_SEND_INTERVAL=50`、
`CALIB_WINDOW_MS=3000`、`CALIB_TIMEOUT_MS=8000`、`CALIB_GYRO_MAX_DPS=6.0`、`CALIB_VAR_MAX=900.0`、
`STILL_BASE_TRACK=0.004`、`ACCEL_TRUST_STILL=0.995`、`ACCEL_TRUST_MOVING=0.98`、`STILL_ABS_DPS=1.2`、
`BUTTON_DEBOUNCE_MS=30`、`BUTTON_LONG_MS=800`、`BUTTON_SHORTMIN_MS=50`、
`ANGLE_MIN_DEG=0.0`、`ANGLE_MAX_DEG=180.0`、`ANGLE_LPF_ALPHA=0.35`、`SERIAL_BAUD=115200`
等 18 个常量 + 模式名/无 delay/serializeJson 共 22 项，**仿真与固件逐项一致**。

---

## 3. 发现的问题与处置

### 3.1 缺陷 1（严重，功能性）：长按保持不放 → 重校准被反复触发

| 项 | 内容 |
| --- | --- |
| 位置 | 原 `WindowsDuo_EthanMaven.ino:649-666`（`updateButton()` 的 `BTN_PRESSED` / `BTN_DEBOUNCE_UP`） |
| 机理 | 长按成立后仅更新 `btnEdgeMs`（L656），**未重置 `btnPressedMs`**；手指仍按住时 `BTN_DEBOUNCE_UP` 把状态打回 `BTN_PRESSED`（L665），于是 `now - btnPressedMs >= BUTTON_LONG_MS` 立刻再次成立 → 每 ~30ms 重复触发一次 |
| 影响 | 按住不放时每几十毫秒重启一次零漂校准，`calibSampleCount` 反复归零，**长按永远无法完成一次有效校准**；串口被 `# calibration started` 刷屏 |
| 最小证据 | `python tools\repro_longpress.py`：按住 1.2s → **长按触发 38 次**（期望 1）；按住 3.0s → **218 次**（期望 1）；两次长按 → **36 次**（期望 2）；短按 300ms → 正常 1 次 |
| 建议 | 增加"本次按住已触发"闩锁，仅在真正松开后清除 |
| 处置 | **lead 已修复**：新增 `btnLongLatched`（声明 L203、触发判定 L655-656、松手清除 L678），并给短按分支加 `!btnLongLatched`（L674）避免长按松手被误判为短按 |

**修复后复验（buggy / fixed 对照，真实输出）**：

```
[单次长按 1.2s 后松开]
  当前固件(buggy): 长按触发=38 次, 校准次数=38, 短按=0
  建议修法(fixed): 长按触发=1 次, 校准次数=1, 短按=0
[单次长按 3.0s 后松开]
  当前固件(buggy): 长按触发=218 次, 校准次数=218, 短按=0
  建议修法(fixed): 长按触发=1 次, 校准次数=1, 短按=0
[两次长按 (各 1.0s, 中间松开 0.5s)]
  当前固件(buggy): 长按触发=36 次, 校准次数=36, 短按=0
  建议修法(fixed): 长按触发=2 次, 校准次数=2, 短按=0
[短按 300ms]
  当前固件(buggy): 长按触发=0 次, 校准次数=0, 短按=1
  建议修法(fixed): 长按触发=0 次, 校准次数=0, 短按=1
```

对照 lead 要求 A 的 4 项：**1.2s→1 次 ✅、3.0s→1 次 ✅、两次长按→2 次 ✅、
短按 300ms→短按 1 次/长按 0 次 ✅**，且长按松手后**未被误判为短按**（fixed 变体短按=0）。
仿真侧另加 `longpress` / `longpress_3s` / `longpress_double` 三个场景守住该行为。

### 3.2 缺陷 2（兼容性）：`angle` 被序列化成 JSON 字符串

| 项 | 内容 |
| --- | --- |
| 位置 | 原 `WindowsDuo_EthanMaven.ino:842`：`doc["angle"] = serialized(String(angleDeg, 1));` |
| 影响 | 产出 `{"angle":"45.3",...}`，`angle` 是 **str** 而非数字。PC 端若按数字实现，真机连上即 `TypeError` 或静默错误（参与比较、送 shader uniform 时尤其危险） |
| 建议 | 保留量化但写数值：先量化成整数再除回来 |
| 处置 | **lead 已修复**：`doc["angle"] = (float)(angleTenths / 10.0);`（L856），debug 的 `gyro/bias/base` 同样处理（L863-868，含负数取整补偿） |

**修复后复验**：

- 静态断言：`serialized(String(` 出现 **0 次**；`doc["angle"]` 为数值赋值命中 **1 处** → PASS
- 仿真断言：对 15 个场景累计 **4964 行 JSON** 逐行 `json.loads`，
  **`isinstance(v["angle"], float)` 全部成立** → PASS（样例
  `{"angle":0.0,"status":"calibrating","mode":"default","author":"EthanMaven","progress":1}`）

### 3.3 设计约束（非缺陷，但必须实物标定）：加速度计 ±90° 量程

| 项 | 内容 |
| --- | --- |
| 证据 | `python tools\verify_pitch_range.py` |
| 机理 | `pitchFromAccel = atan2(-ax, sqrt(ay²+az²))` 第二参数恒非负 ⇒ 值域恒为 **[-90°, +90°]**。传感器俯仰超出后读数**折返**（`theta` 与 `180-theta` 同读数），且折返区间内**纠偏方向相反**：实测灵敏度 `dM/dT` 在 0/30/60/85° 为 **+1.000**，在 90° 为 **0.000**，在 95/120/150/180° 为 **−1.000** |
| 后果 | 固件输出 `|wrap180(pitch-baseline)|` 是 **1:1 无缩放**，所以"展开 ≈180°"要求传感器俯仰实际变化 180°，中间 90° 只能靠陀螺积分撑过去；静止时"基准锚定"会把基准朝加速度计读数方向拽 |
| 实测边界 | 折返边界 = **95°**（5° 步长扫描的首个非单调点；理论上界为 90°）；跨 180° 行程静态可达输出 = **180.00°** |
| 仿真验证 | `full_180` 场景（基准在完全合盖 −90°、展开到 +90°）输出达到 **179.83**、回到合盖 **0.00**、全程单调 —— 说明**在 1:1 行程假定下固件能走满 0..180**，但该假定必须实测量角确认 |
| 建议 | ① 实物量出"屏幕开合角 : 传感器俯仰"的真实比例；若比例明显不是 1:1，端点附近会走不满或反向；② 若确实需要 180° 满量程且比例不足，需改姿态解算（绕竖直轴安装 / 用 `az` 符号做象限判定展开 atan2 量程 / 明确"陀螺积分撑满程+端点校准"并写进文档） |

> 说明：此结论与 `docs/hardware_checklist.md` 第 6 节"合盖 angle≈0、展开 angle≈180"的
> 宣称**不矛盾但需前置条件**。建议 docs 补一句行程比例的前置说明 —— 已在本报告第 6 节列为建议项。

### 3.4 我自己验证代码中的问题（如实记录）

为避免"只报别人的问题"，以下是我在开发验证工具时出现并修正的错误：

| # | 问题 | 处置 |
| --- | --- | --- |
| 1 | 断言写错：`all(... for m in dev.mode_trace + [dev.mode]) if dev.mode_trace else True` —— `mode_trace` 元素是 `(时间, 模式)` 元组，拿元组去比字符串恒为 False | 改为 `for _, m in dev.mode_trace`，并把 `dev.mode` 单独判断 |
| 2 | 场景时序算错：短按场景把"抬起"放在 500ms 段末尾，导致按下时长被算成 0ms、被消抖忽略 | 改为按压段 300ms + 间隔段 200ms |
| 3 | 校准复刻不完整：漏了"开机 setup() 里就启动校准"这一步 | 新增 `begin()` 并在每个场景开头调用，与固件 `startCalibration("boot")` 对应 |
| 4 | **符号约定不一致**导致输出卡在半个量程：陀螺与加速度计的符号未同向翻转，`wrap180` 先把相对角折到 180 附近、`abs()` 又折回 45 | 推导并统一为 `theta=-true_pitch`、`ax=-sin(theta)`、`gyro=-pitch_rate`；推导过程记入 `tools/PORTING_NOTES.md` 第 4 节 |
| 5 | 抖动场景用 `cos` 起振，段首直接从振幅峰值跳变 | 改用 `sin`，从静置姿态平滑接入 |
| 6 | 段统计把姿态/LPF 收敛过程当成噪声，标准差虚高 | 每段前 200ms 作为预热丢弃（`WARMUP_MS`） |
| 7 | 静态检查里两条正则误判（OLED 用了 `now-last < P` 的反向写法；`Serial.write('\n')` 转义层数过多） | 修正为同时接受两种等价非阻塞写法、放宽转义匹配；**确认是工具 bug 而非固件问题** |

第 4 项尤其值得记录：它一度让"角度只能到 90"看起来像固件缺陷，
实际是我把传感器符号约定搞反了。确认固件本身正确后才下结论。

---

## 4. 与我们约定的对齐参数逐条核对

任务书给出的"仿真器与固件必须一致"的参数，逐条核对结果（全部一致）：

| 约定项 | 约定值 | 固件实测 | 结论 |
| --- | --- | --- | --- |
| 互补滤波 alpha | 0.98 | `COMP_FILTER_ALPHA = 0.98f` | ✅ |
| 陀螺零漂校准 | 3 秒 / 约 600 样本 | `CALIB_WINDOW_MS = 3000`；仿真实测 **600 样本** | ✅ |
| 串口输出 | 20Hz(50ms) | `SERIAL_SEND_INTERVAL = 50`；仿真行数符合 | ✅ |
| OLED 刷新 | 200ms | `OLED_UPDATE_INTERVAL = 200`；仿真帧数符合 | ✅ |
| 按键短按 | 50~800ms 切模式 | `BUTTON_SHORTMIN_MS=50`、`BUTTON_LONG_MS=800` | ✅ |
| 按键长按 | ≥800ms 触发重校准 | 同上，且仿真验证 1.2s/3.0s 各只触发 1 次 | ✅ |
| 角度限幅 | 0..180 | `ANGLE_MIN_DEG=0.0`、`ANGLE_MAX_DEG=180.0`；**49640 个样本越界 0 个** | ✅ |
| 模式名 | default/calibrate/debug | 三处文本常量齐备，仿真模式轮换符合 | ✅ |
| 角度语义 | 俯仰轴修正（开机基准减当前俯仰） | `relativeDeg = wrapDeg180(pitchDeg - baselinePitchDeg)` 后取绝对值 | ✅（注：**取的是绝对值**，两个方向都映射到 0..180，与检查表"反向倾倒取绝对值"一致） |
| 时间基准 | 全用 millis() | `delay(` **0 次**；6 类节流全部走 `millis()` | ✅ |
| 全代码无 delay() | 无 | 静态检查确认 0 处 | ✅ |

**一处需要澄清（非缺陷）**：约定说"角度 = 开机基准减去当前俯仰"，
固件实现是 `|wrap180(pitch - baseline)|`（**取绝对值**）。
两个方向的开合都映射到 0..180，与 `docs/hardware_checklist.md` 第 6 节
"反向倾倒取绝对值方向"一致。已在仿真中按绝对值复刻。

---

## 5. PC 端接口核对（只读，未修改 pc/**）

固件 JSON 契约已由静态 + 仿真双侧锁定：

| 场景 | 键集合 | 类型 |
| --- | --- | --- |
| 正常（default/ok） | `{angle, status, mode, author}` | angle: **number**(1 位小数)；status/mode/author: string |
| 调试（debug） | 上表 + `{gyro, bias, base, sp, lp}` | gyro: number(2 位)、bias: number(3 位)、base: number(2 位)、sp/lp: integer |
| 校准中（calibrating） | 基础键 + `progress` | progress: integer 0..100 |

- **`angle` 现在是 JSON 数值**（缺陷 2 修复后），PC 端可直接 `float(v)` 或直接参与算术；
- 遇到 `#` 开头的日志行必须跳过（固件会输出 `# calibration started/...`）；
- 提示：仿真已验证 `angle` 为字符串或数字两种形态都能被健壮解析，
  但**推荐 PC 端仍做 `float(v)` 归一化**以兼容旧固件。

---

## 6. 必须实物验证的条目（本报告无法闭环）

> 完整版见 `tools/README_verify.md` 第 3 节。以下是任务书点名要求的四项 + 本次新增的关键项。
> **第 1 项是其余各项的前置条件**：若行程比例不成立，0~180 的映射语义就不成立，
> 后面所有"端点是否到 0/180"的判断都会失去意义。**请先做第 1 项。**

| # | 条目 | 为什么必须实物 | 判定标准 |
| --- | --- | --- | --- |
| **1** | **屏幕行程 : 传感器转角的真实比例 k** ⭐前置条件 | 固件输出 `\|wrap180(pitch-baseline)\|` 是 **1:1 无缩放**，而加速度计俯仰在 ±90° 外折返（见 3.3）；因此"合盖≈0、展开≈180"要求传感器俯仰变化量 ≈ 屏幕开合变化量 | 合盖位长按校准（读数归 0）→ 手机水平仪/量角器测屏幕夹角 `S1`、读 `angle=A1`；缓开至机械极限再测 `S2`、读 `A2`；`k=(A2-A1)/(S2-S1)`。k≈1 满量程可用；k<1 端点走不满，需 PC 端乘 `1/k` 或改安装朝向/换轴。**完整步骤见 `tools/README_verify.md` 3.1 节** |
| 2 | **两端点标定** | `full_180` 只在"k≈1"的仿真假定下到 179.83 | 完全合盖位长按重校准后读数为 0，再按第 1 项的 k 换算检查另一端点，且全程单调 |
| 3 | **I2C 地址实际响应** | 仿真假定设备永远在线；`Wire.begin` 是否成功、上拉是否够、AD0 是否接地都要实测 | 扫描到 `0x68` 与 `0x3C` |
| 4 | **OLED 实际点亮** | 只验证了节流与调用路径，未渲染任何像素 | 首屏显示 `WindowsDuo EthanMaven / OLED 0x3C ready`，随后主界面角度与进度条正常 |
| 5 | **按键实际电平** | 仿真直接置位 `rawPressed`，没读 GPIO；未确认按下为低、消抖足够 | 未按为高、按下为低；短按只切 1 次模式、长按只触发 1 次校准 |
| 6 | **真实零漂量级** | 仿真用 0.05 dps 白噪声，非实测；温漂完全未建模 | 静置 10 分钟记录 `angle` 漂移；`debug` 模式读 `bias` |
| 7 | **PC 端玻璃效果在 Win10 / Win11 的差异** | 本机无法在两种系统上跑图形栈 | Win11 亚克力/云母路径、Win10 自绘降级路径分别记录视觉与帧率 |
| 8 | **串口端口与占用** | 仿真不碰 COM 口 | 关闭串口监视器后可独占打开同一 COM 口 |

**建议补充验证**（非阻断）：实际 20Hz 节奏与 OLED 200ms 的真实抖动、校准窗口实际样本数、
长按手感与误触发率、振动环境噪声谱、≥30 分钟长时运行与 `millis()` 回绕、温度漂移、
ArduinoJson 实际浮点打印位数、供电与 I2C 上拉可靠性。

---

## 7. 残余风险与置信度

| 风险 | 影响 | 现有的缓解 | 置信度 |
| --- | --- | --- | --- |
| 仿真与固件是两份代码，可能单边改动 | 仿真结论失真 | 22 项常量交叉核对 + `PORTING_NOTES.md` 逐函数对齐表；建议 CI 常跑 | 高（常量层）/ 中（逻辑层） |
| 噪声模型为理想白噪声 | 所有标准差/漂移数字**只代表模型量级**，不是硬件指标 | 报告中已显式标注；实测后回填 | 低（对硬件）/ 高（对算法） |
| 加速度计 ±90° 折返 | 行程比例不足时端点走不满或反向 | `full_180` 已量化在 1:1 假定下可达 179.83；比例必须实测 | 中 |
| 主机→设备命令路径未覆盖 | `cal`/`mode=` 出错离线发现不了 | 列入实物验证清单 | 未覆盖 |
| OLED 渲染未覆盖 | 显示错乱、进度条越界发现不了 | 列入实物验证清单 | 未覆盖 |
| ArduinoJson 实际浮点打印未覆盖 | 小数位可能与仿真不同 | 静态断言已锁定"数值类型 + 量化写法"；真机抓一行确认 | 中高 |

**总体置信度判断**：对"固件逻辑正确性"（姿态链、校准、按键、JSON、调度）置信度**高**；
对"硬件可用性"置信度**低且必须实测** —— 这正是本报告要划清的界线。

---

## 8. 验证工具清单

| 文件 | 说明 |
| --- | --- |
| `tools/verify_firmware.ps1` | 71 项静态/逻辑断言，Windows PowerShell 5.1 可跑，UTF-8 带 BOM，有 FAIL 退出码 1 |
| `tools/demo_device.py` | 固件逻辑逐行复刻的仿真器，15 场景 193 断言，支持 `--quick` / `--scenario` / `--list` / `--json-out` / `--check-firmware` |
| `tools/repro_longpress.py` | 长按反复触发缺陷的最小复现（buggy/fixed 对照），退出码 1 表示复现到差异 |
| `tools/verify_pitch_range.py` | `pitchFromAccel()` 量程/单调性数值分析，输出折返边界与端点灵敏度 |
| `tools/PORTING_NOTES.md` | 仿真↔固件逐函数对齐表、未复刻部分、符号约定推导 |
| `tools/README_verify.md` | 证明边界、必须实物验证清单、实物到货后一键流程 |

> 说明：`tools/verify_firmware_compile.ps1` 与 `tools/verify_stubs/**` 属 lead 的
> 离线编译预检工具（用真实 xtensa 工具链 + 桩头文件），不在本次验证范围内，我未修改。
