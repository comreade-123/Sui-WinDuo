# WinDuo PC 端（上位机）适配说明

> **最短接入路径（只想接进原项目？看这一节就够）**
>
> 你的项目是 Python + PyQt6 + OpenGL，设备 115200 输出 JSON 行。把下面 10 行粘进你的 `QOpenGLWidget` 里即可：
>
> ```python
> import sys; sys.path.insert(0, r"<本目录 pc 的绝对路径>")
> from serial_reader import SerialReader          # ① 后台线程读串口（懒加载 pyserial）
>
> class MyGLWidget(QOpenGLWidget):                # 你原有的窗口类
>     def __init__(self):
>         super().__init__()
>         self.angle01 = 0.0                                        # ② 渲染线程只读这一个数
>         self.reader = SerialReader(on_sample=self.on_sample)      # ③ 启动串口线程（不要让渲染线程读串口）
>         self.reader.start()
>
>     def on_sample(self, s):                                       # ④ 20Hz 回调（串口线程）
>         self.angle01 = s["angle"] / 180.0                         #    s["angle"] 已是 EMA 平滑值；归一化 0..1
>
>     def paintGL(self):                                            # ⑤ 渲染线程：只读，不阻塞
>         super().paintGL()
>         loc = self.gl.glGetUniformLocation(self.program, "u_hingeAngle")
>         self.gl.glUniform1f(loc, self.angle01)                    # 裸 OpenGL 写法
>         # 或 PyQt6 写法： self.program.setUniformValue("u_hingeAngle", self.angle01)
>         # 想同时驱动模糊与高光： setUniformValue("u_blurStrength", ...) / ("u_glassEdge", ...)
> ```
>
> 三个 uniform 的语义（GLSL 声明写在 `gl_shader_blur.py` 的 `GLASS_EDGE_FRAGMENT_SNIPPET` 里，可直接贴）：
>
> | uniform | 取值 | 语义 | 用途 |
> |---|---|---|---|
> | `u_hingeAngle` | 0..1 = `angle/180.0` | 铰链开合角（几何量） | 顶点位移、转轴朝向、边缘倾斜 |
> | `u_blurStrength` | 0..1 | 玻璃模糊/磨砂强度（采样强度） | 模糊采样半径、mip LOD、混合权重 |
> | `u_glassEdge` | 0..1 | 边缘高光带强度（光照量） | fresnel 高光带宽与亮度 |
>
> **渲染线程安全**：`on_sample` 只做一次 float 赋值（GIL 下原子），串口读取全部在后台线程完成，
> 渲染线程绝不阻塞。窗口尺寸/上下文变化时 `paintGL` 会自然取到最新值。
>
> 想连窗口的玻璃效果也一起用（PyQt6 窗口本身变亚克力）：
> ```python
> from glass_overlay import apply_accent_to_hwnd
> apply_accent_to_hwnd(int(self.winId()), alpha=180)     # 0..255，越大越浓
> ```

---

## 离线运行（无 pyserial / 无网环境）★ 当前推荐

**前提**：机器完全没网（pypi / github 都连不上），pip 装不了任何东西；板子已经在 `COM3`
上以 115200 8N1 输出 JSON 行（行尾 `\n`，`#` 开头的是诊断行）。
下面两条路都**零第三方依赖**，不需要 pyserial / PyQt6 / PyOpenGL。

### 路 1（纯 Python，零依赖，推荐）：ctypes 直调 Windows API 读串口

```bash
python pc/run_offline.py --port COM3              # 读串口 + 打印角度 + 更新玻璃覆盖层 alpha
python pc/run_offline.py --port COM3 --no-glass   # 只打印数值，不建窗口
python pc/run_offline.py --simulate               # 没接板子时的全链路自检（内置虚拟设备）
python pc/run_offline.py --list                   # 列出本机可用串口
```

实现要点（`pc/serial_reader_win.py`，只用标准库 `ctypes` + `winreg`）：

