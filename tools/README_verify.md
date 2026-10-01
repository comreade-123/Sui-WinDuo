# 离线验证说明 (tools/README_verify.md)

本目录提供**在没有 ESP32 实物、没有 Adafruit/ArduinoJson 库、网络受限**的条件下，
对 WindowsDuo_EthanMaven 交付物可做的全部验证。核心原则：

> 明确区分「已证明」与「未证明」。凡是必须上电才能确认的，一律列入实物验证清单，
> 不用仿真数字冒充实测结果。

---

## 1. 三个工具与各自的作用

| 工具 | 语言 | 作用 | 依赖 |
| --- | --- | --- | --- |
| `verify_firmware.ps1` | PowerShell 5.1 | 对 `.ino` 做 71 项静态/逻辑断言（结构、库、引脚、周期常量、括号配平、JSON 字段与类型、缺陷回归、编码） | 无（仅系统自带） |
| `demo_device.py` | Python 3 | 把固件的姿态解算/零漂校准/按键状态机/输出节流**逐行复刻**成可执行模型，跑 15 个场景 193 条断言 | 无（标准库） |
| `repro_longpress.py` | Python 3 | 长按反复触发缺陷的**最小复现**（buggy / fixed 两个变体对照） | 无 |
| `verify_pitch_range.py` | Python 3 | 对 `pitchFromAccel()` 做量程/单调性数值分析，给出 ±90° 折返边界与端点灵敏度 | 无 |

配套文档：`PORTING_NOTES.md` 记录仿真与固件的逐函数对齐关系、未复刻部分、符号约定推导。

### 一键运行（Windows，仓库根目录）

```powershell
# 1) 固件静态检查（Windows PowerShell 5.1；有 FAIL 则退出码为 1）
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1

# 2) 设备行为仿真（15 场景 / 193 断言；--quick 可缩短静置窗口做快速回归）
python tools\demo_device.py
python tools\demo_device.py --quick
python tools\demo_device.py --list                 # 列出全部场景
python tools\demo_device.py --scenario jitter      # 只跑一个场景
python tools\demo_device.py --json-out verification\demo_device_result.json

# 3) 仿真参数与固件常量交叉核对（22 项，任一处漂移即 FAIL）
python tools\demo_device.py --check-firmware firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino

# 4) 长按缺陷最小复现（当前固件应为 fixed 一列全部符合预期）
python tools\repro_longpress.py

# 5) 加速度计量程/单调性分析
python tools\verify_pitch_range.py
```

> `verify_firmware.ps1` 带 UTF-8 BOM —— 这是刻意的：Windows PowerShell 5.1 读取
> **无 BOM** 的 UTF-8 脚本时会按 ANSI(GBK) 解码，导致中文断言名乱码。
> `.py` / `.md` 保持无 BOM。

---

## 2. 已验证（无硬件即可确认的事实）

### 2.1 静态结构（71 项断言全 PASS）

- 恰好一个 `setup()` / 一个 `loop()`；圆/花/方括号配平（跳过字符串与注释）；
- **全代码 0 处 `delay()` / `delayMicroseconds()` / `while(true)`**，时间基准为 `millis()`；
- OLED 200ms、串口 50ms、IMU 5ms、按键消抖/长按、校准窗口 **全部走 `millis()` 节流**
  （接受 `now-last >= P` 与 `now-last < P` 两种等价非阻塞写法）；
- 库包含：`Wire / Adafruit_GFX / Adafruit_SSD1306 / Adafruit_MPU6050 / ArduinoJson`；
- 常量：`SDA=23 SCL=22 BUTTON=5 INPUT_PULLUP MPU=0x68 OLED=0x3C BAUD=115200
  ANGLE 0..180 COMP_FILTER_ALPHA=0.98 OLED=200ms SERIAL=50ms BUTTON 30/50/800ms CALIB=3000ms`；
- 文件为**无 BOM 的合法 UTF-8**，中文无乱码迹象（严格 UTF-8 解码 + 乱码启发式）。

### 2.2 JSON 接口契约（193 条断言中与接口相关的部分）

- 每行都是合法 JSON，**键集合与固件一致**：
  `{angle,status,mode,author}`；debug 模式追加 `{gyro,bias,base,sp,lp}`；校准中追加 `progress`；
- **`angle` 是 JSON 数值（float），不是字符串** —— 这是对"曾经用
  `serialized(String(angleDeg,1))` 产出 `"angle":"45.3"`"这一缺陷的**回归断言**，
  静态检查与仿真两侧都覆盖；
