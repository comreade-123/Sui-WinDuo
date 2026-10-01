# -*- coding: utf-8 -*-
"""WinDuo 串口后台读取线程：115200 8N1 逐行读取 + 断线指数退避重连。

关键设计:
    * pyserial **懒加载**：模块导入时不 import serial，只有真正打开串口时才尝试导入；
      未安装时抛出可读的 SerialBackendUnavailable，而不是让整个程序 ImportError 崩溃。
    * 断线重连退避 0.5s -> 1s -> 2s -> 4s -> 5s（封顶），
      等待使用 Event.wait 实现，stop() 可立即打断（不会被退避 sleep 卡住）。
    * 读取用 bytes 块 + LineFramer 分帧，天然容忍半行/粘包；
      解析失败的行只计数不报错。

用法:
    reader = SerialReader(on_sample=lambda s: print(s["angle"]))
    reader.start()
    ...
    reader.stop()
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:  # 直接以脚本方式运行（pc/ 在 sys.path 上）
    from winduo_protocol import (
        MAX_LINE_BYTES,
        AngleSmoother,
        LineFramer,
        is_comment_line,
        parse_line,
    )
except ImportError:  # 作为包导入（python -c "import pc.serial_reader"）
    from .winduo_protocol import (  # type: ignore
        MAX_LINE_BYTES,
        AngleSmoother,
        LineFramer,
        is_comment_line,
        parse_line,
    )

__all__ = [
    "SerialBackendUnavailable",
    "SerialReader",
    "load_pyserial",
    "list_serial_ports",
    "autodetect_port",
    "backoff_delay",
    "BACKOFF_MIN",
    "BACKOFF_MAX",
    "DEFAULT_BAUDRATE",
]

DEFAULT_BAUDRATE = 115200

#: 重连退避区间（秒）
BACKOFF_MIN = 0.5
BACKOFF_MAX = 5.0


def backoff_delay(failures: int) -> float:
    """指数退避：第 1 次失败等 0.5s，之后翻倍，封顶 5s。

    failures=1 -> 0.5, 2 -> 1.0, 3 -> 2.0, 4 -> 4.0, >=5 -> 5.0
    """
    try:
        n = int(failures)
    except Exception:
        n = 1
    if n < 1:
        n = 1
    return min(BACKOFF_MAX, BACKOFF_MIN * (2.0 ** (n - 1)))

#: 常见 USB 转串口芯片关键字，用于自动挑端口
PORT_HINTS = (
    "ch340", "ch341", "cp210", "silicon labs", "usb-serial", "usb serial",
    "arduino", "ft232", "ftdi", "prolific", "wch", "esp32", "stm32", "usb 串行",
)


class SerialBackendUnavailable(RuntimeError):
    """pyserial 缺失或串口不可用（消息为可读中文，供 UI 直接展示）。"""


# ---------------------------------------------------------------------------
# 懒加载后端
# ---------------------------------------------------------------------------

def load_pyserial():
    """懒加载 pyserial，返回模块对象；失败抛 SerialBackendUnavailable。"""
    try:
        import serial  # noqa: F401  —— 唯一 import 点，保证无依赖时也能 import 本模块
        import serial.tools.list_ports  # noqa: F401
    except Exception as exc:
        raise SerialBackendUnavailable(
            "未安装 pyserial，串口功能不可用。请执行: pip install pyserial"
            "（原始错误: %s: %s）" % (type(exc).__name__, exc)
        ) from exc
    return serial


def list_serial_ports() -> List[Dict[str, str]]:
    """列出可用串口；无 pyserial 时抛 SerialBackendUnavailable。"""
    serial = load_pyserial()
    ports: List[Dict[str, str]] = []
    try:
        for info in serial.tools.list_ports.comports():
            ports.append({
                "device": str(getattr(info, "device", "") or ""),
                "description": str(getattr(info, "description", "") or ""),
                "hwid": str(getattr(info, "hwid", "") or ""),
            })
    except Exception as exc:
        raise SerialBackendUnavailable("枚举串口失败: %s" % exc) from exc
    return ports


def autodetect_port() -> Optional[str]:
    """按芯片关键字自动挑选串口；无匹配则取第一个；无串口返回 None。"""
    ports = list_serial_ports()
    for item in ports:
        text = ("%s %s" % (item.get("description", ""), item.get("hwid", ""))).lower()
        if any(hint in text for hint in PORT_HINTS):
            return item.get("device") or None
    if ports:
        return ports[0].get("device") or None
    return None


# ---------------------------------------------------------------------------
# 读取线程
# ---------------------------------------------------------------------------

class SerialReader:
    """后台线程串口读取器。

    参数:
        port:            串口名（如 "COM7"）；None 时每次重连都尝试自动探测。
        baudrate:        默认 115200。
        on_sample:       回调，收到 (dict) 样本时调用；异常被吞掉并计入 callback_errors。
        on_error:        回调，发生异常/断线时调用，参数为可读消息字符串。
        alpha:           EMA 系数，传给 AngleSmoother。
        deadband:        死区，传给 AngleSmoother。
        timeout:         pyserial 读超时（秒），决定线程响应 stop() 的粒度。
        idle_timeout:    已连接但连续多久没有有效数据就判定断线重连；<=0 表示不判定。
        max_line_bytes:  单行最大字节数。
        transport_factory: 可选，替换串口对象工厂（返回带 read(n)/close() 的对象）。
                         用于单元测试/仿真，完全不触发 pyserial 导入。

    样本结构（在 parse_line 结果上扩展）:
        {"angle": 平滑后角度, "angle_raw": 原始角度, "status", "mode", "author", "ts"}
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = DEFAULT_BAUDRATE,
        on_sample: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        alpha: float = 0.25,
        deadband: float = 0.0,
        timeout: float = 0.2,
        idle_timeout: float = 3.0,
        max_line_bytes: int = MAX_LINE_BYTES,
        transport_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.idle_timeout = float(idle_timeout)
        self.max_line_bytes = int(max_line_bytes)
        self.on_sample = on_sample
        self.on_error = on_error
        self.transport_factory = transport_factory

        self._smoother = AngleSmoother(alpha=alpha, deadband=deadband)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._transport: Optional[Any] = None
        self._latest: Optional[Dict[str, Any]] = None
        self._sample_seq = 0
        self._connected = False
        self._last_error: Optional[str] = None
        self._fatal_error: Optional[str] = None
        self._failures = 0  # 连续失败次数，用于指数退避；收到有效数据后清零

        self._stats: Dict[str, int] = {
            "bytes_read": 0,
            "lines": 0,
            "valid": 0,
            "comments": 0,
            "discarded": 0,
            "framer_dropped": 0,
            "connect_attempts": 0,
            "reconnects": 0,
            "callback_errors": 0,
            "read_errors": 0,
            "failures": 0,
        }

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> bool:
        """启动后台线程。返回 False 表示启动失败（原因见 last_error）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True

            # 没有自定义传输层时，先做一次懒加载检查，给出立即、可读的错误
            if self.transport_factory is None:
                try:
                    load_pyserial()
                except SerialBackendUnavailable as exc:
                    self._fatal_error = str(exc)
                    self._last_error = str(exc)
                    self._notify_error(str(exc))
                    return False

            self._stop_event.clear()
            self._fatal_error = None
            self._thread = threading.Thread(
                target=self._run, name="WinDuoSerialReader", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> bool:
        """请求停止并等待线程退出；退避等待会被立即打断。"""
        self._stop_event.set()
        transport = None
        with self._lock:
            transport = self._transport
        # 主动关闭传输层，让阻塞在 read() 上的线程尽快返回
        self._safe_close(transport)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        alive = thread is not None and thread.is_alive()
        with self._lock:
            self._connected = False
            if not alive:
                self._thread = None
        return not alive

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error or self._fatal_error

    # -- 数据访问 -----------------------------------------------------------

    def latest(self) -> Optional[Dict[str, Any]]:
        """最近一个成功解析的样本（副本）；从未收到时返回 None。"""
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def wait_for_sample(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """等待一个新样本到达（或超时），返回样本或 None。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            seq = self._sample_seq
        while time.monotonic() < deadline:
            with self._lock:
                if self._sample_seq != seq and self._latest is not None:
                    return dict(self._latest)
            if self._stop_event.wait(0.01):
                break
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    # -- 内部实现 -----------------------------------------------------------

    def _notify_error(self, message: str) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(message)
        except Exception:
            with self._lock:
                self._stats["callback_errors"] += 1

    def _push(self, sample: Dict[str, Any]) -> None:
        with self._lock:
            self._latest = sample
            self._sample_seq += 1
        if self.on_sample is not None:
            try:
                self.on_sample(sample)
            except Exception:
                with self._lock:
                    self._stats["callback_errors"] += 1

    def _safe_close(self, transport: Optional[Any]) -> None:
        if transport is None:
            return
        for name in ("close", "cancel_read", "flush"):
            func = getattr(transport, name, None)
            if callable(func):
                try:
                    func()
                except Exception:
                    pass

    def _open_transport(self) -> Any:
        """打开一路传输层（真实串口或注入的仿真传输层）。"""
        if self.transport_factory is not None:
            return self.transport_factory()

        serial = load_pyserial()
        port = self.port
        if not port:
            try:
                port = autodetect_port()
            except SerialBackendUnavailable:
                port = None
        if not port:
            raise SerialBackendUnavailable("未找到可用串口，请检查 USB 连接或显式指定 port")

        return serial.Serial(
            port=port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            transport = None
            try:
                transport = self._open_transport()
            except Exception as exc:
                self._handle_failure(exc)
                continue

            with self._lock:
                self._transport = transport
                self._connected = True
                self._stats["connect_attempts"] += 1
                if self._stats["connect_attempts"] > 1:
                    self._stats["reconnects"] += 1
                self._last_error = None

            try:
                self._pump(transport)
            except Exception as exc:
                self._handle_failure(exc)
            finally:
                self._safe_close(transport)
                with self._lock:
                    self._transport = None
                    self._connected = False

        with self._lock:
            self._connected = False

    def _pump(self, transport: Any) -> None:
        """持续读取直到 stop 或断线异常。"""
        framer = LineFramer(self.max_line_bytes)
        last_data = time.monotonic()

        while not self._stop_event.is_set():
            try:
                chunk = transport.read(256)
            except Exception as exc:
                with self._lock:
                    self._stats["read_errors"] += 1
                raise RuntimeError("串口读取失败: %s: %s" % (type(exc).__name__, exc)) from exc

            if chunk:
                last_data = time.monotonic()
                if isinstance(chunk, str):
                    size = len(chunk.encode("utf-8", "replace"))
                else:
                    size = len(chunk)
                with self._lock:
                    self._stats["bytes_read"] += size

                for line in framer.feed(chunk):
                    # 固件注释行（"# calibration done. ..."）静默跳过，单独计数
                    if is_comment_line(line):
                        with self._lock:
                            self._stats["comments"] += 1
                        continue
                    with self._lock:
                        self._stats["lines"] += 1
                    sample = parse_line(line, max_line_bytes=self.max_line_bytes)
                    if sample is None:
                        with self._lock:
                            self._stats["discarded"] += 1
                        continue
                    raw_angle = sample["angle"]
                    smoothed = self._smoother.update(raw_angle)
                    sample["angle_raw"] = raw_angle
                    sample["angle"] = smoothed
                    with self._lock:
                        self._stats["valid"] += 1
                        self._stats["framer_dropped"] = framer.dropped_lines
                        # 数据通了，退避计数清零
                        self._failures = 0
                        self._stats["failures"] = 0
                    self._push(sample)
                continue

            # read 返回空：要么是超时（正常），要么是设备已掉线
            if self.idle_timeout > 0 and (time.monotonic() - last_data) > self.idle_timeout:
                with self._lock:
                    self._stats["framer_dropped"] = framer.dropped_lines
                raise TimeoutError(
                    "连续 %.1fs 未收到有效数据，判定设备断线，准备重连" % self.idle_timeout
                )

    def _handle_failure(self, exc: BaseException) -> None:
        """统一处理打开/读取失败：记录错误并做指数退避等待（stop() 可立即打断）。"""
        with self._lock:
            self._failures += 1
            failures = self._failures
            self._stats["failures"] = failures
            self._last_error = "串口异常(%s): %s" % (type(exc).__name__, exc)
            if isinstance(exc, SerialBackendUnavailable):
                self._fatal_error = str(exc)

        delay = BACKOFF_MAX if isinstance(exc, SerialBackendUnavailable) else backoff_delay(failures)
        message = "%s；%.1fs 后重试（第 %d 次失败）" % (self._last_error, delay, failures)
        with self._lock:
            self._last_error = message
        self._notify_error(message)
        self._stop_event.wait(delay)


# ---------------------------------------------------------------------------
# 命令行自检：python pc/serial_reader.py --list / --dump COM7
# ---------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WinDuo 串口读取自检工具")
    parser.add_argument("--list", action="store_true", help="列出可用串口")
    parser.add_argument("--port", default=None, help="串口名，如 COM7；缺省自动探测")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="波特率")
    parser.add_argument("--seconds", type=float, default=5.0, help="持续打印时长")
    args = parser.parse_args(argv)

    if args.list:
        try:
            ports = list_serial_ports()
        except SerialBackendUnavailable as exc:
            print("[错误] %s" % exc)
            return 2
        if not ports:
            print("未发现串口设备")
            return 0
        for item in ports:
            print("%-10s %s" % (item["device"], item["description"]))
        return 0

    def on_sample(sample: Dict[str, Any]) -> None:
        print(
            "angle=%6.2f (raw=%6.2f) status=%-11s mode=%-9s author=%s"
            % (
                sample["angle"], sample["angle_raw"], sample["status"],
                sample["mode"], sample["author"],
            )
        )

    def on_error(message: str) -> None:
        print("[警告] %s" % message)

    reader = SerialReader(port=args.port, baudrate=args.baud, on_sample=on_sample, on_error=on_error)
    if not reader.start():
        print("[错误] %s" % reader.last_error)
        return 2
    try:
        end = time.monotonic() + max(0.0, args.seconds)
        while time.monotonic() < end:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    print("统计: %s" % reader.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
