# -*- coding: utf-8 -*-
"""WinDuo 零依赖串口读取：ctypes 直接调 kernel32，**不需要 pyserial / pywin32 / 任何第三方库**。

适用场景：完全无网的机器（pip 装不了任何东西），但板子已经在 COM 口上以
115200 8N1 输出固件 JSON 行。只用 Python 标准库 ctypes + winreg。

用到的 Win32 API（全部来自 kernel32）:
    CreateFileW          打开 \\\\.\\COMx
    SetCommState         配置 8N1 / 波特率（DCB 结构体）
    SetCommTimeouts      设置短超时轮询（ReadFile 最多阻塞 ~100ms）
    PurgeComm            清空收发缓冲
    ReadFile             非重叠读取（lpOverlapped = NULL）
    ClearCommError       查询接收队列/驱动错误
    CloseHandle          关闭句柄

接口与 pc/serial_reader.py 的 SerialReader 保持一致（可以直接替换）:
    reader = WinSerialReader(port="COM3", on_sample=cb, on_error=cb2)
    reader.start() / reader.stop() / reader.latest() / reader.stats()
    reader.is_running / reader.is_connected / reader.last_error
内部复用 winduo_protocol 的 LineFramer + parse_line + AngleSmoother。

注意事项:
    * 默认 **不拉高 DTR/RTS**（fDtrControl/fRtsControl = DISABLE）。这一点很重要：
      很多 Arduino 板子（Uno/Nano）的 DTR 自动复位电路会在串口被打开时复位板子，
      pure monitor 不应该打断用户正在跑的设备。需要时用 dtr_enable/rts_enable 打开。
    * stop() 不跨线程关闭句柄（避免句柄重用竞态），而是等 ReadFile 的超时自然返回，
      因此 stop() 的延迟上限约为 read_timeout_ms（默认 100ms）。

命令行:
    python pc/serial_reader_win.py --list
    python pc/serial_reader_win.py --port COM3 --seconds 5
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

try:  # 直接以脚本方式运行（pc/ 在 sys.path 上）
    from winduo_protocol import (
        MAX_LINE_BYTES,
        AngleSmoother,
        LineFramer,
        is_comment_line,
        parse_line,
    )
except ImportError:  # 作为包导入
    from .winduo_protocol import (  # type: ignore
        MAX_LINE_BYTES,
        AngleSmoother,
        LineFramer,
        is_comment_line,
        parse_line,
    )

__all__ = [
    "IS_WINDOWS",
    "WinSerialError",
    "DCB",
    "COMMTIMEOUTS",
    "COMSTAT",
    "WinSerialPort",
    "WinSerialReader",
    "list_com_ports",
    "list_com_port_details",
    "format_win_error",
    "make_dcb",
    "backoff_delay",
    "DEFAULT_BAUDRATE",
    "BACKOFF_MIN",
    "BACKOFF_MAX",
]

IS_WINDOWS = sys.platform.startswith("win")

DEFAULT_BAUDRATE = 115200
BACKOFF_MIN = 0.5
BACKOFF_MAX = 5.0

# --- CreateFileW 参数 ------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_OVERLAPPED = 0x40000000

# --- DCB ---------------------------------------------------------------
NOPARITY = 0
ODDPARITY = 1
EVENPARITY = 2
ONESTOPBIT = 0
ONE5STOPBITS = 1
TWOSTOPBITS = 2

#: DCB.Flags 的位定义（对应 C 里的位域，按位拼装）
DCB_FLAG_FBINARY = 0x0001
DCB_FLAG_FPARITY = 0x0002
DCB_FLAG_FOUTXCTSFLOW = 0x0004
DCB_FLAG_FOUTXDSRFLOW = 0x0008
DCB_FLAG_DTR_ENABLE = 0x0010        # fDtrControl = DTR_CONTROL_ENABLE
DCB_FLAG_FDSRSENSITIVITY = 0x0040
DCB_FLAG_FTXCONTINUEONXOFF = 0x0080
DCB_FLAG_FOUTX = 0x0100
DCB_FLAG_FINX = 0x0200
DCB_FLAG_FERRORCHAR = 0x0400
DCB_FLAG_FNULL = 0x0800
DCB_FLAG_RTS_ENABLE = 0x1000        # fRtsControl = RTS_CONTROL_ENABLE
DCB_FLAG_FABORTONERROR = 0x4000

# --- PurgeComm -------------------------------------------------------------
PURGE_TXABORT = 0x0001
PURGE_RXABORT = 0x0002
PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008

# --- 常见 Win32 错误码 -----------------------------------------------------
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_BAD_COMMAND = 22
ERROR_GEN_FAILURE = 31
ERROR_SHARING_VIOLATION = 32
ERROR_INVALID_PARAMETER = 87
ERROR_SEM_TIMEOUT = 121
ERROR_OPERATION_ABORTED = 995
ERROR_DEVICE_NOT_CONNECTED = 1167

#: (HANDLE)-1，CreateFileW 失败时的返回值
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value if ctypes.sizeof(ctypes.c_void_p) else 0xFFFFFFFFFFFFFFFF

#: 属于「需要用户处理」的错误码：端口不存在/被占用/参数不对，重试多少次都没用
FATAL_PORT_CODES = frozenset({
    ERROR_FILE_NOT_FOUND,
    ERROR_ACCESS_DENIED,
    ERROR_SHARING_VIOLATION,
    ERROR_INVALID_PARAMETER,
})

_ERROR_TEXT = {
    ERROR_FILE_NOT_FOUND: "串口不存在（设备没插上，或端口号写错了）",
    ERROR_ACCESS_DENIED: "串口被占用（串口助手 / Arduino IDE 串口监视器 / 本项目另一个进程已打开），请先关闭占用它的程序",
    ERROR_SHARING_VIOLATION: "串口被其他进程独占（共享冲突），请关闭占用它的程序",
    ERROR_INVALID_HANDLE: "句柄无效（串口已被关闭）",
    ERROR_BAD_COMMAND: "设备不响应（USB 可能已拔出）",
    ERROR_GEN_FAILURE: "设备故障（USB 可能已拔出或驱动异常）",
    ERROR_DEVICE_NOT_CONNECTED: "设备未连接（USB 已拔出）",
    ERROR_INVALID_PARAMETER: "参数无效（波特率或串口名不被该驱动接受）",
    ERROR_SEM_TIMEOUT: "等待超时（当前没有数据，属正常情况）",
    ERROR_OPERATION_ABORTED: "读操作被中断（设备可能已拔出）",
}


class WinSerialError(RuntimeError):
    """串口操作失败；消息为可读中文，code 为 Win32 错误码（无则 0）。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)


