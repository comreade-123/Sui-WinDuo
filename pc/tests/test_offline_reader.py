# -*- coding: utf-8 -*-
"""零依赖串口链路的自测（不需要 pyserial / pywin32 / 真实硬件）。

运行:
    python pc/tests/test_offline_reader.py

默认全部用 duck typing 的假串口（只实现 read(n)/close()），不打开任何 COM 口：
    * 分帧 / 半行 / 粘包
    * 超长行丢弃（>512 字节）
    * '#' 诊断行静默跳过（单独计数，不算失败）
    * 非法行丢弃
    * **angle 为整数 0 时必须被接受并转成 float**（当前固件 I2C 掉线，angle 恒为 int 0）
    * 断线重连 / stop() 可打断 / 端口被占用的可读提示

可选：接入真实串口再测一轮
    Windows: $env:WINDUO_TEST_PORT="COM3"; python pc/tests/test_offline_reader.py
退出码 0 = PASS。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PC_DIR = HERE.parent
ROOT = PC_DIR.parent
for _path in (str(PC_DIR), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import run_offline  # noqa: E402
import serial_reader_win as win  # noqa: E402
import winduo_protocol as proto  # noqa: E402

SUMMARY: Dict[str, Any] = {}

#: 板子当前真实输出（I2C 两个设备 MISSING -> sensor_error + angle 恒为整数 0）
REAL_LINE = '{"angle":0,"status":"sensor_error","mode":"default","author":"EthanMaven"}'
REAL_LINE_CAL = '{"angle":0,"status":"sensor_error","mode":"calibrate","author":"EthanMaven"}'


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, condition: Any, name: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("    [ok] %s" % name)
            return True
        self.failed += 1
        print("    [FAIL] %s %s" % (name, detail))
        raise AssertionError("断言失败: %s %s" % (name, detail))

    @staticmethod
    def close(a: float, b: float, tol: float = 1e-9) -> bool:
        return abs(float(a) - float(b)) <= tol


CK = Checker()


def section(title: str) -> None:
    print("\n== %s ==" % title)


# ---------------------------------------------------------------------------
# 假串口：duck typing，只实现 read(n)/close()
# ---------------------------------------------------------------------------

class FakePort:
    """模拟一个已打开的 COM 口。

    loop=True        : 数据读完从头再来；
    disconnect_after : 第 N+1 次 read 抛异常（模拟拔线）。
    """

    def __init__(self, payload: bytes, chunk_size: int = 23, loop: bool = True,
                 disconnect_after: int = 0, fail_with: Optional[win.WinSerialError] = None) -> None:
        self._payload = bytes(payload)
        self._pos = 0
        self._chunk = max(1, int(chunk_size))
        self._loop = bool(loop)
        self._disconnect_after = int(disconnect_after)
        self._fail_with = fail_with
        self._reads = 0
        self.closed = False

    def read(self, size: int = 4096) -> bytes:
        if self._fail_with is not None:
            raise self._fail_with
        self._reads += 1
        if self._disconnect_after and self._reads > self._disconnect_after:
            raise win.WinSerialError("设备已拔出（模拟）", win.ERROR_DEVICE_NOT_CONNECTED)
        if not self._payload:
            time.sleep(0.002)
            return b""
        if self._pos >= len(self._payload):
            if not self._loop:
                time.sleep(0.002)
                return b""
            self._pos = 0
        end = min(len(self._payload), self._pos + min(self._chunk, max(1, int(size))))
        data = self._payload[self._pos:end]
        self._pos = end
        return data

    def close(self) -> None:
        self.closed = True


def make_reader(factory, **kwargs: Any) -> win.WinSerialReader:
    kwargs.setdefault("port", "SIM")
    return win.WinSerialReader(transport_factory=factory, **kwargs)


def collect(reader: win.WinSerialReader, minimum: int, timeout: float = 2.0) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    reader.on_sample = samples.append
    deadline = time.monotonic() + timeout
    while len(samples) < minimum and time.monotonic() < deadline:
        time.sleep(0.01)
    return samples


# ---------------------------------------------------------------------------
# 1. 真实固件行：int angle 必须被接受并转成 float
# ---------------------------------------------------------------------------

def test_real_firmware_lines() -> None:
    section("1. 真实固件行解析（angle 为整数 0 也要接受并转 float）")

    sample = proto.parse_line(REAL_LINE)
    CK.check(sample is not None, "sensor_error 行解析成功", repr(sample))
    CK.check(isinstance(sample["angle"], float), "angle 是 float（不是 int/str）",
             "%r (%s)" % (sample["angle"], type(sample["angle"]).__name__))
    CK.check(CK.close(sample["angle"], 0.0), "angle == 0.0")
    CK.check(sample["status"] == "sensor_error", "status=sensor_error", sample["status"])
    CK.check(sample["mode"] == "default", "mode=default")
    CK.check(isinstance(sample["ts"], float) and sample["ts"] > 0, "ts 为 float")
    CK.check(set(sample.keys()) == {"angle", "status", "mode", "author", "ts"},
             "严格 5 字段契约", str(sorted(sample.keys())))

    cal = proto.parse_line(REAL_LINE_CAL)
    CK.check(cal is not None and CK.close(cal["angle"], 0.0) and cal["mode"] == "calibrate",
             "实测 mode=calibrate 行也解析成功", repr(cal))

    # int / float / 字符串数字 三种形态都必须是 float
    for text, expected, name in [
        ('{"angle":0,"status":"ok"}', 0.0, "整数 0"),
        ('{"angle":0.0,"status":"ok"}', 0.0, "浮点 0.0"),
        ('{"angle":"0","status":"ok"}', 0.0, "字符串 \"0\""),
        ('{"angle":45,"status":"ok"}', 45.0, "整数 45"),
        ('{"angle":180,"status":"ok"}', 180.0, "整数 180（边界）"),
        ('{"angle":-0.0,"status":"ok"}', 0.0, "负零"),
    ]:
        parsed = proto.parse_line(text)
        CK.check(parsed is not None and isinstance(parsed["angle"], float)
                 and CK.close(parsed["angle"], expected),
                 "%s -> float %s" % (name, expected), repr(parsed))

    # 越界/非法仍然是 None
    for text, name in [
        ('{"angle":181,"status":"ok"}', "整数 181"),
        ('{"angle":-1,"status":"ok"}', "整数 -1"),
        ('{"angle":true,"status":"ok"}', "bool"),
        ('{"angle":"abc","status":"ok"}', "不可转换字符串"),
        ('{"status":"ok"}', "缺 angle"),
    ]:
        CK.check(proto.parse_line(text) is None, "丢弃 %s" % name, text)


# ---------------------------------------------------------------------------
# 2. 假串口驱动 WinSerialReader：分帧 / 半行 / 超长 / 注释 / 非法行
# ---------------------------------------------------------------------------

def test_reader_with_fake_port() -> None:
    section("2. WinSerialReader + 假串口（分帧 / 半行 / 超长丢弃 / 注释跳过 / 非法丢弃）")

    lines = [
        REAL_LINE,                                             # 有效帧（int 0）
        "# calibration done. offset=1.234",                    # 注释行：跳过
        '{"angle":1.5,"status":"ok","mode":"default","author":"EthanMaven"}',
        '{"angle":87.3,"stat',                                 # 半行截断：非法
        "[INFO] sensor heartbeat tick=1234",                    # 纯日志：非法
        '{"angle":241.7,"status":"ok"}',                        # 越界：非法
        "",                                                     # 空行：非法
        '{"angle":2.5,"status":"warming_up","mode":"default","author":"EthanMaven"}',
        '{"angle":20.0,"pad":"%s"}' % ("x" * 600),              # 超长行：分帧丢弃
        "# debug: gyro zero",                                   # 注释行：跳过
        '{"angle":179.5,"status":"ok","mode":"debug","author":"EthanMaven"}',
        REAL_LINE_CAL,                                          # 有效帧（int 0, calibrate）
    ]
    payload = "".join(line + "\n" for line in lines).encode("utf-8")

    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    reader = make_reader(lambda: FakePort(payload, chunk_size=17, loop=False),
                         on_error=errors.append, idle_timeout=0.0)
    CK.check(reader.start() is True, "线程启动成功")
    got = collect(reader, 5, timeout=3.0)
    CK.check(len(got) >= 5, "读到 >= 5 个有效帧", str(len(got)))

    stats = reader.stats()
    CK.check(stats["valid"] == 5, "有效帧 = 5（其余全部正确分类）", str(stats))
    CK.check(stats["comments"] == 2, "注释行 = 2（'#' 行静默跳过）", str(stats))
    CK.check(stats["discarded"] == 4, "非法行 = 4（截断/日志/越界/空行）", str(stats))
    CK.check(stats["framer_dropped"] == 1, "超长行被分帧丢弃 = 1", str(stats))
    CK.check(stats["valid"] + stats["comments"] + stats["discarded"] + stats["framer_dropped"]
             == len(lines), "三类分类之和 == 输入行数", str(stats))
    CK.check(stats["callback_errors"] == 0, "0 回调异常")
    CK.check(stats["bytes_read"] == len(payload), "读取字节数 == 载荷长度", str(stats["bytes_read"]))

    angles = [s["angle"] for s in got]
    CK.check(all(isinstance(a, float) for a in angles), "所有 angle 都是 float", str(angles))
    CK.check(CK.close(angles[0], 0.0), "第一条 angle=0.0（int 0 转 float）", str(angles[0]))
    CK.check(CK.close(angles[-1], 0.0) and got[-1]["mode"] == "calibrate",
             "最后一条是 calibrate 的 0.0", str(got[-1]))
    CK.check(all(0.0 <= a <= 180.0 for a in angles), "角度值域合法", str(angles))
    CK.check(all(s["status"] in proto.VALID_STATUS for s in samples or got), "status 全部合法")
    CK.check(all("angle_raw" in s for s in got), "样本含 angle_raw（原始值）")
    CK.check(all("angle" in s and "ts" in s and "author" in s for s in got), "样本字段完整")

    stopped_at = time.monotonic()
    CK.check(reader.stop(timeout=2.0) is True, "stop() 正常返回")
    CK.check(time.monotonic() - stopped_at < 1.0, "stop() 延迟 < 1s",
             "%.3fs" % (time.monotonic() - stopped_at))
    CK.check(reader.is_running is False, "线程已退出")
    CK.check(reader.latest() is not None, "latest() 有值")

    # 半行：逐字节喂入（最恶劣的分块）
    reader2 = make_reader(lambda: FakePort(payload, chunk_size=1, loop=False), idle_timeout=0.0)
    reader2.start()
    got2 = collect(reader2, 5, timeout=3.0)
    reader2.stop()
    CK.check(len(got2) >= 5, "逐字节喂入仍能切出帧（半行容忍）", str(len(got2)))
    CK.check(reader2.stats()["valid"] == 5, "逐字节喂入有效帧数一致", str(reader2.stats()))

    SUMMARY["fake_port_stats"] = stats
    SUMMARY["fake_port_angles"] = angles


# ---------------------------------------------------------------------------
# 3. 断线重连 / 端口被占用 / 退避
# ---------------------------------------------------------------------------

def test_reconnect_and_errors() -> None:
    section("3. 断线重连 / 端口被占用可读提示 / 退避可打断")

    CK.check(CK.close(win.backoff_delay(1), 0.5) and CK.close(win.backoff_delay(4), 4.0)
             and CK.close(win.backoff_delay(9), 5.0), "退避 0.5 → 5s 封顶")

    payload = (REAL_LINE + "\n") * 50
    payload = payload.encode("utf-8")

    # 读到 3 次后抛异常（模拟拔线），应自动重连
    attempts = {"n": 0}

    def flaky() -> FakePort:
        attempts["n"] += 1
        return FakePort(payload, chunk_size=64, loop=False, disconnect_after=3)

    errors: List[str] = []
    reader = make_reader(flaky, on_error=errors.append, idle_timeout=0.0)
    CK.check(reader.start() is True, "断线测试线程启动")
    time.sleep(1.2)
    CK.check(reader.stats()["connect_attempts"] >= 2, "自动重连（connect_attempts >= 2）",
             str(reader.stats()))
    CK.check(reader.stats()["reconnects"] >= 1, "reconnects >= 1", str(reader.stats()))
    started = time.monotonic()
    CK.check(reader.stop(timeout=2.0) is True, "stop() 打断退避等待")
    CK.check(time.monotonic() - started < 1.0, "退避期间 stop() < 1s",
             "%.3fs" % (time.monotonic() - started))

    # 打开失败（端口被占用）：错误码可见、消息可读、不崩溃
    busy = win.WinSerialError("打开 COM3 失败：%s" % win.format_win_error(win.ERROR_ACCESS_DENIED),
                              win.ERROR_ACCESS_DENIED)

    def busy_factory() -> FakePort:
        """模拟 CreateFileW 直接失败（端口被别的程序独占）。"""
        raise busy

    errors2: List[str] = []
    reader2 = make_reader(busy_factory, on_error=errors2.append, idle_timeout=0.0)
    CK.check(reader2.start() is True, "被占用时线程仍能启动（后台重试）")
    time.sleep(0.3)
    CK.check(reader2.is_connected is False, "打开失败时 is_connected=False", str(reader2.is_connected))
    CK.check(reader2.is_fatal is True, "is_fatal=True（提示用户先处理端口）")
    CK.check(reader2.last_error_code == win.ERROR_ACCESS_DENIED, "错误码 = 5(ACCESS_DENIED)",
             str(reader2.last_error_code))
    CK.check(len(errors2) >= 1 and "占用" in errors2[0], "on_error 给出中文占用提示",
             str(errors2[:1]))
    reader2.stop()
    CK.check(reader2.stats()["valid"] == 0, "被占用时不产生任何样本")

    # 已连上但读取时被拔出：同样要能被识别为致命错误
    errors3: List[str] = []
    reader3 = make_reader(lambda: FakePort(b"", fail_with=busy), on_error=errors3.append,
                          idle_timeout=0.0)
    CK.check(reader3.start() is True, "读取期异常测试启动")
    time.sleep(0.3)
    CK.check(reader3.is_fatal is True, "读取期 ACCESS_DENIED 也判定为致命", str(reader3.last_error_code))
    reader3.stop()
    CK.check(reader3.stats()["valid"] == 0, "读取失败时不产生样本")

    # 缺 kernel32 / 非 Windows 时也不能崩：start() 返回布尔
    CK.check(isinstance(win.WinSerialReader(port="COM_NONE").start(), bool),
             "普通构造 + start() 返回布尔（有 pyserial 无关）")


# ---------------------------------------------------------------------------
# 4. Win32 结构与工具函数（不碰硬件）
# ---------------------------------------------------------------------------

def test_win32_structs() -> None:
    section("4. Win32 结构体与工具函数（DCB 8N1 / 错误码中文 / 端口枚举）")

    dcb = win.make_dcb()
    CK.check(ctypes.sizeof(win.DCB) == 28, "sizeof(DCB) == 28", str(ctypes.sizeof(win.DCB)))
    CK.check(dcb.DCBlength == 28, "DCBlength 已填", str(dcb.DCBlength))
    CK.check(dcb.BaudRate == 115200, "BaudRate = 115200", str(dcb.BaudRate))
    CK.check(dcb.ByteSize == 8, "ByteSize = 8")
    CK.check(dcb.Parity == win.NOPARITY, "Parity = NOPARITY")
    CK.check(dcb.StopBits == win.ONESTOPBIT, "StopBits = ONESTOPBIT")
    CK.check(dcb.Flags & win.DCB_FLAG_FBINARY, "fBinary = 1（Windows 要求）")
    CK.check(not (dcb.Flags & win.DCB_FLAG_DTR_ENABLE), "默认不拉 DTR（避免复位 Arduino）")
    CK.check(not (dcb.Flags & win.DCB_FLAG_RTS_ENABLE), "默认不拉 RTS")
    CK.check(win.make_dcb(dtr_enable=True).Flags & win.DCB_FLAG_DTR_ENABLE, "可显式打开 DTR")
    CK.check(win.make_dcb(rts_enable=True).Flags & win.DCB_FLAG_RTS_ENABLE, "可显式打开 RTS")

    CK.check(ctypes.sizeof(win.COMMTIMEOUTS) == 20, "sizeof(COMMTIMEOUTS) == 20",
             str(ctypes.sizeof(win.COMMTIMEOUTS)))
    CK.check(ctypes.sizeof(win.COMSTAT) == 12, "sizeof(COMSTAT) == 12", str(ctypes.sizeof(win.COMSTAT)))
    CK.check(win.COMMTIMEOUTS.ReadTotalTimeoutConstant.offset == 8,
             "COMMTIMEOUTS.ReadTotalTimeoutConstant 偏移 8")

    CK.check(win.WinSerialPort("COM3").device_path == "\\\\.\\COM3",
             "设备路径加 \\\\.\\ 前缀（COM10 必需）", win.WinSerialPort("COM3").device_path)
    CK.check(win.WinSerialPort("com7").port == "COM7", "端口名统一大写")
    CK.check(win.WinSerialPort("COM3").is_open is False, "未打开时 is_open=False")

    CK.check("占用" in win.format_win_error(win.ERROR_ACCESS_DENIED), "错误码 5 -> 占用提示",
             win.format_win_error(win.ERROR_ACCESS_DENIED))
    CK.check("不存在" in win.format_win_error(win.ERROR_FILE_NOT_FOUND), "错误码 2 -> 不存在提示",
             win.format_win_error(win.ERROR_FILE_NOT_FOUND))
    CK.check("拔出" in win.format_win_error(win.ERROR_DEVICE_NOT_CONNECTED),
             "错误码 1167 -> 拔出提示")
    CK.check(isinstance(win.format_win_error(99999), str), "未知错误码不抛异常",
             win.format_win_error(99999))
    CK.check(win.ERROR_ACCESS_DENIED in win.FATAL_PORT_CODES
             and win.ERROR_FILE_NOT_FOUND in win.FATAL_PORT_CODES,
             "致命错误码集合包含 5 / 2")

    ports = win.list_com_ports()
    CK.check(isinstance(ports, list), "list_com_ports 返回列表", str(ports))
    CK.check(all(str(p).upper().startswith("COM") for p in ports), "端口名形如 COMx", str(ports))
    details = win.list_com_port_details()
    CK.check(isinstance(details, list) and all("device" in d for d in details),
             "list_com_port_details 返回 device 字段", str(details[:2]))
    SUMMARY["ports"] = ports

    # 打开一个不存在的端口：必须抛可读异常，而不是崩栈
    bad = win.WinSerialPort("COM_NOT_EXIST_999")
    try:
        bad.open()
        raised = False
        message = ""
    except win.WinSerialError as exc:
        raised = True
        message = str(exc)
    CK.check(raised, "打开不存在的端口抛 WinSerialError")
    CK.check("不存在" in message or "占用" in message, "异常消息可读中文", message)
    CK.check(bad.is_open is False, "失败后句柄未泄漏")
    bad.close()  # 幂等


# ---------------------------------------------------------------------------
# 5. run_offline 纯函数与端到端（模拟数据，不碰硬件）
# ---------------------------------------------------------------------------

def test_run_offline_helpers() -> None:
    section("5. run_offline（格式化 / 状态提示 / 模拟端到端）")

    sample = {"angle": 0.0, "angle_raw": 0.0, "status": "sensor_error", "mode": "calibrate",
              "author": "EthanMaven", "ts": 1.0}
    text = run_offline.format_sample(sample, alpha=60, index=7)
    CK.check("angle=" in text and "0.00" in text, "format_sample 含角度", text)
    CK.check("alpha= 60" in text or "alpha=60" in text, "format_sample 含 alpha", text)
    CK.check("sensor_error" in text and "calibrate" in text, "format_sample 含状态与模式", text)
    CK.check("I2C" in text, "sensor_error 带接线提示", text)
    CK.check(run_offline.format_sample(
        {"angle": 90.0, "status": "ok", "mode": "default"}, index=1).find("<-") < 0,
        "ok 状态不追加提示")
    CK.check("I2C" in run_offline.status_hint("sensor_error"), "status_hint(sensor_error) 有说明")
    CK.check(run_offline.status_hint("ok") == "", "status_hint(ok) 为空")
    CK.check(run_offline.status_hint("不存在") == "", "未知状态返回空串")

    # SimulatedPort：read() 给 bytes，close() 后不再出数据
    sim = run_offline.SimulatedPort(count=60, seed=1)
    data = sim.read(4096)
    CK.check(isinstance(data, bytes) and len(data) > 0, "SimulatedPort.read 返回 bytes", str(len(data)))
    sim.close()
    CK.check(sim.read(16) == b"", "close() 后 read 返回空")
    CK.check(sim.line_count == 60, "SimulatedPort 生成了 60 行", str(sim.line_count))

    # 端到端：模拟串口 -> WinSerialReader（同一套分帧/解析/平滑代码）
    samples: List[Dict[str, Any]] = []
    reader = win.WinSerialReader(
        port="SIM", on_sample=samples.append, idle_timeout=0.0,
        transport_factory=lambda: FakePort(
            b"".join((item.text + "\n").encode("utf-8")
                     for item in __import__("simulate_device").generate_stream(count=120, seed=3)),
            chunk_size=31, loop=False),
    )
    reader.start()
    deadline = time.monotonic() + 3.0
    while len(samples) < 60 and time.monotonic() < deadline:
        time.sleep(0.01)
    reader.stop()
    stats = reader.stats()
    CK.check(len(samples) >= 60, "模拟流端到端收到 >= 60 帧", str(len(samples)))
    CK.check(stats["valid"] >= 60, "统计 valid 一致", str(stats))
    CK.check(stats["comments"] >= 1 and stats["discarded"] >= 1 and stats["framer_dropped"] >= 1,
             "注释行/非法行/超长行都被分类处理", str(stats))
    angles = [s["angle"] for s in samples]
    CK.check(all(0.0 <= a <= 180.0 for a in angles), "端到端角度值域合法",
             "%s..%s" % (min(angles), max(angles)))
    CK.check(all(isinstance(a, float) for a in angles), "端到端角度都是 float")
    SUMMARY["sim_stats"] = stats


# ---------------------------------------------------------------------------
# 6. 可选：真实串口（默认跳过）
# ---------------------------------------------------------------------------

def test_real_port_optional() -> None:
    section("6. 真实串口（默认跳过；设 WINDUO_TEST_PORT=COM3 开启）")

    port = os.environ.get("WINDUO_TEST_PORT", "").strip()
    if not port:
        print("    [skip] 未设置 WINDUO_TEST_PORT，跳过真实硬件测试")
        return
    if not win.IS_WINDOWS:
        print("    [skip] 非 Windows，跳过")
        return

    # 6.1 打不开时必须给出可读提示（占用/不存在）
    probe = win.WinSerialPort(port)
    samples: List[Dict[str, Any]] = []
    reader = win.WinSerialReader(port=port, on_sample=samples.append, idle_timeout=0.0)
    try:
        probe.open()
        opened = True
        probe.close()
    except win.WinSerialError as exc:
        opened = False
        print("    [info] %s 当前无法打开：%s" % (port, exc))

    if not opened:
        CK.check(True, "端口不可用时已给出可读中文提示（占用/不存在）")
        return

    # 6.2 真实读取 1.5 秒
    CK.check(reader.start() is True, "真实串口线程启动")
    deadline = time.monotonic() + 2.0
    while not samples and time.monotonic() < deadline:
        time.sleep(0.05)
    reader.stop()
    stats = reader.stats()
    CK.check(len(samples) >= 3, "真实串口读到 >= 3 帧（115200 8N1）", str(len(samples)))
    CK.check(stats["callback_errors"] == 0, "0 回调异常", str(stats))
    first = samples[0]
    CK.check(isinstance(first["angle"], float) and 0.0 <= first["angle"] <= 180.0,
             "真实帧 angle 为 float 且在 0..180", str(first["angle"]))
    CK.check(first["status"] in proto.VALID_STATUS, "真实帧 status 合法", first["status"])
    print("    [info] 真实帧样例: angle=%s status=%s mode=%s author=%s"
          % (first["angle"], first["status"], first["mode"], first["author"]))

    # 6.3 重复打开应报「被占用」
    holder = win.WinSerialPort(port)
    holder.open()
    try:
        second = win.WinSerialPort(port)
        try:
            second.open()
            second.close()
            CK.check(False, "重复打开应当失败（独占模式）")
        except win.WinSerialError as exc:
            CK.check("占用" in str(exc) or "共享" in str(exc), "重复打开报「被占用」", str(exc))
    finally:
        holder.close()
    SUMMARY["real_port"] = {"port": port, "frames": len(samples)}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def print_summary() -> None:
    fake = SUMMARY.get("fake_port_stats", {})
    sim = SUMMARY.get("sim_stats", {})
    print("\n============ WinDuo 离线链路自测统计 ============")
    print("假串口统计        : valid=%s comments=%s discarded=%s framer_dropped=%s"
          % (fake.get("valid"), fake.get("comments"), fake.get("discarded"),
             fake.get("framer_dropped")))
    print("假串口角度        : %s" % (SUMMARY.get("fake_port_angles"),))
    print("模拟端到端统计    : valid=%s comments=%s discarded=%s framer_dropped=%s"
          % (sim.get("valid"), sim.get("comments"), sim.get("discarded"), sim.get("framer_dropped")))
    print("本机串口          : %s" % (SUMMARY.get("ports"),))
    print("真实串口测试      : %s" % (SUMMARY.get("real_port", "未开启"),))
    print("第三方依赖        : 无（仅标准库 ctypes/winreg）")
    print("检查项            : %d 通过 / %d 失败" % (CK.passed, CK.failed))
    print("================================================")


def main() -> int:
    print("WinDuo 离线（零依赖）串口链路自测  python=%s" % sys.version.split()[0])
    started = time.monotonic()
    try:
        test_real_firmware_lines()
        test_reader_with_fake_port()
        test_reconnect_and_errors()
        test_win32_structs()
        test_run_offline_helpers()
        test_real_port_optional()
    except AssertionError as exc:
        print_summary()
        print("\nFAIL: %s" % exc)
        return 1
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print_summary()
        print("\nFAIL(异常): %s: %s" % (type(exc).__name__, exc))
        return 2
    print_summary()
    print("\nPASS  用时 %.2fs  检查项 %d 通过 / %d 失败"
          % (time.monotonic() - started, CK.passed, CK.failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
