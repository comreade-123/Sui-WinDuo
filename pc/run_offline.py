# -*- coding: utf-8 -*-
"""WinDuo 离线全链路运行器（零第三方依赖）。

一条命令跑通：读串口 -> 分帧 -> parse_line -> EMA 平滑 -> 打印角度 + 更新玻璃效果 alpha。

无网、没装 pyserial / PyQt6 / PyOpenGL 也能跑：

    python pc/run_offline.py --port COM3              # 默认：读 COM3，并开玻璃覆盖层
    python pc/run_offline.py --port COM3 --no-glass   # 只看数值，不建窗口
    python pc/run_offline.py --simulate               # 没接硬件时的自检（内置虚拟设备）
    python pc/run_offline.py --list                   # 列出可用串口

Ctrl+C 干净退出（停止线程、销毁覆盖层、打印统计）。
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import serial_reader_win as win_reader  # ctypes 串口（零依赖）
except ImportError:  # 作为包导入
    from . import serial_reader_win as win_reader  # type: ignore

__all__ = [
    "SimulatedPort",
    "format_sample",
    "status_hint",
    "build_reader",
    "main",
]

#: 状态提示（帮助用户判断设备当前在干什么）
_STATUS_HINT = {
    "warming_up": "上电预热中",
    "calibrating": "校准中",
    "sensor_error": "传感器未就绪（检查 I2C 接线/供电）",
    "ok": "",
    "unknown": "状态字段缺失或非法",
}


def status_hint(status: str) -> str:
    """把 status 翻译成中文提示；未知状态返回空串。"""
    return _STATUS_HINT.get(str(status or "").lower(), "")


def format_sample(sample: Dict[str, Any], alpha: Optional[int] = None, index: int = 0) -> str:
    """把一条样本格式化成一行可读文本（纯函数，便于测试）。"""
    angle = sample.get("angle", 0.0)
    raw = sample.get("angle_raw", angle)
    parts = [
        "[%5d]" % index,
        "angle=%7.2f°" % float(angle),
        "(raw=%7.2f°)" % float(raw),
    ]
    if alpha is not None:
        parts.append("alpha=%3d" % int(alpha))
    parts.append("status=%-12s" % str(sample.get("status", "")))
    parts.append("mode=%-9s" % str(sample.get("mode", "")))
    hint = status_hint(str(sample.get("status", "")))
    if hint:
        parts.append("<- %s" % hint)
    return " ".join(parts)


class SimulatedPort:
    """duck typing 的假串口：把 simulate_device 生成的行按需吐出来（--simulate 用）。

    只实现 read(n)/close()，因此可以被 WinSerialReader 直接当作串口使用，
    走的是与真机完全相同的分帧/解析/平滑代码路径。
    """

    def __init__(self, count: int = 400, seed: int = 20240521, loop: bool = True,
                 hz: float = 20.0) -> None:
        import simulate_device

        self._chunks: List[bytes] = []
        self._index = 0
        self._offset = 0
        self._loop = bool(loop)
        self._interval = 1.0 / max(1.0, float(hz))
        self.closed = False
        self._lines = list(simulate_device.generate_stream(count=count, seed=seed))
        self._payload = b"".join((item.text + "\n").encode("utf-8") for item in self._lines)

    @property
    def line_count(self) -> int:
        return len(self._lines)

    def read(self, size: int = 4096) -> bytes:
        if self.closed or not self._payload:
            time.sleep(0.005)
            return b""
        want = max(1, min(int(size), 64))  # 小块返回，模拟真实串口的零散到达
        if self._offset >= len(self._payload):
            if not self._loop:
                time.sleep(0.005)
                return b""
            self._offset = 0
        chunk = self._payload[self._offset:self._offset + want]
        self._offset += len(chunk)
        time.sleep(self._interval)  # 约 20Hz 的节奏（与固件一致）
        return chunk

    def close(self) -> None:
        self.closed = True


def build_reader(args: argparse.Namespace, sink: Deque[Dict[str, Any]]) -> Any:
    """按参数创建读取器（默认 ctypes 零依赖实现）。"""
    def on_sample(sample: Dict[str, Any]) -> None:
        sink.append(sample)

    def on_error(message: str) -> None:
        print("[警告] %s" % message)

    if args.simulate:
        return win_reader.WinSerialReader(
            port="SIM",
            on_sample=on_sample,
            on_error=on_error,
            alpha=args.alpha,
            idle_timeout=0.0,
            transport_factory=lambda: SimulatedPort(count=400, seed=args.seed),
        )

    if args.reader == "pyserial":
        from serial_reader import SerialReader  # 懒加载：没装 pyserial 会给出可读错误

        return SerialReader(
            port=args.port, baudrate=args.baud, on_sample=on_sample, on_error=on_error,
            alpha=args.alpha,
        )

    return win_reader.WinSerialReader(
        port=args.port,
        baudrate=args.baud,
        on_sample=on_sample,
        on_error=on_error,
        alpha=args.alpha,
        read_timeout_ms=args.read_timeout,
        dtr_enable=args.dtr,
        rts_enable=args.rts,
    )


def create_overlay(args: argparse.Namespace) -> Optional[Any]:
    """创建玻璃覆盖层；不可用时打印原因并返回 None（自动降级为只打印数值）。"""
    if args.no_glass:
        return None
    try:
        import glass_overlay
    except Exception as exc:
        print("[提示] 玻璃效果不可用（%s），继续只打印数值。" % exc)
        return None
    overlay = glass_overlay.GlassOverlay(
        click_through=True,
        alpha_min=args.alpha_min,
        alpha_max=args.alpha_max,
    )
    if not overlay.create():
        print("[提示] 创建玻璃覆盖层失败（%s），继续只打印数值。" % (overlay.reason or "未知原因"))
        return None
    print("[玻璃] 覆盖层已创建 hwnd=%s accent=%s（点击穿透，按 Ctrl+C 退出）"
          % (overlay.hwnd, overlay.accent_name))
    if not overlay.available:
        print("[提示] 当前系统不支持 SetWindowCompositionAttribute，只更新数值。")
    return overlay


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_offline.py",
        description="WinDuo 离线全链路：读串口(ctypes, 零依赖) -> 解析 -> EMA 平滑 -> 打印 + 玻璃效果",
        epilog="示例：python pc/run_offline.py --port COM3 --no-glass",
    )
    parser.add_argument("--port", default="COM3", help="串口名，默认 COM3")
    parser.add_argument("--baud", type=int, default=115200, help="波特率，默认 115200（8N1 固定）")
    parser.add_argument("--seconds", type=float, default=0.0, help="运行时长（秒），0 = 一直运行到 Ctrl+C")
    parser.add_argument("--no-glass", action="store_true", help="不创建玻璃覆盖层，只打印数值")
    parser.add_argument("--alpha-min", type=int, default=60, help="0° 时的玻璃浓淡（0..255）")
    parser.add_argument("--alpha-max", type=int, default=230, help="180° 时的玻璃浓淡（0..255）")
    parser.add_argument("--reader", choices=("win", "pyserial"), default="win",
                        help="读取后端：win = ctypes 零依赖（默认）；pyserial = 需要先 pip install pyserial")
    parser.add_argument("--alpha", type=float, default=0.25, help="EMA 平滑系数，默认 0.25")
    parser.add_argument("--read-timeout", type=int, default=100, help="单次读取超时（毫秒），默认 100")
    parser.add_argument("--connect-wait", type=float, default=1.5,
                        help="启动后等待首次连接的秒数，默认 1.5（超时会立刻报可读错误）")
    parser.add_argument("--heartbeat", type=float, default=5.0,
                        help="无变化时每隔多少秒打印一次心跳，0 = 不打印")
    parser.add_argument("--verbose", action="store_true", help="每一帧都打印（默认只在变化时打印）")
    parser.add_argument("--simulate", action="store_true",
                        help="无硬件自检：用内置虚拟设备代替串口（不打开 COM 口）")
    parser.add_argument("--seed", type=int, default=20240521, help="--simulate 的随机种子")
    parser.add_argument("--list", action="store_true", help="列出本机可用串口后退出")
    parser.add_argument("--send", action="append", default=None, metavar="CMD",
                        help="连上后向设备发送一行命令（可重复），"
                             "例如 --send 'mode=debug' 切到调试模式看原始读数；"
                             "固件支持 cal / mode=default|calibrate|debug / status")
    parser.add_argument("--dtr", action="store_true",
                        help="打开串口时拉高 DTR（注意：多数 Arduino 会因此复位，默认不拉）")
    parser.add_argument("--rts", action="store_true", help="打开串口时拉高 RTS（默认不拉）")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.list:
        details = win_reader.list_com_port_details()
        if not details:
            print("未发现串口（检查 USB 连接与驱动）")
            return 0
        print("本机可用串口：")
        for item in details:
            print("  %-6s <- %s" % (item["port"], item["device"]))
        return 0

    print("WinDuo 离线运行器  端口=%s  波特率=%d 8N1  后端=%s%s"
          % (args.port if not args.simulate else "SIM", args.baud, args.reader,
             "  [模拟]" if args.simulate else ""))

    sink: Deque[Dict[str, Any]] = collections.deque(maxlen=512)
    reader = build_reader(args, sink)

    if not reader.start():
        print("[错误] %s" % reader.last_error)
        return 2

    # 等待首次连接：端口不存在 / 被占用时立刻给出可读提示，而不是默默重试
    if not args.simulate:
        deadline = time.monotonic() + max(0.2, float(args.connect_wait))
        while time.monotonic() < deadline and not reader.is_connected:
            if reader.is_fatal:
                break
            time.sleep(0.05)
        if not reader.is_connected and reader.is_fatal:
            print("[错误] %s" % (reader.last_error or ("无法打开 %s" % args.port)))
            ports = win_reader.list_com_ports()
            print("当前可用串口：%s" % (", ".join(ports) if ports else "（无）"))
            print("提示：串口监视器 / Arduino IDE / 串口助手会独占 COM 口，请先关闭它们；"
                  "也可以先用 --simulate 验证全链路。")
            reader.stop()
            return 2
        if not reader.is_connected:
            print("[警告] 暂时没连上 %s（%s），继续后台重连…"
                  % (args.port, reader.last_error or "未知原因"))

    print("[串口] %s（Ctrl+C 退出）" % ("已连接" if reader.is_connected else "已启动"))

    # 可选：连上后向设备发送命令（例如切到 debug 模式看原始读数）
    if args.send:
        for command in args.send:
            if hasattr(reader, "send_line") and reader.send_line(command):
                print("[已发送] %s" % command)
            else:
                print("[警告] 无法发送 %r（当前读取后端不支持写，或读线程未运行）" % command)

    overlay = create_overlay(args)

    started = time.monotonic()
    printed = 0
    last_print = 0.0
    last_angle: Optional[float] = None
    last_status: Optional[str] = None
    last_mode: Optional[str] = None
    interrupted = False
    try:
        while True:
            drained = 0
            while sink:
                sample = sink.popleft()
                drained += 1
                alpha = overlay.set_angle(sample["angle"]) if overlay is not None else None
                angle = float(sample["angle"])
                changed = (
                    last_angle is None
                    or sample["status"] != last_status
                    or sample["mode"] != last_mode
                    or abs(angle - (last_angle or 0.0)) >= 0.5
                )
                now = time.monotonic()
                if args.verbose or changed or (args.heartbeat > 0 and now - last_print >= args.heartbeat):
                    printed += 1
                    print(format_sample(sample, alpha, printed))
                    last_angle = angle
                    last_status = sample["status"]
                    last_mode = sample["mode"]
                    last_print = now
            if overlay is not None:
                overlay.pump_messages()
            if args.seconds and args.seconds > 0 and (time.monotonic() - started) >= args.seconds:
                break
            if drained == 0:
                time.sleep(0.01)
    except KeyboardInterrupt:
        interrupted = True
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        reader.stop()
        if overlay is not None:
            overlay.destroy()

    stats = reader.stats()
    elapsed = time.monotonic() - started
    print("\n=========== 运行统计 ===========")
    print("时长            : %.1f s" % elapsed)
    print("有效帧          : %s（约 %.1f 帧/秒）"
          % (stats.get("valid", 0), stats.get("valid", 0) / elapsed if elapsed > 0 else 0.0))
    print("注释行          : %s（'#' 诊断行，已跳过）" % stats.get("comments", 0))
    print("非法行          : %s" % stats.get("discarded", 0))
    print("分帧超长丢弃    : %s" % stats.get("framer_dropped", 0))
    print("读取字节        : %s" % stats.get("bytes_read", 0))
    print("重连次数        : %s" % stats.get("reconnects", 0))
    last = reader.latest()
    if last is not None:
        print("最后一条        : %s" % format_sample(last, index=stats.get("valid", 0)))
        hint = status_hint(str(last.get("status", "")))
        if hint:
            print("状态提示        : %s" % hint)
    if stats.get("valid", 0) == 0:
        print("提示            : 没有解析出任何有效帧。请确认波特率 115200、固件在输出 JSON 行。")
    if interrupted:
        print("（用户中断，已完成清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
