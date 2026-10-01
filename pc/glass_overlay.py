# -*- coding: utf-8 -*-
"""WinDuo Win32 玻璃（Acrylic / Blur）覆盖层。

实现要点（全部通过 ctypes 调用未公开 API，无需第三方库）:
    user32!SetWindowCompositionAttribute(HWND, WINCOMPATTRDATA*)

结构体字段顺序必须与 Windows 内部定义严格一致，否则调用静默失败：
    ACCENT_POLICY     { AccentState, AccentFlags, GradientColor, AnimationId }
    WINCOMPATTRDATA   { Attribute, pvData, cbData }   Attribute = 19 (WCA_ACCENT_POLICY)

AccentState 双分支:
    ACCENT_ENABLE_ACRYLICBLURBEHIND = 4  -> Windows 11 (build >= 22000) 优先，亚克力磨砂
    ACCENT_ENABLE_BLURBEHIND        = 3  -> Windows 10 回退（Win10 上 acrylic 拖窗会卡顿）

GradientColor 为 0xAABBGGRR（ABGR，注意字节序）:
    alpha 决定玻璃浓淡 —— 0x00 几乎全透明（背景完全透出，几乎看不到磨砂），
    0xFF 完全被 tint 颜色覆盖（看不到模糊）。
    因此“模糊强度”映射为 alpha：0° -> alpha_min（最淡），180° -> alpha_max（最浓），线性可配。

窗口扩展风格: WS_EX_LAYERED 必选；WS_EX_TRANSPARENT 可选（点击穿透）。

与 PyQt6 / 原 OpenGL 项目对接的最短路径：
    from glass_overlay import apply_accent_to_hwnd
    apply_accent_to_hwnd(int(self.winId()), alpha=180)   # 把 Qt 窗口变成亚克力玻璃
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from typing import Any, Dict, Optional, Sequence, Tuple

__all__ = [
    "IS_WINDOWS",
    "ACCENT_DISABLED",
    "ACCENT_ENABLE_GRADIENT",
    "ACCENT_ENABLE_TRANSPARENTGRADIENT",
    "ACCENT_ENABLE_BLURBEHIND",
    "ACCENT_ENABLE_ACRYLICBLURBEHIND",
    "WCA_ACCENT_POLICY",
    "ACCENT_POLICY",
    "WINCOMPATTRDATA",
    "ACRYLIC_ALPHA_MIN",
    "ACRYLIC_ALPHA_MAX",
    "DEFAULT_TINT",
    "detect_windows_build",
    "preferred_accent_state",
    "make_gradient_color",
    "angle_to_alpha",
    "apply_accent_to_hwnd",
    "GlassOverlay",
    "get_work_area",
    "default_bottom_half_rect",
]

IS_WINDOWS = sys.platform.startswith("win")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ACCENT_DISABLED = 0
ACCENT_ENABLE_GRADIENT = 1
ACCENT_ENABLE_TRANSPARENTGRADIENT = 2
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
ACCENT_ENABLE_HOSTBACKDROP = 5

#: SetWindowCompositionAttribute 的属性编号
WCA_ACCENT_POLICY = 19

#: Win11 起始内部版本号
WINDOWS_11_BUILD = 22000

#: 窗口扩展风格
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

#: 窗口风格
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

SPI_GETWORKAREA = 0x0030

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCCREATE = 0x0081
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1

#: 默认玻璃浓淡范围（GradientColor 的 alpha 分量）
ACRYLIC_ALPHA_MIN = 60
ACRYLIC_ALPHA_MAX = 230

#: 默认 tint 颜色（RGB），深空灰蓝，与 Win11 亚克力观感接近
DEFAULT_TINT = (0x20, 0x24, 0x2C)


# ---------------------------------------------------------------------------
# 结构体（字段顺序即 ABI，不可调整）
# ---------------------------------------------------------------------------

class ACCENT_POLICY(ctypes.Structure):
    """对应 Windows 内部的 ACCENT_POLICY。"""

    _fields_ = [
        ("AccentState", ctypes.c_uint32),    # 4 = acrylic, 3 = blur
        ("AccentFlags", ctypes.c_uint32),    # 0 即可；0x20 会额外画出四边描边
        ("GradientColor", ctypes.c_uint32),  # 0xAABBGGRR
        ("AnimationId", ctypes.c_uint32),    # 保留字段，0
    ]


class WINCOMPATTRDATA(ctypes.Structure):
    """对应 Windows 内部的 WINDOWCOMPOSITIONATTRIBDATA。"""

    _fields_ = [
        ("Attribute", ctypes.c_int),         # WCA_ACCENT_POLICY = 19
        ("Data", ctypes.c_void_p),           # 指向 ACCENT_POLICY
        ("SizeOfData", ctypes.c_size_t),     # sizeof(ACCENT_POLICY)
    ]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.c_void_p),
    ]


#: WNDPROC 函数原型（仅 Windows 下可用）
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
) if IS_WINDOWS else None


# ---------------------------------------------------------------------------
# 依赖绑定（延迟到首次使用）
# ---------------------------------------------------------------------------

_USER32: Optional[Any] = None
_KERNEL32: Optional[Any] = None
_BIND_ERROR: Optional[str] = None
_SET_WCA: Optional[Any] = None


def _setup_prototypes(user32: Any, kernel32: Optional[Any] = None) -> None:
    """绑定 Win32 函数原型。

    这一步不是可选的：ctypes 对未声明原型的函数默认按 **c_int(32bit)** 传参，
    64 位下指针型参数（如 DefWindowProcW 的 lparam）会溢出并抛 ArgumentError，
    表现为窗口过程静默返回 0 -> WM_NCCREATE 失败 -> CreateWindowExW 返回 NULL
    且 GetLastError=0（极难排查）。因此这里必须显式声明 argtypes/restype。
    """
    signatures = [
        # 名称, argtypes, restype
        ("SetWindowCompositionAttribute", [ctypes.c_void_p, ctypes.POINTER(WINCOMPATTRDATA)], ctypes.c_int),
        ("DefWindowProcW", [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
        ("CreateWindowExW", [
            ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ], ctypes.c_void_p),
        ("RegisterClassExW", [ctypes.c_void_p], ctypes.c_uint),
        ("UnregisterClassW", [ctypes.c_wchar_p, ctypes.c_void_p], ctypes.c_int),
        ("DestroyWindow", [ctypes.c_void_p], ctypes.c_int),
        ("ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        ("SetWindowPos", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_uint], ctypes.c_int),
        ("GetWindowLongPtrW", [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t),
        ("SetWindowLongPtrW", [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        ("GetWindowLongW", [ctypes.c_void_p, ctypes.c_int], ctypes.c_long),
        ("SetWindowLongW", [ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long),
        ("PeekMessageW", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                          ctypes.c_uint], ctypes.c_int),
        ("GetMessageW", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
        ("TranslateMessage", [ctypes.c_void_p], ctypes.c_int),
        ("DispatchMessageW", [ctypes.c_void_p], ctypes.c_int),
        ("PostQuitMessage", [ctypes.c_int], None),
        ("PostThreadMessageW", [ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
        ("BeginPaint", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p),
        ("EndPaint", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        ("SystemParametersInfoW", [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
        ("GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
    ]
    for name, argtypes, restype in signatures:
        func = getattr(user32, name, None)
        if func is None:
            continue
        try:
            func.argtypes = argtypes
            func.restype = restype
        except Exception:
            pass

    if kernel32 is not None:
        for name, argtypes, restype in (
            ("GetModuleHandleW", [ctypes.c_wchar_p], ctypes.c_void_p),
            ("GetCurrentThreadId", [], ctypes.c_uint),
        ):
            func = getattr(kernel32, name, None)
            if func is None:
                continue
            try:
                func.argtypes = argtypes
                func.restype = restype
            except Exception:
                pass


def _load_user32():
    """绑定 user32 相关函数；返回 (user32, set_wca, error)。"""
    global _USER32, _SET_WCA, _BIND_ERROR
    if _USER32 is not None or _BIND_ERROR is not None:
        return _USER32, _SET_WCA, _BIND_ERROR

    if not IS_WINDOWS:
        _BIND_ERROR = "非 Windows 平台，玻璃效果不可用（已降级为无操作）"
        return None, None, _BIND_ERROR

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception as exc:  # pragma: no cover - 极端环境
        _BIND_ERROR = "加载 user32.dll 失败: %s" % exc
        return None, None, _BIND_ERROR

    func = getattr(user32, "SetWindowCompositionAttribute", None)
    _setup_prototypes(user32, _load_kernel32())
    if func is None:
        _BIND_ERROR = (
            "当前系统未导出 SetWindowCompositionAttribute，"
            "玻璃效果需要 Windows 10 1803 及以上版本"
        )
        _USER32 = user32
        return _USER32, None, _BIND_ERROR

    _USER32 = user32
    _SET_WCA = func
    _BIND_ERROR = None
    return _USER32, _SET_WCA, None


def _load_kernel32():
    global _KERNEL32
    if _KERNEL32 is not None or not IS_WINDOWS:
        return _KERNEL32
    try:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        _KERNEL32 = None
    return _KERNEL32


def _log(message: str) -> None:
    """统一提示输出（降级提示只打印一次由调用方控制）。"""
    print("[glass_overlay] %s" % message)


def _hwnd_ptr(value: Optional[int]) -> Optional[Any]:
    """把 HWND 常量转成 c_void_p；-1(HWND_TOPMOST) 需按机器字长取模。"""
    if value is None:
        return None
    bits = ctypes.sizeof(ctypes.c_void_p) * 8
    return ctypes.c_void_p(int(value) & ((1 << bits) - 1))


# ---------------------------------------------------------------------------
# 纯函数（可在无 Windows / 无显示环境下测试）
# ---------------------------------------------------------------------------

def detect_windows_build() -> int:
    """返回 Windows 内部版本号（如 Win11 22621）；非 Windows 返回 0。"""
    if not IS_WINDOWS:
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        pass
    try:
        import platform
        parts = platform.version().split(".")
        return int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        return 0


def preferred_accent_state(force: Optional[int] = None) -> int:
    """按系统版本挑选 AccentState：Win11 -> 4(acrylic)，其他 -> 3(blur)。"""
    if force is not None:
        return int(force)
    if not IS_WINDOWS:
        return ACCENT_ENABLE_BLURBEHIND
    return (
        ACCENT_ENABLE_ACRYLICBLURBEHIND
        if detect_windows_build() >= WINDOWS_11_BUILD
        else ACCENT_ENABLE_BLURBEHIND
    )


def make_gradient_color(rgb: Sequence[int], alpha: int, alpha_scale: int = 255) -> int:
    """打包 GradientColor = 0xAABBGGRR。

    参数:
        rgb:   (r, g, b)，0..255
        alpha: 浓淡，0..255（会对 alpha_scale 做饱和映射后写入）
        alpha_scale: alpha 的标称满量程（默认 255，即 alpha 就是 0..255）
    """
    try:
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except Exception:
        r, g, b = DEFAULT_TINT
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))

    scale = max(1, int(alpha_scale))
    try:
        a = int(round(float(alpha) * 255.0 / scale))
    except Exception:
        a = 0
    a = max(0, min(255, a))
    return (a << 24) | (b << 16) | (g << 8) | r


def angle_to_alpha(
    angle: Any,
    alpha_min: int = ACRYLIC_ALPHA_MIN,
    alpha_max: int = ACRYLIC_ALPHA_MAX,
) -> int:
    """把开合角 0..180 线性映射为玻璃浓淡 alpha。

    0° -> alpha_min（最弱/最通透），180° -> alpha_max（最浓）。
    越界角度夹紧；非法输入返回 alpha_min。
    """
    lo = max(0, min(255, int(alpha_min)))
    hi = max(0, min(255, int(alpha_max)))
    if hi < lo:
        lo, hi = hi, lo
    try:
        value = float(angle)
        if value != value:  # NaN
            raise ValueError
    except Exception:
        return lo
    t = value / 180.0
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return int(round(lo + (hi - lo) * t))


def get_work_area() -> Optional[Tuple[int, int, int, int]]:
    """屏幕工作区 (x, y, w, h)（已排除任务栏）；失败返回 None。"""
    user32, _fn, _err = _load_user32()
    if user32 is None:
        return None
    rect = RECT()
    try:
        ok = user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    except Exception:
        return None
    if not ok:
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return (int(rect.left), int(rect.top), width, height)


def default_bottom_half_rect(inset: int = 0, height_ratio: float = 0.5) -> Tuple[int, int, int, int]:
    """笔记本下半屏默认区域 (x, y, w, h)，参数化可改。"""
    area = get_work_area()
    if area is None:
        area = (0, 0, 1280, 720)
    x, y, w, h = area
    ratio = min(max(float(height_ratio), 0.05), 1.0)
    half = int(h * ratio)
    pad = max(0, int(inset))
    return (x + pad, y + h - half - pad, max(1, w - 2 * pad), max(1, half - 2 * pad))


# ---------------------------------------------------------------------------
# 核心调用
# ---------------------------------------------------------------------------

def apply_accent_to_hwnd(
    hwnd: int,
    alpha: int,
    accent_state: Optional[int] = None,
    tint: Sequence[int] = DEFAULT_TINT,
    accent_flags: int = 0,
) -> bool:
    """把任意已存在窗口（例如 PyQt6 的 winId()）变成亚克力/模糊玻璃。

    返回 True 表示 SetWindowCompositionAttribute 调用成功。
    非 Windows 或 API 不可用时返回 False（不抛异常）。
    """
    user32, set_wca, err = _load_user32()
    if user32 is None or set_wca is None or not hwnd:
        return False

    state = preferred_accent_state(accent_state)
    color = make_gradient_color(tint, alpha)

    policy = ACCENT_POLICY(
        AccentState=int(state),
        AccentFlags=int(accent_flags),
        GradientColor=color,
        AnimationId=0,
    )
    data = WINCOMPATTRDATA(
        Attribute=WCA_ACCENT_POLICY,
        Data=ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
        SizeOfData=ctypes.sizeof(policy),
    )
    try:
        result = set_wca(ctypes.c_void_p(int(hwnd)), ctypes.byref(data))
    except Exception:
        return False
    return bool(result)


# ---------------------------------------------------------------------------
# 覆盖层窗口
# ---------------------------------------------------------------------------

class GlassOverlay:
    """一个可选的 Win32 玻璃覆盖窗口。

    典型用法（独立覆盖层）:
        overlay = GlassOverlay()
        overlay.create()                 # 默认对齐工作区下半屏
        overlay.set_angle(45.0)          # 角度 -> 模糊浓淡
        overlay.run_message_loop()       # 或在自己的循环里反复 pump_messages()

    典型用法（附着到已有 Qt/OpenGL 窗口）:
        overlay = GlassOverlay.attach(int(window.winId()))
        overlay.set_angle(angle)
    """

    def __init__(
        self,
        hwnd: Optional[int] = None,
        rect: Optional[Tuple[int, int, int, int]] = None,
        click_through: bool = True,
        topmost: bool = True,
        accent_state: Optional[int] = None,
        tint: Sequence[int] = DEFAULT_TINT,
        alpha_min: int = ACRYLIC_ALPHA_MIN,
        alpha_max: int = ACRYLIC_ALPHA_MAX,
        accent_flags: int = 0,
        title: str = "WinDuo Glass Overlay",
        owns_window: bool = True,
    ) -> None:
        user32, set_wca, err = _load_user32()
        self._user32 = user32
        self._set_wca = set_wca
        self.available = user32 is not None and set_wca is not None
        self.reason: Optional[str] = err

        self.hwnd: Optional[int] = int(hwnd) if hwnd else None
        self.rect = rect
        self.click_through = bool(click_through)
        self.topmost = bool(topmost)
        self.accent_state = preferred_accent_state(accent_state)
        self.tint = tuple(tint) if tint is not None else DEFAULT_TINT
        self.alpha_min = max(0, min(255, int(alpha_min)))
        self.alpha_max = max(0, min(255, int(alpha_max)))
        self.accent_flags = int(accent_flags)
        self.title = title
        self.owns_window = bool(owns_window) and hwnd is None

        self._class_name: Optional[str] = None
        self._hinstance: Optional[int] = None
        self._wndproc_ref: Optional[Any] = None
        self._last_alpha: Optional[int] = None
        self._last_ok: bool = False
        self._last_angle: Optional[float] = None
        self._warned = False
        self._thread_id: Optional[int] = None

    # -- 构造/附着 ----------------------------------------------------------

    @classmethod
    def attach(cls, hwnd: int, **kwargs: Any) -> "GlassOverlay":
        """附着到已有窗口句柄（不新建窗口，不销毁原窗口）。"""
        kwargs.setdefault("click_through", False)
        overlay = cls(hwnd=hwnd, **kwargs)
        overlay._apply_ex_style()
        return overlay

    def create(self, rect: Optional[Tuple[int, int, int, int]] = None) -> bool:
        """创建并显示覆盖层窗口；不可用时打印一次提示并返回 False。"""
        if not self.available:
            self._warn_once("玻璃效果不可用：%s" % (self.reason or "未知原因"))
            return False
        if self.hwnd:
            return True

        area = rect or self.rect or default_bottom_half_rect()
        x, y, w, h = (int(area[0]), int(area[1]), int(area[2]), int(area[3]))
        user32 = self._user32
        kernel32 = _load_kernel32()

        try:
            hinstance = kernel32.GetModuleHandleW(None) if kernel32 is not None else 0
        except Exception:
            hinstance = 0
        self._hinstance = int(hinstance or 0)

        self._class_name = "WinDuoGlassOverlay_%d_%d" % (os.getpid(), int(time.time()) % 100000)
        self._wndproc_ref = WNDPROC(self._wnd_proc)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = ctypes.c_void_p(self._hinstance)
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None  # 不擦背景，玻璃观感交给 DWM accent
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        wc.hIconSm = None

        try:
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                self._warn_once("注册窗口类失败（GetLastError=%s）" % err)
                return False

            ex_style = WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            if self.click_through:
                ex_style |= WS_EX_TRANSPARENT

            hwnd = user32.CreateWindowExW(
                ex_style,
                self._class_name,
                self.title,
                WS_POPUP,
                x, y, w, h,
                None, None, ctypes.c_void_p(self._hinstance), None,
            )
            if not hwnd:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                self._warn_once("创建覆盖层窗口失败（GetLastError=%s）" % err)
                return False

            self.hwnd = int(hwnd)
            try:
                self._thread_id = int(kernel32.GetCurrentThreadId()) if kernel32 is not None else None
            except Exception:
                self._thread_id = None

            self._apply_ex_style()
            flag = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
            user32.SetWindowPos(
                ctypes.c_void_p(self.hwnd),
                _hwnd_ptr(HWND_TOPMOST if self.topmost else None),
                0, 0, 0, 0, flag,
            )
            user32.ShowWindow(ctypes.c_void_p(self.hwnd), 5)  # SW_SHOW
            # 首次上色，保证窗口一出现就是玻璃而不是空白
            self.apply_accent(self._last_alpha if self._last_alpha is not None else self.alpha_min)
            return True
        except Exception as exc:
            self._warn_once("创建玻璃覆盖层异常: %s" % exc)
            return False

    # -- 玻璃浓淡 -----------------------------------------------------------

    def apply_accent(self, alpha: int) -> bool:
        """按 alpha(0..255) 应用玻璃效果；返回调用是否成功。

        last_alpha 始终记录**本次请求值**（无窗口/无 API 时也可用于 UI 显示），
        last_ok 表示该请求是否真正落实到窗口。
        """
        value = max(0, min(255, int(alpha)))
        self._last_alpha = value
        if not self.available or not self.hwnd:
            self._last_ok = False
            return False
        ok = apply_accent_to_hwnd(
            self.hwnd,
            alpha=value,
            accent_state=self.accent_state,
            tint=self.tint,
            accent_flags=self.accent_flags,
        )
        self._last_ok = bool(ok)
        return ok

    def set_angle(self, angle: Any) -> int:
        """把开合角映射为模糊强度并应用；返回本次写入的 alpha。

        映射：0° -> alpha_min（最淡），180° -> alpha_max（最浓），线性。
        """
        alpha = angle_to_alpha(angle, self.alpha_min, self.alpha_max)
        self._last_angle = None
        try:
            self._last_angle = float(angle)
        except Exception:
            pass
        self.apply_accent(alpha)
        return alpha

    @property
    def last_alpha(self) -> Optional[int]:
        """最近一次请求的玻璃浓淡值（即使没有窗口也会记录）。"""
        return self._last_alpha

    @property
    def last_ok(self) -> bool:
        """最近一次 apply_accent 是否真正生效。"""
        return self._last_ok

    @property
    def accent_name(self) -> str:
        names = {
            ACCENT_ENABLE_BLURBEHIND: "blurbehind(3)",
            ACCENT_ENABLE_ACRYLICBLURBEHIND: "acrylicblurbehind(4)",
            ACCENT_ENABLE_GRADIENT: "gradient(1)",
            ACCENT_ENABLE_TRANSPARENTGRADIENT: "transparentgradient(2)",
            ACCENT_ENABLE_HOSTBACKDROP: "hostbackdrop(5)",
        }
        return names.get(int(self.accent_state), "state(%d)" % self.accent_state)

    # -- 窗口行为 -----------------------------------------------------------

    def _apply_ex_style(self) -> bool:
        if not self.available or not self.hwnd:
            return False
        user32 = self._user32
        get_long = getattr(user32, "GetWindowLongPtrW", None) or getattr(user32, "GetWindowLongW", None)
        set_long = getattr(user32, "SetWindowLongPtrW", None) or getattr(user32, "SetWindowLongW", None)
        if get_long is None or set_long is None:
            return False
        try:
            style = int(get_long(ctypes.c_void_p(self.hwnd), GWL_EXSTYLE))
            style |= WS_EX_LAYERED | WS_EX_NOACTIVATE
            if self.click_through:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            set_long(ctypes.c_void_p(self.hwnd), GWL_EXSTYLE, style)
            flag = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED
            user32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, 0, 0, 0, 0, flag)
            return True
        except Exception:
            return False

    def set_click_through(self, enabled: bool = True) -> bool:
        """开/关点击穿透（WS_EX_TRANSPARENT）。"""
        self.click_through = bool(enabled)
        return self._apply_ex_style()

    def set_topmost(self, enabled: bool = True) -> bool:
        if not self.available or not self.hwnd:
            return False
        self.topmost = bool(enabled)
        try:
            self._user32.SetWindowPos(
                ctypes.c_void_p(self.hwnd),
                _hwnd_ptr(HWND_TOPMOST if self.topmost else None),
                0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            return True
        except Exception:
            return False

    def move(self, rect: Tuple[int, int, int, int]) -> bool:
        if not self.available or not self.hwnd:
            return False
        x, y, w, h = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        try:
            return bool(self._user32.SetWindowPos(
                ctypes.c_void_p(self.hwnd), None, x, y, w, h,
                SWP_NOACTIVATE | SWP_NOZORDER,
            ))
        except Exception:
            return False

    def show(self, visible: bool = True) -> bool:
        if not self.available or not self.hwnd:
            return False
        try:
            self._user32.ShowWindow(ctypes.c_void_p(self.hwnd), 5 if visible else 0)
            return True
        except Exception:
            return False

    def pump_messages(self, max_messages: int = 16) -> int:
        """非阻塞处理窗口消息（放进自己的 render loop 里调用）。返回处理条数。"""
        if self._user32 is None:
            return 0
        msg = ctypes.create_string_buffer(64)
        handled = 0
        for _ in range(max(0, int(max_messages))):
            try:
                has = self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE
            except Exception:
                break
            if not has:
                break
            try:
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                pass
            handled += 1
        return handled

    def run_message_loop(self) -> int:
        """阻塞式消息循环（独立运行覆盖层时用），WM_QUIT 后返回。"""
        if self._user32 is None:
            self._warn_once("玻璃效果不可用：%s" % (self.reason or "未知原因"))
            return -1
        msg = ctypes.create_string_buffer(64)
        try:
            while True:
                ret = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
        except KeyboardInterrupt:
            pass
        return 0

    def stop_message_loop(self) -> None:
        """让 run_message_loop 退出（可跨线程调用）。"""
        if self._user32 is None or self._thread_id is None:
            return
        try:
            self._user32.PostThreadMessageW(ctypes.c_uint(self._thread_id), 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass

    def destroy(self) -> bool:
        """销毁窗口并注销窗口类（附着模式下不动原窗口）。"""
        if not self.available or not self.hwnd:
            return False
        hwnd = self.hwnd
        try:
            if self.owns_window:
                self._user32.DestroyWindow(ctypes.c_void_p(hwnd))
                if self._class_name:
                    try:
                        self._user32.UnregisterClassW(self._class_name, ctypes.c_void_p(self._hinstance or 0))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if self.owns_window:
                self.hwnd = None
        return True

    # -- 内部 ---------------------------------------------------------------

    def _warn_once(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        _log(message)

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """窗口过程：不擦背景、点击穿透、销毁时退出消息循环。

        注意 WM_NCCREATE 必须返回 TRUE(1)，否则 CreateWindowExW 直接失败，
        且 GetLastError 保持 0（本模块曾经踩过这个坑）。
        """
        try:
            if msg == WM_NCCREATE:
                return 1
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                ps = ctypes.create_string_buffer(128)
                self._user32.BeginPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                self._user32.EndPaint(ctypes.c_void_p(hwnd), ctypes.byref(ps))
                return 0
            if msg == WM_NCHITTEST and self.click_through:
                return HTTRANSPARENT
            if msg == WM_DESTROY:
                self._user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        try:
            return int(self._user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam))
        except Exception:
            # 兜底：异常绝不能穿过回调边界，且 WM_NCCREATE 应放行
            return 1 if msg == WM_NCCREATE else 0

    # -- 自检 ---------------------------------------------------------------

    @staticmethod
    def selftest(verbose: bool = True) -> Dict[str, Any]:
        """无窗口自检：结构体布局、系统版本、映射函数。可安全在任何环境运行。"""
        report: Dict[str, Any] = {}
        report["is_windows"] = IS_WINDOWS
        report["windows_build"] = detect_windows_build()
        report["accent_state"] = preferred_accent_state()
        report["accent_name"] = (
            "acrylicblurbehind(4)"
            if preferred_accent_state() == ACCENT_ENABLE_ACRYLICBLURBEHIND
            else "blurbehind(3)"
        )
        report["sizeof_accent_policy"] = ctypes.sizeof(ACCENT_POLICY)
        report["sizeof_wincompattrdata"] = ctypes.sizeof(WINCOMPATTRDATA)
        report["gradient_color_abgr"] = hex(make_gradient_color((0x11, 0x22, 0x33), 0xAA))
        report["alpha_0deg"] = angle_to_alpha(0)
        report["alpha_90deg"] = angle_to_alpha(90)
        report["alpha_180deg"] = angle_to_alpha(180)
        user32, set_wca, err = _load_user32()
        report["set_wca_available"] = set_wca is not None
        report["bind_error"] = err
        report["work_area"] = get_work_area()
        report["bottom_half_rect"] = default_bottom_half_rect()
        if verbose:
            for key in sorted(report):
                print("%-24s = %s" % (key, report[key]))
        return report


# ---------------------------------------------------------------------------
# 命令行：自检（不建窗口）/ 演示（会建窗口）/ mock（无硬件全链路）
# ---------------------------------------------------------------------------

def _run_mock(args: Any) -> int:
    """--mock：不接线、不装 pyserial，用 simulate_device 的数据流驱动玻璃覆盖层。

    链路：simulate_device 生成固件格式行 -> parse_line -> AngleSmoother
          -> angle_to_alpha -> SetWindowCompositionAttribute
    这是给「还没接硬件」的新用户最快看到效果的一条路径。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import simulate_device  # noqa: WPS433  同目录模块，懒加载避免循环依赖
    import winduo_protocol as proto  # noqa: WPS433

    count = max(40, int(round(max(1.0, args.seconds) * max(1.0, args.hz))))
    lines = list(simulate_device.generate_stream(
        count=count, hz=args.hz, seed=args.seed, period_samples=count,
    ))

    overlay: Optional[GlassOverlay] = None
    if not args.no_window:
        overlay = GlassOverlay(click_through=args.click_through)
        if overlay.create():
            print("[mock] 玻璃覆盖层已创建 hwnd=%s accent=%s 区域=%s"
                  % (overlay.hwnd, overlay.accent_name, overlay.rect or default_bottom_half_rect()))
        else:
            print("[mock] 无法创建窗口（%s），降级为纯映射演示（仍可看数值）"
                  % (overlay.reason or "未知原因"))
            overlay = None

    smoother = proto.AngleSmoother(alpha=0.25)
    period = 1.0 / max(1.0, args.hz)
    stats = {"valid": 0, "comments": 0, "invalid": 0}
    mins = 255
    maxs = 0
    started = time.monotonic()
    try:
        for index, item in enumerate(lines):
            if proto.is_comment_line(item.text):
                stats["comments"] += 1
            else:
                sample = proto.parse_line(item.text)
                if sample is None:
                    stats["invalid"] += 1
                else:
                    stats["valid"] += 1
                    angle = smoother.update(sample["angle"])
                    if overlay is not None:
                        alpha = overlay.set_angle(angle)
                    else:
                        alpha = angle_to_alpha(angle, ACRYLIC_ALPHA_MIN, ACRYLIC_ALPHA_MAX)
                    mins = min(mins, alpha)
                    maxs = max(maxs, alpha)
                    if index % max(1, int(round(args.hz / 4.0))) == 0:
                        print("angle=%6.2f -> alpha=%3d  status=%-11s mode=%-9s"
                              % (angle, alpha, sample["status"], sample["mode"]))
            if overlay is not None:
                overlay.pump_messages()
            time.sleep(period)
    except KeyboardInterrupt:
        print("[mock] 用户中断")
    finally:
        if overlay is not None:
            overlay.destroy()

    print("[mock] 完成：有效帧 %d / 注释行 %d / 非法行 %d，alpha 范围 %s..%s，用时 %.1fs"
          % (stats["valid"], stats["comments"], stats["invalid"],
             mins if mins != 255 else "-", maxs, time.monotonic() - started))
    print("[mock] 提示：alpha 越高玻璃越浓；覆盖层为点击穿透置顶窗口，演示结束已自动销毁。")
    return 0