| 步骤 | Win32 API |
|---|---|
| 打开 `\\.\COM3`（独占） | `CreateFileW` |
| 115200 8N1 | `SetCommState` + `DCB`（`DCBlength=28`，`ByteSize=8`，`Parity=NOPARITY`，`StopBits=ONESTOPBIT`） |
| 短超时轮询（ReadFile 最多阻塞 ~100ms） | `SetCommTimeouts` |
| 清历史缓冲 | `PurgeComm` |
| 读取 | `ReadFile`（非重叠，`lpOverlapped=NULL`） |
| 查队列/驱动错误 | `ClearCommError` |
| 关闭 | `CloseHandle` |
| 枚举串口 | `winreg` 读 `HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` |

- 接口与 `pc/serial_reader.py` 的 `SerialReader` 完全一致：`start()/stop()/latest()/stats()/on_sample/on_error`，
  内部同样复用 `winduo_protocol` 的 `LineFramer + parse_line + AngleSmoother`。
- **默认不拉 DTR/RTS**：很多 Arduino（Uno/Nano）的 DTR 自动复位电路会在串口被打开时复位板子，
  纯监视不应该打断正在跑的设备。确实需要时加 `--dtr` / `--rts`。
- 端口被占用（串口监视器 / Arduino IDE 开着）时会立即给出可读提示并退出码 2：

  ```text
  [错误] 串口异常：打开 COM3 失败：串口被占用（串口助手 / Arduino IDE 串口监视器 / 本项目另一个进程已打开），请先关闭占用它的程序（Win32 错误码 5）
  当前可用串口：COM3, COM4, COM5, COM6, COM7
  提示：串口监视器 / Arduino IDE / 串口助手会独占 COM 口，请先关闭它们；也可以先用 --simulate 验证全链路。
  ```

- Ctrl+C 干净退出：停止线程（`~read_timeout_ms` 内返回，不跨线程关句柄）→ 销毁覆盖层 → 打印统计。
- 本机实测（板子 `sensor_error`、`angle` 恒为整数 `0`，I2C 两设备 MISSING 属预期）：

  ```text
  > python pc\run_offline.py --port COM3 --seconds 2 --no-glass
  [串口] 已连接（Ctrl+C 退出）
  [    1] angle=   0.00° (raw=   0.00°) status=sensor_error mode=calibrate <- 传感器未就绪（检查 I2C 接线/供电）
  时长 2.2 s / 有效帧 44（约 19.6 帧/秒）/ 注释行 0 / 非法行 0
  ```

  注意 `angle` 在固件里是**整数 `0`**，`parse_line` 会把它转成 `float 0.0`（自测里有专门断言）。

单独用它看原始数据也行：

```bash
python pc/serial_reader_win.py --list
python pc/serial_reader_win.py --port COM3 --seconds 5
```

### 路 2（纯 PowerShell，零依赖）：官方监视器脚本

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\monitor.ps1 -Port COM3
```

只显示诊断行（`#` 开头）与状态变化，用 `.NET System.IO.Ports.SerialPort`，同样不需要任何安装。
> 提示：Windows PowerShell 5.1 读 `.ps1` 时按 ANSI 解析，脚本文件需要 **UTF-8 带 BOM** 才不出现中文乱码。

### 离线自测（不接硬件）

```bash
python pc/tests/test_offline_reader.py                      # 101 项检查，纯假串口，不碰 COM 口
set WINDUO_TEST_PORT=COM3 && python pc/tests/test_offline_reader.py   # 额外接真实串口测一轮
```

覆盖：分帧 / 半行 / 粘包 / 超长行（>512B）丢弃 / `#` 诊断行静默跳过 / 非法行丢弃 /
**整数 angle 转 float** / 断线重连 / `stop()` 可打断 / 端口被占用的可读提示 / DCB 与错误码。

---

## 0. 两条路径，按你的情况选

| 路径 | 依赖 | 命令 | 用途 |
|---|---|---|---|
| **A. 不含 OpenGL 的独立可跑路径** | **零第三方依赖**（纯 ctypes + 标准库） | `python pc/glass_overlay.py --mock` | 没接硬件、没装 pyserial/PyQt6 也能先看到玻璃效果 |
| **B. 对接原 OpenGL 项目** | pyserial(+PyQt6/PyOpenGL 你已有) | 见上方「最短接入路径」 | 把开合角送进你的着色器 |

路径 A 的证据（本机实测 Win11 build 26200，输出略有截断）：

