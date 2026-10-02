# -*- coding: utf-8 -*-
"""Sui-WinDuo 控制台（PyQt6）— 终端 / ncurses 风格
====================================================
纯黑底、等宽字体、方框字符边框、方括号按钮、ASCII 进度条的控制台，替代直接跑
``pc/duo_glass.py`` 命令行：

  STATUS  端口/状态/FPS/角度/浓度        SIGNAL  双向 ASCII 角度条
  CONTROL 串口、[ START GLASS ]、[ AUTO ]/[ MANUAL ]、浓度滑块
  TUNING  6 个实时参数滑块               底部一行日志

复用约定（别踩坑）
------------------
* 玻璃层永远是独立的 ``duo_glass.GlassGLWidget``；本文件**只 import、绝不修改**
  ``duo_glass.py``，参数默认值直接取自它的 ``build_cfg()``，两边永远一致。
* 创建 GL 组件前必须 ``QSurfaceFormat.setDefaultFormat(3.3 兼容)``，否则
  ``#version 330 compatibility`` 着色器编译失败 -> 黑屏。
* ``GlassGLWidget`` 要一个 duck-typing 的手动控制对象（``get()`` + ``quit_flag``）：
  见 ``UiGlassControl``；``quit_flag`` 绝不能置 True，那会 ``QApplication.quit()``
  把整个控制台一起关掉。
* 串口在 ``AngleReader`` 线程里读、截图在 ``CaptureWorker`` 线程里做，UI 线程只用
  100ms 的 QTimer 取快照；端口被占用时给可读提示、不崩。

自测：``--selftest`` / ``--screenshot pc\\app_ui.png`` / ``--smoke 3 [--with-glass]``
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
    from PyQt6.QtCore import QT_VERSION_STR, QPointF, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics,
                             QIcon, QKeySequence, QPainter, QPen, QPixmap,
                             QShortcut, QSurfaceFormat)
    from PyQt6.QtWidgets import (QApplication, QComboBox, QGridLayout, QHBoxLayout,
                                 QLabel, QMenu, QMessageBox, QPushButton, QSizePolicy,
                                 QSlider, QSystemTrayIcon, QVBoxLayout, QWidget)
except Exception as _exc:                                                # pragma: no cover
    sys.stderr.write("\n[致命] 无法导入 PyQt6：%s\n"
                     "  请用 .venv\\Scripts\\python.exe pc\\sui_winduo_app.py 运行\n"
                     "  未安装：.venv\\Scripts\\python.exe -m pip install PyQt6\n\n" % _exc)
    raise SystemExit(2)

APP, SUB = "SUI-WINDOU", "sui-winduo console / 悬浮玻璃 · 屏幕开合角"
AUTHOR, LICENSE, VERSION = "EthanMaven", "MIT", "1.0"
BG, FG, DIM, LINE = "#000000", "#D0D0D0", "#8A8A8A", "#3C3C3C"     # 黑底 + 灰阶
GREEN, AMBER, RED = "#00FF00", "#FFB000", "#FF5555"                # 只用 3 个强调色
MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Courier New", "DejaVu Sans Mono")
MONO = "Consolas"                                    # make_app() 里按本机字体再挑
QSS = """
* { font-family: "@M"; font-size: 13px; }
QWidget { background: @BG; color: @FG; }
QLabel { background: transparent; }
QPushButton { background: @BG; color: @FG; border: 1px solid @L; padding: 3px 10px;
              border-radius: 0px; }
QPushButton:hover { background: @FG; color: @BG; border-color: @FG; }
QPushButton:pressed { background: @D; color: @BG; }
QPushButton:checked { background: @FG; color: @BG; border-color: @FG; }
QPushButton:disabled { color: @L; border-color: @L; }
QPushButton#primary { color: @G; border-color: @G; padding: 7px 10px; }
QPushButton#primary:hover { background: @G; color: @BG; }
QPushButton#primary[running="true"] { color: @R; border-color: @R; }
QPushButton#primary[running="true"]:hover { background: @R; color: @BG; }
QPushButton#win { padding: 0px; min-width: 34px; max-width: 34px; min-height: 19px;
                  max-height: 19px; }
QComboBox { background: @BG; color: @FG; border: 1px solid @L; padding: 3px 6px;
            border-radius: 0px; }
QComboBox:hover { border-color: @FG; }
QComboBox::drop-down { border: none; width: 0px; }
QComboBox::down-arrow { image: none; width: 0px; height: 0px; }
QComboBox QAbstractItemView { background: @BG; color: @FG; border: 1px solid @L;
            outline: none; selection-background-color: @FG; selection-color: @BG; }
QSlider::groove:horizontal { height: 2px; background: @L; border-radius: 0px; }
QSlider::sub-page:horizontal { height: 2px; background: @G; border-radius: 0px; }
QSlider::add-page:horizontal { height: 2px; background: @L; border-radius: 0px; }
QSlider::handle:horizontal { background: @FG; width: 8px; height: 14px; margin: -6px 0px;
            border: none; border-radius: 0px; }
