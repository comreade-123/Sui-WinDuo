# 仿真器 ↔ 固件 对齐说明 (tools/PORTING_NOTES.md)

`tools/demo_device.py` 不是"另写一个模型"，而是
`firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino` 中
**姿态解算 / 零漂校准 / 按键状态机 / 输出节流** 的逐行复刻。
本文件记录对齐关系、刻意未复刻的部分，以及符号约定的推导 —— 便于后续维护时
两边同步修改，也便于审查"仿真结论能不能代表固件"。

> 复核方式：`python tools/demo_device.py --check-firmware firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino`
> 会逐个比对下表中的常量，任一处不一致即 FAIL。

## 1. 常量对齐表

| 固件常量 (.ino 行号) | 值 | 仿真对应 | 用途 |
| --- | --- | --- | --- |
| `SERIAL_BAUD` (66) | 115200 | `SERIAL_BAUD` | 串口波特率 |
| `OLED_UPDATE_INTERVAL` (67) | 200 | `OLED_UPDATE_INTERVAL` | OLED 刷新节流 |
| `SERIAL_SEND_INTERVAL` (68) | 50 | `SERIAL_SEND_INTERVAL` | JSON 输出 20Hz |
| `COMP_FILTER_ALPHA` (72) | 0.98f | `COMP_FILTER_ALPHA` | 互补滤波系数 |
| `ANGLE_MIN_DEG` / `ANGLE_MAX_DEG` (73-74) | 0.0 / 180.0 | 同名 | 输出限幅 |
| `ANGLE_LPF_ALPHA` (75) | 0.35f | `ANGLE_LPF_ALPHA` | 输出低通 |
| `CALIB_WINDOW_MS` (80) | 3000 | `CALIB_WINDOW_MS` | 校准采样窗口 |
| `CALIB_TIMEOUT_MS` (81) | 8000 | `CALIB_TIMEOUT_MS` | 校准超时保护 |
| `CALIB_GYRO_MAX_DPS` (82) | 6.0f | `CALIB_GYRO_MAX_DPS` | 静止判定(角速度) |
| `CALIB_VAR_MAX` (83) | 900.0f | `CALIB_VAR_MAX` | 静止判定(方差) |
| `STILL_BASE_TRACK` (86) | 0.004f | `STILL_BASE_TRACK` | 静止基准锚定速率 |
| `ACCEL_TRUST_STILL` (87) | 0.995f | `ACCEL_TRUST_STILL` | 静止时陀螺权重 |
| `ACCEL_TRUST_MOVING` (88) | 0.980f | `ACCEL_TRUST_MOVING` | 运动时陀螺权重 |
| `STILL_ABS_DPS` (89) | 1.2f | `STILL_ABS_DPS` | "静止"判定阈值 |
| `BUTTON_DEBOUNCE_MS` (92) | 30 | `BUTTON_DEBOUNCE_MS` | 按键消抖 |
| `BUTTON_LONG_MS` (93) | 800 | `BUTTON_LONG_MS` | 长按阈值 |
| `BUTTON_SHORTMIN_MS` (94) | 50 | `BUTTON_SHORTMIN_MS` | 短按下限 |
| `RAD_TO_DEG_F` (76) | 57.29578f | `RAD_TO_DEG_F` | 弧度转角度 |

采样/姿态节流：固件 `sampleImu()` 用 `now - lastImuSampleMs < 5` 限制到约 200Hz，
`updateAttitude()` 用 `elapsedMs < 5` 同样节流。仿真固定以 `LOOP_DT_MS = 5ms`
步进，因此 3000ms 校准窗口恰好得到 600 个样本（与固件注释"约 600 个样本"一致）。

> **前置条件（本表全部常量成立也仍需实物确认）**：固件输出是 `|wrap180(pitch-baseline)|`，
> **1:1 无缩放**，而加速度计俯仰在 ±90° 外折返（见第 5 节）。因此 0~180 的映射语义
> 额外要求「**屏幕行程 : 传感器转角 k ≈ 1**」；k 的实测方法见
> `tools/README_verify.md` 第 3.1 节第 1 项。**这是本节常量对齐之外、必须实物量出来的前置条件。**

## 2. 逐函数对齐