```text
> python pc\glass_overlay.py --mock --seconds 2
[mock] 玻璃覆盖层已创建 hwnd=5116258 accent=acrylicblurbehind(4) 区域=(0, 510, 1707, 509)
angle=  0.00 -> alpha= 60  status=warming_up  mode=default
angle= 11.86 -> alpha= 71  status=calibrating mode=calibrate
angle=129.12 -> alpha=182  status=ok          mode=default
angle=162.83 -> alpha=214  status=ok          mode=default
[mock] 完成：有效帧 33 / 注释行 2 / 非法行 5，alpha 范围 60..217，用时 2.0s
```

数据来自内置虚拟设备（`simulate_device.py`），不需要接线、不需要 pyserial。
只想看数值不弹窗：`python pc/glass_overlay.py --mock --no-window --seconds 3`。

---

## 1. 依赖安装与降级策略

```bash
# 全部可选；模块导入时都不会 import 这些库，只有真正用到时才懒加载
pip install pyserial          # 真实串口读取（路径 A 不需要）
pip install PyQt6             # 你的原项目已有
pip install PyOpenGL          # 你的原项目已有（裸 glUniform1f 需要）
```

**懒加载 + 优雅降级**（这是硬性设计，任何模块顶层都不 import 第三方库）：

| 缺什么 | 表现 |
|---|---|
| 缺 `pyserial` | `SerialReader.start()` 返回 `False`，`last_error` 是可读中文提示（含 `pip install pyserial`）；`python pc/serial_reader.py --list` 打印错误并退出码 2，不崩栈 |
| 缺 `PyQt6`/`PyOpenGL`/`moderngl` | `AngleUniformAdapter` 后端为 `"none"`：照常计算 `angle/180.0` 但不写 GPU，只打印一条提示 |
| 缺显示/非 Windows | `GlassOverlay.available=False`，`create()` 返回 `False` 并打印原因；`set_angle()` 仍返回映射后的 alpha，便于 UI 显示 |

因此 `python pc/tests/test_pipeline.py` 在**零第三方依赖**的机器上也能跑到 PASS。

---

## 2. 文件与职责

| 文件 | 行数 | 职责 |
|---|---|---|
| `winduo_protocol.py` | 714 | 逐行 JSON 解析（`parse_line`）、字节流分帧（`LineFramer`）、EMA 平滑（`AngleSmoother`/`smooth`）、归一化 |
| `serial_reader.py` | 504 | 后台线程串口读取（pyserial 懒加载），115200 8N1，断线指数退避重连（0.5s→5s 封顶，`stop()` 可立即打断） |
| `serial_reader_win.py` | 958 | **零依赖串口**：ctypes 直调 kernel32（CreateFileW/SetCommState/SetCommTimeouts/ReadFile/CloseHandle），接口同上 |
| `run_offline.py` | 327 | **一条命令跑通离线全链路**：读串口 → 解析 → EMA → 打印 + 玻璃效果 alpha |
| `glass_overlay.py` | 993 | Win32 亚克力/模糊玻璃：ctypes 调 `SetWindowCompositionAttribute`，Win11(4)/Win10(3) 双分支，点击穿透，`--mock` 无硬件演示 |
| `gl_shader_blur.py` | 446 | 角度→uniform 适配（PyQt6 / PyOpenGL / moderngl 三后端 + 降级），GLSL 片段与语义说明 |
| `simulate_device.py` | 471 | 虚拟设备：20Hz 输出固件一致 JSON，含正弦扫描、状态切换、注释行、脏数据注入、文件回放 |
| `tests/test_pipeline.py` | 993 | 全链路自测（244 项检查；另加 `WINDUO_TEST_WINDOW=1` 的真实建窗检查共 249 项），退出码 0 = PASS |
| `tests/test_offline_reader.py` | 549 | 离线零依赖链路自测（101 项检查，假串口；`WINDUO_TEST_PORT=COM3` 再接真机） |
| `tests/sample_stream.jsonl` | 200 行 | `--count 200` 生成的样本流（含 2 行注释、5 行脏数据） |

---

## 3. 协议（与固件一致，勿改）

设备每 50ms 输出一行 115200 8N1 的 JSON，`\n` 结尾：

```json
{"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}
```