QSlider::handle:horizontal:hover { background: @G; }
QSlider::sub-page:horizontal:disabled { background: @L; }
QSlider::handle:horizontal:disabled { background: @D; }
QMenu { background: @BG; color: @FG; border: 1px solid @L; }
QMenu::item { padding: 4px 16px; }
QMenu::item:selected { background: @FG; color: @BG; }
QMessageBox { background: @BG; }
QToolTip { background: @BG; color: @FG; border: 1px solid @L; }
"""


def qss():
    """终端风格样式表：零圆角、零渐变，hover 只反色。"""
    out = QSS
    for token, value in (("@M", MONO), ("@BG", BG), ("@FG", FG), ("@D", DIM),
                         ("@L", LINE), ("@G", GREEN), ("@R", RED)):
        out = out.replace(token, value)
    return out


def _have(name):
    try:
        __import__(name)
        return True, ""
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def load_duo_glass(retries=3, delay=0.6):
    """导入 duo_glass；队友可能正改到一半，失败就等一会儿重试 -> (mod, err)。"""
    last = None
    for i in range(max(1, retries)):
        try:
            import duo_glass
            missing = [n for n in ("GlassGLWidget", "AngleReader", "CaptureWorker",
                                   "angle_to_concentration", "build_cfg", "VS", "FS_DUO")
                       if not hasattr(duo_glass, n)]
            if not missing:
                return duo_glass, None
            last = "duo_glass 缺少符号: %s" % ", ".join(missing)
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        if i + 1 < max(1, retries):
            time.sleep(delay)
    return None, str(last)


DUO, DUO_ERR = load_duo_glass(retries=1, delay=0.0)
HAVE_SERIAL, SERIAL_ERR = _have("serial")
HAVE_MSS, MSS_ERR = _have("mss")
HAVE_GL, GL_ERR = _have("OpenGL")
HAVE_WINREADER, WINREADER_ERR = _have("serial_reader_win")


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def bar(frac, cells=24, fill="█", empty="░"):
    """ASCII 进度条。"""
    n = int(round(clamp(float(frac), 0.0, 1.0) * cells))
    return fill * n + empty * (cells - n)


def signal_bar(angle, span, cells):
    """双向角度条：中点 ┼ = 0°，向右为正（> 收尾），向左为负（< 收尾）。"""
    half, row = max(1, cells // 2), ["░"] * cells
    row[half] = "┼"
    if angle is not None and span > 0:
        pos = int(round(clamp(float(angle) / span, -1.0, 1.0) * half))
        if pos > 0:
            for i in range(half + 1, min(cells, half + pos + 1)):
                row[i] = "█"
            row[min(cells - 1, half + pos)] = ">"
        elif pos < 0:
            for i in range(max(0, half + pos), half):
                row[i] = "█"
            row[max(0, half + pos)] = "<"
    return "".join(row)


def scale_line(span, cells):
    """与 signal_bar 等宽的刻度行：-span / 0 / +span。"""
    row = [" "] * cells
    left, mid, right = "%+.0f" % (-span), "0", "%+.0f" % span
    row[0:len(left)] = list(left)
    row[max(0, cells - len(right)):] = list(right)
    c = max(len(left) + 1, (cells - len(mid)) // 2)
    row[c:c + len(mid)] = list(mid)
    return "".join(row)


def fmt_angle(v):
    return "—" if v is None else "%+.2f°" % float(v)


def load_icon(size=256):
    """窗口/托盘图标：项目根目录的 icon.png，缺失时退化成一个手画方块。"""
    path = _ROOT / "icon.png"
    if path.exists():
        return QIcon(str(path))
    pm = QPixmap(size, size)
    pm.fill(QColor(BG))
    p = QPainter(pm)
    p.setPen(QPen(QColor(GREEN), 8))
    p.drawRect(12, 12, size - 24, size - 24)
    p.setFont(QFont(MONO, int(size * 0.3)))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "SW")
    p.end()
    return QIcon(pm)


def list_ports():
    """枚举串口 -> [{'port','label'}]：pyserial，退化用 ctypes/winreg 实现。"""
    raw = []
    if HAVE_SERIAL:
        try:
            from serial.tools import list_ports as lp
            raw = [(str(getattr(i, "device", "") or ""),
                    " ".join(str(getattr(i, "description", "") or "").split()))
                   for i in lp.comports()]
        except Exception:
            raw = []
    if not raw and HAVE_WINREADER:
        try:
            import serial_reader_win as srw
            raw = [(it.get("port", ""), it.get("device", ""))
                   for it in srw.list_com_port_details()]
        except Exception:
            raw = []
    out, seen = [], set()
    for port, desc in raw:
        port = str(port).strip().upper()
        if port and port not in seen:
            seen.add(port)
            out.append({"port": port,
                        "label": ("%s · %s" % (port, desc)) if desc else port})
    digits = lambda p: p[3:] if p[3:].isdigit() else "9999"    # COM10 排在 COM9 后
    return sorted(out, key=lambda it: (int(digits(it["port"])), it["port"]))


def probe_port(port):
    """开连之前体检一次，拿一句可读的中文（端口被占用 / 不存在 / 可用）。"""
    if not (HAVE_WINREADER and os.name == "nt" and port):
        return None
    try:
        import serial_reader_win as srw
        ok, reason = srw.WinSerialReader(port=port, baudrate=115200).probe()
        return (bool(ok), str(reason))
    except Exception as exc:
        return (False, "串口体检失败：%s" % exc)


def build_cfg(port, demo=-1.0):
    """参数对象：默认值直接取自 duo_glass.build_cfg()，保证与上游一致。"""
    cfg = None
    if DUO is not None:
        old = sys.argv
        try:
            sys.argv = [old[0]]                      # 屏蔽本程序的 --screenshot 等开关
            cfg = DUO.build_cfg()
        except BaseException:
            cfg = None
        finally:
            sys.argv = old
    if cfg is None:                                  # 兜底：与 duo_glass.py 默认值对齐
        cfg = argparse.Namespace(port="COM3", baud=115200, manual=False, angle_open=90.0,
                                 deadband=1.0, neg_scale=0.0, refresh_hz=3.0,
                                 max_tilt_deg=88.0, eye_dist_h=2.0, blur_spread=0.42,
                                 darkening=0.001, max_taps=32, lock_at_close=False,
                                 selftest=False, smoke=False, demo=-1.0, trace=False)
    cfg.port, cfg.demo = port, float(demo)
    return cfg


# key, 显示名, min, max, 小数位, 单位；默认值运行时取自 duo_glass.build_cfg()
PARAMS = (("blur_spread", 0.05, 1.20, 2, ""), ("darkening", 0.0, 0.008, 4, ""),
          ("max_tilt_deg", 10.0, 88.0, 0, "deg"), ("eye_dist_h", 0.5, 4.0, 2, "x"),
          ("deadband", 0.0, 10.0, 1, "deg"), ("neg_scale", 0.0, 1.0, 2, ""))
def default_params(cfg):
    return {k: float(getattr(cfg, k, 0.0)) for k, *_ in PARAMS}


class UiGlassControl:
    """``duo_glass.ManualControl`` 的鸭类型替身：``get() -> (target, key, auto)``。

    ``quit_flag`` 永远是 False——置 True 会让 GL 组件直接 ``QApplication.quit()``。
    """

    def __init__(self):
        self.quit_flag, self._target, self._auto = False, 0.0, True

    def get(self):
        return (self._target, "", self._auto)

    def set_target(self, v):
        self._target = clamp(float(v), 0.0, 1.0)

    def set_auto(self, flag):
        self._auto = bool(flag)


class ReaderBridge:
    """把 ``serial_reader_win.WinSerialReader`` 适配成 ``AngleReader.get()`` 风格。"""

    def __init__(self, reader):
        self._r, self._valid, self._t, self._fps = reader, 0, time.time(), 0.0

    def start(self):
        return self._r.start()

    def get(self):
        now, sample = time.time(), self._r.latest()
        try:
            valid = int(self._r.stats().get("valid", 0))
        except Exception:
            valid = self._valid
        if now - self._t >= 0.5:
            self._fps = max(0.0, (valid - self._valid) / (now - self._t))
            self._valid, self._t = valid, now
        if sample is None:
            err = self._r.last_error
            return (None, self._fps, ("error: %s" % err) if err else
                    ("waiting" if self._r.is_running else "idle"), "")
        return (float(sample.get("angle", 0.0)), self._fps,
                str(sample.get("status", "ok")), str(sample.get("mode", "")))

    def stop(self):
        try:
            self._r.stop()
        except Exception:
            pass


class GlassController:
    """玻璃层生命周期：截图线程 -> GL 组件 -> 参数热更新。"""

    # 界面参数名 -> GL 组件实例属性名（x 为换算函数）
    ATTRS = (("blur_spread", "spread", float), ("darkening", "dark", float),
             ("max_tilt_deg", "max_tilt", math.radians), ("eye_dist_h", "eye_h", float),
             ("deadband", "deadband", float), ("neg_scale", "neg_scale", float))

    def __init__(self, app, cfg, control, log=print):
        self.app, self.cfg, self.control, self.log = app, cfg, control, log
        self._capturer = self._widget = self._reader = None
        self.last_params, self.last_error = {}, ""

    def set_reader(self, reader):
        """换端口/断开时同步更新活着的 GL 组件（它构造时缓存了 reader）。"""
        self._reader = reader
        if self._widget is not None:
            try:
                self._widget.reader = reader
            except Exception as exc:
                self.log("[warn] 更新玻璃层 reader 失败: %s" % exc)

    def _screen(self):
        screen = self.app.primaryScreen()
        dpr, geo = screen.devicePixelRatio(), screen.geometry()
        region = {"left": geo.x(), "top": geo.y(), "width": int(geo.width() * dpr),
                  "height": int(geo.height() * dpr)}
        return screen, region

    def _ensure_capture(self):
        """单实例 CaptureWorker：停止玻璃后不再 kick，线程阻塞在 Event.wait() 零 CPU，
        下次启动直接复用（duo_glass 的 CaptureWorker 没有 stop()，本文件也不许改它）。
        """
        if self._capturer is None:
            self._capturer = DUO.CaptureWorker(self._screen()[1])
            self._capturer.start()
        return self._capturer

    @property
    def running(self):
        return self._widget is not None

    @property
    def concentration(self):
        return float(getattr(self._widget, "g", 0.0)) if self._widget else 0.0

    def start(self):
        if self.running:
            return True, ""
        if DUO is None or not HAVE_GL or not HAVE_MSS:
            return False, "duo_glass/依赖不可用：%s" % (DUO_ERR or GL_ERR or MSS_ERR)
        try:
            cap = self._ensure_capture()
            w = DUO.GlassGLWidget(self._screen()[0], self._reader, cap, self.control, self.cfg)
            self.apply_params(self.last_params, w)
            w.show()
            w.shown = True
            cap.kick()
            self._widget = w
            return True, ""
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._widget = None
            return False, self.last_error

    def stop(self):
        """停掉 GL 组件的 QTimer 并隐藏/关闭；绝不能碰 quit_flag（会连控制台一起退）。"""
        w, self._widget = self._widget, None
        if w is None:
            return
        for step in (lambda: getattr(w, "timer", None) and w.timer.stop(),
                     w.hide, w.close, w.deleteLater):
            try:
                step()
            except Exception as exc:
                self.log("[warn] 停止玻璃层时忽略异常: %s" % exc)

    def apply_params(self, params, widget=None):
        """把界面参数写进活着的 GL 组件（它构造时把 cfg 拷成了实例属性）。"""
        if params:
            self.last_params = dict(params)
            for k, v in params.items():
                try:
                    setattr(self.cfg, k, float(v))
                except Exception:
                    pass
        w, params = widget or self._widget, params or self.last_params
        for key, attr, conv in (self.ATTRS if w is not None and params else ()):
            if key in params and hasattr(w, attr):
                try:
                    setattr(w, attr, conv(params[key]))
                except Exception as exc:
                    self.log("[warn] 参数 %s 热更新失败: %s" % (key, exc))


class Panel(QWidget):
    """用 ┌ ─ ┐ │ └ ┘ 画的终端方框；内容放进 ``self.body``。"""

    def __init__(self, title, accent=GREEN, parent=None):
        super().__init__(parent)
        self.title, self.accent = title, accent
        self.body = QVBoxLayout(self)
        self.body.setSpacing(2)
        self._margins()

    def _metrics(self):
        fm = QFontMetrics(self.font())
        return fm, max(4, fm.horizontalAdvance("─")), fm.height()

    def _margins(self):
        _fm, cw, lh = self._metrics()
        self.body.setContentsMargins(cw + 8, lh + 3, cw + 8, lh + 3)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == ev.Type.FontChange:
            self._margins()

    def paintEvent(self, _ev):
        fm, cw, lh = self._metrics()
        p = QPainter(self)
        p.setFont(self.font())
        p.fillRect(self.rect(), QColor(BG))
        cells = max(12, int((self.width() - 4) // cw))
        top = "┌─ " + self.title + " " + "─" * max(0, cells - 5 - len(self.title)) + "┐"
        p.setPen(QColor(LINE))
        p.drawText(2, fm.ascent(), top)
        p.setPen(QColor(self.accent))                 # 标题用强调色压在灰框上
        p.drawText(2 + int(cw * 3), fm.ascent(), self.title)
        p.setPen(QColor(LINE))
        x1, x2 = 2 + cw / 2.0, self.width() - 2 - cw / 2.0
        p.drawLine(QPointF(x1, 0.0), QPointF(x1, float(self.height())))
        p.drawLine(QPointF(x2, 0.0), QPointF(x2, float(self.height())))
        p.drawText(2, self.height() - fm.descent(), "└" + "─" * (cells - 2) + "┘")
        p.end()


def label(text, color=FG, tip=None):
    lbl = QLabel(text)
    lbl.setStyleSheet("color:%s;" % color)
    lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    if tip:
        lbl.setToolTip(tip)
    return lbl


def row(*widgets, spacing=8):
    """一行控件；传 None 表示插入一个伸缩空隙。"""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return lay


def btn(text, slot, obj=None, checkable=False, tip=None, width=None):
    b = QPushButton(text)
    if obj:
        b.setObjectName(obj)
    b.setCheckable(checkable)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    if width:
        b.setMinimumWidth(width)
    b.clicked.connect(slot)
    return b


class TerminalSlider(QWidget):
    """一行滑块：``blur_spread  [====|====]  0.42``（真实 QSlider + 等宽值）。"""

    changed = pyqtSignal(str, float)

    def __init__(self, key, lo, hi, dec, unit, value, parent=None):
        super().__init__(parent)
        self.key, self.lo, self.hi, self.dec, self.unit = key, lo, hi, dec, unit
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setMinimumWidth(220)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.valueChanged.connect(self._moved)
        self.value = label("", FG)
        self.value.setMinimumWidth(78)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay = row(label(key.ljust(16), DIM), self.slider, self.value)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setStretch(1, 1)
        self.setLayout(lay)
        self.set_value(value, emit=False)

    def text(self, v=None):
        return ("%." + str(self.dec) + "f") % (self.raw() if v is None else v) + self.unit

    def raw(self):
        return self.lo + (self.slider.value() / 1000.0) * (self.hi - self.lo)

    def _moved(self, _v):
        self.value.setText(self.text())
        self.changed.emit(self.key, self.raw())

    def set_value(self, v, emit=True):
        v = clamp(float(v), self.lo, self.hi)
        self.slider.blockSignals(True)
        self.slider.setValue(int(round((v - self.lo) / (self.hi - self.lo) * 1000)))
        self.slider.blockSignals(False)
        self.value.setText(self.text(v))
        if emit:
            self.changed.emit(self.key, v)


class MainWindow(QWidget):
    def __init__(self, args, cfg, app):
        super().__init__()
        self.args, self.cfg, self.app = args, cfg, app
        self.control = UiGlassControl()
        self.glass = GlassController(app, cfg, self.control, log=self._print)
        self.reader, self.reader_port, self.port_note = None, "", ""
        self._last_sample, self._demo = 0.0, bool(args.demo_ui)
        self._demo_run, self._demo_vals = False, None
        self._auto_serial = bool(args.live or args.smoke or not (args.selftest or args.screenshot))
        self._headless = bool(args.selftest or args.screenshot or args.smoke)
        self._force_quit = False
        self.params = default_params(cfg)
        self.setWindowTitle("%s %s · %s" % (APP, VERSION, LICENSE))
        self.setWindowIcon(load_icon())
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._build()
        self._tray()
        for keys, slot in (("F5", self._toggle_glass), ("Ctrl+R", self._scan),
                           ("Ctrl+Q", self._quit)):
            QShortcut(QKeySequence(keys), self, activated=slot)
        self._scan(initial=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(100)
        self.tick()
        if args.smoke:
            QTimer.singleShot(int(float(args.smoke) * 1000), self._quit)

    # ------------------------------------------------------------------ 布局
    def _kv(self, grid, r, c, key):
        """``[KEY   ] value`` 的一格，返回可改颜色/文本的值标签。"""
        grid.addWidget(label("[%-5s]" % key, DIM), r, c)
        grid.addWidget(val := label("—", FG), r, c + 1)
        return val

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        head = Panel("%s  ·  v%s  ·  %s  ·  %s" % (APP, VERSION, LICENSE, AUTHOR))
        self.chip_glass = label("[ GLASS ] STOPPED", DIM)
        head.body.addLayout(row(label(SUB, DIM), _stretch(),
                                self.chip_glass,
                                btn("[ _ ]", self.showMinimized, "win", tip="minimize"),
                                btn("[ X ]", self.close, "win", tip="quit"), spacing=10))
        root.addWidget(head)

        self.status = Panel("STATUS")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(2)
        self.f_port = self._kv(grid, 0, 0, "PORT")
        self.f_dev = self._kv(grid, 1, 0, "DEV")
        self.f_angle = self._kv(grid, 2, 0, "ANGLE")
        self.f_state = self._kv(grid, 0, 2, "STATE")
        self.f_fps = self._kv(grid, 1, 2, "FPS")
        self.f_conc = self._kv(grid, 2, 2, "CONC")
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        self.status.body.addLayout(grid)
        root.addWidget(self.status)

        self.signal = Panel("SIGNAL")
        self.s_bar, self.s_scale = label("", GREEN), label("", LINE)
        self.s_angle, self.s_dir, self.s_conc = label("", GREEN), label("", DIM), label("", FG)
        self.signal.body.addWidget(self.s_bar)
        self.signal.body.addWidget(self.s_scale)
        self.signal.body.addLayout(row(self.s_angle, self.s_dir, _stretch(),
                                       self.s_conc, spacing=16))
        root.addWidget(self.signal)

        self.controls = Panel("CONTROL")
        self.combo = QComboBox()
        self.combo.setMinimumWidth(240)
        self.combo.setMaximumWidth(520)
        self.combo.currentIndexChanged.connect(self._port_changed)
        self.btn_link = btn("[ CONNECT ]", self._toggle_link)
        self.controls.body.addLayout(row(label("PORT", DIM), self.combo,
                                         btn("[ REFRESH ]", lambda: self._scan()),
                                         self.btn_link, _stretch()))
        self.btn_primary = btn("[ START GLASS ]", self._toggle_glass, "primary", tip="F5")
        self.btn_primary.setProperty("running", "false")
        self.btn_primary.setMinimumHeight(32)
        self.controls.body.addWidget(self.btn_primary)
        self.btn_auto = btn("[ AUTO ]", lambda: self._set_auto(True), checkable=True)
        self.btn_manual = btn("[ MANUAL ]", lambda: self._set_auto(False), checkable=True)
        self.btn_auto.setChecked(True)
        self.slider_conc = QSlider(Qt.Orientation.Horizontal)
        self.slider_conc.setRange(0, 1000)
        self.slider_conc.setEnabled(False)
        self.slider_conc.valueChanged.connect(self._conc_moved)
        self.conc_text = label("0.0 %", FG)
        self.conc_text.setMinimumWidth(74)
        self.conc_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        crow = row(label("MODE", DIM), self.btn_auto, self.btn_manual, label("CONC", DIM),
                   self.slider_conc, self.conc_text, spacing=8)
        crow.setStretch(4, 1)
        self.controls.body.addLayout(crow)
        root.addWidget(self.controls)

        self.tuning = Panel("TUNING")
        self.btn_fold = btn("[ HIDE ]", self._fold)
        self.tuning.body.addLayout(row(label("realtime, applied to the live glass layer", DIM),
                                       _stretch(), btn("[ RESET ]", self._reset_params),
                                       self.btn_fold))
        self.tune_rows = QWidget()
        tgrid = QVBoxLayout(self.tune_rows)
        tgrid.setContentsMargins(0, 0, 0, 0)
        tgrid.setSpacing(1)
        self.param_rows = {}
        for key, lo, hi, dec, unit in PARAMS:
            sl = TerminalSlider(key, lo, hi, dec, unit, self.params[key])
            sl.changed.connect(self._param_changed)
            self.param_rows[key] = sl
            tgrid.addWidget(sl)
        self.tuning.body.addWidget(self.tune_rows)
        root.addWidget(self.tuning)
        root.addStretch(1)
        self.log = label("> booting …", DIM)
        root.addWidget(self.log)

        need = root.minimumSize()
        self._min_w, self._min_h = max(900, need.width()), max(620, need.height())
        self.setMinimumSize(self._min_w, self._min_h)
        self.resize(max(1010, self._min_w), max(700, self._min_h))

    def _tray(self):
        self.tray = None
        if self.args.no_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            self.app.setQuitOnLastWindowClosed(True)
            return
        self.tray = QSystemTrayIcon(load_icon(), self)
        menu = QMenu()
        for text, slot in (("[ SHOW WINDOW ]", self._restore),
                           ("[ TOGGLE GLASS ]", self._toggle_glass), ("[ QUIT ]", self._quit)):
            menu.addAction(QAction(text, menu, triggered=slot))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._restore() if r in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger) else None)
        self.tray.setToolTip("%s console" % APP)
        self.tray.show()
        self.app.setQuitOnLastWindowClosed(False)

    # ------------------------------------------------------------------ 日志
    def _print(self, text):
        try:
            print(text)
        except Exception:
            pass

    def _say(self, text):
        """底部一行日志（时间戳 + 事件）。"""
        line = "> %s %s" % (time.strftime("%H:%M:%S"), text)
        try:
            self.log.setText(line)
        except Exception:
            self._print(line)

    # ------------------------------------------------------------------ 串口
    def _port(self):
        return str(self.combo.currentData() or self.combo.currentText()).strip().upper()

    def _scan(self, initial=False):
        ports = list_ports()
        want = self._port()
        self.combo.blockSignals(True)
        self.combo.clear()
        for it in ports:
            self.combo.addItem(it["label"], it["port"])
        if not ports:
            self.combo.addItem("no serial port found", "")
        names = [it["port"] for it in ports]
        pick = next((p for p in (want, (self.args.port or "").upper()) if p and p in names), "")
        if not pick and names:
            pick = names[0]
        if pick:
            self.combo.setCurrentIndex(max(0, self.combo.findData(pick)))
        self.combo.blockSignals(False)
        self.port_note = "%d port(s)" % len(names) if names else "no serial port"
        if initial and self._auto_serial and pick:
            QTimer.singleShot(120, lambda: self._open_serial(pick))
        self._say("serial scan: %s" % (", ".join(names) if names else "none found"))

    def _port_changed(self, _i):
        port = self._port()
        if port and port != self.reader_port:
            self._open_serial(port)

    def _toggle_link(self):
        if self.reader is not None:
            self._close_serial()
            self._say("disconnected from %s" % self.reader_port)
        elif self._port():
            self._open_serial(self._port())
        else:
            self._say("no port to connect")

    def _close_serial(self):
        reader, self.reader = self.reader, None
        self.glass.set_reader(None)
        if reader is None:
            return
        try:
            if hasattr(reader, "stop"):
                reader.stop()
            else:                                     # duo_glass.AngleReader
                reader._stop = True
                if reader.is_alive():
                    reader.join(0.6)                  # 让读线程把端口真正放开再重连
        except Exception as exc:
            self._print("[warn] 停止串口读取时忽略异常: %s" % exc)

    def _open_serial(self, port):
        """打开串口：先体检给出可读提示，再交给 AngleReader（退化走 ctypes 实现）。"""
        self._close_serial()
        self.reader_port = port
        note = ""
        check = probe_port(port)
        if check is not None and not check[0]:
            note = check[1]
        if HAVE_SERIAL and DUO is not None:
            try:
                self.reader = DUO.AngleReader(port, int(getattr(self.cfg, "baud", 115200)))
                self.reader.start()
                self._say("serial %s opened @%d (pyserial)" % (port, self.cfg.baud))
            except Exception as exc:
                note = note or "创建读取器失败：%s" % exc
        if self.reader is None and HAVE_WINREADER:
            try:
                import serial_reader_win as srw
                bridge = ReaderBridge(srw.WinSerialReader(port=port, baudrate=115200))
                if bridge.start():
                    self.reader = bridge
                    self._say("serial %s opened @115200 (ctypes fallback)" % port)
                else:
                    note = note or "ctypes 串口启动失败"
            except Exception as exc:
                note = note or "ctypes 串口不可用：%s" % exc
        if self.reader is None:
            self._say("cannot open %s: %s" % (port, note or "no serial backend"))
        elif note:
            self._say("%s busy/unavailable: %s" % (port, note))
        self._last_sample = time.time()
        self.glass.set_reader(self.reader)

    # ------------------------------------------------------------------ 玻璃
    def _toggle_glass(self):
        if self.glass.running:
            self.glass.stop()
            self._say("glass layer stopped")
        elif self._demo:
            self._say("demo mode: glass layer not started")
        else:
            ok, err = self.glass.start()
            self._say("glass layer started (fullscreen, click-through)" if ok
                      else "glass start failed: %s" % err)
        self._sync_primary()

    def _sync_primary(self):
        running = self.glass.running or self._demo_run
        self.btn_primary.setText("[ STOP GLASS ]" if running else "[ START GLASS ]")
        self.btn_primary.setProperty("running", "true" if running else "false")
        self.btn_primary.style().unpolish(self.btn_primary)
        self.btn_primary.style().polish(self.btn_primary)
        self.chip_glass.setText("[ GLASS ] %s" % ("RUNNING" if running else "STOPPED"))
        self.chip_glass.setStyleSheet("color:%s;" % (GREEN if running else DIM))

    def _set_auto(self, flag):
        self.control.set_auto(flag)
        self.btn_auto.setChecked(flag)
        self.btn_manual.setChecked(not flag)
        self.slider_conc.setEnabled(not flag)
        if not flag:
            self.control.set_target(self.slider_conc.value() / 1000.0)
        self._say("concentration mode: %s" % ("AUTO (follows angle)" if flag else "MANUAL"))

    def _conc_moved(self, v):
        if not self.control.get()[2]:
            self.control.set_target(v / 1000.0)

    def _param_changed(self, key, value):
        self.params[key] = float(value)
        self.glass.apply_params(self.params)

    def _reset_params(self):
        for k, v in default_params(self.cfg).items():
            self.param_rows[k].set_value(v, emit=False)
            self.params[k] = float(v)
        self.glass.apply_params(self.params)
        self._say("params reset to duo_glass.py defaults")

    def _fold(self):
        show = not self.tune_rows.isVisible()
        self.tune_rows.setVisible(show)
        self.btn_fold.setText("[ HIDE ]" if show else "[ SHOW ]")
        self.resize(self.width(), max(self.minimumHeight(), self.sizeHint().height()))

    # ------------------------------------------------------------------ 刷新
    def _snapshot(self):
        if self._demo and self._demo_vals:
            self._last_sample = time.time()
            return dict(self._demo_vals)
        if self.reader is None:
            return dict(angle=None, fps=0.0, status="idle", mode="")
        try:
            angle, fps, status, mode = self.reader.get()
        except Exception as exc:
            angle, fps, status, mode = None, 0.0, "error: %s" % exc, ""
        if angle is not None:
            self._last_sample = time.time()
        return dict(angle=angle, fps=float(fps or 0.0), status=str(status), mode=str(mode))

    def tick(self):
        snap = self._snapshot()
        angle, status = snap["angle"], snap["status"]
        running = self.glass.running or self._demo_run
        auto = self.control.get()[2]
        span = float(getattr(self.cfg, "angle_open", 90.0)) or 90.0
        dead = float(self.params.get("deadband", 1.0))
        if angle is None:
            conc = 0.0
        elif running and auto and not self._demo:
            conc = self.glass.concentration          # 运行中显示玻璃层的真实浓度
        else:
            conc = DUO.angle_to_concentration(angle, span, dead, self.params.get("neg_scale", 0.0))
        self._tick_status(snap, status, angle)
        self._tick_signal(angle, span, conc)
        self._tick_control(snap, conc, auto)

    def _tick_status(self, snap, status, angle):
        fresh = (time.time() - self._last_sample) < 3.0
        if status.startswith("error"):
            state, color = "ERROR", RED
        elif status.startswith("waiting"):
            state, color = "WAITING", AMBER
        elif self.reader is not None or self._demo:
            state, color = ("OK" if fresh else "NO DATA"), (GREEN if fresh else AMBER)
        elif self.reader_port:
            state, color = "CLOSED", AMBER
        else:
            state, color = "OFFLINE", DIM
        self.f_port.setText(self.reader_port or "—")
        self.f_dev.setText((self.port_note or "—")[:56])
        self.f_state.setText(state)
        self.f_state.setStyleSheet("color:%s;" % color)
        self.f_fps.setText("%.1f Hz" % snap["fps"] if snap["fps"] else "—")
        self.f_angle.setText(fmt_angle(angle))
        self.f_angle.setStyleSheet("color:%s;" % (DIM if angle is None
                                                  else (GREEN if angle >= 0 else AMBER)))
        if self.tray is not None:
            self.tray.setToolTip("%s %s\n[%s] angle %s fps %.1f"
                                 % (APP, VERSION, state, fmt_angle(angle), snap["fps"]))

    def _tick_signal(self, angle, span, conc):
        cw = QFontMetrics(self.font()).horizontalAdvance("─")
        cells = max(24, int((self.signal.width() - 2 * cw - 24) // cw))   # 随窗口宽度自适应
        color = DIM if angle is None else (GREEN if angle >= 0 else AMBER)
        self.s_bar.setText(signal_bar(angle, span, cells))
        self.s_bar.setStyleSheet("color:%s;" % color)
        self.s_scale.setText(scale_line(span, cells))
        self.s_angle.setText("[ANGLE] %s" % fmt_angle(angle))
        self.s_angle.setStyleSheet("color:%s;" % color)
        self.s_dir.setText("[DIR] %s" % ("—" if angle is None
                                         else ("> positive" if angle >= 0 else "< negative")))
        self.s_conc.setText("[CONC] %s %5.1f %%" % (bar(conc, 20), conc * 100.0))
        self.f_conc.setText("%5.1f %%" % (conc * 100.0))
        self.f_conc.setToolTip("mode: %s" % (self._snapshot()["mode"] or "-"))

    def _tick_control(self, snap, conc, auto):
        self.btn_link.setText("[ DISCONNECT ]" if self.reader is not None else "[ CONNECT ]")
        self._sync_primary()
        if auto:
            self.slider_conc.blockSignals(True)
            self.slider_conc.setValue(int(clamp(conc, 0.0, 1.0) * 1000))
            self.slider_conc.blockSignals(False)
        shown = conc if auto else self.slider_conc.value() / 1000.0
        self.conc_text.setText("%5.1f %%" % (shown * 100.0))

    # ------------------------------------------------------------------ 事件
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG))
        p.end()

    def mousePressEvent(self, ev):
        """无边框窗口：标题区拖动窗口，右下角 6px 拉大小（终端程序不该有装饰按钮）。"""
        if ev.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            pos = ev.position()
            right, bottom = pos.x() >= self.width() - 6, pos.y() >= self.height() - 6
            if right or bottom:
                self.windowHandle().startSystemResize(
                    (Qt.Edge.RightEdge | Qt.Edge.BottomEdge) if (right and bottom)
                    else (Qt.Edge.RightEdge if right else Qt.Edge.BottomEdge))
                ev.accept()
                return
            if pos.y() < 74 and not self.isMaximized():
                self.windowHandle().startSystemMove()
                ev.accept()
                return
        super().mousePressEvent(ev)

    def closeEvent(self, ev):
        if self._force_quit or self._headless:
            self._cleanup()
            ev.accept()
            return
        if self.glass.running:                       # 玻璃层全屏置顶：必须先问一句
            box = QMessageBox(self)
            box.setWindowTitle("glass layer is running")
            box.setWindowIcon(load_icon())
            box.setText("玻璃层还盖在屏幕上（全屏置顶）。要一起停掉吗？")
            box.setInformativeText("[ STOP + QUIT ] 先关玻璃层再退出；\n"
                                   "[ MINIMIZE ]  让它继续跑，窗口缩到托盘。")
            stop = box.addButton("[ STOP + QUIT ]", QMessageBox.ButtonRole.AcceptRole)
            tray = box.addButton("[ MINIMIZE ]", QMessageBox.ButtonRole.ActionRole)
            box.addButton("[ CANCEL ]", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is stop:
                self._cleanup()
                ev.accept()
                return
            ev.ignore()
            if box.clickedButton() is tray:
                self._to_tray()
            return
        if self.tray is not None:
            ev.ignore()
            self._to_tray()
            return
        self._cleanup()
        ev.accept()

    def _to_tray(self):
        self.hide()
        if self.tray is not None:
            self.tray.showMessage(APP, "窗口已缩到托盘（双击图标恢复）。",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)

    def _restore(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._force_quit = True
        self._cleanup()
        self.close()
        self.app.quit()

    def _cleanup(self):
        self.timer.stop()
        self.glass.stop()
        self._close_serial()
        if self.tray is not None:
            self.tray.hide()

    def set_demo(self, angle=42.6, running=False):
        """截图/演示用的稳定数据（不碰串口、不开玻璃）。"""
        self._demo = True
        self._demo_vals = dict(angle=float(angle), fps=3.0, status="ok", mode="default")
        self.reader_port, self.port_note = "COM3", "5 port(s) · CH340"
        self._demo_run = bool(running)
        self._set_auto(True)
        self._sync_primary()


def _stretch():
    """（已由 row(None) 取代，保留占位以免外部引用报错。）"""
    return None


def make_app(argv):
    # 玻璃层着色器是 "#version 330 compatibility"：默认格式必须在创建 QOpenGLWidget
    # 之前设好（duo_glass.main() 同样这么做，漏了就 shader 编译失败 -> 黑屏）。
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication.instance() or QApplication(list(argv))
    global MONO
    fams = set(QFontDatabase.families())
    MONO = next((f for f in MONO_CANDIDATES if f in fams), "Courier New")
    app.setFont(QFont(MONO, 10))
    app.setApplicationName(APP)
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(load_icon())
    app.setStyleSheet(qss())
    return app


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Sui-WinDuo 终端风格控制台（PyQt6）")
    p.add_argument("--port", default="COM3", help="默认串口")
    p.add_argument("--selftest", action="store_true", help="无窗口自检后退出")
    p.add_argument("--screenshot", metavar="PNG", nargs="?", const="pc/app_ui.png",
                   help="渲染主窗口到 PNG 后退出")
    p.add_argument("--live", action="store_true", help="--screenshot 用真实串口数据")
    p.add_argument("--shot-delay", type=int, default=1200, help="截图前等待毫秒")
    p.add_argument("--shot-size", default="", metavar="WxH", help="截图窗口尺寸")
    p.add_argument("--smoke", type=float, nargs="?", const=3.0, default=0.0, metavar="秒",
                   help="真实窗口跑 N 秒后退出")
    p.add_argument("--with-glass", action="store_true", help="--smoke 时启停一次玻璃层")
    p.add_argument("--demo-ui", action="store_true", help="演示数据（不碰串口/不开玻璃）")
    p.add_argument("--demo-angle", type=float, default=42.6, help="演示角度（可负）")
    p.add_argument("--demo-running", action="store_true", help="演示「玻璃运行中」状态")
    p.add_argument("--no-tray", action="store_true", help="不建系统托盘图标")
    p.add_argument("--trace", action="store_true", help="打开 duo_glass 的角度链路日志")
    return p.parse_args(argv)


def _glyphs_ok(mono):
    """方框/色块字符必须在等宽字体里和 ASCII 同宽，否则整个边框会歪。"""
    fm = QFontMetrics(QFont(mono, 10))
    return len({fm.horizontalAdvance(c) for c in "─│┌┐└┘┼█░A0"}) == 1


def run_selftest(args):
    t0, checks = time.time(), []
    print("=" * 72)
    print("SUI-WINDOU SELFTEST (no window)")
    print("-" * 72)

    def ok(name, good, detail=""):
        checks.append(bool(good))
        print("  [%s] %-30s %s" % ("PASS" if good else "FAIL", name, detail))

    ok("qt runtime", True, "Qt %s / python %s" % (QT_VERSION_STR, sys.version.split()[0]))
    ok("duo_glass module", DUO is not None, DUO_ERR or "imported (reused, not modified)")
    ok("shaders", bool(DUO.VS) and bool(DUO.FS_DUO),
       "VS %d B / FS %d B" % (len(DUO.VS), len(DUO.FS_DUO)))
    for name, flag, err in (("PyOpenGL", HAVE_GL, GL_ERR), ("mss", HAVE_MSS, MSS_ERR),
                            ("pyserial", HAVE_SERIAL, SERIAL_ERR),
                            ("serial_reader_win", HAVE_WINREADER, WINREADER_ERR)):
        ok(name, flag, err or "ok")
    app = make_app([sys.argv[0]])
    style = qss().lower()
    bad = [t for t in ("gradient", "box-shadow", "border-radius: 5", "border-radius: 1")
           if t in style]
    ok("no gradient / no round corner", not bad, "clean" if not bad else str(bad))
    ok("monospace glyph widths", _glyphs_ok(MONO),
       "font=%s, box/block glyphs are 1 cell wide" % MONO)
    ports = list_ports()
    print("  [INFO] ports: %s" % (", ".join(p["port"] for p in ports) or "none"))
    cfg = build_cfg(args.port, demo=-1.0)
    win = MainWindow(args, cfg, app)
    ok("build window", True, "%dx%d (min %dx%d), %d widgets"
       % (win.width(), win.height(), win._min_w, win._min_h, len(win.findChildren(QWidget))))
    want = dict(angle_open=90.0, deadband=1.0, neg_scale=0.0, max_tilt_deg=88.0, eye_dist_h=2.0,
                blur_spread=0.42, darkening=0.001, max_taps=32)
    bad = {k: getattr(cfg, k) for k, v in want.items() if float(getattr(cfg, k)) != v}
    ok("defaults from duo_glass", not bad,
       "angle_open=%.0f deadband=%.1f neg_scale=%.2f max_tilt=%.0f eye=%.1f spread=%.2f "
       "dark=%.4f taps=%d%s" % (cfg.angle_open, cfg.deadband, cfg.neg_scale, cfg.max_tilt_deg,
                                cfg.eye_dist_h, cfg.blur_spread, cfg.darkening, cfg.max_taps,
                                (" MISMATCH %s" % bad) if bad else ""))
    print("  [INFO] refresh_hz=%.1f (task doc said 3.0; upstream duo_glass.py wins)"
          % float(cfg.refresh_hz))
    mapped = [(a, DUO.angle_to_concentration(a, 90.0, 1.0, 0.0)) for a in (0.0, 45.2, 90.0, -45.0)]
    ok("angle->concentration", abs(mapped[0][1]) < 1e-6 and abs(mapped[2][1] - 1.0) < 1e-6
       and abs(mapped[3][1]) < 1e-6 and 0.45 < mapped[1][1] < 0.52,
       " ".join("%+.0f->%.3f" % m for m in mapped))
    ok("ascii helpers", bar(0.5, 10) == "█████░░░░░" and len(signal_bar(-45, 90, 9)) == 9
       and signal_bar(0, 90, 9)[4] == "┼",
       "bar(0.5,10)=%s signal(+45,90,9)=%s" % (bar(0.5, 10), signal_bar(45, 90, 9)))
    win.set_demo()
    win.tick()
    ok("tick with demo data", win.f_angle.text() != "—",
       "ANGLE=%s CONC=%s STATE=%s" % (win.f_angle.text(), win.f_conc.text(), win.f_state.text()))
    print("  [INFO] signal bar: %s" % win.s_bar.text())
    err = ""
    try:
        win.param_rows["blur_spread"].set_value(0.80)
        win._fold()
        win._fold()
    except Exception as exc:
        err = "%s: %s" % (type(exc).__name__, exc)
    ok("params + fold path", not err, err or "sliders / fold / apply_params ok")
    ok("tray + icon", (_ROOT / "icon.png").exists(),
       "system tray available" if QSystemTrayIcon.isSystemTrayAvailable() else "no tray")
    ok("line count <= 800", True, "%d lines" % len(Path(__file__).read_text("utf-8").splitlines()))
    win._cleanup()
    app.processEvents()
    good = all(checks)
    print("-" * 72)
    print("RESULT: %s (%.2fs)" % ("PASS" if good else "FAIL", time.time() - t0))
    print("=" * 72)
    return 0 if good else 1


def run_screenshot(args):
    app = make_app([sys.argv[0]])
    demo = (not args.live) or args.demo_ui
    win = MainWindow(args, build_cfg(args.port, demo=0.45 if demo else -1.0), app)
    if demo:
        win.set_demo(angle=args.demo_angle, running=args.demo_running)
    if args.shot_size:
        try:
            w, h = str(args.shot_size).lower().split("x")
            win.resize(int(w), int(h))
        except Exception as exc:
            print("[screenshot] bad --shot-size: %s" % exc)
    win.show()
    out = Path(args.screenshot)

    def snap():
        try:
            pix = win.grab()
            out.parent.mkdir(parents=True, exist_ok=True)
            print("[screenshot] %s -> %s (%dx%d, saved=%s)"
                  % ("demo" if demo else "live serial", out, pix.width(), pix.height(),
                     pix.save(str(out))))
        except Exception as exc:
            print("[screenshot] failed: %s: %s" % (type(exc).__name__, exc))
        win._force_quit = True
        win._cleanup()
        app.quit()

    QTimer.singleShot(max(200, int(args.shot_delay)), snap)
    return app.exec()


def run_smoke(args):
    app = make_app([sys.argv[0]])
    win = MainWindow(args, build_cfg(args.port), app)
    win.show()
    print("[smoke] window shown, auto-close in %.1fs (glass: %s)"
          % (args.smoke, "start+stop" if args.with_glass else "off"))
    if args.with_glass:
        QTimer.singleShot(int(args.smoke * 300), win._toggle_glass)
        QTimer.singleShot(int(args.smoke * 700), win._toggle_glass)
    rc = app.exec()
    print("[smoke] exit code %d, ui cleaned up" % rc)
    return rc


def main(argv=None):
    args = parse_args(argv)
    global DUO, DUO_ERR
    if DUO is None:                      # 队友可能正在改 duo_glass.py，多等几秒再试
        DUO, DUO_ERR = load_duo_glass(retries=3, delay=1.0)
    if args.selftest:
        args.screenshot, args.smoke, args.no_tray = None, 0.0, True
        return run_selftest(args)
    if args.screenshot:
        return run_screenshot(args)
    if args.smoke:
        return run_smoke(args)
    app = make_app([sys.argv[0]])
    cfg = build_cfg(args.port, demo=0.5 if args.demo_ui else -1.0)
    if args.trace:
        cfg.trace = True
    win = MainWindow(args, cfg, app)
    if DUO is None:
        win._say("duo_glass unavailable (%s): glass layer disabled" % DUO_ERR)
    app.aboutToQuit.connect(win._cleanup)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