| 固件函数 | 仿真对应 | 复刻程度 |
| --- | --- | --- |
| `pitchFromAccel()` (283-285) | `pitch_from_accel()` | 完全一致（含 `atan2(-ax, sqrt(ay²+az²))` 的 ±90 量程特性） |
| `clampFloat()` (225-228) | `clamp_float()` | 完全一致 |
| `wrapDeg180()` (232-235) | `wrap_deg180()` | 完全一致 |
| `updateAttitude()` (565-623) | `FirmwareModel.update_attitude()` | 完全一致，含"静止时信任加速度计 / 运动时信任陀螺"与基准锚定分支 |
| `startCalibration()` / `updateCalibration()` / `finishCalibration()` (469-558) | `start_calibration()` / `update_calibration()` / `finish_calibration()` | 完全一致，含方差+角速度双重静止判定、超时保护、"拒绝时不覆盖旧零偏" |
| `updateButton()` (630-687) | `FirmwareModel.update_button()` | 完全一致，含 `btnLongLatched` 闩锁（缺陷修复后） |
| `nextMode()` (690-701) | `FirmwareModel._next_mode()` | 完全一致，含 `default -> calibrate` 会顺带启动校准 |
| `sendJsonLine()` (845-878) | `FirmwareModel.send_json_line()` | 一致，含"先量化成整数再除回来"的数值写法与 debug/calibration 附加字段 |
| `updateOled()` (713-741) | `FirmwareModel.update_oled()` | 只复刻**节流与重绘判定**，不渲染像素 |

## 3. 刻意未复刻的部分（仿真结论不覆盖这些）

1. **I2C 总线行为**：`Wire.begin/setClock`、`mpu.getEvent()` 的返回值、
   `oled.begin()` 的成功/失败分支。仿真里传感器永远"在线"。
2. **OLED 像素渲染**：`drawMainScreen()` / `drawCalibrationScreen()` 的绘制内容、
   字体、进度条几何。仿真只记录"是否刷新/刷新了什么量级"。
3. **ArduinoJson 的真实序列化器**：仿真用 Python `json.dumps` 逼近。
   已验证的是**字段集合与类型约定**，不是 ArduinoJson 的浮点格式化细节
   （例如 `45.2f` 转 double 后的实际打印位数需要真机抓串口确认）。
4. **`Serial` 命令解析**（`pollSerialCommand()` / `handleCommand()`，878-938）：
   主循环里的接收方向目前未纳入场景断言。
5. **中断/看门狗/内存**：ESP32 上的栈、堆、`millis()` 溢出（49.7 天）行为。
6. **真实噪声统计**：仿真用高斯白噪声（陀螺 0.05 dps、加速度计 0.20 deg）。
   真机噪声谱（尤其振动环境下）需要实测。**所有"标准差/漂移"数字只代表模型量级，
   不是硬件指标。**

## 4. 符号约定推导（曾导致量程只有一半的坑，记录以免重犯）

固件用 `pitchFromAccel = atan2(-ax, sqrt(ay²+az²))`，输出角为
`|wrap180(pitch - baseline)|`，是 **1:1** 映射（没有倍数缩放）。要让仿真自洽：

* 传感器模型必须让"开合角速度 `gy` 的符号"与"俯仰读数的变化方向"一致，
  否则 `wrap180` 会先把相对角折到 180 附近，`abs()` 再折回 45，
  表现为**角度卡在半个量程**（这正是开发过程中先出现、后被修正的现象）。
* 最终采用：`theta = -true_pitch`，`ax = -sin(theta)`，`gyro_y = -pitch_rate`。
  三者同号自洽；`--check-firmware` 与 `full_180` 场景分别从常量与端到端行为上
  守住了这条约定。

## 5. 物理量程约束（重要，见 tools/verify_pitch_range.py）

`atan2(-ax, sqrt(ay²+az²))` 的第二参数恒为非负，因此返回值恒在 **[-90, +90]**：

* 传感器俯仰超过 ±90° 时读数折返（`theta` 与 `180-theta` 同读数）；
* 在折返区间的**加速度计纠偏方向是反的**（灵敏度 `dM/dT = -1`），
  互补滤波此时只能靠陀螺积分维持，而"静止锚定"会把基准朝读数方向拽。

因此"合盖 ≈ 0、展开 ≈ 180"这一文档宣称，要求传感器俯仰实际变化 180 度
（即屏幕行程与传感器转角 1:1），且必须由陀螺积分把中间 90 度撑过去。
`full_180` 场景验证了在该条件下输出确实能到 **179.83**（陀螺积分 + 端点校准），
但**这是设计约束，必须实物标定**，见 `tools/README_verify.md`。