- `angle` 量化到 1 位小数；`gyro/bias/base` 的量化位数分别为 2/3/2；
- 噪声行（`#` 日志行、截断 JSON、乱码、`1e999999` 溢出、半行、超长行）**全部被安全忽略**，
  解析器不抛异常；`angle` 为字符串或数字两种形态都能解析。

### 2.3 姿态链的数值行为（仿真，15 场景 / 193 断言）

| 场景 | 真实输出（实测自本机运行） |
| --- | --- |
| `static` 静止 30s | 静态标准差 **0.0073°**，30s 漂移估计 **+0.0008°**，越界 0/6800 |
| `close` 合盖到机械极限 | 输出稳定在摆幅附近，无跳变 |
| `open_back` 双向 | 合盖/展开双向单调（单调比 ≥0.95），两端极差 <1° |
| `full_180` 全 180° 行程 | 输出达到 **179.83**，回到合盖 **0.00**，全程单调 1.000，越界 0/8300 |
| `jitter` 15Hz±8° 抖动 | 抖动段标准差 **13.67°** vs 静止段 **0.0074°**（>5 倍，滤波有效） |
| `jitter_low` 15Hz±45° 大幅抖动 | 角度仍被夹在 [0,180]，越界 0/3300 |
| `drift` 注入 2.0 dps 零漂静置 20s | 30s 漂移估计 **+0.0027°**（静止锚定把它压住了） |
| `noise` 噪声注入 | 60 行合法 JSON 全部解析成功，0 失败 |

**断言层面还覆盖**：角度恒在 [0,180]、无 NaN、无越界回卷、限幅生效、
模式名恒为 `default/calibrate/debug`、状态名恒为 `warming_up/calibrating/ok/sensor_error`、
串口行数与 OLED 帧数落在节流预期内。

### 2.4 校准静止判定真的会拒绝"边动边校准"

`calib_motion` 场景（在 3s 校准窗口内合盖，角速度约 30 dps）：

- `accepted=False`，`meanAbsGyro=30.00 > CALIB_GYRO_MAX_DPS(6.0)`；
- 零偏保持 0、基准未被污染（**没有把错误值写进系统**）——
  对应固件 `finishCalibration(false)` 分支，行为正确。

### 2.5 按键时序

| 输入 | 期望 | 实测 |
| --- | --- | --- |
| 长按 1.2s 后松开 | 长按事件 1 次 | **1 次** |
| 长按 3.0s 后松开 | 长按事件 1 次 | **1 次** |
| 两次长按（各 1.0s，中间松开 0.5s） | 长按事件 2 次 | **2 次** |
| 短按 300ms ×3 | 短按 3 次、长按 0 次、模式轮换 `default→calibrate→debug→default` | **全部符合** |
| 长按松手后 | 不得被误判为短按 | **未产生短按** |

### 2.6 已修复缺陷的回归覆盖

1. **长按反复触发**（原 `.ino:649-666`）：`repro_longpress.py` 显示修复前按住 1.2s 触发
   **38 次**、3.0s 触发 **218 次**；修复后均为 **1 次**。静态检查另加 5 条断言
   （`btnLongLatched` 声明/置位/清除、长按与短按分支的 `!btnLongLatched` 条件）。
2. **`angle` 被序列化为字符串**：静态断言"不得出现 `serialized(String(`"
   "`doc["angle"]` 必须是数值赋值"，仿真断言 `isinstance(angle, float)`。

### 2.7 物理量程约束的量化（重要设计发现）

`verify_pitch_range.py` 证明 `atan2(-ax, sqrt(ay²+az²))` 的值域是 **[-90°, +90°]**：

- 传感器俯仰超过 ±90° 后读数**折返**（`theta` 与 `180-theta` 同读数）；
- 折返区间内加速度计的**纠偏方向相反**（灵敏度 `dM/dT = −1`，在 90° 处为 0）；
- 固件输出是 `|wrap180(pitch-baseline)|`，**1:1 无缩放**，因此"展开 ≈180°"
  要求传感器俯仰实际变化 180°，中间 90° 只能靠陀螺积分撑过去。

**结论**：固件能把 0..180 走完（`full_180` 实测 179.83），但**前提是屏幕行程与
传感器转角约为 1:1**。这个比例必须实物标定，不能靠仿真假定。

---

## 3. 未验证 —— 必须实物确认的清单

> 以下每一条都**无法**由本目录的工具证明。请在硬件到货后逐条打勾。

