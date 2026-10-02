"""Sui-WinDuo 控制台（PyQt6）— 终端内容 + 毛玻璃外壳
=====================================================
外壳 = Windows 原生标题栏 + 客户区亚克力磨砂（复用 ``pc/glass_overlay.py`` 已验证的
``SetWindowCompositionAttribute`` + ``ACCENT_ENABLE_ACRYLICBLURBEHIND``）。
内容 = 中文等宽终端：方框标题线面板、``[字段  ] 值`` 左对齐行、ASCII 角度条、方括号按钮。
面板只画 ``─`` 上下横线、不画 ``│`` 竖边（CJK 双宽，竖边一混排就错位）。

坑：① 玻璃层永远是独立的 ``duo_glass.GlassGLWidget``，本文件只 import、绝不改
``duo_glass.py``，默认参数直接取自 ``build_cfg()``；② 建 GL 组件前必须
``QSurfaceFormat.setDefaultFormat(3.3 兼容)``，否则 shader 编译失败 -> 黑屏；
③ ``UiGlassControl.quit_flag`` 只能是 False（置 True 会让 GL 组件 quit 整个 app）；
④ 串口读在 ``AngleReader`` 线程、截图在 ``CaptureWorker`` 线程，UI 只用 100ms QTimer；
⑤ 截屏区域必须是**屏幕物理分辨率**（geom×dpr）且不传 scale，否则 GL 会把低分辨率
截图放大铺满全屏，"透视拉伸"就退化成"放大窗口 + 变糊"。

自测：``--selftest`` / ``--screenshot pc\\app_ui.png`` / ``--smoke 4 [--with-glass]``
作者 EthanMaven · github.com/comreade-123 · MIT License
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:                       # 双击 / 从任意目录运行
    sys.path.insert(0, str(_HERE))

try:
    from PyQt6.QtCore import (QT_VERSION_STR, QEasingCurve, QPointF, QPropertyAnimation,
                              QRectF, Qt, QTimer, pyqtSignal)
    from PyQt6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics, QIcon,
                             QKeySequence, QPainter, QPen, QPixmap, QShortcut,
                             QSurfaceFormat)
    from PyQt6.QtWidgets import (QApplication, QComboBox, QGraphicsDropShadowEffect,
                                 QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
                                 QSizePolicy, QSlider, QSystemTrayIcon, QVBoxLayout,
                                 QWidget)
except Exception as _exc:                                                # pragma: no cover
    sys.stderr.write("\n[致命] 无法导入 PyQt6：%s\n"
                     "  请用 .venv\\Scripts\\python.exe pc\\sui_winduo_app.py 运行\n"
                     "  未安装：.venv\\Scripts\\python.exe -m pip install PyQt6\n\n" % _exc)
    raise SystemExit(2)

APPNAME = "Sui-WinDuo"
WINDOW_TITLE = "Sui-WinDuo 悬浮玻璃"
SUB = "悬浮玻璃 · 屏幕开合角控制台"
AUTHOR, GITHUB, LICENSE, VERSION = "EthanMaven", "github.com/comreade-123", "MIT 许可", "1.0"
GREEN, DIM, RED, YELLOW = "#4AF626", "#8B949E", "#FF5F56", "#FFBD2E"
FG = "#EDEDED"
GLASS_RGBA = (16, 18, 20, 190)                       # 客户区磨砂底色（带 alpha）
CARD_RGBA, EDGE_RGBA, FRAME_RGBA = (255, 255, 255, 10), (255, 255, 255, 30), (255, 255, 255, 60)
MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Courier New", "DejaVu Sans Mono")
MONO = "Consolas"                                    # make_app() 里按本机字体再挑
G = {}                                               # 字形表，见 pick_glyphs()
# 固件 JSON 的状态码 -> 界面中文（日志里仍保留原始值，便于对照）
STATUS_CN = {"ok": "正常", "calibrating": "校准中", "calibrate": "校准中",
             "warning_up": "预热中", "warmup": "预热中", "sensor_error": "传感器异常",
             "error": "固件报错", "connected": "已连接", "idle": "空闲"}
PARAM_NAMES = {"blur_spread": "模糊扩散", "darkening": "变暗强度", "max_tilt_deg": "最大倾角",
               "eye_dist_h": "视距倍数", "deadband": "死区", "neg_scale": "负向系数"}
PARAM_TERMS = {"blur_spread": "blur_spread", "darkening": "darkening", "max_tilt_deg": "max_tilt",
               "eye_dist_h": "eye_dist", "deadband": "deadband", "neg_scale": "neg_scale"}
TUNING_HINT = ("实时生效 · 截屏按屏幕物理分辨率（不缩放，不会把画面放大）·"
               " 省 CPU 请调刷新率 refresh_hz（默认 2Hz 约占 5% 单核）")
PARAMS = (("blur_spread", 0.05, 1.20, 2, ""), ("darkening", 0.0, 0.008, 4, ""),
          ("max_tilt_deg", 10.0, 88.0, 0, " deg"), ("eye_dist_h", 0.5, 4.0, 2, " x"),
          ("deadband", 0.0, 10.0, 1, " deg"), ("neg_scale", 0.0, 1.0, 2, ""))


def _have(name):
    try:
        __import__(name)
        return True, ""
    except Exception as exc: return False, "%s: %s" % (type(exc).__name__, exc)


def load_duo_glass(retries=3, delay=0.6):
    """导入 duo_glass；队友可能正改到一半，失败就等一会儿重试 -> (mod, err)。"""
    last = None
    for i in range(max(1, retries)):
        try:
            import duo_glass
            missing = [n for n in ("GlassGLWidget", "AngleReader", "CaptureWorker", "build_cfg",
                                   "angle_to_concentration", "VS", "FS_DUO")
                       if not hasattr(duo_glass, n)]
            if not missing: return duo_glass, None
            last = "duo_glass 缺少符号: %s" % ", ".join(missing)
        except Exception as exc: last = "%s: %s" % (type(exc).__name__, exc)
        if i + 1 < max(1, retries): time.sleep(delay)
    return None, str(last)


DUO, DUO_ERR = load_duo_glass(retries=1, delay=0.0)
HAVE_SERIAL, SERIAL_ERR = _have("serial")
HAVE_MSS, MSS_ERR = _have("mss")
HAVE_GL, GL_ERR = _have("OpenGL")
HAVE_WINREADER, WINREADER_ERR = _have("serial_reader_win")
HAVE_GLASS, GLASS_ERR = _have("glass_overlay")


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def qss():
    """终端控件样式：等宽、5px 圆角、hover 只反色、无渐变过渡。"""
    return """