# ---------------------------------------------------------------------------
# 结构体（字段顺序即 ABI，勿改）
# ---------------------------------------------------------------------------

class DCB(ctypes.Structure):
    """串口配置块（对应 Windows DCB，x64 下 sizeof == 28）。

    C 里 Flags 是位域（fBinary/fParity/fDtrControl/fRtsControl...），
    这里用一个 DWORD 按位表示，位定义见 DCB_FLAG_*。
    """

    _fields_ = [
        ("DCBlength", ctypes.c_uint32),   # 必须 = sizeof(DCB)
        ("BaudRate", ctypes.c_uint32),    # 115200
        ("Flags", ctypes.c_uint32),       # 位域打包
        ("wReserved", ctypes.c_uint16),
        ("XonLim", ctypes.c_uint16),
        ("XoffLim", ctypes.c_uint16),
        ("ByteSize", ctypes.c_ubyte),     # 8
        ("Parity", ctypes.c_ubyte),       # NOPARITY = 0
        ("StopBits", ctypes.c_ubyte),     # ONESTOPBIT = 0
        ("XonChar", ctypes.c_ubyte),
        ("XoffChar", ctypes.c_ubyte),
        ("ErrorChar", ctypes.c_ubyte),
        ("EofChar", ctypes.c_ubyte),
        ("EvtChar", ctypes.c_ubyte),
        ("wReserved1", ctypes.c_uint16),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    """读写超时（毫秒）。"""

    _fields_ = [
        ("ReadIntervalTimeout", ctypes.c_uint32),
        ("ReadTotalTimeoutMultiplier", ctypes.c_uint32),
        ("ReadTotalTimeoutConstant", ctypes.c_uint32),
        ("WriteTotalTimeoutMultiplier", ctypes.c_uint32),
        ("WriteTotalTimeoutConstant", ctypes.c_uint32),
    ]


class COMSTAT(ctypes.Structure):
    """串口状态（cbInQue = 接收缓冲区里的字节数）。"""

    _fields_ = [
        ("Flags", ctypes.c_uint32),   # 位域：fCtsHold/fDsrHold/fRlsdHold/.../fEof
        ("cbInQue", ctypes.c_uint32),
        ("cbOutQue", ctypes.c_uint32),
    ]


def make_dcb(
    baudrate: int = DEFAULT_BAUDRATE,
    bytesize: int = 8,
    parity: int = NOPARITY,
    stopbits: int = ONESTOPBIT,
    dtr_enable: bool = False,
    rts_enable: bool = False,
) -> DCB:
    """构造 8N1 的 DCB。默认不拉 DTR/RTS，避免复位 Arduino 板子。"""
    flags = DCB_FLAG_FBINARY  # 二进制模式必须为 1
    if dtr_enable:
        flags |= DCB_FLAG_DTR_ENABLE
    if rts_enable:
        flags |= DCB_FLAG_RTS_ENABLE
    dcb = DCB()
    dcb.DCBlength = ctypes.sizeof(DCB)
    dcb.BaudRate = int(baudrate)
    dcb.Flags = flags
    dcb.wReserved = 0
    dcb.XonLim = 0
    dcb.XoffLim = 0
    dcb.ByteSize = int(bytesize)
    dcb.Parity = int(parity)
    dcb.StopBits = int(stopbits)
    dcb.XonChar = 0
    dcb.XoffChar = 0
    dcb.ErrorChar = 0
    dcb.EofChar = 0
    dcb.EvtChar = 0
    dcb.wReserved1 = 0
    return dcb


def format_win_error(code: int) -> str:
    """Win32 错误码 -> 可读中文（未知码走系统 FormatError）。"""
    try:
        value = int(code)
    except Exception:
        value = 0
    text = _ERROR_TEXT.get(value)
    if text is None:
        fallback = ""
        if IS_WINDOWS:
            try:
                fallback = ctypes.FormatError(value).strip()
            except Exception:
                fallback = ""
        text = fallback or "未知错误"
    return "%s（Win32 错误码 %d）" % (text, value)


# ---------------------------------------------------------------------------
# kernel32 绑定
# ---------------------------------------------------------------------------

_KERNEL32: Optional[Any] = None
_BIND_ERROR: Optional[str] = None


def _load_kernel32() -> Tuple[Optional[Any], Optional[str]]:
    """绑定 kernel32 及其函数原型；返回 (kernel32, error)。"""
    global _KERNEL32, _BIND_ERROR
    if _KERNEL32 is not None or _BIND_ERROR is not None:
        return _KERNEL32, _BIND_ERROR
    if not IS_WINDOWS:
        _BIND_ERROR = "非 Windows 平台，ctypes 串口不可用（请改用 pc/serial_reader.py + pyserial）"
        return None, _BIND_ERROR
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception as exc:
        _BIND_ERROR = "加载 kernel32.dll 失败: %s" % exc
        return None, _BIND_ERROR

    # 必须显式声明原型：64 位下句柄按 c_int 传会被截断
    signatures = [
        ("CreateFileW", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_void_p),
        ("SetCommState", [ctypes.c_void_p, ctypes.POINTER(DCB)], ctypes.c_int),
        ("SetCommTimeouts", [ctypes.c_void_p, ctypes.POINTER(COMMTIMEOUTS)], ctypes.c_int),
        ("GetCommState", [ctypes.c_void_p, ctypes.POINTER(DCB)], ctypes.c_int),
        ("PurgeComm", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        ("ReadFile", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p], ctypes.c_int),
        ("WriteFile", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p], ctypes.c_int),
        ("ClearCommError", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                            ctypes.POINTER(COMSTAT)], ctypes.c_int),
        ("CloseHandle", [ctypes.c_void_p], ctypes.c_int),
        ("GetLastError", [], ctypes.c_uint32),
    ]
    for name, argtypes, restype in signatures:
        func = getattr(kernel32, name, None)
        if func is None:
            continue
        try:
            func.argtypes = argtypes
            func.restype = restype
        except Exception:
            pass

    _KERNEL32 = kernel32
    _BIND_ERROR = None
    return _KERNEL32, None