### 3.1 必须实物验证（阻断性，按优先级排序）

> **第 1 项是其余各项的前置条件**：若行程比例不成立，0~180 的映射语义就不成立，
> 后面所有"端点是否到 0/180"的判断都会失去意义。**请先做第 1 项。**

| # | 项目 | 为什么必须实物 | 怎么验 |
| --- | --- | --- | --- |
| **1** | **屏幕行程 : 传感器转角的真实比例 k** ⭐前置条件 | 决定 0~180 能否走满。固件输出是 `|wrap180(pitch - baseline)|`，**1:1 无缩放**；而加速度计俯仰在 ±90° 外折返（见 `verify_pitch_range.py`），所以"合盖≈0、展开≈180"要求**传感器的俯仰变化量 ≈ 屏幕的开合变化量**。比例不足时端点走不满 | 见下方"实测方法" |
| 2 | **两端点标定** | `full_180` 只在"k≈1"的仿真假定下到 179.83 | 在完全合盖位长按重新校准（此时读数为 0），再按第 1 项的 k 换算检查另一端点 |
| 3 | **I2C 地址实际响应** | 仿真假定设备永远在线 | 跑 I2C 扫描，确认 `0x68` 与 `0x3C` 都出现；确认 AD0 接地 |
| 4 | **OLED 实际点亮** | 只验证了节流与调用，没渲染像素 | 上电看首屏 `WindowsDuo EthanMaven / OLED 0x3C ready`，再看主界面角度/进度条 |
| 5 | **按键实际电平** | 仿真直接置 `rawPressed`，没读引脚 | 万用表/串口确认未按时 GPIO5 为高、按下为低；确认不会一直触发短按 |
| 6 | **真实零漂量级** | 仿真用 0.05 dps 高斯白噪声，非实测 | 静置 10 分钟记录 `angle` 漂移；`debug` 模式看 `bias` |
| 7 | **PC 端玻璃效果在 Win10 / Win11 的差异** | 本机无法在两种系统上跑图形栈 | Win11 看亚克力/云母路径；Win10 看自绘降级路径；记录帧率与视觉差异 |
| 8 | **串口端口与占用** | 仿真不碰 COM 口 | 确认 PC 端脚本能在关闭 Arduino 串口监视器后独占打开同一 COM 口 |

#### 第 1 项的实测方法（屏幕行程 : 传感器转角比例 k）

目的：测出**屏幕相对底座的开合角变化 Δscreen** 与**固件读数的变化 Δsensor** 之比
`k = Δsensor / Δscreen`，用来判断 0~180 能否走满。

步骤：

1. 把装置固定到笔记本上，**在完全合盖位**开机，等 3 秒校准结束
   （串口出现 `# calibration done...`）。
2. 若此刻读数不是 0，**长按按键 ≥800ms** 重新校准一次 —— 长按会以当前位置为新的零点，
   因此后续读数就是从这一端起算的行程。
3. 用**手机水平仪**（或量角器/倾角仪）贴在**屏幕面**上，测屏幕相对底座平面的夹角，
   记为 `S1`；同时记录固件的 `angle` 值，记为 `A1`（通常 A1≈0）。
4. 把屏幕**缓慢开到最大**（机械极限，不外掰），量角器再测一次，记为 `S2`；
   记录固件的 `angle` 值 `A2`。建议同时确认读数在移动全过程中**单调不反向**。
5. 计算：
   - `Δscreen = |S2 - S1|`（单位：度，屏幕实际转过的角度）
   - `Δsensor = |A2 - A1|`（单位：度，固件读数变化）
   - **`k = Δsensor / Δscreen`**
6. 判读：

| k 实测值 | 含义 | 处置 |
| --- | --- | --- |
| `k ≈ 1.0`（0.9~1.1） | 满量程可用，0~180 语义成立 | 直接使用；按检查表确认两端点即可 |
| `0.5 < k < 0.9` | 端点走不满，读数范围被压缩 | 二选一：① 在 PC 端乘 `1/k` 换算成真实屏幕角；② 改安装朝向/换轴，让传感器转角与屏幕行程匹配 |
| `k ≤ 0.5` | 传感器只覆盖了一小段行程 | 必须改安装方式（或按 `verify_pitch_range.py` 第 5 节的建议改姿态解算），否则大量读数挤在中间、端点无法分辨 |
| `k > 1.1` | 传感器转角大于屏幕行程 | 检查转轴是否装错轴；确认 `MPU_MOUNT_ROTATION_MATRIX` 是否需要改 |