- `angle`：**JSON 数字**，`0.0~180.0`，一位小数。
  同时向后兼容早期固件缺陷版本 `{"angle":"45.2"}`（字符串数字）：能 `float()` 且在区间内即接受，不可转换（`"abc"`）或越界（`181`、`-1`）一律丢弃。
- `status ∈ {ok, calibrating, sensor_error, warming_up}`；`mode ∈ {default, calibrate, debug}`。
- 扩展字段：`debug` 模式多出 `gyro/bias/base/sp/lp`，校准中多出 `progress(0-100)`；
  默认忽略（`parse_line` 严格返回 5 个字段），需要时 `parse_line(line, with_extra=True)`。
- **注释行**：固件会输出 `# calibration done. offset=1.234` 这类非 JSON 行，必须**静默跳过**，
  用 `is_comment_line()` 判定，统计上与非法行分开计。

`parse_line()` 的容错（任何输入都不抛异常，非法一律返回 `None`）：
整数/浮点、字符串数字、字段顺序变化、首尾空白、`\r\n`、前后夹杂日志、半行截断、超长行（>512 字节）、
`NaN/Infinity`、`null/true/数组`、非法 UTF-8、空行。

**统计口径分开**（`ParseStats`）：`valid`（有效帧）／`comments`（注释行）／`discarded`（非法行）／`framer_dropped`（分帧超长丢弃）。

### 平滑

`AngleSmoother(alpha=0.25)`：EMA；`|Δ| > 40°` 时快速跟随（`fast_alpha=1.0`，避免机械拖尾）；
可选死区 `deadband` 抑制抖动；非法输入返回上次有效值。模块级 `smooth(angle)` 为同参数共享实例。

### 重连退避

`backoff_delay(n)`：`0.5 / 1 / 2 / 4 / 5 / 5 …`（封顶 5s）；等待用 `Event.wait` 实现，
`stop()` 会立即打断（实测 < 1s 返回）。收到有效数据后退避计数清零。

---

## 4. 常用命令

```bash
# 全链路自测（必须退出码 0，244 项检查）
python pc/tests/test_pipeline.py
# 额外做真实建窗 + SetWindowCompositionAttribute 验证（会闪现玻璃窗口，249 项检查）
WINDUO_TEST_WINDOW=1 python pc/tests/test_pipeline.py     # Windows 下用 $env:WINDUO_TEST_WINDOW="1"

# 离线零依赖自测（不需要 pyserial，假串口）
python pc/tests/test_offline_reader.py
$env:WINDUO_TEST_PORT="COM3"; python pc/tests/test_offline_reader.py   # 再接真机测一轮

# 生成/回放样本流（无需硬件）
python pc/simulate_device.py --count 200 --out pc/tests/sample_stream.jsonl
python pc/simulate_device.py --replay pc/tests/sample_stream.jsonl
python pc/simulate_device.py --count 50 --realtime          # 按 20Hz 打到标准输出

# 玻璃效果（路径 A）
python pc/glass_overlay.py --mock                           # 建窗演示 6 秒
python pc/glass_overlay.py --mock --no-window --seconds 3   # 只看数值
python pc/glass_overlay.py --selftest                        # 结构体/映射自检，不建窗

# 真实串口（路径 B 的调试用）
python pc/serial_reader.py --list
python pc/serial_reader.py --port COM7 --seconds 5

# 着色器 uniform 自检
python pc/gl_shader_blur.py
```

---

## 5. 玻璃效果实现要点（为什么这么写）

- `SetWindowCompositionAttribute` 是未公开 API，结构体字段顺序即 ABI：
  `ACCENT_POLICY{AccentState, AccentFlags, GradientColor, AnimationId}`（16 字节）、
  `WINCOMPATTRDATA{Attribute, pvData, cbData}`（x64 24 字节），`Attribute = 19 (WCA_ACCENT_POLICY)`。
  顺序写错不会报错，只会"没效果"。
- `AccentState`：`4 = ACCENT_ENABLE_ACRYLICBLURBEHIND`（Win11，build ≥ 22000 优先）、
  `3 = ACCENT_ENABLE_BLURBEHIND`（Win10 回退，Win10 上 acrylic 拖窗会卡顿）。
