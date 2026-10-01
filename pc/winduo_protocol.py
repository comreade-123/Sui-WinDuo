# -*- coding: utf-8 -*-
"""WinDuo 上位机协议层：JSON 行解析 + 字节流分帧 + 角度指数平滑。

设计约束（与固件真实协议一致，不得更改协议本身）：
    设备每 50ms 输出一行 115200 8N1 的 JSON，形如:
        {"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}\\n
    angle  : **JSON 数字**（int/float），0.0~180.0，一位小数。
             同时向后兼容早期固件缺陷版本 {"angle":"45.2"}（字符串数字），
             能 float() 且在合法区间即接受；不可转换则丢弃。
    status : ok | calibrating | sensor_error | warming_up
    mode   : default | calibrate | debug
    扩展字段：debug 模式会多出 gyro/bias/base/sp/lp，校准中多出 progress(0-100)，
             默认忽略（保持 5 字段契约），需要时用 parse_line(raw, with_extra=True)。
    注释行：固件会输出 "# calibration done. ..." 这类非 JSON 行，
             必须**静默跳过**并单独计数（is_comment_line / ParseStats.comments），
             不能记成解析失败。

容错要求：数值整数/浮点、字符串数字、字段顺序变化、行首行尾空白、\\r\\n、
前后夹杂日志行、半行截断、超长行(>512 字节)、angle 越界 ——
任何输入都**不得抛异常**，非法一律返回 None。

依赖：仅标准库（json/math/threading/time）。numpy 为可选加速，本模块不强制使用。
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

__all__ = [
    "ANGLE_MIN",
    "ANGLE_MAX",
    "MAX_LINE_BYTES",
    "DEFAULT_ALPHA",
    "DEFAULT_FAST_JUMP_DEG",
    "VALID_STATUS",
    "VALID_MODE",
    "COMMENT_PREFIX",
    "CONTRACT_FIELDS",
    "EXTRA_FIELDS",
    "ParseStats",
    "LineFramer",
    "AngleSmoother",
    "parse_line",
    "parse_all",
    "is_comment_line",
    "smooth",
    "reset_smoother",
    "normalize_angle",
    "angle_to_uniform",
    "clamp",
]

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

ANGLE_MIN = 0.0
ANGLE_MAX = 180.0
ANGLE_SPAN = ANGLE_MAX - ANGLE_MIN

#: 单行最大字节数，超过即视为脏数据丢弃（避免串口噪声导致内存无限增长）
MAX_LINE_BYTES = 512

#: 默认 EMA 系数
DEFAULT_ALPHA = 0.25

#: 超过该角速度（度）视为“突跳”，直接快速跟随，避免机械延迟造成的拖尾
DEFAULT_FAST_JUMP_DEG = 40.0

VALID_STATUS = frozenset({"ok", "calibrating", "sensor_error", "warming_up"})
VALID_MODE = frozenset({"default", "calibrate", "debug"})

#: 固件注释行前缀（如 "# calibration done. offset=1.23"），必须静默跳过
COMMENT_PREFIX = "#"

#: parse_line 固定返回的字段（对外契约）
CONTRACT_FIELDS = ("angle", "status", "mode", "author", "ts")

#: debug / calibrate 模式下的扩展字段（默认忽略，with_extra=True 时保留）
EXTRA_FIELDS = ("gyro", "bias", "base", "sp", "lp", "progress")

#: 可选字段缺失时的兜底值（不因为缺字段就丢数据）
FALLBACK_STATUS = "unknown"
FALLBACK_MODE = "default"
FALLBACK_AUTHOR = ""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    """把 value 夹到 [low, high]，非数值返回 low。"""
    try:
        if value != value:  # NaN
            return low
        if value < low:
            return low
        if value > high:
            return high
        return value
    except Exception:
        return low


def _to_float(value: Any) -> Optional[float]:
    """把任意输入转成有限浮点数；失败或非有限值返回 None。"""
    if isinstance(value, bool):
        # bool 是 int 的子类，JSON 里 "angle": true 属于脏数据
        return None
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except Exception:
            return None
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        try:
            result = float(value.strip())
        except Exception:
            return None
        return result if math.isfinite(result) else None
    return None


def _reject_constant(name: str) -> float:
    """json.loads 的 parse_constant 钩子：拒绝 NaN / Infinity / -Infinity。"""
    raise ValueError("非法 JSON 常量: %s" % name)


def is_comment_line(raw: Union[bytes, bytearray, memoryview, str, None]) -> bool:
    """判断是否为固件注释/日志行（以 '#' 开头的非 JSON 行）。

    这类行必须静默跳过，单独计入 ParseStats.comments，而不是解析失败。
    任何输入都不抛异常。
    """
    try:
        if raw is None:
            return False
        if isinstance(raw, (bytes, bytearray, memoryview)):
            try:
                text = bytes(raw).decode("utf-8", "replace")
            except Exception:
                return False
        elif isinstance(raw, str):
            text = raw
        else:
            return False
        stripped = text.strip()
        return stripped.startswith(COMMENT_PREFIX)
    except Exception:
        return False


def _extract_angle(value: Any) -> Tuple[float, bool]:
    """提取角度：**number 优先**，并向后兼容旧固件的字符串数字形式。

    返回 (angle, ok)：
        - ok=True  : angle 为 0.0~180.0 的有限浮点，可安全使用；
        - ok=False : 必须丢弃（bool / 不可转换的字符串 / 越界 / NaN / 其他类型）。

    主路径（当前固件）：value 是真正的 JSON 数字 -> isinstance(v, (int, float))
                        且 not isinstance(v, bool)，再校验 0.0 <= v <= 180.0。
    兼容路径（旧固件缺陷版 {"angle":"45.2"}）：字符串能 float() 且落在合法区间即接受；
                        字符串不可转换（如 "abc"）返回 ok=False。
    """
    # --- 主路径：真正的 JSON 数字 ---
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except Exception:
            return ANGLE_MIN, False
        if not math.isfinite(number) or number < ANGLE_MIN or number > ANGLE_MAX:
            return ANGLE_MIN, False
        return (0.0 if number == 0.0 else number), True

    # --- 兼容路径：字符串数字（旧固件）---
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ANGLE_MIN, False
        try:
            number = float(text)
        except Exception:
            return ANGLE_MIN, False
        if not math.isfinite(number) or number < ANGLE_MIN or number > ANGLE_MAX:
            return ANGLE_MIN, False
        return (0.0 if number == 0.0 else number), True

    # bool / None / 数组 / 对象 / 其他：一律丢弃
    return ANGLE_MIN, False


def _normalize_enum(value: Any, allowed: frozenset, fallback: str) -> str:
    """把枚举字段规整为小写字符串；非法值退化为 fallback，不抛异常。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in allowed:
            return text
        # 允许作者名等带空格的情况，只对已知枚举做严格匹配
        return fallback if text else fallback
    if value is None:
        return fallback
    try:
        text = str(value).strip().lower()
    except Exception:
        return fallback
    return text if text in allowed else fallback