# ---------------------------------------------------------------------------
# 串口枚举（winreg，标准库）
# ---------------------------------------------------------------------------

def list_com_port_details() -> List[Dict[str, str]]:
    """枚举本机串口：{'port': 'COM3', 'device': '\\Device\\Serial20'}。"""
    if not IS_WINDOWS:
        return []
    try:
        import winreg  # 标准库
    except Exception:
        return []
    result: List[Dict[str, str]] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except Exception:
        return []
    try:
        index = 0
        while True:
            try:
                name, value, _type = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            port = str(value).strip().upper()
            if port.startswith("COM"):
                result.append({"port": port, "device": str(name)})
    except Exception:
        pass
    finally:
        try:
            winreg.CloseKey(key)
        except Exception:
            pass
    # 去重 + 按 COM 号排序（COM10 排在 COM9 之后）
    unique: Dict[str, Dict[str, str]] = {}
    for item in result:
        unique[item["port"]] = item

    def sort_key(item: Dict[str, str]) -> Tuple[int, str]:
        digits = item["port"][3:]
        return (int(digits) if digits.isdigit() else 9999, item["port"])

    return sorted(unique.values(), key=sort_key)


def list_com_ports() -> List[str]:
    """枚举本机串口名列表，如 ['COM3', 'COM5']。"""
    return [item["port"] for item in list_com_port_details()]


