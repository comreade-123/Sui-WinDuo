# -*- coding: utf-8 -*-
"""WinDuo PC 端全链路自测：parse -> smooth -> normalize -> shader uniform。

运行:
    python pc/tests/test_pipeline.py

退出码 0 = PASS（并打印统计数字）；非 0 = 有断言失败。
断言失败必须修代码，禁止放宽断言。
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
PC_DIR = HERE.parent
ROOT = PC_DIR.parent
for _path in (str(PC_DIR), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import glass_overlay  # noqa: E402
import gl_shader_blur  # noqa: E402
import serial_reader  # noqa: E402
import simulate_device  # noqa: E402
import winduo_protocol as proto  # noqa: E402

SAMPLE_STREAM = HERE / "sample_stream.jsonl"
CANONICAL_LINE = '{"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}'

SUMMARY: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 极简断言框架
# ---------------------------------------------------------------------------

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
        message = "断言失败: %s %s" % (name, detail)
        print("    [FAIL] %s %s" % (name, detail))
        raise AssertionError(message)

    def close(self, a: float, b: float, tol: float = 1e-9) -> bool:
        return abs(float(a) - float(b)) <= tol


CK = Checker()


def section(title: str) -> None:
    print("\n== %s ==" % title)


# ---------------------------------------------------------------------------
# 仿真传输层（不依赖 pyserial）
# ---------------------------------------------------------------------------

class FakeTransport:
    """带 read(n)/close() 的仿真串口。

    loop=True       : 数据读完从头再来（模拟持续输出）；
    disconnect_after: 读到第 N+1 次时抛异常（模拟拔线）。
    """

    def __init__(self, payload: bytes, chunk_size: int = 32, loop: bool = True,
                 disconnect_after: int = 0) -> None:
        self._payload = bytes(payload)
        self._pos = 0
        self._chunk_size = max(1, int(chunk_size))
        self._loop = bool(loop)
        self._disconnect_after = int(disconnect_after)
        self._reads = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        self._reads += 1
        if self._disconnect_after and self._reads > self._disconnect_after:
            raise OSError("simulated device unplugged")
        if not self._payload:
            time.sleep(0.002)
            return b""
        if self._pos >= len(self._payload):
            if not self._loop:
                time.sleep(0.002)
                return b""
            self._pos = 0
        end = min(len(self._payload), self._pos + min(self._chunk_size, max(1, int(size))))
        chunk = self._payload[self._pos:end]
        self._pos = end
        return chunk

    def close(self) -> None:
        self.closed = True


def jumps(series: List[float]) -> List[float]:
    return [abs(b - a) for a, b in zip(series, series[1:])]


def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# 1. parse_line 容错
# ---------------------------------------------------------------------------

def test_parse_line() -> None:
    section("1. parse_line 容错（任何输入都不得抛异常）")

    sample = proto.parse_line(CANONICAL_LINE)
    CK.check(isinstance(sample, dict), "标准行解析成功", repr(sample))
    CK.check(set(sample.keys()) == {"angle", "status", "mode", "author", "ts"},
             "返回字段恰好为 angle/status/mode/author/ts", str(sorted(sample.keys())))
    CK.check(CK.close(sample["angle"], 45.2), "angle=45.2", str(sample["angle"]))
    CK.check(isinstance(sample["angle"], float), "angle 是 float（不是字符串）")
    CK.check(sample["status"] == "ok" and sample["mode"] == "default", "枚举解析正确")
    CK.check(sample["author"] == "EthanMaven", "author 解析正确")
    CK.check(isinstance(sample["ts"], float) and sample["ts"] > 0, "ts 为 float")

    sample_bytes = proto.parse_line(CANONICAL_LINE.encode("utf-8"))
    CK.check(sample_bytes is not None and CK.close(sample_bytes["angle"], 45.2), "bytes 输入等价")

    # 字段顺序变化 / 整数角度 / 空白 / \r\n / 前后日志
    tolerant = [
        ('{"status":"ok","angle":90,"mode":"default","author":"EthanMaven"}', 90.0, "字段顺序变化+整数角度"),
        ('\r\n   {"angle": 0.0 , "status" : "ok" }  \r\n', 0.0, "首尾空白与追加空格"),
        ('{"angle":180.0,"status":"ok","mode":"default","author":"EthanMaven"}', 180.0, "边界 180.0"),
        ('{"angle":0,"status":"ok","mode":"default","author":"EthanMaven"}', 0.0, "边界 0"),
        ('[INFO] boot ok {"angle":33.3,"status":"ok","mode":"default","author":"EthanMaven"}', 33.3, "前置日志"),
        ('{"angle":44.4,"status":"ok","mode":"default","author":"EthanMaven"} tail log', 44.4, "后置日志"),
        ('{"angle":"55.5","status":"ok"}', 55.5, "数值字符串容忍"),
        ('{"angle":45.0,"status":"OK"}', 45.0, "枚举大小写归一化"),
        ('{"angle":-0.0,"status":"ok"}', 0.0, "负零归一化"),
    ]
    for text, expected, name in tolerant:
        parsed = proto.parse_line(text)
        CK.check(parsed is not None and CK.close(parsed["angle"], expected), "容忍: %s" % name,
                 repr(parsed))

    # 必须被拒绝的输入
    rejected = [
        ("", "空字符串"),
        ("   ", "纯空白"),
        (None, "None"),
        (b"", "空 bytes"),
        ("not a json line at all", "非 JSON 文本"),
        ('{"angle":87.3,"stat', "半行截断"),
        ('{"angle":45.0', "缺少右花括号"),
        ("}{", "只有花括号"),
        ('{"angle":45.0}}', "多余右花括号"),
        ('{"angle":241.7,"status":"ok"}', "angle 越界上"),
        ('{"angle":-12.0,"status":"ok"}', "angle 越界下"),
        ('{"angle":180.6,"status":"ok"}', "angle 略超 180"),
        ('{"status":"ok","mode":"default"}', "缺少 angle"),
        ('{"angle":null}', "angle=null"),
        ('{"angle":true}', "angle=bool"),
        ('{"angle":[45.0]}', "angle=数组"),
        ('{"angle":NaN}', "angle=NaN"),
        ('{"angle":Infinity}', "angle=Infinity"),
        ('{"angle":-Infinity}', "angle=-Infinity"),
        ("[1, 2, 3]", "JSON 数组"),
        ("45.2", "裸数字"),
        ("{}", "空对象"),
        ("{", "半个花括号"),
        (12345, "整数输入"),
        ({"angle": 45.0}, "dict 输入"),
        (b"\xff\xfe\x00\x01garbage", "非法字节"),
        ("{" + '{"a":1}' * 40 + "}", "嵌套脏数据"),
    ]
    for raw, name in rejected:
        CK.check(proto.parse_line(raw) is None, "拒绝: %s" % name, repr(raw))

    # 超长行（>512 字节）直接丢弃
    oversized = '{"angle":45.0,"pad":"%s"}' % ("x" * 600)
    CK.check(len(oversized.encode("utf-8")) > proto.MAX_LINE_BYTES, "构造超长行 >512B")
    CK.check(proto.parse_line(oversized) is None, "拒绝: 超长行")

    # status/mode 非法值退化但不丢数据
    weird = proto.parse_line('{"angle":45.0,"status":"???","mode":"???"}')
    CK.check(weird is not None and weird["status"] == "unknown" and weird["mode"] == "default",
             "非法枚举退化为 unknown/default 而非丢行", repr(weird))

    # 非法 UTF-8：宽容解码后仍能取出 angle
    bad_utf8 = proto.parse_line(b'{"angle":45.0,"status":"\xff\xfe"}')
    CK.check(bad_utf8 is not None and CK.close(bad_utf8["angle"], 45.0), "非法 UTF-8 不崩溃")

    # 绝不抛异常（模糊测试：脏输入轰炸）
    exceptions = 0
    fuzz_inputs: List[Any] = [raw for raw, _n in rejected]
    fuzz_inputs += [b"\x00" * 600, b'{"angle":' + b"9" * 400, "x" * 5000, "\ud800", 3.14,
                    [], (), object(), float("nan"), float("inf")]
    for raw in fuzz_inputs:
        try:
            proto.parse_line(raw)
        except Exception:
            exceptions += 1
    CK.check(exceptions == 0, "模糊输入 0 异常（%d 条）" % len(fuzz_inputs), "exceptions=%d" % exceptions)
    SUMMARY["parse_exceptions"] = exceptions


# ---------------------------------------------------------------------------
# 1b. angle 字段契约：number 主路径 + 字符串数字向后兼容
# ---------------------------------------------------------------------------

def test_angle_contract() -> None:
    section("1b. angle 契约（JSON number 主路径 + 字符串数字兼容）")

    # 主路径：真正的 JSON 数字（当前固件）
    for text, expected, name in [
        ('{"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}', 45.2, "浮点 45.2"),
        ('{"angle":45,"status":"ok"}', 45.0, "整数 45"),
        ('{"angle":0,"status":"ok"}', 0.0, "下边界 0"),
        ('{"angle":180,"status":"ok"}', 180.0, "上边界 180"),
        ('{"angle":180.0,"status":"ok"}', 180.0, "上边界 180.0"),
        ('{"angle":89.95,"status":"ok"}', 89.95, "两位小数也接受"),
    ]:
        parsed = proto.parse_line(text)
        CK.check(parsed is not None and isinstance(parsed["angle"], float)
                 and CK.close(parsed["angle"], expected), "number 主路径: %s" % name, repr(parsed))

    # 兼容路径：字符串数字（旧固件缺陷版本）
    for text, expected, name in [
        ('{"angle":"45.2","status":"ok","mode":"default","author":"EthanMaven"}', 45.2, '"45.2"'),
        ('{"angle":"45","status":"ok"}', 45.0, '"45"'),
        ('{"angle":" 90.5 ","status":"ok"}', 90.5, '" 90.5 "（带空格）'),
        ('{"angle":"0","status":"ok"}', 0.0, '"0"'),
        ('{"angle":"180.0","status":"ok"}', 180.0, '"180.0"'),
    ]:
        parsed = proto.parse_line(text)
        CK.check(parsed is not None and CK.close(parsed["angle"], expected),
                 "字符串数字兼容: %s" % name, repr(parsed))

    # 必须丢弃
    for text, name in [
        ('{"angle":"abc","status":"ok"}', '"abc" 不可转换'),
        ('{"angle":"","status":"ok"}', '空字符串'),
        ('{"angle":"45.2.3","status":"ok"}', '"45.2.3" 非法'),
        ('{"angle":"1e999"}', '"1e999" -> inf'),
        ('{"angle":181,"status":"ok"}', '整数 181 越界'),
        ('{"angle":181.0,"status":"ok"}', '浮点 181.0 越界'),
        ('{"angle":"181","status":"ok"}', '字符串 "181" 越界'),
        ('{"angle":-0.1,"status":"ok"}', '浮点 -0.1 越界'),
        ('{"angle":"-1","status":"ok"}', '字符串 "-1" 越界'),
        ('{"angle":true,"status":"ok"}', '布尔 true'),
        ('{"angle":false,"status":"ok"}', '布尔 false'),
        ('{"angle":null,"status":"ok"}', 'null'),
        ('{"status":"ok","mode":"default"}', '缺少 angle'),
        ('{"author":"EthanMaven"}', '缺少 angle（仅 author）'),
    ]:
        CK.check(proto.parse_line(text) is None, "丢弃: %s" % name, text)


# ---------------------------------------------------------------------------
# 1c. 注释行（固件 "# calibration done. ..."）
# ---------------------------------------------------------------------------

def test_comment_lines() -> None:
    section("1c. 注释行静默跳过 + 统计口径分开（有效帧/注释行/非法行）")

    for raw, expected, name in [
        ("# calibration done. offset=1.234 gain=0.998", True, "标准注释行"),
        ("   # 缩进注释", True, "带前导空白"),
        (b"# boot ok\n", True, "bytes 注释行"),
        ("## double hash", True, "双井号"),
        ('{"angle":45.0,"status":"ok"}', False, "JSON 行不是注释"),
        ("#not json but starts with hash", True, "紧贴井号"),
        ("", False, "空行不是注释"),
        (None, False, "None 不是注释"),
        (123, False, "整数不是注释"),
        (b"\xff\xfe#", False, "非法字节安全处理"),
    ]:
        CK.check(proto.is_comment_line(raw) is expected, "is_comment_line: %s" % name, repr(raw))

    CK.check(proto.parse_line("# calibration done. offset=1.234") is None,
             "parse_line 对注释行返回 None（不抛异常）")
    CK.check(proto.parse_line(b"  # debug: gyro zero\n") is None, "bytes 注释行同样返回 None")

    # 统计口径：有效帧 / 注释行 / 非法行 三者互斥
    chunks = [
        '{"angle":10.0,"status":"ok","mode":"default","author":"EthanMaven"}\n',
        "# calibration start\n",
        '{"angle":20.0,"status":"ok"}\n',
        "garbage line without braces\n",
        "# calibration done. offset=1.234\n",
        '{"angle":30.0,"status":"ok"}\n',
    ]
    samples, stats = proto.parse_all(chunks)
    CK.check(len(samples) == 3, "有效帧 = 3", str(len(samples)))
    CK.check(stats.valid == 3, "stats.valid = 3", str(stats))
    CK.check(stats.comments == 2, "stats.comments = 2（注释行单独计数）", str(stats))
    CK.check(stats.discarded == 1, "stats.discarded = 1（只算非法行）", str(stats))
    CK.check(stats.errors == 0, "stats.errors = 0")
    CK.check("注释行" in str(stats) and "非法行" in str(stats), "统计字符串口径可读", str(stats))
    CK.check(stats.valid + stats.comments + stats.discarded + stats.framer_dropped == 6,
             "统计口径之和等于输入行数", str(stats.as_dict()))

    # debug / calibrate 扩展字段：默认忽略（保持 5 字段契约），with_extra=True 时保留
    debug_line = ('{"angle":27.8,"status":"ok","mode":"debug","author":"EthanMaven",'
                  '"gyro":0.139,"bias":-0.011,"base":0.011,"sp":0.999,"lp":0.977}')
    plain = proto.parse_line(debug_line)
    CK.check(set(plain.keys()) == {"angle", "status", "mode", "author", "ts"},
             "debug 行默认仍是 5 字段契约", str(sorted(plain.keys())))
    rich = proto.parse_line(debug_line, with_extra=True)
    CK.check(rich is not None and set(rich["extra"].keys()) == {"gyro", "bias", "base", "sp", "lp"},
             "with_extra=True 保留 gyro/bias/base/sp/lp", str(rich.get("extra") if rich else None))
    CK.check(CK.close(rich["extra"]["gyro"], 0.139), "扩展字段数值原样保留")

    calib_line = ('{"angle":12.1,"status":"calibrating","mode":"calibrate",'
                  '"author":"EthanMaven","progress":55}')
    calib = proto.parse_line(calib_line, with_extra=True)
    CK.check(CK.close(calib["extra"]["progress"], 55.0), "校准 progress 保留")
    out_of_band = proto.parse_line(
        '{"angle":12.1,"status":"calibrating","progress":180}', with_extra=True)
    CK.check(CK.close(out_of_band["extra"]["progress"], 100.0), "progress 超范围夹紧到 0..100")
    bad_progress = proto.parse_line(
        '{"angle":12.1,"status":"calibrating","progress":"n/a"}', with_extra=True)
    CK.check("progress" not in bad_progress["extra"], "非数值 progress 被剔除而不是崩溃")
    CK.check(set(proto.parse_line(calib_line).keys()) == {"angle", "status", "mode", "author", "ts"},
             "calibrate 行默认仍是 5 字段契约")


# ---------------------------------------------------------------------------
# 2. LineFramer 分帧
# ---------------------------------------------------------------------------

def test_framer() -> None:
    section("2. LineFramer 分帧（半行 / 粘包 / 超长）")

    framer = proto.LineFramer()
    chunks = [
        b'{"angle":12.5,"st',
        b'atus":"ok","mode":"default","author":"EthanMaven"}\r\n{"angle":',
        b'90.0,"status":"ok","mode":"default","author":"EthanMaven"}\n',
    ]
    lines: List[str] = []
    for chunk in chunks:
        lines.extend(framer.feed(chunk))
    CK.check(len(lines) == 2, "半行拼接 + 粘包切分 = 2 行", str(lines))
    first = proto.parse_line(lines[0])
    second = proto.parse_line(lines[1])
    CK.check(first is not None and CK.close(first["angle"], 12.5), "第 1 行 angle=12.5")
    CK.check(second is not None and CK.close(second["angle"], 90.0), "第 2 行 angle=90.0")
    CK.check(framer.pending_bytes == 0, "缓冲区已排空")

    framer2 = proto.LineFramer()
    CK.check(framer2.feed(b"x" * 700 + b"\n") == [], "超长行被丢弃")
    CK.check(framer2.dropped_lines == 1, "超长行计数 = 1", str(framer2.dropped_lines))
    CK.check(framer2.feed(b"y" * 700) == [] and framer2.dropped_lines == 2,
             "无换行的超长残留也被丢弃", str(framer2.pending_bytes))
    CK.check(framer2.pending_bytes == 0, "超长残留后缓冲区清空")

    framer3 = proto.LineFramer()
    out = framer3.feed(b'{"angle":1.0}\n\n\n{"angle":2.0}\n')
    CK.check(len(out) == 4, "空行也作为帧返回（由解析器判非法）", str(len(out)))
    CK.check(proto.parse_line(out[0]) is not None and proto.parse_line(out[1]) is None,
             "空行不会与相邻行粘连")

    # 逐字节喂入（最恶劣的半行场景）
    framer4 = proto.LineFramer()
    payload = (CANONICAL_LINE + "\n") * 5
    collected: List[str] = []
    for index in range(len(payload)):
        collected.extend(framer4.feed(payload[index:index + 1].encode("utf-8")))
    CK.check(len(collected) == 5, "逐字节喂入仍切出 5 行", str(len(collected)))
    CK.check(all(proto.parse_line(line) is not None for line in collected), "逐字节喂入全部可解析")

    # 回归：超长行的“尾巴”绝不能被当成新行吐出（结果必须与分块大小无关）
    mixed = (
        b'{"angle":10.0,"status":"ok","mode":"default","author":"EthanMaven"}\n'
        + b'{"angle":20.0,"pad":"' + b"x" * 600 + b'"}\n'
        + b'{"angle":30.0,"status":"ok","mode":"default","author":"EthanMaven"}\n'
    )
    signatures = {}
    for chunk_size in (1, 3, 37, 128, 4096):
        fr = proto.LineFramer()
        frames: List[str] = []
        for offset in range(0, len(mixed), chunk_size):
            frames.extend(fr.feed(mixed[offset:offset + chunk_size]))
        signatures[chunk_size] = (
            fr.frames, fr.dropped_lines,
            tuple((proto.parse_line(f) or {}).get("angle", -1.0) for f in frames),
            fr.pending_bytes,
        )
    CK.check(len(set(signatures.values())) == 1,
             "超长行丢弃与分块大小无关（1/3/37/128/4096 结果一致）", str(signatures))
    CK.check(signatures[1][0] == 2 and signatures[1][1] == 1,
             "超长行只计 1 次丢弃、前后两行正常切出", str(signatures[1]))
    CK.check(signatures[1][2] == (10.0, 30.0), "超长行前后的角度值正确", str(signatures[1][2]))
    CK.check(signatures[1][3] == 0, "混有超长行后缓冲区无残留")


# ---------------------------------------------------------------------------
# 3. AngleSmoother
# ---------------------------------------------------------------------------

def test_smoother() -> None:
    section("3. AngleSmoother（EMA / 快速跟随 / 死区 / 非法输入）")

    smoother = proto.AngleSmoother(alpha=0.25)
    CK.check(CK.close(smoother.update(60.0), 60.0), "首个样本直接作为初值")
    CK.check(CK.close(smoother.update(80.0), 65.0), "EMA: 60 + 0.25*(80-60) = 65")
    CK.check(CK.close(smoother.update(80.0), 68.75), "EMA 继续收敛")

    jumper = proto.AngleSmoother()
    CK.check(CK.close(jumper.update(0.0), 0.0), "初值 0")
    CK.check(CK.close(jumper.update(50.0), 50.0), "跳变 50°(>40°) 直接跟随，无拖尾")
    CK.check(jumper.fast_follows == 1, "快速跟随计数 = 1")

    deadband = proto.AngleSmoother(alpha=0.25, deadband=2.0)
    deadband.update(90.0)
    CK.check(CK.close(deadband.update(91.0), 90.0), "死区内保持不变（滞回）")
    CK.check(deadband.deadband_holds == 1, "死区保持计数 = 1")
    CK.check(CK.close(deadband.update(93.0), 90.75), "超出死区后正常 EMA")

    invalid = proto.AngleSmoother(alpha=0.25)
    invalid.update(45.0)
    for bad in (None, "abc", float("nan"), -5.0, 200.0, [1], {}):
        value = invalid.update(bad)
        CK.check(CK.close(value, 45.0), "非法输入返回上次有效值 (%r)" % (bad,))
    CK.check(invalid.invalid_inputs == 7, "非法输入计数 = 7", str(invalid.invalid_inputs))

    try:
        proto.AngleSmoother(alpha=0.0)
        raised = False
    except ValueError:
        raised = True
    CK.check(raised, "alpha 越界时抛 ValueError（配置错误应显式报错）")

    # 模块级 smooth() 与 AngleSmoother 序列一致
    series = [0.0, 10.0, 20.0, 60.0, 61.0, 30.0, 30.5, 179.0, 180.0]
    reference = proto.AngleSmoother(alpha=0.25)
    expected = [reference.update(v) for v in series]
    proto.reset_smoother()
    actual = [proto.smooth(v) for v in series]
    CK.check(all(CK.close(a, b) for a, b in zip(actual, expected)),
             "模块级 smooth() 与 AngleSmoother 序列一致", "%s vs %s" % (actual, expected))

    proto.reset_smoother()
    CK.check(CK.close(proto.smooth(90.0), 90.0), "reset 后 smooth() 重新初始化")

    CK.check(CK.close(proto.normalize_angle(90.0), 0.5), "normalize_angle(90)=0.5")
    CK.check(CK.close(proto.normalize_angle(-10), 0.0) and CK.close(proto.normalize_angle(999), 1.0),
             "normalize_angle 越界夹紧")
    CK.check(CK.close(proto.normalize_angle("bad"), 0.0), "normalize_angle 非法输入返回 0.0")


# ---------------------------------------------------------------------------
# 4. 全链路：仿真 -> 解析 -> 平滑 -> 归一化 -> uniform
# ---------------------------------------------------------------------------

def test_full_pipeline() -> None:
    section("4. 全链路（simulate -> parse -> smooth -> normalize -> uniform）")

    emitted = list(simulate_device.generate_stream(count=200, hz=20.0, seed=20240521))
    CK.check(len(emitted) == 200, "仿真输出 200 行", str(len(emitted)))
    expected_ok = sum(1 for item in emitted if item.expected_ok)
    comment_count = sum(1 for item in emitted if item.kind in simulate_device.EXPECTED_COMMENTS)
    injected_bad = len(emitted) - expected_ok - comment_count
    CK.check((expected_ok, comment_count, injected_bad) == (193, 2, 5),
             "三类行数 = 有效 193 / 注释 2 / 脏数据 5",
             "%d/%d/%d" % (expected_ok, comment_count, injected_bad))

    # 逐行断言：有效帧 100% 解析、注释行 100% 跳过、脏数据 100% 丢弃
    exceptions = 0
    wrong = []
    for item in emitted:
        try:
            parsed = proto.parse_line(item.text)
            commented = proto.is_comment_line(item.text)
        except Exception:
            exceptions += 1
            continue
        outcome = simulate_device.expected_kind(item.kind)
        if outcome == "ok" and parsed is None:
            wrong.append(("应成功却失败", item.index, item.kind, item.text[:60]))
        if outcome == "comment" and (parsed is not None or not commented):
            wrong.append(("注释行未被跳过", item.index, item.kind, item.text[:60]))
        if outcome == "bad" and (parsed is not None or commented):
            wrong.append(("应丢弃却成功", item.index, item.kind, item.text[:60]))
    CK.check(exceptions == 0, "逐行解析 0 异常")
    CK.check(not wrong, "有效帧 100% 解析 / 注释行 100% 跳过 / 脏数据 100% 丢弃", str(wrong[:3]))

    # 真实分帧路径（bytes + LineFramer），注释行单独计数
    framer = proto.LineFramer()
    samples: List[Dict[str, Any]] = []
    comments = 0
    discarded = 0
    for item in emitted:
        for line in framer.feed((item.text + "\n").encode("utf-8")):
            if proto.is_comment_line(line):
                comments += 1
                continue
            parsed = proto.parse_line(line)
            if parsed is None:
                discarded += 1
            else:
                samples.append(parsed)

    CK.check(len(samples) == expected_ok, "分帧路径有效帧数 = 193", str(len(samples)))
    CK.check(comments == comment_count, "分帧路径注释行数 = 2（不算失败）", str(comments))
    CK.check(discarded + framer.dropped_lines == injected_bad,
             "非法行 + 分帧丢弃 = 注入脏数据数",
             "%d + %d vs %d" % (discarded, framer.dropped_lines, injected_bad))
    CK.check(framer.dropped_lines == 1, "分帧阶段丢弃超长行 1 条", str(framer.dropped_lines))

    raw_angles = [s["angle"] for s in samples]
    CK.check(all(0.0 <= a <= 180.0 for a in raw_angles), "原始角度全部在 0..180")
    CK.check(all(s["status"] in proto.VALID_STATUS for s in samples), "status 全部合法")
    CK.check(all(s["mode"] in proto.VALID_MODE for s in samples), "mode 全部合法")
    CK.check(any(s["status"] == "warming_up" for s in samples)
             and any(s["status"] == "sensor_error" for s in samples)
             and any(s["status"] == "calibrating" for s in samples)
             and any(s["mode"] == "calibrate" for s in samples)
             and any(s["mode"] == "debug" for s in samples),
             "状态切换覆盖 warming_up/calibrating/sensor_error/calibrate/debug")

    # 平滑
    smoother = proto.AngleSmoother(alpha=0.25)
    smoothed = [smoother.update(a) for a in raw_angles]
    raw_jump = jumps(raw_angles)
    smooth_jump = jumps(smoothed)
    summary = {
        "valid": len(samples),
        "comments": comments,
        "discarded": discarded + framer.dropped_lines,
        "raw_max_jump": max(raw_jump),
        "smooth_max_jump": max(smooth_jump),
        "raw_mean_jump": mean(raw_jump),
        "smooth_mean_jump": mean(smooth_jump),
    }
    CK.check(summary["smooth_max_jump"] <= summary["raw_max_jump"] + 1e-9,
             "平滑后最大帧间跳变 <= 平滑前",
             "%.4f vs %.4f" % (summary["smooth_max_jump"], summary["raw_max_jump"]))
    CK.check(summary["smooth_mean_jump"] <= summary["raw_mean_jump"] + 1e-9,
             "平滑后平均帧间跳变 <= 平滑前",
             "%.4f vs %.4f" % (summary["smooth_mean_jump"], summary["raw_mean_jump"]))
    CK.check(all(0.0 <= v <= 180.0 for v in smoothed), "平滑值域仍在 0..180")
    CK.check(max(smooth_jump) <= 45.0, "单帧最大修正量 <= alpha*180")
    CK.check(sum(smooth_jump) < sum(raw_jump),
             "平滑后总路程小于原始（抖动被抑制）",
             "%.2f vs %.2f" % (sum(smooth_jump), sum(raw_jump)))
    repeated = sum(1 for a, b in zip(smooth_jump, raw_jump) if a > b + 1e-9)
    summary["per_frame_violations"] = repeated
    summary["path_raw"] = sum(raw_jump)
    summary["path_smooth"] = sum(smooth_jump)

    # 归一化 + uniform（手动映射）
    uniform_series: Dict[str, List[float]] = {name: [] for name in gl_shader_blur.UNIFORM_NAMES}
    manual = proto.AngleSmoother(alpha=0.25)
    for angle in raw_angles:
        values = gl_shader_blur.angle_to_uniforms(manual.update(angle))
        for name, value in values.items():
            uniform_series[name].append(value)

    for name in gl_shader_blur.UNIFORM_NAMES:
        values = uniform_series[name]
        CK.check(all(0.0 <= v <= 1.0 for v in values), "%s 值域在 0..1" % name,
                 "%s..%s" % (min(values), max(values)))
        CK.check((max(values) - min(values)) >= 0.9, "%s 覆盖 0..1 的 90%% 以上" % name,
                 "span=%.4f" % (max(values) - min(values)))
    hinge = uniform_series[gl_shader_blur.UNIFORM_HINGE_ANGLE]
    CK.check(all(CK.close(v, proto.normalize_angle(s), 1e-12) for v, s in zip(hinge, smoothed)),
             "u_hingeAngle 严格等于 normalize_angle(平滑值)")
    summary["uniform_range"] = {
        name: (min(uniform_series[name]), max(uniform_series[name]))
        for name in gl_shader_blur.UNIFORM_NAMES
    }

    # 适配器路径应与手动映射完全一致（同一 alpha 的独立平滑器）
    adapter = gl_shader_blur.AngleUniformAdapter(backend="none", verbose=False)
    adapter_series = [adapter.update_angle_uniform(a)[gl_shader_blur.UNIFORM_HINGE_ANGLE]
                      for a in raw_angles]
    CK.check(all(CK.close(a, b, 1e-12) for a, b in zip(adapter_series, hinge)),
             "AngleUniformAdapter 与手动映射一致")
    CK.check(CK.close(adapter.values()[gl_shader_blur.UNIFORM_HINGE_ANGLE], hinge[-1], 1e-12),
             "adapter.values() 反映最近一次写入")

    SUMMARY["pipeline"] = summary
    SUMMARY["samples"] = samples


# ---------------------------------------------------------------------------
# 5. 样本文件全量回放
# ---------------------------------------------------------------------------

def test_replay() -> None:
    section("5. sample_stream.jsonl 全量回放（不同分块大小结果一致）")

    if not SAMPLE_STREAM.exists():
        simulate_device.emit_stream(
            simulate_device.generate_stream(count=200), out_path=str(SAMPLE_STREAM), announce=False
        )
        print("    [info] 样本文件不存在，已现场生成 %s" % SAMPLE_STREAM.name)

    raw = SAMPLE_STREAM.read_bytes()
    # 逐行计数：\n 的个数即记录数（其中 1 条是故意注入的空行，也必须被计数）
    line_count = raw.count(b"\n")
    non_empty = len([line for line in raw.split(b"\n") if line])
    CK.check(line_count == 200, "样本文件 200 条记录", str(line_count))
    CK.check(line_count - non_empty == 1, "恰好 1 条注入的空行记录",
             "%d - %d" % (line_count, non_empty))

    results = {}
    for chunk_size in (1, 7, 37, 4096):
        results[chunk_size] = simulate_device.replay_file(str(SAMPLE_STREAM), chunk_size=chunk_size)
    CK.check(results[1]["valid"] == results[4096]["valid"]
             and results[1]["comments"] == results[4096]["comments"]
             and results[1]["discarded"] == results[4096]["discarded"]
             and results[1]["framer_dropped"] == results[4096]["framer_dropped"],
             "逐字节回放与整块回放结果一致", str(results))
    base = results[37]
    CK.check(base["valid"] >= 190, "回放有效帧 >= 190", str(base["valid"]))
    CK.check(base["comments"] >= 1, "回放注释行单独计数 >= 1", str(base["comments"]))
    CK.check(base["valid"] + base["comments"] + base["discarded"] + base["framer_dropped"] == 200,
             "有效 + 注释 + 非法 + 分帧丢弃 = 200（无丢失、无崩溃）", str(base))
    CK.check(base["discarded"] >= 4, "至少丢弃 4 行脏数据", str(base["discarded"]))
    CK.check(base["framer_dropped"] >= 1, "至少分帧丢弃 1 行超长数据", str(base["framer_dropped"]))
    CK.check(base["last_angle"] is not None and 0.0 <= base["last_angle"] <= 180.0,
             "回放末行角度合法", str(base["last_angle"]))
    SUMMARY["replay"] = base


# ---------------------------------------------------------------------------
# 6. SerialReader（仿真传输层，验证线程/回调/重连/停止）
# ---------------------------------------------------------------------------

def test_serial_reader() -> None:
    section("6. SerialReader 线程（回调 / 重连退避 / stop 可打断）")

    CK.check(CK.close(serial_reader.backoff_delay(1), 0.5)
             and CK.close(serial_reader.backoff_delay(2), 1.0)
             and CK.close(serial_reader.backoff_delay(3), 2.0)
             and CK.close(serial_reader.backoff_delay(4), 4.0)
             and CK.close(serial_reader.backoff_delay(5), 5.0)
             and CK.close(serial_reader.backoff_delay(99), 5.0),
             "退避序列 0.5/1/2/4/5/5s（封顶 5s）")

    emitted = list(simulate_device.generate_stream(count=200, seed=7))
    payload = b"".join((item.text + "\n").encode("utf-8") for item in emitted)

    received: List[Dict[str, Any]] = []
    errors: List[str] = []
    reader = serial_reader.SerialReader(
        port="SIM",
        transport_factory=lambda: FakeTransport(payload, chunk_size=17, loop=True),
        on_sample=received.append,
        on_error=errors.append,
        idle_timeout=0.0,
    )
    CK.check(reader.start() is True, "仿真传输层启动成功")
    deadline = time.monotonic() + 2.0
    while len(received) < 120 and time.monotonic() < deadline:
        time.sleep(0.01)
    CK.check(len(received) >= 120, "1 秒内收到 >= 120 个样本", str(len(received)))
    CK.check(all("angle_raw" in s and "angle" in s for s in received), "回调样本含 angle/angle_raw")
    CK.check(all(0.0 <= s["angle"] <= 180.0 for s in received), "回调平滑角度均在 0..180")
    latest = reader.latest()
    CK.check(latest is not None and latest == received[-1], "latest() 返回最近样本（副本）")

    started = time.monotonic()
    stopped = reader.stop(timeout=2.0)
    stop_latency = time.monotonic() - started
    CK.check(stopped and stop_latency < 1.0, "stop() 立即返回", "%.3fs" % stop_latency)
    CK.check(reader.is_running is False, "stop() 后线程已退出")
    stats = reader.stats()
    CK.check(stats["valid"] >= 120 and stats["callback_errors"] == 0,
             "统计有效样本与 0 回调异常", str(stats))
    CK.check(stats["discarded"] >= 1, "仿真流中的脏数据被计数丢弃", str(stats["discarded"]))
    CK.check(stats["framer_dropped"] >= 1, "仿真流中的超长行被分帧丢弃", str(stats["framer_dropped"]))
    CK.check(stats["comments"] >= 1, "仿真流中的注释行单独计数（不计失败）", str(stats["comments"]))
    SUMMARY["reader_stats"] = stats

    # 断开重连：读到 3 次后抛异常，应触发退避重连
    attempts = {"n": 0}

    def flaky_factory() -> FakeTransport:
        attempts["n"] += 1
        return FakeTransport(payload, chunk_size=64, loop=False, disconnect_after=3)

    reconnect_errors: List[str] = []
    reader2 = serial_reader.SerialReader(
        port="SIM",
        transport_factory=flaky_factory,
        on_error=reconnect_errors.append,
        idle_timeout=0.0,
    )
    CK.check(reader2.start() is True, "断线重连测试启动")
    time.sleep(1.2)
    started = time.monotonic()
    stopped2 = reader2.stop(timeout=2.0)
    stop_latency2 = time.monotonic() - started
    stats2 = reader2.stats()
    CK.check(stats2["connect_attempts"] >= 2, "断线后自动重连（connect_attempts>=2）", str(stats2))
    CK.check(stats2["reconnects"] >= 1, "reconnects>=1", str(stats2))
    CK.check(len(reconnect_errors) >= 1 and "重试" in reconnect_errors[0],
             "on_error 收到可读重连提示", str(reconnect_errors[:1]))
    CK.check(stopped2 and stop_latency2 < 1.0, "退避等待可被 stop() 立即打断",
             "%.3fs" % stop_latency2)
    SUMMARY["reconnect_stats"] = stats2

    # 无 pyserial 时的优雅降级
    has_pyserial = importlib.util.find_spec("serial") is not None
    reader3 = serial_reader.SerialReader(port="COM_NOT_EXIST_999")
    started_ok = reader3.start()
    if has_pyserial:
        CK.check(started_ok in (True, False), "pyserial 已安装：start() 返回布尔")
        reader3.stop()
    else:
        CK.check(started_ok is False, "无 pyserial 时 start() 返回 False（不崩溃）")
        CK.check("pyserial" in (reader3.last_error or ""), "错误信息可读且提示 pip install",
                 repr(reader3.last_error))
        CK.check(reader3.is_running is False, "无 pyserial 时不启动线程")
        try:
            serial_reader.list_serial_ports()
            raise AssertionError("未安装 pyserial 时 list_serial_ports 应抛 SerialBackendUnavailable")
        except serial_reader.SerialBackendUnavailable as exc:
            CK.check("pyserial" in str(exc), "list_serial_ports 抛出可读异常", str(exc))
    SUMMARY["pyserial_installed"] = has_pyserial


# ---------------------------------------------------------------------------
# 7. 玻璃覆盖层（结构体 ABI 与映射，默认不创建窗口）
# ---------------------------------------------------------------------------

def test_glass_overlay() -> None:
    section("7. GlassOverlay（结构体 ABI / 浓淡映射 / 降级）")

    policy = glass_overlay.ACCENT_POLICY
    CK.check(ctypes.sizeof(policy) == 16, "sizeof(ACCENT_POLICY) == 16", str(ctypes.sizeof(policy)))
    CK.check(policy.AccentState.offset == 0 and policy.AccentFlags.offset == 4,
             "字段顺序 AccentState(0), AccentFlags(4)")
    CK.check(policy.GradientColor.offset == 8 and policy.AnimationId.offset == 12,
             "字段顺序 GradientColor(8), AnimationId(12)")
    data = glass_overlay.WINCOMPATTRDATA
    CK.check(data.Attribute.offset == 0, "WINCOMPATTRDATA.Attribute 在偏移 0")
    CK.check(data.Data.offset == ctypes.sizeof(ctypes.c_void_p),
             "WINCOMPATTRDATA.Data 对齐到指针边界", str(data.Data.offset))
    CK.check(data.SizeOfData.offset == data.Data.offset + ctypes.sizeof(ctypes.c_void_p),
             "WINCOMPATTRDATA.SizeOfData 紧随 Data")
    CK.check(ctypes.sizeof(data) == data.SizeOfData.offset + ctypes.sizeof(ctypes.c_size_t),
             "sizeof(WINCOMPATTRDATA) 与字段布局一致", str(ctypes.sizeof(data)))
    CK.check(glass_overlay.WCA_ACCENT_POLICY == 19, "Attribute = 19 (WCA_ACCENT_POLICY)")

    CK.check(glass_overlay.make_gradient_color((0x11, 0x22, 0x33), 0xAA) == 0xAA332211,
             "GradientColor 为 ABGR 打包 0xAABBGGRR",
             hex(glass_overlay.make_gradient_color((0x11, 0x22, 0x33), 0xAA)))
    CK.check(glass_overlay.make_gradient_color((0, 0, 0), 0) == 0, "alpha=0 -> 全透明")
    CK.check(glass_overlay.make_gradient_color((255, 255, 255), 255) == 0xFFFFFFFF, "alpha=255 -> 满")

    lo = glass_overlay.ACRYLIC_ALPHA_MIN
    hi = glass_overlay.ACRYLIC_ALPHA_MAX
    CK.check(glass_overlay.angle_to_alpha(0.0) == lo, "0° -> 最弱模糊 alpha_min", str(lo))
    CK.check(glass_overlay.angle_to_alpha(180.0) == hi, "180° -> 最强模糊 alpha_max", str(hi))
    CK.check(glass_overlay.angle_to_alpha(90.0) == int(round((lo + hi) / 2.0)), "90° -> 线性中值")
    CK.check(glass_overlay.angle_to_alpha(-30.0) == lo and glass_overlay.angle_to_alpha(999.0) == hi,
             "越界角度夹紧")
    CK.check(glass_overlay.angle_to_alpha(None) == lo, "非法角度返回 alpha_min")
    CK.check(glass_overlay.preferred_accent_state(3) == glass_overlay.ACCENT_ENABLE_BLURBEHIND,
             "可强制 Win10 回退分支 (3)")
    CK.check(glass_overlay.preferred_accent_state(4) == glass_overlay.ACCENT_ENABLE_ACRYLICBLURBEHIND,
             "可强制 Win11 分支 (4)")
    CK.check(glass_overlay.ACCENT_ENABLE_ACRYLICBLURBEHIND == 4
             and glass_overlay.ACCENT_ENABLE_BLURBEHIND == 3, "AccentState 常量 4 / 3")

    rect = glass_overlay.default_bottom_half_rect()
    CK.check(len(rect) == 4 and rect[2] > 0 and rect[3] > 0, "下半屏区域参数化", str(rect))
    area = glass_overlay.get_work_area()
    CK.check(area is not None, "能取到工作区（%s）" % str(area))
    if area is not None:
        CK.check(rect[1] >= area[1] and rect[1] + rect[3] <= area[1] + area[3] + 1,
                 "下半屏区域落在工作区内", "%s in %s" % (rect, area))

    overlay = glass_overlay.GlassOverlay(click_through=True)
    CK.check(overlay.hwnd is None, "构造 GlassOverlay 不创建窗口（测试安全）")
    CK.check(overlay.set_angle(0.0) == lo and overlay.set_angle(180.0) == hi,
             "set_angle 线性映射到 alpha")
    CK.check(overlay.last_alpha == hi, "记录最近一次 alpha（无窗口也记录请求值）",
             str(overlay.last_alpha))
    CK.check(overlay.last_ok is False, "无窗口时 last_ok=False（未真正生效，不算崩溃）")
    CK.check(overlay.accent_name.startswith(("acrylic", "blur", "gradient", "transparent", "host", "state")),
             "accent_name 可读", overlay.accent_name)
    CK.check(isinstance(overlay.available, bool), "available 为布尔（不可用时安全降级）")

    report = glass_overlay.GlassOverlay.selftest(verbose=False)
    CK.check(report["sizeof_accent_policy"] == 16 and report["set_wca_available"] in (True, False),
             "selftest 报告结构体与 API 可用性", str(report["set_wca_available"]))
    SUMMARY["glass"] = {
        "is_windows": report["is_windows"],
        "build": report["windows_build"],
        "accent_state": report["accent_state"],
        "accent_name": report["accent_name"],
        "set_wca_available": report["set_wca_available"],
        "rect": rect,
    }

    # 真实建窗验证（默认跳过，避免自测时弹窗）：
    #   Windows: $env:WINDUO_TEST_WINDOW="1"; python pc/tests/test_pipeline.py
    if os.environ.get("WINDUO_TEST_WINDOW") == "1":
        real = glass_overlay.GlassOverlay(click_through=True)
        created = real.create()
        CK.check(created is True, "真实创建玻璃覆盖层窗口", str(real.reason))
        alpha = real.set_angle(120.0)
        CK.check(alpha == glass_overlay.angle_to_alpha(120.0), "真实窗口 set_angle 返回映射值")
        CK.check(real.last_ok is True,
                 "SetWindowCompositionAttribute 调用成功（返回 TRUE）",
                 "last_ok=%s accent=%s" % (real.last_ok, real.accent_name))
        CK.check(real.pump_messages() >= 0, "消息泵可调用")
        CK.check(real.destroy() is True and real.hwnd is None, "窗口已销毁")
        SUMMARY["glass"]["window_ok"] = True
    else:
        print("    [skip] 真实建窗验证（设 WINDUO_TEST_WINDOW=1 可开启）")


# ---------------------------------------------------------------------------
# 8. 着色器 uniform 适配
# ---------------------------------------------------------------------------

def test_shader_uniform() -> None:
    section("8. GL uniform 适配（三段后端 / 降级 / uniform 语义）")

    CK.check(CK.close(gl_shader_blur.angle_to_uniform(0.0), 0.0)
             and CK.close(gl_shader_blur.angle_to_uniform(180.0), 1.0)
             and CK.close(gl_shader_blur.angle_to_uniform(45.0), 0.25),
             "angle_to_uniform: 0/45/180 -> 0.0/0.25/1.0")
    CK.check(CK.close(gl_shader_blur.angle_to_uniform(None), 0.0)
             and CK.close(gl_shader_blur.angle_to_uniform(999.0), 1.0), "越界/非法输入安全")
    values = gl_shader_blur.angle_to_uniforms(90.0)
    CK.check(set(values.keys()) == set(gl_shader_blur.UNIFORM_NAMES), "返回三个 uniform",
             str(sorted(values)))
    CK.check(all(CK.close(v, 0.5) for v in values.values()), "90° -> 三个 uniform 全为 0.5")
    CK.check(gl_shader_blur.UNIFORM_NAMES == ("u_hingeAngle", "u_blurStrength", "u_glassEdge"),
             "uniform 命名固定")

    snippet = gl_shader_blur.GLASS_EDGE_FRAGMENT_SNIPPET
    CK.check(all(name in snippet for name in gl_shader_blur.UNIFORM_NAMES),
             "GLSL 片段声明了三个 uniform")
    CK.check(all(name in gl_shader_blur.UNIFORM_SEMANTICS for name in gl_shader_blur.UNIFORM_NAMES),
             "三个 uniform 均有语义说明")
    CK.check("glUniform1f" in gl_shader_blur.MINIMAL_INTEGRATION_SNIPPET
             and "setUniformValue" in gl_shader_blur.MINIMAL_INTEGRATION_SNIPPET,
             "最短路径示例同时给出裸 GL 与 PyQt6 写法")

    backends = gl_shader_blur.available_backends()
    CK.check(set(backends.keys()) == {"pyqt6", "pyopengl", "moderngl"}, "后端探测返回三项",
             str(backends))
    CK.check(gl_shader_blur.resolve_backend("none") == "none", "可强制 none 后端")

    degraded = gl_shader_blur.AngleUniformAdapter(backend="none", verbose=False)
    first = degraded.update_angle_uniform(90.0)
    CK.check(CK.close(first[gl_shader_blur.UNIFORM_HINGE_ANGLE], 0.5), "降级模式仍算出 uniform")
    CK.check(degraded.active is False, "无 program 时不写 GPU")

    # 裸 OpenGL 写入路径：注入 FakeGL，验证 glUniform1f 被正确调用
    class FakeGL:
        def __init__(self) -> None:
            self.locations = {"u_hingeAngle": 101, "u_blurStrength": 102, "u_glassEdge": 103}
            self.writes: Dict[int, float] = {}
            self.lookups: List[str] = []

        def glGetUniformLocation(self, program: Any, name: str) -> int:
            self.lookups.append(name)
            return self.locations.get(name, -1)

        def glUniform1f(self, location: int, value: float) -> None:
            self.writes[int(location)] = float(value)

    fake_gl = FakeGL()
    raw_adapter = gl_shader_blur.AngleUniformAdapter(
        program="prog", backend="pyopengl", gl_module=fake_gl, verbose=False
    )
    raw_values = raw_adapter.update_angle_uniform(45.0)
    CK.check(raw_adapter.active is True, "注入 GL 模块后 active=True")
    CK.check(fake_gl.writes.get(101) == raw_values[gl_shader_blur.UNIFORM_HINGE_ANGLE],
             "glUniform1f 写入 u_hingeAngle", str(fake_gl.writes))
    CK.check(CK.close(fake_gl.writes.get(101), 0.25), "45° -> u_hingeAngle=0.25")
    CK.check(len(fake_gl.writes) == 3 and raw_adapter.write_count == 3, "三个 uniform 各写一次")
    CK.check(fake_gl.lookups == list(gl_shader_blur.UNIFORM_NAMES), "缓存了 uniform location",
             str(fake_gl.lookups))

    # PyQt6 写入路径：QOpenGLShaderProgram.setUniformValue
    class FakeQtProgram:
        def __init__(self) -> None:
            self.calls: List[Any] = []

        def setUniformValue(self, name: Any, value: Any) -> None:
            self.calls.append((name, float(value)))

        def uniformLocation(self, name: str) -> int:
            return 7

    qt_program = FakeQtProgram()
    qt_adapter = gl_shader_blur.AngleUniformAdapter(
        program=qt_program, backend="pyqt6", verbose=False
    )
    qt_values = qt_adapter.update_angle_uniform(90.0)
    CK.check(len(qt_program.calls) == 3, "setUniformValue 被调用 3 次", str(qt_program.calls))
    hinge_call = [c for c in qt_program.calls if c[0] == gl_shader_blur.UNIFORM_HINGE_ANGLE]
    CK.check(bool(hinge_call) and CK.close(hinge_call[0][1], 0.5),
             "PyQt6 路径 u_hingeAngle=0.5", str(hinge_call))
    CK.check(CK.close(qt_values[gl_shader_blur.UNIFORM_BLUR_STRENGTH], 0.5),
             "PyQt6 路径其余 uniform 同步")
    SUMMARY["gl"] = {"backends": backends, "resolved": gl_shader_blur.resolve_backend("auto")}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def print_summary() -> None:
    pipeline = SUMMARY.get("pipeline", {})
    replay = SUMMARY.get("replay", {})
    gl_info = SUMMARY.get("gl", {})
    glass = SUMMARY.get("glass", {})

    print("\n================ WinDuo PC 端自测统计 ================")
    print("有效帧数                : %s / 200" % pipeline.get("valid"))
    print("注释行数                : %s（'#' 行静默跳过，不算失败）" % pipeline.get("comments"))
    print("非法行数                : %s（含分帧超长丢弃 1）" % pipeline.get("discarded"))
    print("解析异常数              : %s" % SUMMARY.get("parse_exceptions"))
    print("平滑前最大帧间跳变      : %.4f °" % pipeline.get("raw_max_jump", float("nan")))
    print("平滑后最大帧间跳变      : %.4f °（<= 平滑前 ✓）" % pipeline.get("smooth_max_jump", float("nan")))
    print("平滑前平均帧间跳变      : %.4f °" % pipeline.get("raw_mean_jump", float("nan")))
    print("平滑后平均帧间跳变      : %.4f °" % pipeline.get("smooth_mean_jump", float("nan")))
    print("逐帧跳变反向计数        : %s（快速跟随帧允许相等）" % pipeline.get("per_frame_violations"))
    for name, (lo, hi) in pipeline.get("uniform_range", {}).items():
        print("%-23s : %.4f .. %.4f" % (name + " 值域", lo, hi))
    print("样本文件回放            : 有效 %s / 注释 %s / 非法 %s / 分帧丢弃 %s (chunk=%s)"
          % (replay.get("valid"), replay.get("comments"), replay.get("discarded"),
             replay.get("framer_dropped"), replay.get("chunk_size")))
    print("串口线程统计            : %s" % SUMMARY.get("reader_stats"))
    print("断线重连统计            : attempts=%s reconnects=%s"
          % (SUMMARY.get("reconnect_stats", {}).get("connect_attempts"),
             SUMMARY.get("reconnect_stats", {}).get("reconnects")))
    print("pyserial 已安装         : %s" % SUMMARY.get("pyserial_installed"))
    print("Windows build / accent  : %s / %s" % (glass.get("build"), glass.get("accent_name")))
    print("SetWindowCompositionAttr: %s" % glass.get("set_wca_available"))
    print("GlassOverlay 默认区域   : %s" % (glass.get("rect"),))
    print("GL 后端可用性           : %s（选用 %s）" % (gl_info.get("backends"), gl_info.get("resolved")))
    print("检查项                  : %d 通过 / %d 失败" % (CK.passed, CK.failed))
    print("====================================================")


def main() -> int:
    print("WinDuo PC 端全链路自测  python=%s" % sys.version.split()[0])
    print("工作区: %s" % ROOT)
    started = time.monotonic()
    try:
        test_parse_line()
        test_angle_contract()
        test_comment_lines()
        test_framer()
        test_smoother()
        test_full_pipeline()
        test_replay()
        test_serial_reader()
        test_glass_overlay()
        test_shader_uniform()
    except AssertionError as exc:
        print_summary()
        print("\nFAIL: %s" % exc)
        return 1
    except Exception as exc:  # 非断言异常同样视为失败
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