def _normalize_text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return fallback
    try:
        return str(value).strip()
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# 解析统计
# ---------------------------------------------------------------------------

class ParseStats:
    """解析统计（只用于观测，不影响解析行为）。

    统计口径（三者互斥）：
        有效帧数 valid   +  注释行数 comments  +  非法行数 discarded  +  分帧丢弃 framer_dropped
    """

    __slots__ = ("total", "valid", "comments", "discarded", "framer_dropped", "errors")

    def __init__(self) -> None:
        self.total = 0            # 送入解析的原始块/行数
        self.valid = 0            # 成功解析出的样本数（有效帧）
        self.comments = 0         # '#' 开头的注释/日志行（静默跳过，不算失败）
        self.discarded = 0        # 非法行数（非 JSON/截断/越界/空行）
        self.framer_dropped = 0   # 分帧阶段丢弃的超长行数
        self.errors = 0           # 解析过程中被兜住的异常数（应为 0）

    def as_dict(self) -> Dict[str, int]:
        return {
            "total": self.total,
            "valid": self.valid,
            "comments": self.comments,
            "discarded": self.discarded,
            "framer_dropped": self.framer_dropped,
            "errors": self.errors,
        }

    def __str__(self) -> str:
        return (
            "输入块=%d 有效帧=%d 注释行=%d 非法行=%d 超长丢弃=%d 异常兜底=%d"
            % (self.total, self.valid, self.comments, self.discarded,
               self.framer_dropped, self.errors)
        )

    __repr__ = __str__