def backoff_delay(failures: int) -> float:
    """指数退避：第 1 次失败 0.5s，之后翻倍，封顶 5s。"""
    try:
        count = int(failures)
    except Exception:
        count = 1
    if count < 1:
        count = 1
    return min(BACKOFF_MAX, BACKOFF_MIN * (2.0 ** (count - 1)))


# ---------------------------------------------------------------------------
# 真实串口对象（API 与 pyserial 的 read(n)/close() 兼容）
# ---------------------------------------------------------------------------

class WinSerialPort:
    """用 ctypes 打开并读取一个 COM 口。

    read(n) 语义与 pyserial 一致：返回 bytes（可能是 b""，表示这一轮没有数据，
    因为设置了短超时）；close() 关闭句柄。
    """

    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = DEFAULT_BAUDRATE,
        read_timeout_ms: int = 100,
        read_interval_ms: int = 50,
        dtr_enable: bool = False,
        rts_enable: bool = False,
    ) -> None:
        self.port = str(port).strip().upper()
        self.baudrate = int(baudrate)
        self.read_timeout_ms = max(0, int(read_timeout_ms))
        self.read_interval_ms = max(0, int(read_interval_ms))
        self.dtr_enable = bool(dtr_enable)
        self.rts_enable = bool(rts_enable)
        self.handle: Optional[int] = None

    # -- 路径/属性 ---------------------------------------------------------

    @property
    def device_path(self) -> str:
        """Win32 设备路径：COM10 及以上必须用 \\\\.\\ 前缀，统一都加上最省事。"""
        name = self.port
        if name.upper().startswith("\\\\.\\"):
            return name
        return "\\\\.\\" + name

    @property
    def is_open(self) -> bool:
        return self.handle is not None

    # -- 打开/关闭 ---------------------------------------------------------

    def open(self) -> "WinSerialPort":
        """打开并配置串口；失败抛 WinSerialError（消息可读）。"""
        if self.handle is not None:
            return self
        kernel32, error = _load_kernel32()
        if kernel32 is None:
            raise WinSerialError(error or "kernel32 不可用")

        handle = kernel32.CreateFileW(
            self.device_path,
            GENERIC_READ | GENERIC_WRITE,
            0,                       # 独占：dwShareMode = 0
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,   # 非重叠 I/O（靠 COMMTIMEOUTS 短超时轮询）
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            raise WinSerialError(
                "打开 %s 失败：%s" % (self.port, format_win_error(code)), code
            )
        self.handle = int(handle)

        try:
            # 1) 8N1 + 波特率
            dcb = make_dcb(
                baudrate=self.baudrate,
                dtr_enable=self.dtr_enable,
                rts_enable=self.rts_enable,
            )
            if not kernel32.SetCommState(ctypes.c_void_p(self.handle), ctypes.byref(dcb)):
                code = ctypes.get_last_error()
                raise WinSerialError(
                    "配置 %s (baud=%d 8N1) 失败：%s"
                    % (self.port, self.baudrate, format_win_error(code)), code
                )

            # 2) 短超时轮询：ReadFile 最多阻塞 read_timeout_ms 毫秒
            timeouts = COMMTIMEOUTS()
            timeouts.ReadIntervalTimeout = self.read_interval_ms
            timeouts.ReadTotalTimeoutMultiplier = 0
            timeouts.ReadTotalTimeoutConstant = self.read_timeout_ms
            timeouts.WriteTotalTimeoutMultiplier = 0
            timeouts.WriteTotalTimeoutConstant = 200
            if not kernel32.SetCommTimeouts(ctypes.c_void_p(self.handle), ctypes.byref(timeouts)):
                code = ctypes.get_last_error()
                raise WinSerialError(
                    "设置 %s 超时失败：%s" % (self.port, format_win_error(code)), code
                )

            # 3) 清空历史缓冲，避免拿到上一次打开时的残留数据
            kernel32.PurgeComm(
                ctypes.c_void_p(self.handle),
                PURGE_RXCLEAR | PURGE_TXCLEAR | PURGE_RXABORT | PURGE_TXABORT,
            )
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """关闭句柄（幂等，不抛异常）。"""
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        kernel32, _error = _load_kernel32()
        if kernel32 is None:
            return
        try:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass

    # -- 读 ---------------------------------------------------------------

    def read(self, size: int = 4096) -> bytes:
        """读最多 size 字节；超时返回 b""（不是错误）；设备拔出抛 WinSerialError。"""
        if self.handle is None:
            raise WinSerialError("串口 %s 未打开" % self.port, ERROR_INVALID_HANDLE)
        kernel32, error = _load_kernel32()
        if kernel32 is None:
            raise WinSerialError(error or "kernel32 不可用")

        want = max(1, int(size))
        buffer = ctypes.create_string_buffer(want)
        read_bytes = ctypes.c_uint32(0)
        ok = kernel32.ReadFile(
            ctypes.c_void_p(self.handle),
            ctypes.cast(buffer, ctypes.c_void_p),
            want,
            ctypes.byref(read_bytes),
            None,  # 非重叠
        )
        if not ok:
            code = ctypes.get_last_error()
            if code in (ERROR_SEM_TIMEOUT, ERROR_OPERATION_ABORTED):
                return b""  # 超时/被取消：本轮无数据
            raise WinSerialError(
                "读取 %s 失败：%s" % (self.port, format_win_error(code)), code
            )
        count = int(read_bytes.value)
        if count <= 0:
            return b""
        return bytes(buffer.raw[:count])

    def write(self, data: Union[bytes, bytearray, str]) -> int:
        """向串口写数据，返回写入字节数。设备拔出/句柄失效抛 WinSerialError。"""
        if self.handle is None:
            raise WinSerialError("串口 %s 未打开" % self.port, ERROR_INVALID_HANDLE)
        if isinstance(data, str):
            payload = data.encode("utf-8", "replace")
        else:
            payload = bytes(data)
        if not payload:
            return 0

        kernel32, error = _load_kernel32()
        if kernel32 is None:
            raise WinSerialError(error or "kernel32 不可用")

        buffer = ctypes.create_string_buffer(payload, len(payload))
        written = ctypes.c_uint32(0)
        ok = kernel32.WriteFile(
            ctypes.c_void_p(self.handle),
            ctypes.cast(buffer, ctypes.c_void_p),
            len(payload),
            ctypes.byref(written),
            None,
        )
        if not ok:
            code = ctypes.get_last_error()
            raise WinSerialError(
                "写入 %s 失败：%s" % (self.port, format_win_error(code)), code
            )
        return int(written.value)

    def flush_input(self) -> None:
        """清空接收缓冲（打开后丢弃开机残留数据）。"""
        if self.handle is None:
            return
        kernel32, _ = _load_kernel32()
        if kernel32 is not None:
            kernel32.PurgeComm(ctypes.c_void_p(self.handle), PURGE_RXCLEAR | PURGE_RXABORT)

    def in_queue(self) -> int:
        """接收缓冲区中待读字节数（ClearCommError）。"""
        if self.handle is None:
            return 0
        kernel32, _error = _load_kernel32()
        if kernel32 is None:
            return 0
        errors = ctypes.c_uint32(0)
        status = COMSTAT()
        try:
            if not kernel32.ClearCommError(
                ctypes.c_void_p(self.handle), ctypes.byref(errors), ctypes.byref(status)
            ):
                return 0
        except Exception:
            return 0
        return int(status.cbInQue)

    def __enter__(self) -> "WinSerialPort":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 读取线程（接口与 serial_reader.SerialReader 对齐）
# ---------------------------------------------------------------------------

class WinSerialReader:
    """后台线程读取 COM 口：分帧 -> parse_line -> EMA 平滑 -> 回调。

    参数:
        port:          串口名，如 "COM3"。
        baudrate:      默认 115200，8N1。
        on_sample:     样本回调（后台线程调用），样本含 angle(平滑)/angle_raw/status/mode/author/ts。
        on_error:      错误回调（可读中文消息）。
        alpha/deadband: 传给 AngleSmoother。
        read_timeout_ms: ReadFile 的单次超时（决定 stop() 的响应粒度与 CPU 占用）。
        idle_timeout:   连接正常但连续多久没有有效数据就判定断线重连（秒，<=0 关闭）。
        transport_factory: 注入 duck typing 的假串口（只要求 read(n)/close()），
                        用于单元测试，完全不碰真实硬件。
    """

    def __init__(
        self,
        port: str = "COM3",
        baudrate: int = DEFAULT_BAUDRATE,
        on_sample: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        alpha: float = 0.25,
        deadband: float = 0.0,
        read_timeout_ms: int = 100,
        read_chunk: int = 4096,
        idle_timeout: float = 3.0,
        max_line_bytes: int = MAX_LINE_BYTES,
        dtr_enable: bool = False,
        rts_enable: bool = False,
        transport_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.port = str(port).strip().upper()
        self.baudrate = int(baudrate)
        self.read_timeout_ms = max(0, int(read_timeout_ms))
        self.read_chunk = max(64, int(read_chunk))
        self.idle_timeout = float(idle_timeout)
        self.max_line_bytes = int(max_line_bytes)
        self.dtr_enable = bool(dtr_enable)
        self.rts_enable = bool(rts_enable)
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
        self._last_error_code = 0
        self._failures = 0
        self._send_queue: List[str] = []      # 待发送命令（主线程入队，读线程发送）

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

    def probe(self) -> Tuple[bool, str]:
        """试打开一次串口（立即关闭），用于给出「占用/不存在」这类即时可读提示。

        注：默认不拉 DTR/RTS，因此不会复位 Arduino 板子。
        使用注入的假串口时直接返回成功（不碰硬件）。
        """
        if self.transport_factory is not None:
            return True, "仿真传输层（未访问真实串口）"
        port = self._new_port()
        try:
            port.open()
        except WinSerialError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "打开 %s 失败：%s: %s" % (self.port, type(exc).__name__, exc)
        finally:
            port.close()
        return True, ""

    def start(self) -> bool:
        """启动后台线程；返回 False 表示启动失败（原因见 last_error）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            kernel32, error = _load_kernel32()
            if self.transport_factory is None and kernel32 is None:
                self._fatal_error = error or "kernel32 不可用"
                self._last_error = self._fatal_error
                self._notify_error(self._fatal_error)
                return False
            self._stop_event.clear()
            self._fatal_error = None
            self._thread = threading.Thread(target=self._run, name="WinDuoWinSerial", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> bool:
        """请求停止并等待线程退出。

        不跨线程关句柄：ReadFile 的超时上限是 read_timeout_ms，
        因此线程会在 ~read_timeout_ms 内自己退出并关闭端口。
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.2, float(timeout)))
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

    @property
    def last_error_code(self) -> int:
        """最近一次失败的 Win32 错误码（0 表示没有错误码，例如纯 Python 异常）。"""
        with self._lock:
            return self._last_error_code

    @property
    def is_fatal(self) -> bool:
        """当前失败是否属于「端口不存在/被占用/参数错」这类需要用户先处理的问题。"""
        return self.last_error_code in FATAL_PORT_CODES

    # -- 数据访问 -----------------------------------------------------------

    def latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest) if self._latest is not None else None

    def wait_for_sample(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """等待新样本（或超时），返回最近样本或 None。"""
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

    # -- 内部 ---------------------------------------------------------------

    def _new_port(self) -> WinSerialPort:
        return WinSerialPort(
            port=self.port,
            baudrate=self.baudrate,
            read_timeout_ms=self.read_timeout_ms,
            dtr_enable=self.dtr_enable,
            rts_enable=self.rts_enable,
        )

    def _open_transport(self) -> Any:
        if self.transport_factory is not None:
            return self.transport_factory()
        return self._new_port().open()

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

    def _close_quietly(self, transport: Optional[Any]) -> None:
        if transport is None:
            return
        try:
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    def send_line(self, text: str, newline: bool = True) -> bool:
        """把一行命令排入发送队列，由读线程在安全时机真正写出去。

        这样调用方不必关心串口句柄的线程安全问题（句柄在重连时会被替换）。
        返回 False 表示读线程未运行，命令不会发出。
        """
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            payload = str(text)
            if newline and not payload.endswith("\n"):
                payload += "\n"
            self._send_queue.append(payload)
            self._stats["sent_lines"] = self._stats.get("sent_lines", 0) + 1
            return True

    def _flush_send_queue(self, transport: Any) -> None:
        """在读线程里发送积压的命令；失败只记错误，不中断读取。"""
        while True:
            with self._lock:
                if not self._send_queue:
                    return
                payload = self._send_queue.pop(0)
            try:
                write = getattr(transport, "write", None)
                if callable(write):
                    write(payload)
                else:
                    with self._lock:
                        self._stats["write_errors"] = self._stats.get("write_errors", 0) + 1
            except Exception as exc:
                with self._lock:
                    self._stats["write_errors"] = self._stats.get("write_errors", 0) + 1
                self._notify_error("写入 %s 失败：%s" % (self.port, exc))

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
                self._close_quietly(transport)
                with self._lock:
                    self._transport = None
                    self._connected = False

        with self._lock:
            self._connected = False

    def _pump(self, transport: Any) -> None:
        """分帧读取，直到 stop() 或断线异常。"""
        framer = LineFramer(self.max_line_bytes)
        last_data = time.monotonic()

        while not self._stop_event.is_set():
            self._flush_send_queue(transport)
            try:
                chunk = transport.read(self.read_chunk)
            except Exception as exc:
                with self._lock:
                    self._stats["read_errors"] += 1
                if isinstance(exc, WinSerialError):
                    raise
                raise WinSerialError("读取 %s 失败：%s: %s" % (self.port, type(exc).__name__, exc)) from exc

            if chunk:
                last_data = time.monotonic()
                size = len(chunk.encode("utf-8", "replace")) if isinstance(chunk, str) else len(chunk)
                with self._lock:
                    self._stats["bytes_read"] += size

                for line in framer.feed(chunk):
                    # 固件诊断行（'#' 开头）静默跳过，单独计数
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
                    sample["angle_raw"] = raw_angle
                    sample["angle"] = self._smoother.update(raw_angle)
                    with self._lock:
                        self._stats["valid"] += 1
                        self._stats["framer_dropped"] = framer.dropped_lines
                        self._failures = 0
                        self._stats["failures"] = 0
                    self._push(sample)
                continue

            # read 返回 b""：可能是短超时（正常），也可能是设备静默/掉线
            if self.idle_timeout > 0 and (time.monotonic() - last_data) > self.idle_timeout:
                with self._lock:
                    self._stats["framer_dropped"] = framer.dropped_lines
                raise WinSerialError(
                    "连续 %.1fs 未收到数据，判定设备断线，准备重连" % self.idle_timeout,
                    ERROR_SEM_TIMEOUT,
                )

    def _handle_failure(self, exc: BaseException) -> None:
        """记录错误 + 指数退避（stop() 可立即打断）。"""
        code = int(getattr(exc, "code", 0) or 0)
        with self._lock:
            self._failures += 1
            failures = self._failures
            self._stats["failures"] = failures
            self._last_error_code = code
            self._last_error = "串口异常：%s" % exc
            if isinstance(exc, WinSerialError):
                self._fatal_error = str(exc)

        delay = backoff_delay(failures)
        # 端口被占用/不存在这类问题给用户留出处理时间，退避拉满
        if code in FATAL_PORT_CODES:
            delay = BACKOFF_MAX
        message = "%s；%.1fs 后重试（第 %d 次失败）" % (self._last_error, delay, failures)
        with self._lock:
            self._last_error = message
        self._notify_error(message)
        self._stop_event.wait(delay)


# ---------------------------------------------------------------------------
# 命令行：自检 / 看数据
# ---------------------------------------------------------------------------

def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="WinDuo 零依赖串口读取（ctypes 直调 kernel32，不需要 pyserial）"
    )
    parser.add_argument("--list", action="store_true", help="列出本机可用串口")
    parser.add_argument("--port", default="COM3", help="串口名，默认 COM3")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="波特率，默认 115200")
    parser.add_argument("--seconds", type=float, default=5.0, help="持续时长（秒），0 = 一直读")
    parser.add_argument("--quiet", action="store_true", help="只打印统计，不逐帧打印")
    args = parser.parse_args(argv)

    if args.list:
        details = list_com_port_details()
        if not details:
            print("未发现串口（检查 USB 连接）")
            return 0
        for item in details:
            print("%-6s <- %s" % (item["port"], item["device"]))
        return 0

    def on_sample(sample: Dict[str, Any]) -> None:
        if args.quiet:
            return
        print("angle=%7.2f (raw=%7.2f) status=%-12s mode=%-9s %s"
              % (sample["angle"], sample["angle_raw"], sample["status"],
                 sample["mode"], sample.get("author", "")))

    def on_error(message: str) -> None:
        print("[警告] %s" % message)

    reader = WinSerialReader(port=args.port, baudrate=args.baud, on_sample=on_sample, on_error=on_error)
    ok, reason = reader.probe()
    if not ok:
        print("[错误] %s" % reason)
        ports = list_com_ports()
        if ports:
            print("当前可用串口：%s" % ", ".join(ports))
        return 2
    if not reader.start():
        print("[错误] %s" % reader.last_error)
        return 2

    print("开始读取 %s @ %d 8N1（Ctrl+C 停止）" % (reader.port, reader.baudrate))
    try:
        if args.seconds and args.seconds > 0:
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                time.sleep(0.05)
        else:
            while True:
                time.sleep(0.05)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        reader.stop()
    print("统计：%s" % reader.stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