def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="WinDuo 玻璃覆盖层（无 PyQt6/OpenGL 依赖，纯 Win32 ctypes 实现）"
    )
    parser.add_argument("--selftest", action="store_true",
                        help="只做结构体与映射自检，不创建窗口")
    parser.add_argument("--mock", action="store_true",
                        help="推荐：无硬件演示，用 simulate_device 数据流驱动玻璃效果"
                             "（不需要 pyserial / PyQt6 / OpenGL）")
    parser.add_argument("--no-window", action="store_true",
                        help="--mock 时不创建窗口，只打印 角度->浓淡 映射")
    parser.add_argument("--demo", action="store_true", help="创建覆盖层并做角度扫描（内置正弦）")
    parser.add_argument("--seconds", type=float, default=6.0, help="演示时长（默认 6 秒）")
    parser.add_argument("--hz", type=float, default=20.0, help="--mock 的数据频率（默认 20Hz）")
    parser.add_argument("--seed", type=int, default=20240521, help="--mock 的随机种子")
    parser.add_argument("--click-through", action="store_true", default=True, help="点击穿透（默认开）")
    parser.add_argument("--no-click-through", dest="click_through", action="store_false")
    args = parser.parse_args(argv)

    if args.mock:
        return _run_mock(args)

    if not args.demo:
        GlassOverlay.selftest()
        return 0

    overlay = GlassOverlay(click_through=args.click_through)
    if not overlay.create():
        print("创建覆盖层失败：%s" % overlay.reason)
        return 2
    print("覆盖层已创建 hwnd=%s accent=%s" % (overlay.hwnd, overlay.accent_name))
    import math
    end = time.monotonic() + max(0.5, args.seconds)
    start = time.monotonic()
    try:
        while time.monotonic() < end:
            t = (time.monotonic() - start) / max(0.5, args.seconds)
            angle = 90.0 - 90.0 * math.cos(2 * math.pi * t)
            alpha = overlay.set_angle(angle)
            overlay.pump_messages()
            print("angle=%6.2f -> alpha=%3d" % (angle, alpha))
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        overlay.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