# ---------------------------------------------------------------------------
# 逐行解析
# ---------------------------------------------------------------------------

def parse_line(
    raw: Union[bytes, bytearray, memoryview, str, None],
    now: Optional[float] = None,
    max_line_bytes: int = MAX_LINE_BYTES,
    with_extra: bool = False,
) -> Optional[Dict[str, Any]]:
    """解析一行设备输出。

    参数:
        raw:  一行原始数据（bytes 或 str），可带 \\r\\n 与首尾空白，也允许前后夹杂日志。
        now:  时间戳来源；None 时优先用 JSON 内的 "ts"，否则用 time.time()。
        max_line_bytes: 超过该字节数直接丢弃。
        with_extra: True 时额外返回 "extra" 字典，保留 debug/calibrate 的扩展字段
                    （gyro/bias/base/sp/lp/progress）。默认 False，严格保持 5 字段契约。

    返回:
        成功 -> {"angle": float(0..180), "status": str, "mode": str, "author": str, "ts": float}
                （with_extra=True 时多一个 "extra" 键）
        失败 -> None（任何异常都被吞掉，调用方无需 try/except）
        注释行（'#' 开头）-> None；请用 is_comment_line() 单独计数，勿记为失败。
    """
    try:
        if raw is None:
            return None

        # 1) 归一化为 str，并做超长检查
        if isinstance(raw, (bytes, bytearray, memoryview)):
            try:
                data = bytes(raw)
            except Exception:
                return None
            if len(data) > max_line_bytes:
                return None
            try:
                text = data.decode("utf-8", "replace")
            except Exception:
                return None
        elif isinstance(raw, str):
            text = raw
            if len(text.encode("utf-8", "replace")) > max_line_bytes:
                return None
        else:
            return None

        # 2) 去掉 \r\n（只取第一行，防止多行粘连）
        text = text.strip()
        if not text:
            return None
        if "\n" in text or "\r" in text:
            parts = text.replace("\r", "\n").split("\n")
            text = next((p.strip() for p in parts if p.strip()), "")
            if not text:
                return None

        # 2b) 注释行（固件 "# calibration done. ..."）不是帧，直接跳过
        if text.startswith(COMMENT_PREFIX):
            return None

        # 3) 定位 JSON 主体：容忍前后夹杂日志行
        if text.startswith("{") and text.endswith("}"):
            candidate = text
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return None
            candidate = text[start:end + 1]

        # 4) 解析 JSON（拒绝 NaN/Infinity）
        try:
            obj = json.loads(candidate, parse_constant=_reject_constant)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None

        # 5) 必需字段 angle：number 优先，字符串数字向后兼容
        if "angle" not in obj:
            return None
        angle, ok = _extract_angle(obj.get("angle"))
        if not ok:
            return None

        # 6) 时间戳
        ts: Optional[float] = None
        if now is not None:
            ts = _to_float(now)
        if ts is None:
            ts = _to_float(obj.get("ts"))
        if ts is None:
            ts = time.time()

        result: Dict[str, Any] = {
            "angle": angle,
            "status": _normalize_enum(obj.get("status"), VALID_STATUS, FALLBACK_STATUS),
            "mode": _normalize_enum(obj.get("mode"), VALID_MODE, FALLBACK_MODE),
            "author": _normalize_text(obj.get("author"), FALLBACK_AUTHOR),
            "ts": ts,
        }

        # 7) 可选：保留 debug/calibrate 扩展字段（progress 归一化到 0..100）
        if with_extra:
            extra: Dict[str, Any] = {
                key: value for key, value in obj.items() if key not in CONTRACT_FIELDS
            }
            if "progress" in extra:
                progress = _to_float(extra["progress"])
                if progress is None:
                    extra.pop("progress", None)
                else:
                    extra["progress"] = clamp(progress, 0.0, 100.0)
            result["extra"] = extra

        return result
    except Exception:
        # 兜底：协议层绝不向调用方抛异常
        return None


