# -*- coding: utf-8 -*-
"""WinDuo 虚拟设备：以 20Hz 输出与固件完全一致的 JSON 行。

用途：
    * 上位机联调（没有实机时也能跑通串口/解析/平滑/着色器全链路）；
    * 容错自测（故意注入截断行、日志行、越界角度、超长行、空行）。

输出格式与固件一致（单行、\\n 结尾、一位小数）：
    {"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}

常用命令：
    python pc/simulate_device.py --count 200 --out pc/tests/sample_stream.jsonl
    python pc/simulate_device.py --count 50                    # 打印到标准输出
    python pc/simulate_device.py --count 50 --realtime          # 按 20Hz 实时输出
    python pc/simulate_device.py --replay pc/tests/sample_stream.jsonl
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from collections import namedtuple
from typing import Any, Dict, Iterable, Iterator, List, Optional

try:  # 直接以脚本方式运行
    from winduo_protocol import MAX_LINE_BYTES, LineFramer, is_comment_line, parse_line
except ImportError:  # 作为包导入
    from .winduo_protocol import (  # type: ignore
        MAX_LINE_BYTES,
        LineFramer,
        is_comment_line,
        parse_line,
    )

__all__ = [
    "KIND_VALID",
    "KIND_VALID_INT",
    "KIND_VALID_LOG",
    "KIND_VALID_DEBUG",
    "KIND_COMMENT",
    "KIND_TRUNCATED",
    "KIND_NOISE",
    "KIND_OUT_OF_RANGE",
    "KIND_EMPTY",
    "KIND_OVERSIZED",
    "EXPECTED_PARSE_OK",
    "EXPECTED_COMMENTS",
    "EmittedLine",
    "format_line",
    "format_debug_line",
    "format_comment",
    "expected_kind",
    "generate_stream",
    "emit_stream",
    "replay_file",
    "main",
]

# --- 行类型（用于自测断言） ----------------------------------------------
KIND_VALID = "valid"                 # 正常行，应解析成功
KIND_VALID_INT = "valid_int"         # 整数角度（固件可能省掉 .0），应解析成功
KIND_VALID_LOG = "valid_log"         # JSON 后跟日志尾巴，应解析成功（宽容）
KIND_VALID_DEBUG = "valid_debug"     # debug 模式行（含 gyro/bias/base/sp/lp），应解析成功
KIND_COMMENT = "comment"             # "# calibration done. ..." 注释行，应静默跳过
KIND_TRUNCATED = "truncated"         # 半行截断，应丢弃
KIND_NOISE = "noise"                 # 纯日志行，应丢弃
KIND_OUT_OF_RANGE = "out_of_range"   # angle 越界，应丢弃
KIND_EMPTY = "empty"                 # 空行，应丢弃
KIND_OVERSIZED = "oversized"         # 超过 512 字节，应丢弃

EXPECTED_PARSE_OK = frozenset({KIND_VALID, KIND_VALID_INT, KIND_VALID_LOG, KIND_VALID_DEBUG})
EXPECTED_COMMENTS = frozenset({KIND_COMMENT})


def expected_kind(kind: str) -> str:
    """行类型的期望结果：'ok' | 'comment' | 'bad'。"""
    if kind in EXPECTED_PARSE_OK:
        return "ok"
    if kind in EXPECTED_COMMENTS:
        return "comment"
    return "bad"

DEFAULT_AUTHOR = "EthanMaven"
DEFAULT_HZ = 20.0
DEFAULT_PERIOD_SAMPLES = 160         # 8 秒一个 0->180->0 完整扫描（20Hz 下）

#: 固件注释行前缀
COMMENT_PREFIX = "#"

EmittedLine = namedtuple("EmittedLine", "index text kind expected_ok")


# ---------------------------------------------------------------------------
# 单行格式化
# ---------------------------------------------------------------------------

def format_line(angle: float, status: str = "ok", mode: str = "default",
                author: str = DEFAULT_AUTHOR) -> str:
    """按固件格式生成一行 JSON（一位小数、紧凑分隔符、字段顺序固定）。"""
    value = float(angle)
    if value < 0.0:
        value = 0.0
    elif value > 180.0:
        value = 180.0
    return '{"angle":%.1f,"status":"%s","mode":"%s","author":"%s"}' % (
        value, status, mode, author,
    )


def format_line_int(angle: int, status: str = "ok", mode: str = "default",
                    author: str = DEFAULT_AUTHOR) -> str:
    """整数角度版本（验证解析器对 int/float 都容忍）。"""
    return '{"angle":%d,"status":"%s","mode":"%s","author":"%s"}' % (
        int(round(angle)), status, mode, author,
    )


def format_debug_line(angle: float, gyro: float = 0.0, bias: float = 0.0, base: float = 0.0,
                      sp: float = 0.0, lp: float = 0.0,
                      author: str = DEFAULT_AUTHOR) -> str:
    """debug 模式行：额外带 gyro/bias/base/sp/lp（解析器默认忽略，with_extra=True 时保留）。"""
    value = max(0.0, min(180.0, float(angle)))
    return ('{"angle":%.1f,"status":"ok","mode":"debug","author":"%s",'
            '"gyro":%.3f,"bias":%.3f,"base":%.3f,"sp":%.3f,"lp":%.3f}') % (
        value, author, gyro, bias, base, sp, lp,
    )


def format_calibrate_line(angle: float, progress: float,
                          author: str = DEFAULT_AUTHOR) -> str:
    """校准模式行：额外带 progress(0-100)。"""
    value = max(0.0, min(180.0, float(angle)))
    pct = max(0.0, min(100.0, float(progress)))
    return ('{"angle":%.1f,"status":"calibrating","mode":"calibrate","author":"%s",'
            '"progress":%.0f}') % (value, author, pct)


def format_comment(text: str) -> str:
    """固件注释行（'#' 开头），上位机必须静默跳过。"""
    body = str(text).strip().lstrip(COMMENT_PREFIX).strip()
    return "%s %s" % (COMMENT_PREFIX, body)


# ---------------------------------------------------------------------------
# 事件注入表
# ---------------------------------------------------------------------------

#: (位置比例, 行类型)
_EVENT_PLAN = (
    (0.15, KIND_COMMENT),
    (0.25, KIND_TRUNCATED),
    (0.35, KIND_NOISE),
    (0.45, KIND_OUT_OF_RANGE),
    (0.55, KIND_EMPTY),
    (0.65, KIND_OVERSIZED),
    (0.75, KIND_COMMENT),
    (0.85, KIND_VALID_LOG),
)


def _build_event_map(count: int) -> Dict[int, str]:
    """把位置比例换算成具体行号，避免冲突。"""
    events: Dict[int, str] = {}
    if count < 12:
        return events
    for ratio, kind in _EVENT_PLAN:
        idx = int(count * ratio)
        if idx <= 0 or idx >= count - 1:
            continue
        if idx in events:
            continue
        events[idx] = kind
    return events


def _make_bad_line(kind: str, angle: float) -> str:
    """构造各类脏数据行（不含结尾换行）。"""
    if kind == KIND_TRUNCATED:
        # 半行截断：JSON 未闭合
        return '{"angle":%.1f,"stat' % angle
    if kind == KIND_NOISE:
        # 纯日志行（无 JSON）
        return "[INFO] sensor task heartbeat tick=1234 adc=2047"
    if kind == KIND_OUT_OF_RANGE:
        # 越界角度：必须被丢弃
        return '{"angle":241.7,"status":"ok","mode":"default","author":"%s"}' % DEFAULT_AUTHOR
    if kind == KIND_EMPTY:
        return ""
    if kind == KIND_OVERSIZED:
        # 超过 512 字节：必须被丢弃
        pad = "x" * (MAX_LINE_BYTES + 120)
        return '{"angle":45.0,"status":"ok","mode":"default","author":"%s","pad":"%s"}' % (
            DEFAULT_AUTHOR, pad,
        )
    if kind == KIND_VALID_LOG:
        # 合法 JSON 后面粘了一条日志（上位机应仍能解析出 angle）
        return '%s [dbg] uart tx ok' % format_line(angle)
    if kind == KIND_COMMENT:
        # 固件注释行：必须静默跳过，不算解析失败
        return format_comment("calibration done. offset=1.234 gain=0.998")
    return format_line(angle)


def _schedule_state(index: int, count: int) -> tuple:
    """按行号返回 (status, mode)，模拟固件启动/校准/调试状态切换。"""
    if index < max(3, count // 25):
        return ("warming_up", "default")
    if count // 25 <= index < max(6, count // 12):
        return ("calibrating", "calibrate")
    if index == int(count * 0.95):
        return ("sensor_error", "default")
    if index > int(count * 0.9):
        return ("ok", "debug")
    return ("ok", "default")


# ---------------------------------------------------------------------------
# 数据流生成
# ---------------------------------------------------------------------------

def generate_stream(
    count: int = 200,
    hz: float = DEFAULT_HZ,
    seed: int = 20240521,
    jitter: float = 0.6,
    spike_prob: float = 0.04,
    spike_max: float = 12.0,
    period_samples: int = DEFAULT_PERIOD_SAMPLES,
    include_events: bool = True,
    author: str = DEFAULT_AUTHOR,
) -> Iterator[EmittedLine]:
    """生成 count 行数据（含正弦扫描、状态切换、噪声与脏数据注入）。

    返回迭代器，元素为 EmittedLine(index, text, kind, expected_ok)。
    kind='comment' 的行为固件注释行（'#' 开头），应被静默跳过而非判为失败。
    """
    rng = random.Random(seed)
    period = max(8, int(period_samples))
    events = _build_event_map(count) if include_events else {}

    for index in range(max(0, int(count))):
        # 0 -> 180 -> 0 的正弦扫描
        base = 90.0 - 90.0 * math.cos(2.0 * math.pi * index / period)

        kind = events.get(index, KIND_VALID)
        outcome = expected_kind(kind)

        if outcome == "comment":
            yield EmittedLine(index, _make_bad_line(kind, base), kind, False)
            continue
        if outcome == "bad":
            yield EmittedLine(index, _make_bad_line(kind, base), kind, False)
            continue

        if kind == KIND_VALID_LOG:
            value = base + rng.gauss(0.0, jitter * 0.3)
            yield EmittedLine(index, _make_bad_line(KIND_VALID_LOG, value), kind, True)
            continue

        # 正常行：抖动 + 偶发尖峰（尖峰仍是合法角度，用于验证平滑的快速跟随）
        value = base + rng.gauss(0.0, jitter)
        if spike_prob > 0 and rng.random() < spike_prob:
            value += rng.uniform(-spike_max, spike_max)
        value = max(0.0, min(180.0, value))

        status, mode = _schedule_state(index, count)
        if include_events and index > 0 and index % 37 == 0:
            yield EmittedLine(index, format_line_int(value, status, mode, author), KIND_VALID_INT, True)
        elif status == "sensor_error":
            yield EmittedLine(index, format_line(value, status, mode, author), KIND_VALID, True)
        elif mode == "debug":
            # debug 模式：带扩展字段 gyro/bias/base/sp/lp
            yield EmittedLine(
                index,
                format_debug_line(
                    value,
                    gyro=value / 180.0 * 0.9,
                    bias=rng.gauss(0.0, 0.01),
                    base=rng.gauss(0.0, 0.02),
                    sp=1.0 + rng.gauss(0.0, 0.001),
                    lp=0.98 + rng.gauss(0.0, 0.001),
                    author=author,
                ),
                KIND_VALID_DEBUG,
                True,
            )
        elif mode == "calibrate":
            # 校准模式：带 progress(0-100)
            progress = 100.0 * index / max(1, count)
            yield EmittedLine(index, format_calibrate_line(value, progress, author), KIND_VALID, True)
        else:
            yield EmittedLine(index, format_line(value, status, mode, author), KIND_VALID, True)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def emit_stream(
    lines: Iterable[EmittedLine],
    out_path: Optional[str] = None,
    hz: float = DEFAULT_HZ,
    realtime: bool = False,
    port: Optional[str] = None,
    baudrate: int = 115200,
    stream: Any = None,
    announce: bool = True,
) -> Dict[str, Any]:
    """把生成的数据写入文件 / 标准输出 / 串口。返回统计字典。"""
    stats: Dict[str, Any] = {
        "lines": 0,
        "expected_ok": 0,
        "injected_bad": 0,
        "comments": 0,
        "target": "stdout",
    }
    period = 1.0 / hz if hz and hz > 0 else 0.0

    handle = None
    serial_port = None
    try:
        if port:
            try:
                import serial  # 懒加载：没装 pyserial 也能用文件/标准输出模式
            except Exception as exc:
                raise RuntimeError(
                    "未安装 pyserial，无法写串口。请执行: pip install pyserial（%s）" % exc
                ) from exc
            serial_port = serial.Serial(port=port, baudrate=baudrate, timeout=1.0,
                                        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                        stopbits=serial.STOPBITS_ONE)
            stats["target"] = "serial:%s" % port
        elif out_path:
            directory = os.path.dirname(os.path.abspath(out_path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            handle = open(out_path, "w", encoding="utf-8", newline="\n")
            stats["target"] = "file:%s" % out_path
        else:
            handle = stream if stream is not None else sys.stdout
            stats["target"] = "stdout"

        for item in lines:
            payload = item.text + "\n"
            if serial_port is not None:
                serial_port.write(payload.encode("utf-8"))
            else:
                handle.write(payload)
                if handle is sys.stdout:
                    handle.flush()
            stats["lines"] += 1
            if item.kind in EXPECTED_COMMENTS:
                stats["comments"] += 1
            elif item.expected_ok:
                stats["expected_ok"] += 1
            else:
                stats["injected_bad"] += 1
            if realtime and period > 0:
                time.sleep(period)
    finally:
        if handle is not None and handle is not sys.stdout and stream is None:
            try:
                handle.close()
            except Exception:
                pass
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass

    if announce:
        print(
            "[simulate_device] 输出 %d 行 -> %s（有效帧 %d / 注释行 %d / 注入脏数据 %d）"
            % (stats["lines"], stats["target"], stats["expected_ok"],
               stats["comments"], stats["injected_bad"])
        )
    return stats


# ---------------------------------------------------------------------------
# 回放（用解析器全量过一遍，验证 0 崩溃）
# ---------------------------------------------------------------------------

def replay_file(path: str, chunk_size: int = 37) -> Dict[str, Any]:
    """按固定块大小回放文件，模拟真实串口的分块到达（含半行/粘包）。

    统计口径分开：有效帧 / 注释行 / 非法行 / 分帧丢弃。
    """
    framer = LineFramer()
    valid = 0
    comments = 0
    discarded = 0
    samples: List[Dict[str, Any]] = []
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            for line in framer.feed(chunk):
                if is_comment_line(line):
                    comments += 1
                    continue
                sample = parse_line(line)
                if sample is None:
                    discarded += 1
                else:
                    valid += 1
                    samples.append(sample)
    return {
        "valid": valid,
        "comments": comments,
        "discarded": discarded,
        "framer_dropped": framer.dropped_lines,
        "chunk_size": chunk_size,
        "total": valid + comments + discarded + framer.dropped_lines,
        "last_angle": samples[-1]["angle"] if samples else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="WinDuo 虚拟设备：输出与固件一致的 JSON 行（20Hz）"
    )
    parser.add_argument("--count", type=int, default=200, help="输出行数")
    parser.add_argument("--out", default=None, help="写入文件（UTF-8，\\n 结尾）")
    parser.add_argument("--port", default=None, help="写入串口，如 COM7")
    parser.add_argument("--baud", type=int, default=115200, help="串口波特率")
    parser.add_argument("--hz", type=float, default=DEFAULT_HZ, help="输出频率（默认 20Hz）")
    parser.add_argument("--seed", type=int, default=20240521, help="随机种子")
    parser.add_argument("--period", type=int, default=DEFAULT_PERIOD_SAMPLES,
                        help="正弦扫描周期（采样点数）")
    parser.add_argument("--realtime", action="store_true",
                        help="按 --hz 实时输出（写文件时默认关闭，便于快速生成）")
    parser.add_argument("--no-events", action="store_true", help="不注入脏数据")
    parser.add_argument("--replay", default=None, help="回放已有文件并统计解析结果")
    args = parser.parse_args(argv)

    if args.replay:
        result = replay_file(args.replay)
        print("[replay] %s -> %s" % (args.replay, result))
        return 0

    lines = generate_stream(
        count=args.count,
        hz=args.hz,
        seed=args.seed,
        period_samples=args.period,
        include_events=not args.no_events,
    )
    emit_stream(
        lines,
        out_path=args.out,
        hz=args.hz,
        realtime=bool(args.realtime or args.port),
        port=args.port,
        baudrate=args.baud,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
