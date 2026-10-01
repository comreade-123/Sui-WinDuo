# WindowsDuo-EthanMaven

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Platform: Arduino IDE](https://img.shields.io/badge/Platform-Arduino%20IDE-blue.svg)
![Board: ESP32-WROOM-32E](https://img.shields.io/badge/Board-ESP32--WROOM--32E-orange.svg)
![Status: awaiting hardware validation](https://img.shields.io/badge/Status-awaiting%20hardware%20validation-yellow.svg)

> 用一块 ESP32 + MPU6050 读取笔记本屏幕的开合角度，通过 115200 串口把角度以 JSON 行发给 PC 端，
> 驱动窗口的玻璃模糊 / 悬浮视觉效果；板载 SSD1306 OLED 与按键提供完全离线的本地 UI 与交互。

**本仓库是开源项目 [WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo) 的二次创作（二创）版本，
采用纯 Arduino IDE 工程结构。** 原项目作者与链接见 [第 2 节](#2-二创说明与致敬) 与 [第 11 节](#11-作者许可与致谢)。

---

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 二创说明与致敬](#2-二创说明与致敬)
- [3. 硬件清单与接线](#3-硬件清单与接线)
- [4. Arduino IDE 环境准备](#4-arduino-ide-环境准备)
- [5. 烧录步骤](#5-烧录步骤)
- [6. PC 端运行步骤](#6-pc-端运行步骤)
- [7. 串口协议](#7-串口协议)
- [8. 目录结构](#8-目录结构)
- [9. 常见问题（FAQ）](#9-常见问题faq)
- [10. 待实物验证](#10-待实物验证)
- [11. 作者、许可与致谢](#11-作者许可与致谢)

---

## 1. 项目简介

WindowsDuo-EthanMaven 是一个「笔记本屏幕开合角 → PC 端视觉反馈」的小型硬件 + 软件项目：

- **采集**：ESP32 通过 I2C 读取 MPU6050 的加速度与角速度，用互补滤波（陀螺仪积分 + 加速度计倾角修正）解算出开合角。
- **本地呈现**：SSD1306 128×64 OLED 实时显示角度大字、进度条、系统状态与当前模式；按键可切换模式、触发重新校准。
- **上报**：串口每 50ms 输出一行 JSON（约 20Hz），字段固定为 `angle / status / mode / author`。
- **PC 端**：Python 读取串口 → 解析协议 → 平滑角度 → 用 OpenGL 叠加层绘制玻璃模糊 / 悬浮效果。

数据流：

```
+-----------+   I2C    +--------------+   串口 115200 8N1   +----------------+   OpenGL   +------------------+
|  MPU6050  | -------> |  ESP32 固件  | ------------------> |  PC 端 Python  | ---------> |  玻璃模糊 / 悬浮 |
|  0x68     |          | 互补滤波解算 |   每 50ms 一行 JSON |  解析 + 平滑   |            |  叠加层          |
+-----------+          +--------------+                     +----------------+            +------------------+
                              |
                              +--> SSD1306 128x64 OLED（角度 / 进度条 / 状态 / 模式，200ms 刷新）
                              |
                              +--> 按键 GPIO5（短按切换模式，长按重新零漂校准）
```

固件设计要点（与 `firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino` 一致）：

- 全代码**不使用 `delay()`**，串口发送、OLED 刷新、按键扫描、IMU 采样、按键消抖全部基于 `millis()` 非阻塞调度。
- 开机静止约 **3 秒**自动完成零漂校准；运行中在静止状态下缓慢跟踪残余零偏，抑制长时间漂移。
- 输入输出全部经过限幅：`angle` 恒在 0.0 ~ 180.0 之间，保留一位小数。
- JSON 使用 ArduinoJson 7.x 序列化，不手工拼接浮点字符串。

---

## 2. 二创说明与致敬

本项目**基于开源项目 WindowsDuo 二次创作**：

- 原项目：**WindowsDuo**
- 原项目作者：**KaedeharaKazuha1029**
- 原项目地址：<https://github.com/KaedeharaKazuha1029/WindowsDuo>

原项目的具体实现细节（工程组织、内部文件与函数命名、PC 端方案等）请**以原仓库为准**；本仓库不复制、不转述其内部实现，
仅在其「用硬件传感器驱动 Windows 桌面视觉效果」的思路之上，重写了一套 Arduino IDE 版本。

与原版的主要差异：

| 维度 | 原版 WindowsDuo | 本版 WindowsDuo-EthanMaven |
| --- | --- | --- |
| 固件工程 | ESP-IDF 工程（具体实现以原仓库为准） | 纯 Arduino IDE 工程，唯一草图 `firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino`，无需额外构建工具链 |
| 本地显示 | 新版新增（原版方案以原仓库为准） | 新增 SSD1306 128×64 OLED 本地 UI：角度大字、进度条、状态行、模式行、校准进度界面 |
| 交互 | 新版新增（原版方案以原仓库为准） | 新增按键交互：短按（50~800ms）循环切换模式，长按（≥800ms）重新零漂校准 |
| 数据协议 | 新版定义（原版方案以原仓库为准） | 新增每 50ms 一行 JSON 文本协议（115200 8N1），字段 `angle / status / mode / author` |
| JSON | 新版新增 | 使用 **ArduinoJson 7.x** 的 `JsonDocument` 序列化输出 |
| PC 端 | 以原仓库为准 | 本仓库提供 Python 端实现：串口读取、协议解析、角度平滑、OpenGL 玻璃模糊叠加层，见 [pc/README_pc.md](pc/README_pc.md) |

> 致谢：感谢 KaedeharaKazuha1029 及 WindowsDuo 项目提供的原始创意与开源实现。
> 原项目所采用的开源许可请以其仓库页面为准；**本仓库自身以 MIT 许可发布**（见 [LICENSE](LICENSE)）。

---

## 3. 硬件清单与接线

### 3.1 物料清单

| 序号 | 物料 | 型号 / 规格 | 数量 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 主控开发板 | ESP32-WROOM-32E（Maker-ESP32） | 1 | 本项目使用纯 Arduino IDE 工程 |
| 2 | 六轴姿态传感器 | MPU6050 模块（如 GY-521） | 1 | I2C 地址 0x68（AD0 接 GND） |
| 3 | OLED 显示屏 | SSD1306 128×64，I2C 接口 | 1 | I2C 地址 0x3C（模块默认） |
| 4 | 按键 | 轻触按键模块或独立轻触开关 | 1 | 一端接 GPIO5，另一端接 GND |
| 5 | 杜邦线 | 母-母 / 母-公 若干 | 若干 | 建议按颜色区分 3V3 / GND / SDA / SCL |
| 6 | USB 数据线 | Micro-USB 或 Type-C（按开发板接口） | 1 | 必须是数据线，纯充电线无法烧录 |

### 3.2 接线表

MPU6050 与 SSD1306 **共用同一条 I2C 总线**（SDA=GPIO23、SCL=GPIO22），两个模块的 VCC 接 3V3、GND 与开发板共地。

| 模块 | 模块引脚 | 接到 ESP32 | 说明 |
| --- | --- | --- | --- |
| MPU6050 | VCC | 3V3 | 3.3V 供电 |
| MPU6050 | GND | GND | 与开发板共地 |
| MPU6050 | SDA | GPIO23 | 与 SSD1306 共用 I2C 数据线 |
| MPU6050 | SCL | GPIO22 | 与 SSD1306 共用 I2C 时钟线 |
| MPU6050 | AD0 | GND | 拉低后 7 位地址为 0x68 |
| SSD1306 128×64 | VCC | 3V3 | 3.3V 供电 |
| SSD1306 128×64 | GND | GND | 与开发板共地 |
| SSD1306 128×64 | SDA | GPIO23 | 与 MPU6050 共用 I2C 数据线 |
| SSD1306 128×64 | SCL | GPIO22 | 与 MPU6050 共用 I2C 时钟线 |
| 按键 | 一端 | GPIO5 | 固件使用 `INPUT_PULLUP`，按下读到低电平 |
| 按键 | 另一端 | GND | 按下时把 GPIO5 拉低 |

补充说明：

- **I2C 地址**：MPU6050 的 AD0 接地时为 `0x68`；SSD1306 模块默认 `0x3C`（少数模块为 `0x3D`，可改固件里的 `SSD1306_ADDR`）。
- **上拉电阻**：MPU6050 与 SSD1306 模块通常各自板载 I2C 上拉电阻，一般可直接并联到同一总线；若通信不稳定，再检查总线上拉是否过强或过弱。
- **固件中的 I2C 时钟**为 400kHz（快速模式）。
- 接线前请断开 USB 供电，避免带电插拔造成模块损坏。

### 3.3 连线示意（ASCII）

```
                    ESP32-WROOM-32E (Maker-ESP32)
                    3V3    GND    GPIO23   GPIO22   GPIO5
                     |      |       |        |        |
                     |      |       |        |        +--> 按键一端
                     |      |       |        |               （按键另一端 --> GND）
                     |      |       |        |
                     |      |       |        +-------------> SSD1306 SCL
                     |      |       |        +-------------> MPU6050 SCL
                     |      |       |
                     |      |       +----------------------> SSD1306 SDA
                     |      |       +----------------------> MPU6050 SDA
                     |      |
                     |      +------------------------------> SSD1306 GND
                     |      +------------------------------> MPU6050 GND
                     |      +------------------------------> 按键另一端
                     |
                     +-------------------------------------> SSD1306 VCC (3V3)
                     +-------------------------------------> MPU6050 VCC (3V3)

    I2C 拓扑（一条总线，两个从机）：

        ESP32（主机，SDA=GPIO23 / SCL=GPIO22 / 400kHz）
           |
           +-----------------------------+-----------------------------+
           |                                                           |
     [ MPU6050  地址 0x68 ]                                  [ SSD1306  地址 0x3C ]
```

### 3.4 安装朝向

MPU6050 在笔记本上的贴装方向决定了角度符号与零点的物理含义。固件里预留了安装朝向矩阵
`MPU_MOUNT_ROTATION_MATRIX`（3×3，行优先，默认单位矩阵），如果角度方向与预期相反，可只改这一处宏，
常见朝向的取值已写在固件注释里。实际手感与方向需要在实物上确认，见 [第 10 节](#10-待实物验证)。

### 3.5 机械安装前提

固件的开合角算法是 `angle = |wrap180(pitch - baseline)|`，即**角度映射为 1:1、不做任何缩放**：
屏幕相对底座的夹角变化 ≈ MPU6050 的俯仰（pitch）变化。
**0~180 满量程能走满的前提是「屏幕行程 : 传感器转角 ≈ 1:1」（k ≈ 1）。**

- **k ≈ 1**：合盖到全开的变化可以完整映射到 0~180。
- **k < 1**：端点走不满。例如 k = 0.5 时，角度顶多读到约 **90~95°**，其后要靠陀螺仪积分继续增长，不再是加速度计直接测量。
- **90° 附近是加速度计的奇异点**：加速度计倾角在传感器自身 **±90°** 处存在折返边界（`atan2` 的天顶奇异）。
  独立验证以 **5° 步长扫描**得到的数值结果：折返边界出现在 **95°**（理论值 **90°**）；
  纠偏灵敏度 dM/dT 在 **0° / 30° / 60° / 85°** 为 **+1.000**，在 **90°** 为 **0.000**，在 **95° / 120° / 150° / 180°** 为 **-1.000**。
  也就是说：**90° 之后靠陀螺仪积分撑过去**，静止时由静止锚定（零偏跟踪）把结果拉回真实值——这属于正常现象，不是故障。
  在 1:1 假设下，跨 180° 行程的静态可达输出为 **180.00°**（仿真可达 **179.83°**）。

**如果实测比例不是 1**，两种处理方式：

1. **PC 端标定**：把收到的 `angle` 乘以 `1/k` 再驱动效果（例如实测 k = 0.5 就乘 2）。
2. **更换安装朝向 / 测量轴**：按固件顶部的 `MPU_MOUNT_ROTATION_MATRIX` 宏调整（见 [3.4 安装朝向](#34-安装朝向)），
   让实际测量轴真正对应屏幕转轴。

> 上述 95° 折返边界与灵敏度数值来自对倾角解算的**数值扫描验证**（5° 步长），**不是实物测试结果**；
> 实机测量方法见 [docs/hardware_checklist.md](docs/hardware_checklist.md) 第 6 节。

---

## 4. Arduino IDE 环境准备

### 4.1 安装 Arduino IDE

从 <https://www.arduino.cc/en/software> 下载并安装 Arduino IDE（推荐 2.x 版本）。

### 4.2 添加 ESP32 开发板支持

1. 打开 **文件 → 首选项**（Windows 快捷键 `Ctrl + ,`）。
2. 在 **附加开发板管理器网址** 中粘贴下面这一行（已有其他网址时用英文逗号分隔）：

   ```text
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```

3. 打开 **工具 → 开发板 → 开发板管理器**，搜索 `esp32`，安装 **esp32 by Espressif Systems**。
   版本建议选列表中的最新稳定版（本项目开发时使用 Arduino-ESP32 3.x，**具体版本以开发板管理器显示的最新稳定版为准**）。
4. 安装完成后在 **工具 → 开发板** 中选择：
   - 首选：**Maker-ESP32**
   - 备选：**ESP32 Dev Module**（同为 ESP32-WROOM-32E 模组时可用）

### 4.3 安装依赖库

打开 **工具 → 管理库**（Library Manager），按下列搜索名逐个安装：

| 库（库管理器搜索名） | 推荐版本 | 用途 |
| --- | --- | --- |
| Adafruit SSD1306 | >= 2.5.9 | SSD1306 OLED 驱动 |
| Adafruit GFX Library | >= 1.11.9 | 图形与字体基础库（SSD1306 依赖） |
| Adafruit MPU6050 | >= 2.2.6 | MPU6050 驱动 |
| Adafruit Unified Sensor | >= 1.1.14 | 传感器抽象层（Adafruit MPU6050 依赖） |
| ArduinoJson | >= 7.0.0 | JSON 序列化输出 |

说明：

- 安装 **Adafruit MPU6050** 时，库管理器会提示一并安装依赖 **Adafruit BusIO** 与 **Adafruit Unified Sensor**，选择「全部安装」即可。
- 上表版本为本项目验证过的**最低建议版本**；库管理器中的版本号会持续更新，**以库管理器显示的最新版为准**。
- 固件使用的库内 API 均为 Adafruit 官方长期维护的公开接口，未使用任何私有或未公开函数。

### 4.4 端口驱动

如果 **工具 → 端口** 中看不到开发板，多半是 USB 转串口驱动未安装：常见芯片为 **CP210x**（Silicon Labs）或 **CH340/CH341**（沁恒），
请按开发板实际芯片安装对应驱动后重新插拔 USB。

---

## 5. 烧录步骤

1. 用 **USB 数据线**把开发板接到电脑。
2. 在 Arduino IDE 中打开固件草图：

   ```text
   firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino
   ```

   （Arduino IDE 要求 `.ino` 文件名与所在文件夹同名，本仓库的目录结构已满足该要求。）
3. **工具 → 开发板** 选 `Maker-ESP32`（或 `ESP32 Dev Module`）。
4. **工具 → 端口** 选择开发板对应的 `COM` 口。
5. 点击工具栏的 **上传（→）** 按钮。若一直停在 `Connecting...`，可在出现该提示时按住板上的 **BOOT** 键，个别板子需要手动进入下载模式。
6. 上传完成后打开 **工具 → 串口监视器**，把波特率设为 **115200**，即可看到每行一条 JSON：

   ```json
   {"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}
   ```

   串口监视器除了查看输出，也可以直接向设备发送 7.3 节里的行命令（例如 `status`、`mode=debug`）；
   发送时把行尾符设为「NL 和 CR」或「换行符」即可被固件正确解析。

7. 同一块板子被 Arduino 串口监视器占用时，PC 端 Python 脚本无法再打开该串口；两者只能二选一，切换前先关闭串口监视器。

---

## 6. PC 端运行步骤

PC 端代码位于 `pc/` 目录，负责串口读取、协议解析、角度平滑与玻璃效果绘制。**参数与命令行选项以 [pc/README_pc.md](pc/README_pc.md) 为准。**

### 6.1 文件职责

| 文件 | 职责 |
| --- | --- |
| [pc/winduo_protocol.py](pc/winduo_protocol.py) | 串口 JSON 行协议解析 |
| [pc/serial_reader.py](pc/serial_reader.py) | 串口读取与角度输出入口 |
| [pc/glass_overlay.py](pc/glass_overlay.py) | 玻璃模糊 / 悬浮效果叠加层 |
| [pc/gl_shader_blur.py](pc/gl_shader_blur.py) | OpenGL 模糊着色器相关实现 |
| [pc/simulate_device.py](pc/simulate_device.py) | 无实物时产生模拟串口数据 |
| [pc/README_pc.md](pc/README_pc.md) | PC 端详细文档 |
| [pc/tests/test_pipeline.py](pc/tests/test_pipeline.py) | PC 端流水线测试 |

> 上表为文件职责概要，**具体实现与调用方式以对应源码和 [pc/README_pc.md](pc/README_pc.md) 为准**。

### 6.2 安装依赖

在**仓库根目录**打开 PowerShell：

```powershell
cd D:\Deepseek-API-project\DSH\WinDuo-EthanMaven   # 换成你自己的仓库路径

# 建议使用虚拟环境（可选；.venv/ 已被 .gitignore 忽略）
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install pyserial PyQt6 PyOpenGL
```

### 6.3 运行

```powershell
# 串口读取（COM 口与参数以 pc/README_pc.md 及脚本自带的帮助输出为准）
python pc\serial_reader.py
```

**没有实物硬件时**，可以先用 `pc/simulate_device.py` 产生模拟数据流来联调 PC 端效果，用法见 [pc/README_pc.md](pc/README_pc.md)；
`tools/` 目录下另有演示与固件静态检查脚本，说明见 [tools/README_verify.md](tools/README_verify.md)。

### 6.4 Windows 10 / Windows 11 的玻璃效果差异

| 系统 | 情况 | 差异与降级路径 |
| --- | --- | --- |
| Windows 11 | 系统原生提供窗口背景材质与圆角等现代外观能力 | 可与系统原生观感搭配，效果更接近「真玻璃」 |
| Windows 10 | 缺少 Windows 11 那套公开的系统背景材质接口 | 以本项目的自绘叠加层 + 着色器模糊为主，观感与性能取决于 GPU 与桌面合成设置 |

因此：**Win11 上优先走系统原生材质路径，Win10 上回退到 OpenGL 自绘叠加层**。具体的开关、参数与降级策略
（以及关闭透明效果 / 性能不足时的处理）请以 [pc/README_pc.md](pc/README_pc.md) 与 `pc/` 下源码为准。
若效果异常，先确认系统「透明效果」已开启、显卡驱动为最新版。

---

## 7. 串口协议

- **物理层**：USB 串口，**115200 8N1**（8 位数据位、无校验、1 位停止位）。
- **方向**：设备 → 主机为**每 50ms 一行 JSON**（约 20Hz），每行以 `\n` 结束；
  主机 → 设备为可选的行命令（见 7.3）。
- **编码**：UTF-8 / ASCII 文本，逐行解析（`readline` 风格）。

### 7.1 字段表

| 字段 | 类型 | 取值 | 含义 |
| --- | --- | --- | --- |
| angle | number | 0.0 ~ 180.0，保留一位小数 | 屏幕开合角（度） |
| status | string | ok / calibrating / sensor_error / warming_up | 固件运行状态 |
| mode | string | default / calibrate / debug | 当前工作模式 |
| author | string | EthanMaven | 作者标识（固定值） |

真实示例行：

```json
{"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}
```

`status` 取值含义：

| status | 含义 |
| --- | --- |
| warming_up | 开机预热，等待传感器稳定 |
| calibrating | 正在执行零漂校准（此时不应移动设备） |
| ok | 正常工作 |
| sensor_error | 传感器或 OLED 初始化 / 通信异常 |

`mode` 取值含义：

| mode | 含义 |
| --- | --- |
| default | 默认模式：正常输出 JSON 并刷新 OLED |
| calibrate | 校准模式：OLED 显示校准进度 |
| debug | 调试模式：JSON 中额外附带调试字段 |

### 7.2 模式相关的附加字段

在 `debug` 模式或校准过程中，同一行 JSON 会**额外**携带下列字段（核心四字段始终存在）：

| 附加字段 | 类型 | 出现条件 | 含义 |
| --- | --- | --- | --- |
| gyro | number | mode=debug | 当前开合方向的角速度（度/秒，两位小数） |
| bias | number | mode=debug | 当前 Y 轴零偏估计（度/秒，三位小数） |
| base | number | mode=debug | 开机基准俯仰角（度，两位小数） |
| sp | number | mode=debug | 累计短按次数 |
| lp | number | mode=debug | 累计长按次数 |
| progress | number | 校准进行中 | 校准进度百分比（0~100） |

### 7.3 主机 → 设备命令（可选）

固件同时支持从串口接收简单的行命令（大小写不敏感，以换行结束）：

| 命令 | 作用 | 说明 |
| --- | --- | --- |
| cal 或 calibrate | 立即开始一次零漂校准 | 执行期间需保持设备静止 |
| status | 立即输出一行 JSON | 不用等到下一个 50ms 周期 |
| mode=default | 切换到默认模式 | 也可写作 mode=calibrate、mode=debug |

### 7.4 以 `#` 开头的注释行

除 JSON 之外，固件还会在串口输出以 `#` 开头的人类可读提示行，例如开机校准开始 / 结束、模式切换提示。
**PC 端解析时请忽略以 `#` 开头的行**，只处理以 `{` 开头的 JSON 行。示例（格式示意）：

```text
# calibration started: boot
# calibration done. gyro_bias(dps)=0.123,-0.045,0.010 baseline_pitch=-1.234
{"angle":0.0,"status":"ok","mode":"default","author":"EthanMaven"}
# mode switched to debug
```

### 7.5 按键与开机行为

| 行为 | 触发条件 | 效果 |
| --- | --- | --- |
| 开机自动校准 | 上电后 | 静止约 3 秒完成零漂校准，期间 `status` 为 `calibrating` |
| 短按 | 按下 50 ~ 800ms | 循环切换模式：default → calibrate → debug → default |
| 长按 | 按下 ≥ 800ms | 立即触发一次重新零漂校准，并进入 calibrate 模式 |

按键带 30ms 软件消抖，全部在主循环里非阻塞扫描。

---

## 8. 目录结构

```text
WindowsDuo-EthanMaven/
├─ firmware/
│  └─ WindowsDuo_EthanMaven/
│     └─ WindowsDuo_EthanMaven.ino     # 纯 Arduino IDE 固件（唯一草图文件）
├─ pc/
│  ├─ winduo_protocol.py               # 串口 JSON 行协议解析
│  ├─ serial_reader.py                 # 串口读取与角度输出入口
│  ├─ glass_overlay.py                 # 玻璃模糊 / 悬浮效果叠加层
│  ├─ gl_shader_blur.py                # OpenGL 模糊着色器相关实现
│  ├─ simulate_device.py               # 无实物时的模拟串口数据源
│  ├─ README_pc.md                     # PC 端详细文档
│  └─ tests/
│     └─ test_pipeline.py              # PC 端流水线测试
├─ tools/
│  ├─ verify_firmware.ps1              # 固件静态检查脚本
│  ├─ verify_firmware_compile.ps1      # 离线编译预检脚本（可选开发工具）
│  ├─ verify_stubs/*.h                 # 预检用最小桩头文件（Adafruit_Sensor/MPU6050/GFX/SSD1306/ArduinoJson）
│  ├─ demo_device.py                   # 演示用设备数据脚本
│  └─ README_verify.md                 # 校验脚本使用说明
├─ verification/
│  └─ VERIFICATION_REPORT.md           # 本仓库验证记录（静态检查 / 仿真）
├─ docs/
│  ├─ git_guide.md                     # Git 与 GitHub 操作指南
│  └─ hardware_checklist.md            # 实物上电验证清单
├─ README.md
├─ LICENSE
├─ CONTRIBUTING.md
└─ .gitignore
```

> 注释为文件职责概要，**具体实现以对应源码为准**。
> `verification/VERIFICATION_REPORT.md` 记录的是静态检查与仿真验证结果，**不代表已完成实物测试**（见 [第 10 节](#10-待实物验证)）。

> **关于 `tools/verify_stubs/` 与 `tools/verify_firmware_compile.ps1`**：这是**可选开发工具**，用于在没有网络、也没有安装 Arduino 库的环境下，
> 用真实的 `xtensa-esp-elf-g++` 配合最小桩头文件对固件做**语法级预检**（已用 esp32 核心 3.3.7 的 xtensa 编译器跑通，无 error）。
> 它**不参与烧录，也不替代 Arduino IDE 的真实编译**——正式编译与上传请始终以 Arduino IDE 的结果为准。

---

## 9. 常见问题（FAQ）

### 9.1 OLED 完全不亮

1. 确认模块是 **I2C 接口**的 SSD1306 128×64（有些模块是 SPI 版本，引脚不同）。
2. 确认地址是 **0x3C**：少数模块为 `0x3D`，需要改固件中的 `SSD1306_ADDR` 常量。
3. 检查 **SDA 是否在 GPIO23、SCL 是否在 GPIO22**（两者接反是最常见原因），以及 VCC 是否接 3V3、GND 是否共地。
4. 用 I2C 扫描程序确认总线上能看到 `0x3C` 与 `0x68` 两个地址；只有其中一个地址出现，说明另一个模块的接线或供电有问题。
5. 若固件把 `oledReady` 判为失败，串口会持续输出 `sensor_error`，可据此判断是初始化失败而非显示问题。

### 9.2 角度缓慢漂移或静止时数值乱跑

- 上电后请让设备**保持静止约 3 秒**，等待开机自动校准完成（`status` 从 `calibrating` 变为 `ok`）。
- 已开机运行很久后感觉零点偏了：**长按按键 ≥ 800ms** 触发一次重新零漂校准，此时必须保持静止。
- 校准期间若设备被移动，固件会**拒绝本次校准结果并保留旧零偏**，串口会打印 `# calibration rejected: device was moving, keep previous bias`——静止后重新校准即可。
- 校准有 **8000ms 超时保护**：若设备持续抖动，最多约 8 秒后结束本次校准并**沿用旧零偏**（属保护行为，不是故障）。
- 强震动、风扇气流、桌面晃动都会影响加速度计，尽量放在稳定的桌面上。

### 9.3 串口被占用 / 打不开 COM 口

- Arduino **串口监视器**打开时，PC 端脚本无法再打开同一串口；反之亦然。切换前先关闭串口监视器。
- 其他工具（如某些串口助手、烧录工具）也可能占用，可在任务管理器里关掉可疑进程，或直接拔插 USB。
- 若报「拒绝访问」，先确认没有第二个程序在读同一 COM 口。

### 9.4 按键没有反应

1. 检查按键是否一端接 **GPIO5**、另一端接 **GND**（固件用内部上拉，按下时为低电平，不需要外接上拉电阻）。
2. 按下时间过短（< 50ms）会被忽略；保持 50~800ms 才会被识别为短按。
3. 若按键接的是 3V3，逻辑正好相反，必须改到 GND。
4. 可在 `debug` 模式下观察 `sp` / `lp` 计数是否增长，从而判断按键是否被识别。

### 9.5 上传失败 / 找不到端口

- 先确认 USB 线是**数据线**而不是纯充电线。
- 安装 **CP210x / CH340** 驱动后重新插拔（见 4.4）。
- 一直停在 `Connecting...` 时按住 **BOOT** 键再试。
- 关闭占用串口的软件后重试。

### 9.6 编译报错找不到库

按 [4.3](#43-安装依赖库) 的搜索名逐个安装库；`Adafruit_Sensor.h` 找不到时说明 **Adafruit Unified Sensor** 未安装，
`ArduinoJson.h` 找不到时说明 **ArduinoJson** 未安装或装成了 6.x 版本。

### 9.7 串口监视器里是乱码

波特率必须设为 **115200**。另外请确认打开的端口是开发板对应的 COM 口，而不是别的设备。

### 9.8 角度方向与预期相反

修改固件中的安装朝向矩阵 `MPU_MOUNT_ROTATION_MATRIX`（见 [3.4](#34-安装朝向)），固件注释里给出了几种常见朝向的取值。
若角度**变化幅度**与屏幕实际夹角不成 1:1（感觉端点走不满 180），见 [3.5 机械安装前提](#35-机械安装前提)。

---

## 10. 待实物验证

> **重要声明**：本仓库当前提供的是代码与文档，**尚未完成实物上电测试**，因此本文档中不包含任何实测数据或实测结论。
> 已完成的仅为**静态检查与仿真**（包括用真实 `xtensa-esp-elf-g++` 配合桩头文件做的语法级预检），它**不能替代硬件行为验证**。
> 上电后的逐条核对请使用 [docs/hardware_checklist.md](docs/hardware_checklist.md)。

需要在实物上确认的内容：

- [ ] I2C 总线上能同时扫描到 `0x3C`（SSD1306）与 `0x68`（MPU6050）。
- [ ] 开机后 OLED 正常点亮并显示标题 / 主界面，无花屏、无重影。
- [ ] 开机静止约 3 秒完成自动校准（`status` 由 `calibrating` 变为 `ok`）。
- [ ] 校准超时保护：持续抖动时约 8 秒后结束本次校准并沿用旧零偏，串口打印 `# calibration rejected: ...`。
- [ ] 串口实际输出速率约为 20 行/秒（每 50ms 一行），字段与取值符合 [第 7 节](#7-串口协议)。
- [ ] `angle` 随屏幕开合在 0 ~ 180 之间**单调变化**，方向与安装方式一致，合盖 / 展开的端点符合预期。
- [ ] 短按按键能依次切换 `default → calibrate → debug → default`。
- [ ] 长按 ≥ 800ms 能触发重新零漂校准。
- [ ] 长时间静止后角度漂移量在可接受范围内。
- [ ] OLED 刷新观感与主循环响应（无 `delay()`，理论不应有卡顿）。
- [ ] PC 端在 Windows 10 与 Windows 11 上的玻璃 / 悬浮效果表现与帧率。
- [ ] PC 端对 `#` 注释行与 JSON 行的混合流解析正确。

在完成上述核对之前，请勿把任何条目当作已验证结论引用。

---

## 11. 作者、许可与致谢

- **作者**：EthanMaven
- **GitHub**：<https://github.com/comreade-123>
- **许可**：本项目以 **MIT License** 发布，详见 [LICENSE](LICENSE)（版权行：`Copyright (c) 2026 EthanMaven`）。
- **致谢**：基于开源项目 **WindowsDuo**（作者 KaedeharaKazuha1029，<https://github.com/KaedeharaKazuha1029/WindowsDuo>）二次创作，
  感谢原作者的开源分享。

其他文档：

| 文档 | 内容 |
| --- | --- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [docs/git_guide.md](docs/git_guide.md) | Git 与 GitHub 操作指南（PowerShell） |
| [docs/hardware_checklist.md](docs/hardware_checklist.md) | 实物上电验证清单 |
| [pc/README_pc.md](pc/README_pc.md) | PC 端详细说明 |
| [tools/README_verify.md](tools/README_verify.md) | 校验脚本使用说明 |
| [verification/VERIFICATION_REPORT.md](verification/VERIFICATION_REPORT.md) | 本仓库验证记录 |

---

如果这个项目对你有帮助，欢迎 Star；也请一并支持原作 [WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo)。