def parse_all(
    chunks: Iterable[Union[bytes, str]],
    framer: Optional["LineFramer"] = None,
    max_line_bytes: int = MAX_LINE_BYTES,
    on_sample: Optional[Callable[[Dict[str, Any]], None]] = None,
    with_extra: bool = False,
) -> Tuple[List[Dict[str, Any]], ParseStats]:
    """把任意字节块/整行序列全量解析为样本列表。

    - 传入 bytes（原始串口块）时按 \\n 分帧；
    - 传入 str 且不含 \\n 时按整行处理；
    - '#' 注释行静默跳过并单独计数（stats.comments），不计入非法行；
    - 全程不抛异常，统计信息见 ParseStats。
    """
    stats = ParseStats()
    samples: List[Dict[str, Any]] = []
    fr = framer if framer is not None else LineFramer(max_line_bytes)

    try:
        iterator = iter(chunks)
    except Exception:
        stats.errors += 1
        return samples, stats

    while True:
        try:
            raw = next(iterator)
        except StopIteration:
            break
        except Exception:
            stats.errors += 1
            break

        stats.total += 1
        try:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                candidates = fr.feed(raw)
            elif isinstance(raw, str):
                candidates = fr.feed(raw) if ("\n" in raw or "\r" in raw) else [raw]
            else:
                candidates = []
        except Exception:
            stats.errors += 1
            candidates = []

        for line in candidates:
            if is_comment_line(line):
                stats.comments += 1
                continue
            sample = parse_line(line, max_line_bytes=max_line_bytes, with_extra=with_extra)
            if sample is None:
                stats.discarded += 1
                continue
            stats.valid += 1
            samples.append(sample)
            if on_sample is not None:
                try:
                    on_sample(sample)
                except Exception:
                    stats.errors += 1

    stats.framer_dropped = fr.dropped_lines
    return samples, stats


# ---------------------------------------------------------------------------
# 字节流分帧（半行 / 粘包 / 超长行）
# ---------------------------------------------------------------------------