> 记录建议：把 `S1/S2/A1/A2/k` 四个数写进 `docs/hardware_checklist.md` 第 8 节验证记录表，
> 并在 `verification/VERIFICATION_REPORT.md` 的"实物验证回填"处补一条结论。
>
> 注意 `k` 与"反向倾倒取绝对值"无关：固件对相对角取绝对值，所以两个方向都映射到 0~180，
> 但**每个方向的单边行程上限仍受 ±90° 折返约束**（这就是需要 k 的原因）。

### 3.2 建议实物验证（非阻断）

| # | 项目 | 说明 |
| --- | --- | --- |
| 9 | 串口 20Hz 与 OLED 200ms 的**实际**节奏 | 用逻辑分析仪/时间戳统计；`millis()` 在真实负载下的抖动 |
| 10 | 校准窗口实际样本数 | 固件按 `dtMs>=5` 计数，真机循环更快，实测应 ≈600（本仿真恰好 600） |
| 11 | 长按 800ms 的真实手感与误触发率 | 机械按键抖动、氧化、接触电阻 |
| 12 | 振动/移动场景下的噪声谱 | 仿真用白噪声；真机在颠簸环境可能显著更差 |
| 13 | 长时运行（≥30 分钟）稳定性 | 看是否崩溃、内存是否持续增长、`millis()` 回绕（49.7 天） |
| 14 | 温度漂移 | MPU6050 零偏随温度变化，冷启动 vs 热机后各测一次 |
| 15 | ArduinoJson 实际浮点打印 | 仿真用 Python `json.dumps` 逼近，真机需抓一行确认小数位 |
| 16 | 供电/接线可靠性 | 3V3 电流、I2C 上拉电阻、线长导致的通信错误率 |

### 3.3 本目录明确不覆盖的代码路径

- `pollSerialCommand()` / `handleCommand()` 的**主机→设备命令**（`cal` / `mode=` / `status`）；
- OLED 绘制函数（`drawMainScreen()` / `drawCalibrationScreen()`）的像素结果；
- 传感器掉线重连（`mpu.begin()` 失败重试、`STATUS_SENSOR_ERROR` 恢复路径）；
- ESP32 特有的栈/堆、看门狗、`Serial` 缓冲行为。

---

## 4. 实物到货后的一键验证流程

```powershell
# 步骤 0：先跑一遍离线三项，确认交付物没有被改坏
powershell -NoProfile -ExecutionPolicy Bypass -File tools\verify_firmware.ps1
python tools\demo_device.py --quick
python tools\demo_device.py --check-firmware firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino

# 步骤 1：接线自检（不烧固件）
#   - 确认 SDA=GPIO23 / SCL=GPIO22 / 按键=GPIO5→GND / MPU AD0→GND
#   - 用 I2C 扫描确认 0x68 与 0x3C

# 步骤 2：烧录后看串口（115200）
#   - 期望先看到 "# calibration started: boot"
#   - 3 秒后 "# calibration done. gyro_bias(dps)=...,... baseline_pitch=..."
#   - 之后每 50ms 一行 JSON，angle 为数字（不带引号）

# 步骤 3：对照 docs\hardware_checklist.md 第 6 节逐项打勾
#   - 合盖端点 angle≈0，展开端点 angle≈180，过程单调不跳变

# 步骤 4：把实测值填进 docs\hardware_checklist.md 第 8 节验证记录表
#   并把结论回填到 verification\VERIFICATION_REPORT.md 的"实物验证"一节
```

---

## 5. 已知残余风险（离线无法闭环）

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| `pitchFromAccel` 在 ±90° 外折返 | 屏幕行程若明显大于传感器转角，端点附近角度会走不满或反向 | 实物标定行程比例；必要时按 `verify_pitch_range.py` 的建议改姿态解算 |
| 仿真噪声为理想白噪声 | 所有标准差/漂移数字**只代表模型量级**，不是硬件指标 | 实测量级后回填本文件与报告 |
| 仿真与固件是两份代码 | 未来单边改动会造成结论失真 | `--check-firmware` 常跑；`PORTING_NOTES.md` 记录对齐关系 |
| 未覆盖主机→设备命令路径 | `cal` / `mode=` 出错时离线检查发现不了 | 实物阶段手工发命令验证 |
| OLED/渲染未覆盖 | 显示错乱、进度条越界发现不了 | 实物目视确认 |