- `GradientColor = 0xAABBGGRR`（**ABGR**，注意字节序）：**alpha 决定玻璃浓淡** ——
  `0x00` 几乎全透明（背景完全透出、几乎看不到磨砂），`0xFF` 完全被 tint 覆盖（看不到模糊）。
  因此"模糊强度"就映射成 alpha：`0° -> alpha_min(60)`，`180° -> alpha_max(230)`，线性可配
  （`angle_to_alpha()`）。想更浓/更淡直接改这两个参数。
- 窗口：`WS_EX_LAYERED`（必选）+ `WS_EX_TRANSPARENT`（点击穿透，默认开）
  + `WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW`；窗口过程不擦背景（`WM_ERASEBKGND` 返回 1），
  玻璃观感完全交给 DWM。
- 默认区域 = 工作区下半屏（已排除任务栏），`default_bottom_half_rect()` 参数化可改。

### 踩坑记录（复现即用）

1. **ctypes 必须显式声明 argtypes/restype**。不声明时 64 位指针型参数按 `c_int` 传，
   `DefWindowProcW` 会在 `WM_NCCREATE` 上抛 `ArgumentError` 被吞掉 → 返回 0 →
   `CreateWindowExW` 返回 NULL 且 `GetLastError()==0`（完全看不出原因）。见 `_setup_prototypes()`。
2. `HWND_TOPMOST = -1` 不能直接塞 `c_void_p`，需按机器字长取模，见 `_hwnd_ptr()`。
3. `LineFramer` 判定超长行后必须进入"丢弃态"直到下一个 `\n`，
   否则超长行的**尾巴**会被当成新行吐出去，导致分块大小不同统计结果不同（自测里有回归项）。

---

## 6. 自测覆盖（`tests/test_pipeline.py`，244 项检查 / 实测 1.25s）

1. `parse_line` 容错：标准行、字段乱序、整数/浮点/字符串数字、前后日志、`\r\n`、
   注释行、越界、`NaN/Infinity`、空/非 JSON、非法 UTF-8、超长行、模糊输入 0 异常。
2. `angle` 契约：number 主路径 + 字符串数字兼容 + `"abc"`/`181`/`true`/缺字段必须丢弃。
3. 注释行：`is_comment_line` 判定 + 统计口径分开（有效帧/注释行/非法行三者互斥）。
4. `LineFramer`：半行、粘包、超长行、逐字节喂入、**超长行丢弃与分块大小无关**（回归项）。
5. `AngleSmoother`：EMA 数值、突跳快速跟随、死区、非法输入、与模块级 `smooth()` 一致。
6. 全链路 200 行：有效帧 100% 解析 / 注释行 100% 跳过 / 脏数据 100% 丢弃；
   平滑后最大与平均帧间跳变 ≤ 平滑前；uniform 值域 ⊆ [0,1] 且覆盖 ≥ 90%。
7. 样本文件回放：chunk = 1 / 7 / 37 / 4096 结果完全一致，无丢失、无崩溃。
8. `SerialReader`：回调、`latest()`、`stop()` 可打断、断线重连（`reconnects≥1`）、
   无 pyserial 时返回 `False` 且错误可读。
9. 玻璃：结构体偏移/大小、ABGR 打包、浓淡映射、Win10/Win11 分支、`selftest`。
10. 着色器：三个 uniform 映射、GLSL 片段、后端探测、注入 FakeGL 验证 `glUniform1f`、
    注入 FakeQtProgram 验证 `setUniformValue`、无依赖时降级不报错。

---

## 7. 与原项目集成点清单

- 串口线程：`SerialReader(on_sample=..., on_error=...)`，`start()/stop()/latest()/stats()`。
- 平滑：`SerialReader` 内部已做 EMA（`s["angle"]`），原始值在 `s["angle_raw"]`。
- 归一化：`angle/180.0`，或 `winduo_protocol.normalize_angle()` / `gl_shader_blur.angle_to_uniform()`。
- 着色器：`AngleUniformAdapter(program=your_program)`，回调里 `update_angle_uniform(angle)`；
  或直接 `glUniform1f(loc, angle/180.0)` / `program.setUniformValue("u_hingeAngle", v)`。
- 窗口玻璃：`apply_accent_to_hwnd(int(self.winId()), alpha)`，或 `GlassOverlay.attach(winId)` 后 `set_angle()`。