class LineFramer:
    """把串口字节流切分为完整文本行。

    - 半行（未收到 \\n）留在内部缓冲区，等下个 chunk 拼起来；
    - 粘包（一次 read 返回多行）全部切出；
    - 缓冲超过 max_line_bytes 仍无换行 -> 判定为脏数据，丢弃**整行**（含后续到达的尾巴，
      直到遇到下一个 \\n 为止），保证结果与分块大小无关。
    """

    __slots__ = ("_buf", "_dropping", "max_line_bytes", "encoding", "dropped_lines", "frames")

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES, encoding: str = "utf-8") -> None:
        self._buf = bytearray()
        self._dropping = False  # 已判定超长行，正在丢弃其剩余部分
        self.max_line_bytes = int(max_line_bytes)
        self.encoding = encoding
        self.dropped_lines = 0
        self.frames = 0

    def feed(self, chunk: Union[bytes, bytearray, memoryview, str, None]) -> List[str]:
        """喂入一段数据，返回本次能切出的完整行（已去掉 \\r，未去首尾空白）。"""
        if not chunk:
            return []
        out: List[str] = []
        try:
            if isinstance(chunk, str):
                data = chunk.encode(self.encoding, "replace")
            elif isinstance(chunk, (bytes, bytearray, memoryview)):
                data = bytes(chunk)
            else:
                return []
            self._buf.extend(data)
        except Exception:
            return out

        while True:
            idx = self._buf.find(0x0A)  # \n
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[:idx + 1]
            if self._dropping:
                # 这是超长行的尾巴，整行丢弃且不重复计数
                self._dropping = False
                continue
            if len(line) > self.max_line_bytes:
                self.dropped_lines += 1
                continue
            self.frames += 1
            out.append(line.decode(self.encoding, "replace").rstrip("\r"))

        if self._dropping:
            # 丢弃态下继续清理缓冲，避免无换行脏数据把内存撑爆（不重复计数）
            if len(self._buf) > self.max_line_bytes:
                self._buf.clear()
        elif len(self._buf) > self.max_line_bytes:
            # 无换行的超长脏数据：丢弃头部并进入丢弃态，直到遇到下一个 \n
            self.dropped_lines += 1
            self._dropping = True
            self._buf.clear()

        return out

    def flush(self, force: bool = False) -> List[str]:
        """取出缓冲区里的残留半行（force=True 时即使没有 \\n 也返回）。"""
        if not self._buf or self._dropping:
            return []
        if not force:
            return []
        line = bytes(self._buf)
        self._buf.clear()
        if len(line) > self.max_line_bytes:
            self.dropped_lines += 1
            return []
        self.frames += 1
        return [line.decode(self.encoding, "replace").rstrip("\r")]

    def reset(self) -> None:
        self._buf.clear()
        self._dropping = False
        self.dropped_lines = 0
        self.frames = 0

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


# ---------------------------------------------------------------------------
# 角度平滑
# ---------------------------------------------------------------------------

class AngleSmoother:
    """角度指数平滑（EMA），带突跳快速跟随与可选死区。

    行为:
        - 首个有效样本直接作为初值；
        - |delta| > fast_jump_deg 时按 fast_alpha（默认 1.0 = 立即跟随）更新，避免拖尾；
        - deadband > 0 且 |delta| < deadband 时保持不动（滞回，抑制抖动）；
        - 其余情况 value += alpha * (angle - value)。

    无效输入（None/非数/越界）不抛异常，返回上一次有效输出；
    若从未收到有效样本，则返回 ANGLE_MIN(0.0) 作为占位。
    """

    __slots__ = (
        "alpha", "deadband", "fast_jump_deg", "fast_alpha",
        "_value", "samples", "invalid_inputs", "fast_follows", "deadband_holds",
    )

    def __init__(
        self,
        alpha: float = DEFAULT_ALPHA,
        deadband: float = 0.0,
        fast_jump_deg: float = DEFAULT_FAST_JUMP_DEG,
        fast_alpha: float = 1.0,
    ) -> None:
        alpha_value = _to_float(alpha)
        if alpha_value is None or not (0.0 < alpha_value <= 1.0):
            raise ValueError("alpha 必须在 (0, 1] 区间内，当前为 %r" % (alpha,))
        fast_alpha_value = _to_float(fast_alpha)
        if fast_alpha_value is None or not (0.0 < fast_alpha_value <= 1.0):
            raise ValueError("fast_alpha 必须在 (0, 1] 区间内，当前为 %r" % (fast_alpha,))
        fast_jump_value = _to_float(fast_jump_deg)
        deadband_value = _to_float(deadband)

        self.alpha = alpha_value
        self.deadband = max(0.0, deadband_value if deadband_value is not None else 0.0)
        self.fast_jump_deg = fast_jump_value if fast_jump_value is not None else DEFAULT_FAST_JUMP_DEG
        self.fast_alpha = fast_alpha_value

        self._value: Optional[float] = None
        self.samples = 0
        self.invalid_inputs = 0
        self.fast_follows = 0
        self.deadband_holds = 0

    # -- 属性 ---------------------------------------------------------------

    @property
    def value(self) -> float:
        """当前平滑值（无样本时为 ANGLE_MIN）。"""
        return ANGLE_MIN if self._value is None else self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    @property
    def normalized(self) -> float:
        """当前平滑值归一化到 0..1。"""
        return normalize_angle(self.value)

    # -- 主流程 -------------------------------------------------------------

    def update(self, angle: Any) -> float:
        """喂入一个原始角度，返回平滑后的角度。"""
        value = _to_float(angle)
        if value is None or value < ANGLE_MIN or value > ANGLE_MAX:
            self.invalid_inputs += 1
            return self.value

        self.samples += 1
        if self._value is None:
            self._value = value
            return self._value

        delta = value - self._value
        if abs(delta) > self.fast_jump_deg:
            # 突跳：快速跟随（默认直接到位），避免拖尾
            self._value += self.fast_alpha * delta
            self.fast_follows += 1
        elif self.deadband > 0.0 and abs(delta) < self.deadband:
            # 死区：保持不动，抑制机械抖动
            self.deadband_holds += 1
        else:
            self._value += self.alpha * delta

        # 数值安全：保持始终落在合法值域
        if self._value < ANGLE_MIN:
            self._value = ANGLE_MIN
        elif self._value > ANGLE_MAX:
            self._value = ANGLE_MAX
        return self._value

    def reset(self, value: Optional[float] = None) -> None:
        """清空状态；给定 value 时作为新初值。"""
        initial = _to_float(value)
        self._value = None if initial is None else clamp(initial, ANGLE_MIN, ANGLE_MAX)
        self.samples = 0
        self.invalid_inputs = 0
        self.fast_follows = 0
        self.deadband_holds = 0

    def __call__(self, angle: Any) -> float:
        return self.update(angle)