* { font-family: "@M"; font-size: 13px; }
QWidget { color: @F; }
QLabel { background: transparent; }
QPushButton { background: transparent; color: @F; border: none; padding: 3px 8px; border-radius: 5px; }
QPushButton:hover { background: @F; color: #101214; }
QPushButton:checked { background: @F; color: #101214; }
QPushButton:disabled { color: #5A6068; }
QPushButton#primary { color: @G; border: 1px solid rgba(74,246,38,120); font-weight: bold; padding: 8px 12px; }
QPushButton#primary:hover { background: @G; color: #0B1206; border-color: @G; }
QPushButton#primary[running="true"] { color: @R; border-color: rgba(255,95,86,140); }
QPushButton#primary[running="true"]:hover { background: @R; color: #1A0A08; border-color: @R; }
QComboBox { background: rgba(255,255,255,14); color: @F; border: 1px solid rgba(255,255,255,45);
            padding: 4px 8px; border-radius: 5px; }
QComboBox:hover { border-color: rgba(255,255,255,90); }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView { background: #16181B; color: @F; border: 1px solid rgba(255,255,255,45);
            outline: none; selection-background-color: @G; selection-color: #0B1206; }
QSlider::groove:horizontal { height: 3px; background: rgba(255,255,255,28); border-radius: 2px; }
QSlider::sub-page:horizontal { height: 3px; background: @G; border-radius: 2px; }
QSlider::add-page:horizontal { height: 3px; background: rgba(255,255,255,28); border-radius: 2px; }
QSlider::handle:horizontal { background: #FFFFFF; width: 12px; height: 12px; margin: -5px 0px;
            border: none; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: @G; }
QSlider::handle:horizontal:disabled { background: #7A8088; }
QSlider::sub-page:horizontal:disabled { background: rgba(255,255,255,30); }
QMenu { background: #16181B; color: @F; border: 1px solid rgba(255,255,255,45);
        border-radius: 6px; padding: 4px; }
QMenu::item { padding: 5px 18px; border-radius: 4px; }
QMenu::item:selected { background: @G; color: #0B1206; }
QMessageBox { background: #16181B; }
QToolTip { background: #16181B; color: @F; border: 1px solid rgba(255,255,255,45); }
""".replace("@M", MONO).replace("@F", FG).replace("@G", GREEN).replace("@R", RED)


def pick_glyphs(mono):
    """挑字形：字体缺 ◄ ► ◆ ┼ █ ░ 或方框字符时退到 ASCII，保证不错位。"""
    fm = QFontMetrics(QFont(mono, 10))
    has = lambda *cs: all(fm.inFont(c) for c in cs)                    # noqa: E731
    g = {"l": "<", "r": ">", "m": "+", "k": "*", "full": "#", "void": "-",
         "tl": "+", "h": "-", "tr": "+", "bl": "+", "br": "+"}
    if has("◄", "►", "◆", "┼"): g.update(l="◄", r="►", m="┼", k="◆")
    if has("█", "░"): g.update(full="█", void="░")
    if has("┌", "─", "┐", "└", "┘"): g.update(tl="┌", h="─", tr="┐", bl="└", br="┘")
    return g


def bar(frac, cells=18):
    """ASCII 进度条：█ 已填充，░ 空。"""
    n = int(round(clamp(float(frac), 0.0, 1.0) * cells))
    return G["full"] * n + G["void"] * (cells - n)


def signal_bar(angle, span, cells):
    """双向角度条 ◄────┼────► + ◆ 标记当前值（值为 0 时保留中点 ┼）。"""
    half = max(2, cells // 2)
    line = [G["h"]] * cells
    line[0], line[-1], line[half] = G["l"], G["r"], G["m"]
    if angle is not None and span > 0:
        pos = int(round(clamp(float(angle) / span, -1.0, 1.0) * half))
        if pos: line[clamp(half + pos, 1, cells - 2)] = G["k"]
    return "".join(line)


def scale_line(span, cells):
    """与 signal_bar 等宽的刻度行（纯 ASCII/等宽字形，不会因中文错位）。"""
    line = [" "] * cells
    left, mid, right = "%+.0f" % (-span), "0", "%+.0f" % span
    line[0:len(left)] = list(left)
    line[max(0, cells - len(right)):] = list(right)
    c = clamp((cells - len(mid)) // 2, len(left) + 1, cells - len(right) - 2)
    line[c:c + len(mid)] = list(mid)
    return "".join(line)


def fmt_angle(v):
    return "无数据" if v is None else "%+.2f deg" % float(v)


def cn_status(raw):
    """固件状态码 -> 中文（未知的保留原样，方便对照）。"""
    raw = (raw or "").strip()
    return STATUS_CN.get(raw.lower(), raw or "未知")


def label(text, color=FG, tip=None, width=None):
    lbl = QLabel(text)
    lbl.setStyleSheet("color:%s;" % color)
    lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    if tip: lbl.setToolTip(tip)
    if width: lbl.setFixedWidth(width)
    return lbl


def row(*widgets, spacing=8):
    """一行控件；传 None 表示插入一个伸缩空隙。"""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        lay.addStretch(1) if w is None else (lay.addWidget(w) if isinstance(w, QWidget)
                                             else lay.addLayout(w))
    return lay


def btn(text, slot, obj=None, checkable=False, tip=None):
    b = QPushButton(text)
    if obj: b.setObjectName(obj)
    b.setCheckable(checkable); b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip: b.setToolTip(tip)
    b.clicked.connect(slot)
    return b


def rgba(r, g, b, a):
    c = QColor(r, g, b)
    c.setAlpha(a)
    return c


def load_icon():
    """窗口/托盘图标：项目根目录 icon.png（缺失时退化成一个手画方块）。"""
    if (_ROOT / "icon.png").exists(): return QIcon(str(_ROOT / "icon.png"))
    pm = QPixmap(256, 256)
    pm.fill(QColor(0, 0, 0))
    p = QPainter(pm)
    p.setPen(QPen(QColor(GREEN), 8)); p.drawRect(12, 12, 232, 232)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "SW"); p.end()
    return QIcon(pm)


def list_ports():
    """枚举串口 -> [{'port','label'}]：pyserial 为主，退化用 ctypes/winreg 实现。"""
    raw = []
    try:
        if HAVE_SERIAL:
            from serial.tools import list_ports as lp
            raw = [(str(getattr(i, "device", "") or ""),
                    " ".join(str(getattr(i, "description", "") or "").split()))
                   for i in lp.comports()]
        if not raw and HAVE_WINREADER:
            import serial_reader_win as srw
            raw = [(it.get("port", ""), it.get("device", ""))
                   for it in srw.list_com_port_details()]
    except Exception: raw = []
    out, seen = [], set()
    for port, desc in raw:
        port = str(port).strip().upper()
        if port and port not in seen:
            seen.add(port)
            out.append({"port": port, "label": ("%s · %s" % (port, desc)) if desc else port})
    digits = lambda p: p[3:] if p[3:].isdigit() else "9999"      # COM10 排在 COM9 后
    return sorted(out, key=lambda it: (int(digits(it["port"])), it["port"]))


def probe_port(port):
    """开连之前体检一次，拿一句可读的中文（端口被占用 / 不存在 / 可用）。"""
    if not (HAVE_WINREADER and os.name == "nt" and port): return None
    try:
        import serial_reader_win as srw
        ok, reason = srw.WinSerialReader(port=port, baudrate=115200).probe()
        return (bool(ok), str(reason))
    except Exception as exc: return (False, "串口体检失败：%s" % exc)


def build_cfg(port, demo=-1.0):
    """参数对象：默认值直接取自 duo_glass.build_cfg()，保证与上游一致。"""
    cfg = None
    if DUO is not None:
        old = sys.argv
        try:
            sys.argv = [old[0]]                      # 屏蔽本程序的 --screenshot 等开关
            cfg = DUO.build_cfg()
        except BaseException: cfg = None
        finally: sys.argv = old
    if cfg is None:                                  # 兜底：与 duo_glass.py 默认值对齐
        cfg = argparse.Namespace(port="COM3", baud=115200, manual=False, angle_open=90.0,
                                 deadband=1.0, neg_scale=0.0, refresh_hz=3.0, max_tilt_deg=88.0,
                                 eye_dist_h=2.0, blur_spread=0.42, darkening=0.001, max_taps=32,
                                 lock_at_close=False, selftest=False, smoke=False, demo=-1.0,
                                 trace=False)
    cfg.port, cfg.demo = port, float(demo)
    return cfg


def default_params(cfg):
    return {k: float(getattr(cfg, k, 0.0)) for k, *_ in PARAMS}


def enable_glass(win, on=True):
    """窗口客户区亚克力/模糊（复用 pc/glass_overlay.py 的实现）。-> (ok, 描述)"""
    if not HAVE_GLASS: return False, "glass_overlay 不可用：%s" % GLASS_ERR
    if os.environ.get("SUI_NO_ACRYLIC"):              # A/B 对比亚克力是否真生效
        return False, "已关闭（SUI_NO_ACRYLIC）"
    try:
        import glass_overlay as go
        state = go.preferred_accent_state() if on else 0          # 0 = ACCENT_DISABLED
        ok = go.apply_accent_to_hwnd(int(win.winId()), GLASS_RGBA[3], accent_state=state,
                                     tint=GLASS_RGBA[:3])
        return bool(ok), {4: "亚克力 acrylic(4)", 3: "模糊 blurbehind(3)",
                          0: "已关闭"}.get(state, str(state))
    except Exception as exc: return False, "%s: %s" % (type(exc).__name__, exc)


def force_topmost(win, on=True):
    """截图时把窗口临时置顶（否则可能被别的窗口挡住，拍不到亚克力效果）。"""
    if os.name != "nt": return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(win.winId())), ctypes.c_void_p(-1 if on else -2), 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0040))                # NOSIZE | NOMOVE | SHOWWINDOW
    except Exception: return False


def dark_titlebar(win):
    """把原生标题栏涂成和磨砂客户区同色（Win11 支持；失败静默忽略）。"""
    if os.name != "nt": return False
    try:
        import ctypes
        r, g, b = GLASS_RGBA[:3]
        for attr, value in ((20, 1), (35, (b << 16) | (g << 8) | r), (36, 0xEDEDED)):
            v = ctypes.c_uint32(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(win.winId())), ctypes.c_uint32(attr),
                ctypes.byref(v), ctypes.sizeof(v))
        return True
    except Exception: return False


class UiGlassControl:
    """``duo_glass.ManualControl`` 的鸭类型替身：``get() -> (target, key, auto)``。

    ``quit_flag`` 永远是 False——置 True 会让 GL 组件直接 ``QApplication.quit()``，
    把整个控制台一起关掉；停止玻璃层由 ``GlassController.stop()`` 负责。
    """

    def __init__(self):
        self.quit_flag, self._target, self._auto = False, 0.0, True

    def get(self):
        return (self._target, "", self._auto)

    def set_target(self, v):
        self._target = clamp(float(v), 0.0, 1.0)

    def set_auto(self, flag):
        self._auto = bool(flag)


class GlassController:
    """玻璃层生命周期：截图线程 -> GL 组件 -> 参数热更新。"""

    ATTRS = (("blur_spread", "spread", float), ("darkening", "dark", float),
             ("max_tilt_deg", "max_tilt", math.radians), ("eye_dist_h", "eye_h", float),
             ("deadband", "deadband", float), ("neg_scale", "neg_scale", float))

    def __init__(self, app, cfg, control, log=print):
        self.app, self.cfg, self.control, self.log = app, cfg, control, log
        self._capturer = self._widget = self._reader = None
        self.last_params, self.last_error = {}, ""

    def screen_region(self):
        """截屏区域 = 屏幕物理分辨率（geom × dpr），不传 scale，GL 视口与之一致。"""
        screen = self.app.primaryScreen()
        dpr, geo = screen.devicePixelRatio(), screen.geometry()
        return screen, {"left": geo.x(), "top": geo.y(), "width": int(geo.width() * dpr),
                        "height": int(geo.height() * dpr)}

    capture_region = lambda self: self.screen_region()[1]        # 自检用

    def set_reader(self, reader):
        """换端口/断开时同步更新活着的 GL 组件（它构造时缓存了 reader）。"""
        self._reader = reader
        if self._widget is not None:
            try: self._widget.reader = reader
            except Exception as exc: self.log("[warn] 更新玻璃层 reader 失败: %s" % exc)

    def _ensure_capture(self):
        """单实例 CaptureWorker：停止玻璃后不再 kick，线程阻塞在 Event.wait() 零 CPU，
        下次启动复用（duo_glass 的 CaptureWorker 没有 stop()，本文件也不许改它）。
        """
        if self._capturer is None:
            self._capturer = DUO.CaptureWorker(self.screen_region()[1])
            self._capturer.start()
        return self._capturer

    @property
    def running(self):
        return self._widget is not None

    @property
    def concentration(self):
        return float(getattr(self._widget, "g", 0.0)) if self._widget else 0.0

    def start(self):
        if self.running: return True, ""
        if DUO is None or not HAVE_GL or not HAVE_MSS:
            return False, "duo_glass/依赖不可用：%s" % (DUO_ERR or GL_ERR or MSS_ERR)
        try:
            cap = self._ensure_capture()
            w = DUO.GlassGLWidget(self.screen_region()[0], self._reader, cap, self.control,
                                  self.cfg)
            self.apply_params(self.last_params, w); w.show()
            w.shown = True
            cap.kick()
            self._widget = w
            return True, ""
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._widget = None
            return False, self.last_error

    def stop(self):
        w, self._widget = self._widget, None
        if w is None: return
        for step in (lambda: getattr(w, "timer", None) and w.timer.stop(),
                     w.hide, w.close, w.deleteLater):
            try: step()
            except Exception as exc: self.log("[warn] 停止玻璃层时忽略异常: %s" % exc)

    def apply_params(self, params, widget=None):
        """把界面参数写进活着的 GL 组件（它构造时把 cfg 拷成了实例属性）。"""
        if params:
            self.last_params = dict(params)
            for k, v in params.items():
                try: setattr(self.cfg, k, float(v))
                except Exception: pass
        w, params = widget or self._widget, params or self.last_params
        if w is None or not params: return
        for key, attr, conv in self.ATTRS:
            if key in params and hasattr(w, attr):
                try: setattr(w, attr, conv(params[key]))
                except Exception as exc: self.log("[warn] 参数 %s 热更新失败: %s" % (key, exc))


class Panel(QWidget):
    """毛玻璃圆角卡片（8px 圆角 + 1px 高光 + 轻阴影）+ ┌─ 标题 ─┐ / └──┘ 上下横线。

    刻意不画 ``│`` 竖边：面板里混排中文时 CJK 是双宽字符，竖边一错位就很明显；
    上下横线的 ``─`` 数量按像素宽度算，中英混排也不会歪。
    """

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.body = QVBoxLayout(self)
        self.body.setSpacing(2); self._margins()
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18); shadow.setOffset(0, 3); shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)

    def _fm(self):
        fm = QFontMetrics(self.font())
        return fm, max(4, fm.horizontalAdvance("─")), fm.height()

    def _margins(self):
        _fm, cw, lh = self._fm()
        self.body.setContentsMargins(cw + 10, lh + 6, cw + 10, lh + 6)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == ev.Type.FontChange: self._margins()

    def chars(self):
        """面板内可用的等宽字符列数（只给 ASCII 条用；中文不参与这个计算）。"""
        cw = max(4, QFontMetrics(self.font()).horizontalAdvance("─"))
        return max(20, int((self.width() - 2 * cw - 24) // cw))

    def paintEvent(self, _ev):
        fm, cw, _lh = self._fm()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True); p.setPen(QPen(rgba(*EDGE_RGBA), 1))
        p.setBrush(rgba(*CARD_RGBA))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), 8, 8)
        p.setFont(self.font())
        head, x, y = G["tl"] + G["h"] + " ", 4, fm.ascent() + 2
        p.setPen(rgba(*FRAME_RGBA))
        room = self.width() - 8 - fm.horizontalAdvance(head + " " + self.title + " " + G["tr"])
        p.drawText(x, y, head + " " + self.title + " " + G["h"] * max(0, int(room // cw)) + G["tr"])
        p.setPen(QColor(GREEN))
        p.drawText(x + fm.horizontalAdvance(head), y, self.title)     # 标题用强调色
        p.setPen(rgba(*FRAME_RGBA))
        p.drawText(x, self.height() - fm.descent() - 3,
                   G["bl"] + G["h"] * max(0, int((self.width() - 8) // cw) - 2) + G["br"])
        p.end()


class Dot(QWidget):
    """状态点：连接正常绿色 #30D158 / 异常红色 #FF453A，带一圈柔光。"""

    def __init__(self, size=9, parent=None):
        super().__init__(parent)
        self._d = size
        self._c = QColor(DIM)
        self.setFixedSize(size + 8, size + 8)

    def set_color(self, color):
        if QColor(color) != self._c:
            self._c = QColor(color)
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        p.setPen(Qt.PenStyle.NoPen)
        for radius, alpha in ((self._d * 0.95, 40), (self._d * 0.72, 70)):
            p.setBrush(rgba(self._c.red(), self._c.green(), self._c.blue(), alpha))
            p.drawEllipse(c, radius, radius)
        p.setBrush(self._c); p.drawEllipse(c, self._d / 2.0, self._d / 2.0); p.end()


class Slider(QWidget):
    """参数一行：``模糊扩散 (blur_spread)  [——●——]  0.42``。

    标签用**固定像素宽度**（不是字符数），中英混排也不会把后面的滑块挤歪。
    """

    changed = pyqtSignal(str, float)

    def __init__(self, key, lo, hi, dec, unit, value, parent=None):
        super().__init__(parent)
        self.key, self.lo, self.hi, self.dec, self.unit = key, lo, hi, dec, unit
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000); self.slider.setMinimumWidth(180)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.valueChanged.connect(self._moved)
        self.value = label("", FG)
        self.value.setMinimumWidth(74)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        name = "%s (%s)" % (PARAM_NAMES.get(key, key), PARAM_TERMS.get(key, key))
        lay = row(label(name, DIM, width=250), self.slider, self.value, spacing=10)
        lay.setContentsMargins(0, 0, 0, 0); lay.setStretch(1, 1)
        self.setLayout(lay); self.set_value(value, emit=False)

    def text(self, v=None):
        return ("%." + str(self.dec) + "f") % (self.raw() if v is None else v) + self.unit

    def raw(self):
        return self.lo + (self.slider.value() / 1000.0) * (self.hi - self.lo)

    def _moved(self, _v):
        self.value.setText(self.text()); self.changed.emit(self.key, self.raw())

    def set_value(self, v, emit=True):
        v = clamp(float(v), self.lo, self.hi)
        self.slider.blockSignals(True)
        self.slider.setValue(int(round((v - self.lo) / (self.hi - self.lo) * 1000)))
        self.slider.blockSignals(False); self.value.setText(self.text(v))
        if emit: self.changed.emit(self.key, v)


class MainWindow(QWidget):
    def __init__(self, args, cfg, app):
        super().__init__()
        self.args, self.cfg, self.app = args, cfg, app
        self.control = UiGlassControl()
        self.glass = GlassController(app, cfg, self.control, log=self._print)
        self.reader, self.reader_port, self.port_note = None, "", ""
        self._last_sample, self._demo = 0.0, bool(args.demo_ui)
        self._demo_run, self._demo_vals = False, None
        self._conc_shown, self._glass_info = 0.0, "off"
        self._auto_serial = bool(args.live or args.smoke or not (args.selftest or args.screenshot))
        self._headless = bool(args.selftest or args.screenshot or args.smoke)
        self._force_quit = False
        self.params = default_params(cfg)
        self.setWindowTitle(WINDOW_TITLE)            # 原生标题栏 + 任务栏用它
        self.setWindowIcon(load_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)  # 让亚克力透出来
        self._build(); self._tray()
        for keys, slot in (("F5", self._toggle_glass), ("Ctrl+R", self._scan),
                           ("Ctrl+Q", self._quit)):
            QShortcut(QKeySequence(keys), self, activated=slot)
        self._scan(initial=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick); self.timer.start(100)
        self.tick()
        self._fit()                                  # 文案填好后再定尺寸，避免压掉最后一行
        if args.smoke: QTimer.singleShot(int(float(args.smoke) * 1000), self._quit)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)      # 充足留白
        root.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(load_icon().pixmap(20, 20)); icon.setFixedSize(20, 20)
        root.addLayout(row(icon, label(APPNAME, FG), label(SUB, DIM),
                           label("v%s" % VERSION, DIM), None, spacing=10))
        self.signal = Panel("信号")
        self.big_angle = label("无数据", FG)
        self.big_angle.setStyleSheet("color:%s; font-size:40px; font-weight:bold;" % FG)
        self.s_bar, self.s_scale, self.s_info = label("", GREEN), label("", DIM), label("", DIM)
        for w in (self.big_angle, self.s_bar, self.s_scale, self.s_info):
            self.signal.body.addWidget(w)
        root.addWidget(self.signal)
        self.status = Panel("状态")
        self.dot, self.link_text = Dot(9), label("未连接", DIM)
        self.status.body.addLayout(row(self.dot, self.link_text, None))
        self.status_rows = label("", FG)
        self.status.body.addWidget(self.status_rows); root.addWidget(self.status)
        self.controls = Panel("控制")
        self.combo = QComboBox()
        self.combo.setMinimumWidth(260); self.combo.setMaximumWidth(520)
        self.combo.currentIndexChanged.connect(self._port_changed)
        self.btn_link = btn("[ 连接 ]", self._toggle_link)
        self.controls.body.addLayout(row(label("[端口  ]", DIM), self.combo,
                                         btn("[ 刷新端口 ]", lambda: self._scan()),
                                         self.btn_link, None))
        self.btn_primary = btn("[ 启动玻璃效果 ]", self._toggle_glass, "primary",
                               tip="F5 · 启动 / 停止悬浮玻璃层")
        self.btn_primary.setProperty("running", "false"); self.btn_primary.setMinimumHeight(34)
        self.controls.body.addWidget(self.btn_primary)
        self.btn_auto = btn("[ 自动 ]", lambda: self._set_auto(True), checkable=True)
        self.btn_manual = btn("[ 手动 ]", lambda: self._set_auto(False), checkable=True)
        self.btn_auto.setChecked(True)
        self.slider_conc = QSlider(Qt.Orientation.Horizontal)
        self.slider_conc.setRange(0, 1000); self.slider_conc.setEnabled(False)
        self.slider_conc.valueChanged.connect(self._conc_moved)
        self.conc_text = label("0.0 %", FG, width=70)
        self.conc_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        crow = row(label("[浓度  ]", DIM), self.btn_auto, self.btn_manual, self.slider_conc,
                   self.conc_text, spacing=8)
        crow.setStretch(3, 1); self.controls.body.addLayout(crow)
        root.addWidget(self.controls)
        self.tuning = Panel("参数微调")
        self.btn_fold = btn("[ 收起 ]", self._fold)
        self.tuning.body.addLayout(row(label(TUNING_HINT, DIM), None,
                                       btn("[ 恢复默认 ]", self._reset_params), self.btn_fold))
        self.tune_rows = QWidget()
        tgrid = QVBoxLayout(self.tune_rows)
        tgrid.setContentsMargins(0, 0, 0, 0); tgrid.setSpacing(2)
        self.param_rows = {}
        for key, lo, hi, dec, unit in PARAMS:
            sl = Slider(key, lo, hi, dec, unit, self.params[key])
            sl.changed.connect(self._param_changed)
            self.param_rows[key] = sl
            tgrid.addWidget(sl)
        self.tuning.body.addWidget(self.tune_rows); root.addWidget(self.tuning); root.addStretch(1)
        self.log = label("> 正在启动 …", DIM)
        root.addWidget(self.log)
        root.addWidget(label("%s · %s · %s · Qt %s"
                             % (AUTHOR, GITHUB, LICENSE, QT_VERSION_STR), DIM))
        self._min_w, self._min_h = 880, 620
        self.setMinimumSize(self._min_w, self._min_h)
        self.resize(1020, 700)

    def _fit(self):
        """按「已经填好文案」的布局定尺寸：空 QLabel 的 sizeHint 会低估多行文本高度，
        之前先算尺寸再 tick，结果状态面板最后一行被压掉。"""
        lay = self.layout()
        need, want = lay.minimumSize(), lay.sizeHint()
        self._min_w, self._min_h = max(880, need.width()), max(620, need.height())
        self.setMinimumSize(self._min_w, self._min_h)
        self.resize(max(1020, want.width()), max(700, want.height()))

    def showEvent(self, ev):
        """HWND 建好后才能上亚克力/深色标题栏（只做一次）。"""
        super().showEvent(ev)
        if self._glass_info != "off": return
        ok, info = enable_glass(self)
        dark_titlebar(self)
        self._glass_info = ("on:" if ok else "fail:") + info
        self._print("[毛玻璃] %s -> %s" % (info, "已生效" if ok else "未生效"))

    def _tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.app.setQuitOnLastWindowClosed(True)
            return
        self.tray = QSystemTrayIcon(load_icon(), self)
        menu = QMenu()
        for text, slot in (("显示主窗口", self._restore), ("启动玻璃效果", self._toggle_glass),
                           ("退出", self._quit)):
            act = QAction(text, menu)
            act.triggered.connect(slot); menu.addAction(act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._restore() if r in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger) else None)
        self.tray.setToolTip(WINDOW_TITLE); self.tray.show()
        self.app.setQuitOnLastWindowClosed(False)

    def _print(self, text):
        try: print(text)
        except Exception: pass

    def _say(self, text):
        """底部一行日志：``> 12:04:31  串口 COM3 已连接 @115200``。"""
        line = "> %s  %s" % (time.strftime("%H:%M:%S"), text)
        try: self.log.setText(line)
        except Exception: self._print(line)

    def _port(self):
        return str(self.combo.currentData() or self.combo.currentText()).strip().upper()

    def _scan(self, initial=False):
        ports = list_ports()
        want = self._port()
        self.combo.blockSignals(True); self.combo.clear()
        for it in ports: self.combo.addItem(it["label"], it["port"])
        if not ports: self.combo.addItem("未发现串口", "")
        names = [it["port"] for it in ports]
        pick = next((p for p in (want, (self.args.port or "").upper()) if p and p in names), "")
        if not pick and names: pick = names[0]
        if pick: self.combo.setCurrentIndex(max(0, self.combo.findData(pick)))
        self.combo.blockSignals(False)
        self.port_note = "发现 %d 个串口" % len(names) if names else "未发现串口"
        if initial and self._auto_serial and pick:
            QTimer.singleShot(120, lambda: self._open_serial(pick))
        self._say("端口扫描完成：%s" % ("、".join(names) if names else "未发现串口"))

    def _port_changed(self, _i):
        if self._port() and self._port() != self.reader_port: self._open_serial(self._port())

    def _toggle_link(self):
        if self.reader is not None: self._close_serial(); self._say("已断开 %s" % self.reader_port)
        elif self._port(): self._open_serial(self._port())
        else: self._say("没有可连接的串口")

    def _close_serial(self):
        reader, self.reader = self.reader, None
        self.glass.set_reader(None)
        if reader is None: return
        try:
            stop = getattr(reader, "stop", None)
            if callable(stop): stop()
            else:                                     # duo_glass.AngleReader：用 _stop 标记退出
                reader._stop = True
                reader.__dict__.pop("_stop", None)    # 归还 Thread._stop，否则 join 内部会炸
                if reader.is_alive(): reader.join(0.6)  # 让读线程把端口真正放开再重连
        except Exception as exc: self._print("[warn] 停止串口读取时忽略异常: %s" % exc)

    def _open_serial(self, port):
        """打开串口：先用 probe 给出可读提示，再交给 duo_glass.AngleReader 读。"""
        self._close_serial()
        self.reader_port = port
        note = ""
        check = probe_port(port)
        if check is not None and not check[0]: note = check[1]
        if HAVE_SERIAL and DUO is not None:
            try:
                self.reader = DUO.AngleReader(port, int(getattr(self.cfg, "baud", 115200)))
                self.reader.start(); self._say("串口 %s 已连接 @%d" % (port, self.cfg.baud))
            except Exception as exc: note = note or "创建读取器失败：%s" % exc
        if self.reader is None:
            self._say("打不开 %s：%s" % (port, note or "缺少 pyserial（duo_glass 需要它）"))
        elif note: self._say("%s 打不开：%s" % (port, note))
        self._last_sample = time.time()
        self.glass.set_reader(self.reader)

    def _toggle_glass(self):
        if self.glass.running: self.glass.stop(); self._say("玻璃层已停止")
        elif self._demo: self._say("演示模式：不会真的启动玻璃层")
        else:
            ok, err = self.glass.start()
            self._say("玻璃层已启动（全屏置顶 · 点击穿透）" if ok else "玻璃层启动失败：%s" % err)
        self._sync_primary()

    def _sync_primary(self):
        running = self.glass.running or self._demo_run
        self.btn_primary.setText("[ 停止玻璃效果 ]" if running else "[ 启动玻璃效果 ]")
        self.btn_primary.setProperty("running", "true" if running else "false")
        self.btn_primary.style().unpolish(self.btn_primary)
        self.btn_primary.style().polish(self.btn_primary)

    def _set_auto(self, flag):
        self.control.set_auto(flag); self.btn_auto.setChecked(flag)
        self.btn_manual.setChecked(not flag); self.slider_conc.setEnabled(not flag)
        if not flag: self.control.set_target(self.slider_conc.value() / 1000.0)
        self._say("浓度模式：%s" % ("自动跟随角度" if flag else "手动"))

    def _conc_moved(self, v):
        if not self.control.get()[2]: self.control.set_target(v / 1000.0)

    def _param_changed(self, key, value):
        self.params[key] = float(value)
        self.glass.apply_params(self.params)

    def _reset_params(self):
        for k, v in default_params(self.cfg).items():
            self.param_rows[k].set_value(v, emit=False)
            self.params[k] = float(v)
        self.glass.apply_params(self.params); self._say("参数已恢复为 duo_glass.py 的默认值")

    def _fold(self):
        """收起 / 展开参数区：QPropertyAnimation 180ms OutCubic，平滑不跳变。"""
        show = not getattr(self, "_folded", False)
        natural = max(1, self.tune_rows.sizeHint().height())
        start = self.tune_rows.height() if getattr(self, "_folded", False) else 0
        self.btn_fold.setText("[ 收起 ]" if show else "[ 展开 ]"); self.tune_rows.setVisible(True)
        anim = QPropertyAnimation(self.tune_rows, b"maximumHeight", self)
        anim.setDuration(180); anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(start); anim.setEndValue(natural if show else 0)
        anim.finished.connect(lambda: (self.tune_rows.setVisible(show),
                                       setattr(self, "_folded", not show)))
        self._anim = anim
        anim.start()

    def _snapshot(self):
        if self._demo and self._demo_vals:
            self._last_sample = time.time()
            return dict(self._demo_vals)
        if self.reader is None: return dict(angle=None, fps=0.0, status="idle", mode="")
        try: angle, fps, status, mode = self.reader.get()
        except Exception as exc: angle, fps, status, mode = None, 0.0, "error: %s" % exc, ""
        if angle is not None: self._last_sample = time.time()
        return dict(angle=angle, fps=float(fps or 0.0), status=str(status), mode=str(mode))

    def _state_of(self, status):
        """-> (中文状态, 颜色, 类型)。waiting/error 是本地态，其余是固件态。"""
        fresh = (time.time() - self._last_sample) < 3.0
        if status.startswith("error"): return "串口异常", RED, "error"
        if status.startswith("waiting"): return "正在重连", YELLOW, "warn"
        if self.reader is not None or self._demo:
            return (("已连接" if fresh else "已连接 · 无数据"),
                    GREEN if fresh else YELLOW, "ok" if fresh else "warn")
        return ("未连接", YELLOW if self.reader_port else DIM, "warn" if self.reader_port else "idle")

    def tick(self):
        snap = self._snapshot()
        angle, status = snap["angle"], snap["status"]
        running = self.glass.running or self._demo_run
        auto = self.control.get()[2]
        span = float(getattr(self.cfg, "angle_open", 90.0)) or 90.0
        dead = float(self.params.get("deadband", 1.0))
        if angle is None: target = 0.0
        elif running and auto and not self._demo:
            target = self.glass.concentration         # 运行中显示玻璃层的真实浓度
        else:
            target = DUO.angle_to_concentration(angle, span, dead,
                                                self.params.get("neg_scale", 0.0))
        self._conc_shown += (target - self._conc_shown) * 0.35   # 平滑过渡，不跳变
        if abs(target - self._conc_shown) < 0.002: self._conc_shown = target
        state, color, kind = self._state_of(status)
        self.dot.set_color({"ok": "#30D158", "warn": YELLOW,
                            "error": "#FF453A"}.get(kind, DIM))
        self.link_text.setText("%s · %s @115200 · 玻璃层%s"
                               % (state, self.reader_port or "未选端口",
                                  "运行中" if running else "已停止"))
        self.link_text.setStyleSheet("color:%s;" % color)
        self.status_rows.setText("\n".join((
            "[端口  ] %s" % (self.reader_port or "未选择"),
            "[状态  ] %s · 固件 %s" % (state, cn_status(status)),
            "[角度  ] %s" % fmt_angle(angle),
            "[帧率  ] %s" % (("%.1f Hz" % snap["fps"]) if snap["fps"] else "无数据"),
            "[模式  ] %s" % (cn_status(snap["mode"]) if snap["mode"] else "—"),
            "[浓度  ] %s  %5.1f %%" % (bar(self._conc_shown), self._conc_shown * 100.0))))
        cw = max(4, QFontMetrics(self.font()).horizontalAdvance("─"))
        cells = max(24, int((self.signal.width() - 2 * cw - 24) // cw))
        self.big_angle.setText("无数据" if angle is None else "%+.2f°" % angle)
        self.big_angle.setStyleSheet("color:%s; font-size:40px; font-weight:bold;" % color)
        self.s_bar.setText(signal_bar(angle, span, cells)); self.s_bar.setStyleSheet("color:%s;" % color)
        self.s_scale.setText(scale_line(span, cells))
        self.s_info.setText("[方向  ] %s    [量程  ] %+.0f deg    [死区  ] %+.1f deg"
                            "    [模式  ] %s"
                            % ("—" if angle is None else
                               ("正角 · 向观察者拉伸" if angle >= 0 else "负角 · 反向"),
                               span, dead, cn_status(snap["mode"]) if snap["mode"] else "—"))
        self.btn_link.setText("[ 断开 ]" if self.reader is not None else "[ 连接 ]")
        self._sync_primary()
        if auto:
            self.slider_conc.blockSignals(True)
            self.slider_conc.setValue(int(clamp(self._conc_shown, 0.0, 1.0) * 1000))
            self.slider_conc.blockSignals(False)
        shown = self._conc_shown if auto else self.slider_conc.value() / 1000.0
        self.conc_text.setText("%5.1f %%" % (shown * 100.0))
        if self.tray is not None:
            self.tray.setToolTip("%s\n%s · 角度 %s · 帧率 %.1f Hz"
                                 % (WINDOW_TITLE, state, fmt_angle(angle), snap["fps"]))

    def paintEvent(self, _ev):
        """客户区磨砂底：半透明深色（亚克力在它后面模糊桌面）。"""
        p = QPainter(self)
        p.fillRect(self.rect(), rgba(*GLASS_RGBA)); p.setPen(QPen(rgba(*EDGE_RGBA), 1))
        p.drawLine(0, 0, self.width(), 0)             # 顶边 1px 高光
        p.end()

    def closeEvent(self, ev):
        if self._force_quit or self._headless:
            self._cleanup(); ev.accept()
            return
        if self.glass.running:                        # 玻璃层全屏置顶：必须先问一句
            box = QMessageBox(self)
            box.setWindowTitle("玻璃层正在运行"); box.setWindowIcon(load_icon())
            box.setText("悬浮玻璃层还盖在屏幕上（全屏置顶）。要一起停掉吗？")
            box.setInformativeText("[ 停止并退出 ] 先关玻璃层再退出；[ 最小化到托盘 ] 让它继续跑。")
            stop = box.addButton("[ 停止并退出 ]", QMessageBox.ButtonRole.AcceptRole)
            tray = box.addButton("[ 最小化到托盘 ]", QMessageBox.ButtonRole.ActionRole)
            box.addButton("[ 取消 ]", QMessageBox.ButtonRole.RejectRole); box.exec()
            hit = box.clickedButton()
            if hit is stop:
                self._cleanup(); ev.accept()
                return
            ev.ignore()
            if hit is tray: self._to_tray()
            return
        if self.tray is not None:
            ev.ignore(); self._to_tray()
            return
        self._cleanup(); ev.accept()

    def _to_tray(self):
        self.hide()
        if self.tray is not None:
            self.tray.showMessage(WINDOW_TITLE, "窗口已缩到托盘，双击图标可恢复。",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)

    def _restore(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _quit(self):
        self._force_quit = True
        self._cleanup(); self.close(); self.app.quit()

    def _cleanup(self):
        self.timer.stop(); self.glass.stop()
        self._close_serial()
        if self.tray is not None: self.tray.hide()

    def set_demo(self, angle=42.6, running=False):
        """截图/演示用的稳定数据（不碰串口、不开玻璃）。"""
        self._demo = True
        self._demo_vals = dict(angle=float(angle), fps=20.0, status="ok", mode="ok")
        self.reader_port, self.port_note = "COM3", "发现 5 个串口"
        self._demo_run = bool(running)
        self._conc_shown = 0.0
        self._set_auto(True); self._sync_primary()

    def grab_composited(self):
        """截「屏幕上这一块」：能同时拍到原生标题栏 + 亚克力磨砂后的桌面。"""
        geo = self.frameGeometry()
        screen = self.screen() or self.app.primaryScreen()
        pix = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
        return pix if not pix.isNull() else self.grab()


def make_app(argv):
    # 玻璃层着色器是 "#version 330 compatibility"：默认格式必须在创建 QOpenGLWidget
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setSwapInterval(1); QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication.instance() or QApplication(list(argv))
    global MONO, G
    fams = set(QFontDatabase.families())
    MONO = next((f for f in MONO_CANDIDATES if f in fams), "Courier New")
    app.setFont(QFont(MONO, 10))
    G = pick_glyphs(MONO)                            # 字体缺字形时自动退 ASCII
    app.setApplicationName(APPNAME); app.setApplicationVersion(VERSION)
    app.setWindowIcon(load_icon()); app.setStyleSheet(qss())
    return app


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Sui-WinDuo 控制台（终端内容 + 毛玻璃外壳）")
    p.add_argument("--port", default="COM3", help="默认串口")
    p.add_argument("--selftest", action="store_true", help="无窗口自检后退出")
    p.add_argument("--screenshot", metavar="PNG", nargs="?", const="pc/app_ui.png",
                   help="截图后退出（默认拍合成后的窗口，含原生标题栏与磨砂）")
    p.add_argument("--live", action="store_true", help="--screenshot 用真实串口数据")
    p.add_argument("--smoke", type=float, nargs="?", const=4.0, default=0.0, metavar="秒",
                   help="真实窗口跑 N 秒后退出")
    p.add_argument("--with-glass", action="store_true", help="--smoke 时启停一次玻璃层")
    p.add_argument("--demo-ui", action="store_true", help="演示数据（不碰串口/不开玻璃）")
    p.add_argument("--demo-angle", type=float, default=42.6, help="演示角度（可负，看反向）")
    p.add_argument("--demo-running", action="store_true", help="演示「玻璃运行中」状态")
    return p.parse_args(argv)


def run_selftest(args):
    """无窗口自检：依赖 / 字形 / 界面对象 / 映射 / 截屏区域 / 毛玻璃，全过才回 0。"""
    t0, checks = time.time(), []
    print("=" * 72 + "\nSUI-WINDOU 自检（无窗口）\n" + "-" * 72)

    def ok(name, good, detail=""):
        checks.append(bool(good))
        print("  [%s] %-22s %s" % ("通过" if good else "失败", name, detail))

    ok("Qt 运行时", True, "Qt %s / python %s" % (QT_VERSION_STR, sys.version.split()[0]))
    ok("duo_glass 模块", DUO is not None, DUO_ERR or "已导入（只复用，未修改）")
    for name, flag, err in (("着色器源码", DUO is not None and bool(DUO.VS), DUO_ERR),
                            ("PyOpenGL", HAVE_GL, GL_ERR), ("mss", HAVE_MSS, MSS_ERR),
                            ("pyserial", HAVE_SERIAL, SERIAL_ERR),
                            ("serial_reader_win", HAVE_WINREADER, WINREADER_ERR),
                            ("glass_overlay 亚克力", HAVE_GLASS, GLASS_ERR)):
        ok(name, flag, err or "ok")
    app = make_app([sys.argv[0]])
    ok("等宽字形宽度", _glyphs_ok(MONO), "字体 %s，字形 %s"
       % (MONO, "".join(G[k] for k in ("tl", "h", "tr", "bl", "br", "l", "m", "r", "k",
                                       "full", "void"))))
    ok("串口枚举", True, "、".join(p["port"] for p in list_ports()) or "无")
    cfg = build_cfg(args.port, demo=-1.0)
    win = MainWindow(args, cfg, app)
    ok("构建主窗口", True, "%dx%d（最小 %dx%d），%d 个控件"
       % (win.width(), win.height(), win._min_w, win._min_h, len(win.findChildren(QWidget))))
    ok("原生标题栏", not (win.windowFlags() & Qt.WindowType.FramelessWindowHint)
       and win.windowTitle() == WINDOW_TITLE, "无边框关闭，标题=%r" % win.windowTitle())
    scre, reg = app.primaryScreen(), win.glass.capture_region()
    ok("截屏区域=屏幕物理分辨率",
       reg["width"] == int(scre.geometry().width() * scre.devicePixelRatio())
       and reg["height"] == int(scre.geometry().height() * scre.devicePixelRatio()),
       "%dx%d（dpr=%.2f，未传 scale，GL 视口一致 -> 不会被放大）"
       % (reg["width"], reg["height"], scre.devicePixelRatio()))
    want = dict(angle_open=90.0, deadband=1.0, neg_scale=0.0, max_tilt_deg=88.0, eye_dist_h=2.0,
                blur_spread=0.42, darkening=0.001, max_taps=32)
    bad = {k: getattr(cfg, k) for k, v in want.items() if float(getattr(cfg, k)) != v}
    ok("默认参数取自 duo_glass", not bad, "angle_open=%.0f deadband=%.1f neg_scale=%.2f "
       "max_tilt=%.0f eye=%.1f spread=%.2f dark=%.4f taps=%d refresh=%.1f%s"
       % (cfg.angle_open, cfg.deadband, cfg.neg_scale, cfg.max_tilt_deg, cfg.eye_dist_h,
          cfg.blur_spread, cfg.darkening, cfg.max_taps, float(cfg.refresh_hz),
          (" 不一致 %s" % bad) if bad else "（refresh_hz 以 duo_glass.py 为准）"))
    mapped = [(a, DUO.angle_to_concentration(a, 90.0, 1.0, 0.0)) for a in (0.0, 45.2, 90.0, -45.0)]
    ok("角度→浓度映射", abs(mapped[0][1]) < 1e-6 and abs(mapped[2][1] - 1.0) < 1e-6
       and abs(mapped[3][1]) < 1e-6 and 0.45 < mapped[1][1] < 0.52,
       " ".join("%+.0f→%.3f" % m for m in mapped))
    ok("ASCII 条助手", bar(0.5, 10).count(G["full"]) == 5
       and len(signal_bar(-45, 90, 12)) == 12 and G["m"] in signal_bar(0, 90, 12),
       "bar(0.5,10)=%s 条(+45)=%s" % (bar(0.5, 10), signal_bar(45, 90, 12)))
    win.set_demo()
    for _ in range(8): win.tick()
    ok("界面刷新（演示数据）", win.big_angle.text() != "无数据",
       "角度=%s 浓度=%s" % (win.big_angle.text(), win.conc_text.text()))
    for line in win.status_rows.text().splitlines() + [win.s_bar.text(), win.s_info.text()]:
        print("  [信息] %s" % line)
    err = ""
    try: win.param_rows["blur_spread"].set_value(0.80); win._fold(); win._fold()
    except Exception as exc: err = "%s: %s" % (type(exc).__name__, exc)
    ok("参数滑块 + 折叠动画", not err, err or "滑块 / 折叠 / apply_params 均正常")
    ok("托盘 + 图标", (_ROOT / "icon.png").exists(),
       "托盘可用" if QSystemTrayIcon.isSystemTrayAvailable() else "本机无托盘")
    win.show(); app.processEvents()
    ok("毛玻璃", win._glass_info.startswith("on:"),
       "enable_glass -> %s" % win._glass_info)
    win._cleanup(); app.processEvents()
    print("-" * 72 + "\n结果：%s（%.2fs）\n"
          % ("通过" if all(checks) else "失败", time.time() - t0) + "=" * 72)
    return 0 if all(checks) else 1


def _glyphs_ok(mono):
    """方框/色块字符必须和 ASCII 同宽，否则方框和 ASCII 条会错位。"""
    fm = QFontMetrics(QFont(mono, 10))
    chars = "".join(G.get(k, "") for k in ("tl", "h", "tr", "bl", "br", "m", "k",
                                           "full", "void")) + "A0"
    return len({fm.horizontalAdvance(c) for c in chars if c}) == 1


def run_screenshot(args):
    app = make_app([sys.argv[0]])
    demo = (not args.live) or args.demo_ui
    win = MainWindow(args, build_cfg(args.port, demo=0.45 if demo else -1.0), app)
    if demo: win.set_demo(angle=args.demo_angle, running=args.demo_running)
    win.show()
    out = Path(args.screenshot)

    def snap():
        try:
            force_topmost(win, True)                  # 别被别的窗口挡住
            app.processEvents(); time.sleep(0.25)
            pix = win.grab_composited()
            out.parent.mkdir(parents=True, exist_ok=True)
            print("[截图] 合成（含原生标题栏与磨砂）-> %s（%dx%d，保存=%s，毛玻璃=%s）"
                  % (out, pix.width(), pix.height(), pix.save(str(out)), win._glass_info))
            force_topmost(win, False)
        except Exception as exc: print("[截图] 失败：%s: %s" % (type(exc).__name__, exc))
        win._force_quit = True
        win._cleanup(); app.quit()

    QTimer.singleShot(1200, snap)
    return app.exec()


def main(argv=None):
    args = parse_args(argv)
    global DUO, DUO_ERR
    if DUO is None:                      # 队友可能正在改 duo_glass.py，多等几秒再试
        DUO, DUO_ERR = load_duo_glass(retries=3, delay=1.0)
    if args.selftest:
        args.screenshot, args.smoke = None, 0.0
        return run_selftest(args)
    if args.screenshot: return run_screenshot(args)
    app = make_app([sys.argv[0]])
    win = MainWindow(args, build_cfg(args.port, demo=0.5 if args.demo_ui else -1.0), app)
    if args.with_glass:                  # 冒烟用：1/3 处启动、2/3 处停止玻璃层
        QTimer.singleShot(int(args.smoke * 300), win._toggle_glass)
        QTimer.singleShot(int(args.smoke * 700), win._toggle_glass)
    app.aboutToQuit.connect(win._cleanup); win.show()
    if args.smoke:
        print("[冒烟] hwnd=%s 标题=%r 毛玻璃=%s 边框=%s"
              % (int(win.winId()), win.windowTitle(), win._glass_info,
                 "原生" if not (win.windowFlags() & Qt.WindowType.FramelessWindowHint) else "自绘"))
        print("[冒烟] 窗口已显示，%.1fs 后自动关闭（期间启停一次玻璃层）" % args.smoke)
    elif DUO is None: win._say("duo_glass 不可用（%s）：玻璃层已禁用" % DUO_ERR)
    return app.exec()


if __name__ == "__main__": sys.exit(main())