#: 模块级默认平滑器缓存：key = (alpha, deadband, fast_jump_deg, fast_alpha)
_SMOOTHERS: Dict[Tuple[float, float, float, float], AngleSmoother] = {}
_SMOOTHERS_LOCK = threading.Lock()


def smooth(
    angle: Any,
    alpha: Optional[float] = None,
    deadband: Optional[float] = None,
    fast_jump_deg: Optional[float] = None,
    fast_alpha: Optional[float] = None,
) -> float:
    """模块级便捷接口：EMA 平滑，alpha 默认 0.25。

    smooth(45.0) 使用默认参数的共享平滑器（有状态）；
    显式传入参数时会按参数组合缓存一个独立平滑器，同样有状态。
    """
    key = (
        DEFAULT_ALPHA if alpha is None else float(alpha),
        0.0 if deadband is None else float(deadband),
        DEFAULT_FAST_JUMP_DEG if fast_jump_deg is None else float(fast_jump_deg),
        1.0 if fast_alpha is None else float(fast_alpha),
    )
    with _SMOOTHERS_LOCK:
        smoother = _SMOOTHERS.get(key)
        if smoother is None:
            smoother = AngleSmoother(
                alpha=key[0], deadband=key[1], fast_jump_deg=key[2], fast_alpha=key[3]
            )
            _SMOOTHERS[key] = smoother
    return smoother.update(angle)


def reset_smoother() -> None:
    """重置所有模块级共享平滑器（测试/重连时用）。"""
    with _SMOOTHERS_LOCK:
        for smoother in _SMOOTHERS.values():
            smoother.reset()


# ---------------------------------------------------------------------------
# 归一化 / uniform 映射
# ---------------------------------------------------------------------------

def normalize_angle(angle: Any) -> float:
    """把 0..180 角度线性归一化到 0..1（越界夹紧，非法输入返回 0.0）。"""
    value = _to_float(angle)
    if value is None:
        return 0.0
    return clamp((value - ANGLE_MIN) / ANGLE_SPAN, 0.0, 1.0)


def angle_to_uniform(angle: Any) -> float:
    """角度 -> 着色器 uniform 值 0..1（= angle / 180.0）。"""
    value = _to_float(angle)
    if value is None:
        return 0.0
    return clamp(value / ANGLE_MAX, 0.0, 1.0)
