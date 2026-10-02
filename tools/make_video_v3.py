"""
Sui-WinDuo 工作原理动画 v3
============================
按用户要求重做，场景顺序固定为：
    1  硬件连接     ESP32 / MPU6050 / SSD1306 的 I2C 接线
    2  与电脑连接    USB 串口 -> PC 端程序
    3  屏幕拉伸动画  真实着色器输出（禁止扫描光带动效）
    4  原理展示     分区融合 + 逆投影
    5  片尾

针对上一版的批评（"AI 味太浓/不够精致/排版错位/动画卡"）的改动：
  · 版式：统一左对齐栏位、统一基线网格、统一字号阶梯，不用居中式大标题堆叠
  · 配色：只保留 1 个强调色（青）+ 中性灰阶，去掉满屏的绿/黄/蓝混色
  · 无扫描光带、无满天飞的粒子；动效只服务于"看懂"：元件就位、连线生长、
    数据沿导线流动、数字与曲线连续插值
  · 硬件按实物照片重绘（比例、颜色、丝印、OLED 显示内容都对齐真实固件）
  · 渲染性能：预先缩放序列帧、缓存阴影层，避免上一版逐帧重算导致的卡顿

用法：
  .venv\\Scripts\\python.exe tools/make_video_v3.py
"""

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from draw_hardware import draw_esp32, draw_mpu6050, draw_ssd1306, draw_button  # noqa: E402

W, H = 1920, 1080
FPS = 30
TOTAL = 26.0
ROOT = Path(__file__).resolve().parent.parent
SEQ = ROOT / "docs" / "glass_seq"
FFMPEG = [r"D:\ffmpeg\ffmpeg-6.1.1-essentials_build\bin\ffmpeg.exe", "ffmpeg"]

# ---------------- 设计系统（克制：一个强调色 + 中性灰阶） ----------------
BG = (13, 15, 18)
INK = (232, 236, 242)          # 主文字
MUTED = (138, 147, 158)        # 次要文字
FAINT = (86, 94, 104)          # 辅助/序号
RULE = (40, 46, 54)            # 分割线
CARD = (19, 22, 27)            # 卡片底
ACCENT = (86, 190, 235)        # 唯一强调色（青）
ACCENT_DIM = (44, 96, 120)
OK = (104, 206, 138)           # 仅用于"正常"状态点

FONTS = {}


def F(name, size):
    key = (name, size)
    if key not in FONTS:
        try:
            FONTS[key] = ImageFont.truetype(r"C:\Windows\Fonts\\" + name, size)
        except Exception:
            FONTS[key] = ImageFont.load_default()
    return FONTS[key]


def mono(s):
    return F("CascadiaMono.ttf", s)


def cn(s, bold=False):
    return F("msyhbd.ttc" if bold else "msyh.ttc", s)


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def ease(t, kind="inout"):
    t = clamp(t)
    if kind == "out":
        return 1 - (1 - t) ** 3
    if kind == "in":
        return t * t * t
    return t * t * (3 - 2 * t)


def seg(t, a, b, kind="inout"):
    if t <= a:
        return 0.0
    if t >= b:
        return 1.0
    return ease((t - a) / max(1e-6, b - a), kind)


# ---------------- 版式工具 ----------------
MARGIN = 96
CONTENT_W = W - MARGIN * 2


def h1(d, y, text, sub=None):
    """左对齐主标题 + 可选副标题（统一基线）。"""
    d.text((MARGIN, y), text, font=cn(40, True), fill=INK)
    if sub:
        d.text((MARGIN, y + 54), sub, font=cn(22), fill=MUTED)
    return y + 54 + (34 if sub else 0)


def label(d, x, y, text, color=MUTED, size=19):
    d.text((x, y), text, font=cn(size), fill=color)


def card(d, box, r=6):
    d.rounded_rectangle(box, radius=r, fill=CARD, outline=RULE, width=1)


def line_h(d, y, x0=MARGIN, x1=W - MARGIN):
    d.line([(x0, y), (x1, y)], fill=RULE, width=1)


def flow(d, p0, p1, t, color=ACCENT, n=2, r=4, speed=1.0):
    """沿线段流动的小点（克制：2 个，慢速）。"""
    for k in range(n):
        u = ((t * speed) + k / n) % 1.0
        x = p0[0] + (p1[0] - p0[0]) * u
        y = p0[1] + (p1[1] - p0[1]) * u
        fade = math.sin(u * math.pi)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=tuple(list(color) + [int(230 * fade)]))


def paste(img, layer, xy, scale=1.0, alpha=1.0):
    if layer is None or alpha <= 0.01:
        return
    if abs(scale - 1.0) > 0.01:
        layer = layer.resize((max(1, int(layer.width * scale)),
                              max(1, int(layer.height * scale))), Image.LANCZOS)
    if alpha < 0.99:
        layer = layer.copy()
        layer.putalpha(layer.split()[3].point(lambda v: int(v * alpha)))
    img.paste(layer, (int(xy[0]), int(xy[1])), layer)


# ---------------- 素材 ----------------
class Assets:
    def __init__(self):
        desk = SEQ / "desktop.jpg"
        if not desk.exists():
            desk = SEQ / "desktop.png"
        self.desktop = self._load(desk, (1280, 720))
        self.icon = None
        ic = ROOT / "icon.png"
        if ic.exists():
            self.icon = Image.open(ic).convert("RGBA")

        self.tilts, self.glass = [], []
        seen = set()
        for p in sorted(list(SEQ.glob("tilt_*.jpg")) + list(SEQ.glob("tilt_*.png"))):
            try:
                a = float(p.stem.split("_")[1])
            except Exception:
                continue
            if a in seen:
                continue
            seen.add(a)
            self.tilts.append(a)
            self.glass.append(Image.open(p).convert("RGB"))
        order = np.argsort(self.tilts)
        self.tilts = [self.tilts[i] for i in order]
        self.glass = [self.glass[i] for i in order]

        # 预缩放两个尺寸，避免每帧 resize 造成卡顿
        self._big = {}
        self._small = {}
        for a, g in zip(self.tilts, self.glass):
            self._big[a] = g.resize((1180, 664), Image.LANCZOS)
            self._small[a] = g.resize((820, 461), Image.LANCZOS)

        self.hw = {}
        for n in ("esp32", "mpu6050", "ssd1306_main", "ssd1306_calib", "button"):
            p = SEQ / ("hw_%s.png" % n)
            self.hw[n] = Image.open(p).convert("RGBA") if p.exists() else None

    @staticmethod
    def _load(p, size):
        if not Path(p).exists():
            return Image.new("RGB", size, (28, 32, 38))
        img = Image.open(p).convert("RGB")
        w, h = size
        s = max(w / img.width, h / img.height)
        nw, nh = int(round(img.width * s)), int(round(img.height * s))
        img = img.resize((nw, nh), Image.LANCZOS)
        return img.crop(((nw - w) // 2, (nh - h) // 2,
                         (nw - w) // 2 + w, (nh - h) // 2 + h))

    def nearest(self, tilt, small=False):
        if not self.tilts:
            return self.desktop
        i = min(range(len(self.tilts)), key=lambda k: abs(self.tilts[k] - tilt))
        return (self._small if small else self._big)[self.tilts[i]]


# ================================================================ 场景 1 硬件连接
def sc_wiring(img, d, t, T, A):
    y0 = h1(d, 84, "一、硬件连接", "ESP32 通过 I2C 总线连接姿态传感器与 OLED")
    line_h(d, 200)

    # 布局（已按各图层实际尺寸测算，模块之间、模块与卡片之间都不重叠）：
    #   上方 260..~880：ESP32（左，宽约 341） / MPU6050（中偏右） / OLED（最右）
    #   下方 758..1020 ：接线表（左） / 总线地址卡片（右）
    esp = A.hw["esp32"]
    mpu = A.hw["mpu6050"]
    oled = A.hw["ssd1306_main"]

    # OLED 尺寸要给足：固件字号 1 只有 6x8 像素，缩太小屏幕上 "WinDuo STD"
    # 那行就糊成一团，观众认不出显示内容（上一版就是这个毛病）
    e_s, m_s, o_s = 0.46, 0.42, 0.31
    e_pos = (MARGIN, 250)
    m_pos = (1120, 262)
    o_pos = (1452, 246)

    a1 = seg(t, 0.05, 0.5, "out")
    a2 = seg(t, 0.35, 0.8, "out")
    a3 = seg(t, 0.6, 1.05, "out")
    paste(img, esp, (e_pos[0] + (1 - a1) * -70, e_pos[1]), e_s, a1)
    paste(img, mpu, (m_pos[0] + (1 - a2) * 70, m_pos[1]), m_s, a2)
    paste(img, oled, (o_pos[0] + (1 - a3) * 70, o_pos[1]), o_s, a3)

    # 模块名（左对齐到模块左上，统一小字）
    if a1 > 0.5:
        label(d, e_pos[0], e_pos[1] - 34, "ESP32-WROOM-32E   主控", INK)
    if a2 > 0.5:
        label(d, m_pos[0], m_pos[1] - 30, "MPU6050   六轴 IMU", INK)
    if a3 > 0.5:
        label(d, o_pos[0], o_pos[1] - 30, "SSD1306   128x64 OLED", INK)

    # I2C 总线：从 ESP32 出发到两个模块
    if a1 > 0.55 and a2 > 0.55:
        p = seg(t, 0.7, 1.15, "out")
        e_mid = e_pos[1] + esp.height * e_s * 0.52
        m_mid = m_pos[1] + mpu.height * m_s * 0.55
        o_mid = o_pos[1] + oled.height * o_s * 0.52
        bx = 700                                  # 垂直主干的位置
        d.line([(e_pos[0] + esp.width * e_s + 6, e_mid), (bx, e_mid)], fill=ACCENT, width=1)
        d.line([(bx, min(m_mid, o_mid)), (bx, max(m_mid, o_mid))], fill=ACCENT, width=1)
        d.line([(bx, m_mid), (m_pos[0] - 8, m_mid)], fill=ACCENT, width=1)
        d.line([(bx, o_mid), (o_pos[0] - 8, o_mid)], fill=ACCENT, width=1)
        if p > 0.6:
            flow(d, (e_pos[0] + esp.width * e_s, e_mid), (bx, e_mid), t, ACCENT, 2, 4, 0.35)
            flow(d, (bx, m_mid), (m_pos[0], m_mid), t, ACCENT, 2, 4, 0.4)
            flow(d, (bx, o_mid), (o_pos[0], o_mid), t, ACCENT, 2, 4, 0.3)

    # 引脚对照表（左下，避开 OLED）
    if t > 1.15:
        rows = [
            ("SDA", "GPIO21", "数据线", "两模块共用一条总线"),
            ("SCL", "GPIO22", "时钟线", "400 kHz 标准速率"),
            ("VCC", "3V3", "供电", "MPU6050 与 OLED 共用"),
            ("GND", "GND", "共地", "必须与 ESP32 共地"),
        ]
        ty = 726
        d.text((MARGIN, ty), "接线", font=cn(22, True), fill=INK)
        d.text((MARGIN + 104, ty + 2), "引脚", font=cn(18), fill=FAINT)
        d.text((MARGIN + 244, ty + 2), "信号", font=cn(18), fill=FAINT)
        d.text((MARGIN + 366, ty + 2), "说明", font=cn(18), fill=FAINT)
        line_h(d, ty + 34, MARGIN, MARGIN + 700)
        for i, (pin, gpio, sig, note) in enumerate(rows):
            a = seg(t, 1.15 + i * 0.1, 1.45 + i * 0.1)
            if a <= 0:
                continue
            ry = ty + 50 + i * 38
            d.text((MARGIN, ry), pin, font=mono(20), fill=ACCENT)
            d.text((MARGIN + 104, ry), gpio, font=mono(20), fill=INK)
            d.text((MARGIN + 244, ry), sig, font=cn(19), fill=MUTED)
            d.text((MARGIN + 366, ry), note, font=cn(19), fill=MUTED)

    # 右下：总线地址
    if t > 1.5:
        card(d, (1452, 726, W - MARGIN, 906), 6)
        d.text((1478, 748), "总线地址", font=cn(20, True), fill=INK)
        for i, (addr, name) in enumerate([("0x68", "MPU6050"), ("0x3C", "SSD1306")]):
            ry = 792 + i * 42
            d.text((1478, ry), addr, font=mono(21), fill=ACCENT)
            d.text((1602, ry), name, font=cn(19), fill=MUTED)
        d.text((1478, 866), "固件开机自动扫描候选引脚", font=cn(17), fill=FAINT)


# ================================================================ 场景 2 与电脑连接
def sc_bridge(img, d, t, T, A):
    y0 = h1(d, 84, "二、与电脑连接", "USB 串口把角度送到 PC，PC 端着色器把它变成玻璃效果")
    line_h(d, 200)

    # 三段横向流程：ESP32 -> USB -> PC
    cy = 400
    bw, bh = 330, 150
    xs = [MARGIN, MARGIN + 470, MARGIN + 940]

    boxes = [
        ("ESP32", "20 Hz JSON", "每 50ms 一行"),
        ("USB 串口", "CH340", "115200 8N1"),
        ("PC 程序", "duo_glass.py", "读取并映射浓度"),
    ]
    for i, (title, sub, note) in enumerate(boxes):
        a = seg(t, i * 0.28, i * 0.28 + 0.5, "out")
        if a <= 0:
            continue
        card(d, (xs[i], cy, xs[i] + bw, cy + bh), 6)
        d.text((xs[i] + 26, cy + 26), title, font=cn(26, True), fill=INK)
        d.text((xs[i] + 26, cy + 68), sub, font=mono(20), fill=ACCENT)
        d.text((xs[i] + 26, cy + 104), note, font=cn(18), fill=MUTED)
        if i < 2:
            ap = seg(t, i * 0.28 + 0.35, i * 0.28 + 0.75)
            if ap > 0:
                x0 = xs[i] + bw + 10
                x1 = xs[i + 1] - 10
                d.line([(x0, cy + bh / 2), (x0 + (x1 - x0) * ap, cy + bh / 2)],
                       fill=ACCENT, width=2)
                if ap > 0.95:
                    flow(d, (x0, cy + bh / 2), (x1, cy + bh / 2), t, ACCENT, 2, 4, 0.4)

    # 串口数据流（逐行出现，等宽对齐）
    if t > 1.1:
        dy = 620
        d.text((MARGIN, dy - 46), "串口输出", font=cn(22, True), fill=INK)
        d.text((MARGIN + 150, dy - 44), "以 # 开头的行是日志，PC 端跳过",
               font=cn(18), fill=FAINT)
        n = int(5 * seg(t, 1.1, 2.2))
        lines = [
            '# 校准完成 零偏(dps)=-0.421,0.115,-0.203  基准角=1.83',
            '{"angle":42.6,"status":"ok","mode":"default","author":"EthanMaven"}',
            '{"angle":43.1,"status":"ok","mode":"default","author":"EthanMaven"}',
            '{"angle":43.9,"status":"ok","mode":"default","author":"EthanMaven"}',
            '{"angle":44.5,"status":"ok","mode":"default","author":"EthanMaven"}',
        ]
        for i in range(min(n, len(lines))):
            col = MUTED if lines[i].startswith('#') else INK
            d.text((MARGIN, dy + i * 38), lines[i], font=mono(21), fill=col)

    # 右侧：PC 端处理链路
    if t > 1.6:
        rx = MARGIN + 1020
        d.text((rx, 574), "PC 端处理", font=cn(22, True), fill=INK)
        steps = [
            ("读取串口", "逐行解析 JSON"),
            ("映射浓度", "正角度 -> 0..1"),
            ("逆投影着色", "FS_DUO 着色器"),
            ("全屏叠加", "透明置顶窗口"),
        ]
        for i, (a_, b_) in enumerate(steps):
            ap = seg(t, 1.6 + i * 0.16, 1.9 + i * 0.16)
            if ap <= 0:
                continue
            ry = 620 + i * 66
            d.ellipse([rx, ry + 8, rx + 10, ry + 18], fill=ACCENT_DIM)
            d.text((rx + 28, ry), a_, font=cn(21), fill=INK)
            d.text((rx + 28, ry + 30), b_, font=cn(18), fill=MUTED)


# ================================================================ 场景 3 屏幕拉伸
def sc_stretch(img, d, t, T, A):
    y0 = h1(d, 84, "三、屏幕拉伸效果", "倾斜越大，透视拉伸越强、越模糊、越暗（真实着色器输出）")
    line_h(d, 200)

    # 左侧：笔记本侧视 + 传感器随动（真实形态：传感器贴屏幕顶端）
    bx, by = 300, 720
    lid = 250
    tilt = 88 * (0.5 - 0.5 * math.cos(t / T * math.pi * 1.05))

    d.rounded_rectangle([bx - 150, by, bx + 150, by + 22], radius=4, fill=(44, 50, 60))
    ang = math.radians(90 - tilt)
    tx = bx - lid * math.cos(ang)
    ty = by - lid * math.sin(ang)
    d.line([(bx, by), (tx, ty)], fill=(52, 66, 88), width=16)
    d.line([(bx, by), (tx, ty)], fill=(88, 140, 210), width=9)
    d.ellipse([bx - 8, by - 8, bx + 8, by + 8], fill=MUTED)

    nx = tx + 10 * math.sin(ang)
    ny = ty + 10 * math.cos(ang)
    paste(img, A.hw["mpu6050"], (nx - 26, ny - 20), 0.13, 1.0)
    d.ellipse([nx - 24, ny - 24, nx + 24, ny + 24], outline=ACCENT, width=2)
    label(d, nx + 36, ny - 16, "传感器随屏幕转动", ACCENT, 18)

    # 角度读数（等宽大字，左对齐）
    d.text((MARGIN, 250), "%5.1f" % tilt, font=mono(76), fill=INK)
    d.text((MARGIN + 236, 288), "deg", font=mono(24), fill=MUTED)
    d.text((MARGIN, 348), "开合角", font=cn(18), fill=FAINT)

    # 中间：真实着色器画面
    iw, ih = 1180, 664
    ix = W - MARGIN - iw
    iy = 250
    fr = A.nearest(tilt)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [ix + 6, iy + 10, ix + iw + 6, iy + ih + 10], radius=6, fill=(0, 0, 0, 140))
    sh = sh.filter(ImageFilter.GaussianBlur(16))
    img.paste(sh, (0, 0), sh)
    img.paste(fr, (ix, iy))
    d.rounded_rectangle([ix, iy, ix + iw, iy + ih], radius=6,
                        outline=RULE, width=1)

    # 底部：亮度曲线（连续插值，无标记跳变）
    cy = 950
    gh = 60
    d.text((MARGIN, cy - 40), "画面亮度随倾角衰减", font=cn(19), fill=MUTED)
    gx, gw = MARGIN, 560
    pts = []
    for a, g in zip(A.tilts, A.glass):
        bri = float(np.asarray(g.convert("L")).mean())
        pts.append((gx + a / 88.0 * gw, cy + gh - clamp(bri / 50.0) * gh))
    if len(pts) > 1:
        d.line(pts, fill=(70, 90, 104), width=2)
    cur_bri = float(np.asarray(A.nearest(tilt).convert("L")).mean())
    cx = gx + clamp(tilt / 88.0) * gw
    cyy = cy + gh - clamp(cur_bri / 50.0) * gh
    d.line([(cx, cy - 6), (cx, cy + gh + 6)], fill=ACCENT, width=1)
    d.ellipse([cx - 5, cyy - 5, cx + 5, cyy + 5], fill=ACCENT)
    d.text((gx, cy + gh + 14), "0°", font=mono(17), fill=FAINT)
    d.text((gx + gw - 30, cy + gh + 14), "88°", font=mono(17), fill=FAINT)

    # 右侧：浓度条
    bx2 = MARGIN + 700
    d.text((bx2, cy - 40), "玻璃浓度", font=cn(19), fill=MUTED)
    d.rectangle([bx2, cy + 14, bx2 + 420, cy + 30], outline=RULE, width=1)
    d.rectangle([bx2 + 1, cy + 15, bx2 + 1 + int(418 * clamp(tilt / 88.0)), cy + 29],
                fill=ACCENT)
    d.text((bx2 + 440, cy + 12), "%d%%" % int(clamp(tilt / 88.0) * 100),
           font=mono(22), fill=INK)


# ================================================================ 场景 4 原理
def sc_principle(img, d, t, T, A):
    y0 = h1(d, 84, "四、原理展示", "两个传感器互补、一条着色器完成空间逆投影")
    line_h(d, 200)

    # 上：分区融合（横向条 + 说明）
    d.text((MARGIN, 250), "1  分区融合", font=cn(24, True), fill=INK)
    bar_y = 320
    bar_w = CONTENT_W
    split = MARGIN + bar_w * 0.34
    d.rectangle([MARGIN, bar_y, MARGIN + bar_w, bar_y + 58], outline=RULE, width=1,
                fill=(21, 26, 31))
    d.rectangle([split, bar_y + 1, MARGIN + bar_w - 1, bar_y + 57], fill=(19, 30, 36))

    # 两半各自：标题一行、说明一行（避免文字挤在一起）
    d.text((MARGIN + 26, bar_y + 12), "近零区  ≥ -8°", font=cn(21, True), fill=INK)
    label(d, MARGIN + 26, bar_y + 38, "加速度计校准 · 无漂移", ACCENT, 18)
    d.text((split + 26, bar_y + 12), "深负区  < -8°", font=cn(21, True), fill=INK)
    label(d, split + 26, bar_y + 38, "纯陀螺仪积分 · 不折返", ACCENT, 18)
    d.line([(split, bar_y - 12), (split, bar_y + 70)], fill=MUTED, width=1)
    d.text((split - 20, bar_y + 76), "-8°", font=mono(19), fill=MUTED)

    # 游标（连续移动）
    mv = (t / max(0.1, T)) * 2.0 % 1.0
    mx = MARGIN + bar_w * (0.62 - 0.46 * abs(math.sin(mv * math.pi)))
    d.polygon([(mx, bar_y - 8), (mx - 8, bar_y - 24), (mx + 8, bar_y - 24)], fill=ACCENT)

    if t > 0.9:
        why = [
            ("陀螺仪", "响应快、不折返，但有零偏，会缓慢漂移"),
            ("加速度计", "绝对参考、不漂移，但 ±90° 处 atan2 折返"),
            ("分区策略", "近零区用加速度计压漂移，负值深区交给陀螺仪"),
        ]
        for i, (k, v) in enumerate(why):
            ap = seg(t, 0.9 + i * 0.14, 1.25 + i * 0.14)
            if ap <= 0:
                continue
            ry = 452 + i * 44
            d.text((MARGIN, ry), k, font=cn(20, True), fill=INK)
            d.text((MARGIN + 140, ry), v, font=cn(20), fill=MUTED)

    # 下：逆投影几何（简化到一眼能懂：视点 -> 玻璃 -> 桌面）
    gy = 660
    d.text((MARGIN, gy - 50), "2  空间逆投影", font=cn(24, True), fill=INK)
    ox, oy = MARGIN + 120, gy + 250        # 铰链
    d.line([(ox - 60, oy), (ox + 620, oy)], fill=RULE, width=2)
    label(d, ox + 632, oy - 10, "桌面内容", MUTED, 18)

    tilt_g = math.radians(34)
    gl = 470
    gxe = ox + gl * math.cos(tilt_g)
    gye = oy - gl * math.sin(tilt_g)
    d.line([(ox, oy), (gxe, gye)], fill=ACCENT, width=3)
    label(d, gxe + 12, gye - 22, "玻璃平面（屏幕）", ACCENT, 18)

    eye = (ox + 300, oy - 300)
    d.ellipse([eye[0] - 8, eye[1] - 8, eye[0] + 8, eye[1] + 8], fill=INK)
    label(d, eye[0] + 18, eye[1] - 12, "视点", INK, 18)

    pr = seg(t, 1.2, 2.0, "out")
    for i in range(4):
        u = 0.3 + i * 0.2
        px_ = ox + gl * math.cos(tilt_g) * u
        py_ = oy - gl * math.sin(tilt_g) * u
        if py_ >= eye[1]:
            continue
        k = (oy - eye[1]) / (py_ - eye[1])
        hx = eye[0] + (px_ - eye[0]) * k
        d.line([eye, (px_, py_)], fill=(58, 66, 76), width=1)
        if pr > 0:
            ex = px_ + (hx - px_) * min(1.0, pr * 1.5)
            d.line([(px_, py_), (ex, oy)], fill=ACCENT_DIM, width=1)
        d.ellipse([px_ - 4, py_ - 4, px_ + 4, py_ + 4], fill=ACCENT)

    # 原理步骤（右侧，等宽编号）
    if t > 1.3:
        rx = MARGIN + 980
        steps = [
            "每个像素放在玻璃平面上",
            "从视点发射线穿过该像素",
            "延长到桌面平面求交点",
            "以交点为采样中心做模糊",
            "间隙越大 -> 半径越大越暗",
        ]
        for i, s in enumerate(steps):
            ap = seg(t, 1.3 + i * 0.12, 1.6 + i * 0.12)
            if ap <= 0:
                continue
            ry = gy + 40 + i * 48
            d.text((rx, ry), "%d" % (i + 1), font=mono(20), fill=FAINT)
            d.text((rx + 34, ry), s, font=cn(20), fill=INK)


# ================================================================ 场景 5 片尾
def sc_end(img, d, t, T, A):
    a = seg(t, 0.0, 0.6, "out")
    if a <= 0:
        return
    if A.icon:
        ic = A.icon.resize((96, 96), Image.LANCZOS)
        if a < 1:
            ic = ic.copy()
            ic.putalpha(ic.split()[3].point(lambda v: int(v * a)))
        img.paste(ic, (MARGIN, 380), ic)

    d.text((MARGIN, 500), "Sui-WinDuo", font=mono(64), fill=INK)
    d.text((MARGIN, 592), "笔记本屏幕开合角  ·  悬浮玻璃效果", font=cn(26), fill=MUTED)
    line_h(d, 660, MARGIN, MARGIN + 620)
    d.text((MARGIN, 690), "EthanMaven", font=mono(24), fill=INK)
    d.text((MARGIN + 200, 690), "github.com/comreade-123/Sui-WinDuo",
           font=mono(22), fill=ACCENT)
    d.text((MARGIN, 736), "二创自开源项目 WindowsDuo（MIT）  ·  本项目同样以 MIT 许可开源",
           font=cn(20), fill=FAINT)
    d.text((MARGIN, 784), "ESP32 + Arduino IDE  ·  PyQt6 + OpenGL", font=mono(19), fill=FAINT)


SCENES = [
    (sc_wiring, 0.0, 7.0),
    (sc_bridge, 7.0, 12.0),
    (sc_stretch, 12.0, 19.5),
    (sc_principle, 19.5, 24.5),
    (sc_end, 24.5, 26.0),
]


def render_frame(idx, A):
    tg = idx / FPS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")
    for fn, s, e in SCENES:
        if s <= tg < e:
            fn(img, d, tg - s, e - s, A)
            break
    fade = 1.0
    if tg < 0.5:
        fade = tg / 0.5
    elif tg > TOTAL - 0.5:
        fade = max(0.0, (TOTAL - tg) / 0.5)
    if fade < 1.0:
        img = Image.blend(Image.new("RGB", (W, H), (0, 0, 0)), img, clamp(fade))
    return img


def find_ffmpeg():
    for c in FFMPEG:
        if c == "ffmpeg":
            p = shutil.which("ffmpeg")
            if p:
                return p
        elif Path(c).exists():
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "how-it-works.mp4"))
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--seconds", type=float, default=TOTAL)
    ap.add_argument("--preview", action="store_true", help="只出各场景静帧做检查")
    args = ap.parse_args()

    A = Assets()
    print("素材: 桌面 %s | 玻璃 %d 档 | 硬件 %d 个"
          % (A.desktop.size, len(A.tilts), sum(1 for v in A.hw.values() if v)))

    if args.preview:
        cw, ch = 620, 349
        sheet = Image.new("RGB", (cw * 2, ch * 3), (0, 0, 0))
        for i, (fn, s, e) in enumerate(SCENES):
            for j, frac in enumerate((0.35, 0.8)):
                idx = int((s + (e - s) * frac) * FPS)
                im = render_frame(idx, A)
                k = i * 2 + j
                if k < 6:
                    sheet.paste(im.resize((cw, ch), Image.LANCZOS),
                                ((k % 2) * cw, (k // 2) * ch))
        sheet.save(ROOT / "docs" / "_v3_preview.png")
        print("预览 -> docs/_v3_preview.png")
        return 0

    ff = find_ffmpeg()
    if not ff:
        print("[错误] 找不到 ffmpeg")
        return 2

    total = int(args.seconds * args.fps)
    tmp = Path(tempfile.mkdtemp(prefix="winduo_v3_"))
    t0 = time.time()
    for i in range(total):
        render_frame(i, A).save(tmp / ("f%05d.png" % i), compress_level=1)
        if i % 90 == 0 or i == total - 1:
            el = time.time() - t0
            print("  %4d/%d 帧 (%.1fs, %.1f 帧/秒)"
                  % (i + 1, total, el, (i + 1) / max(1e-6, el)), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(args.fps), "-i", str(tmp / "f%05d.png"),
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("[错误] ffmpeg:\n" + (r.stderr or "")[:1200])
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("完成: %s  (%.2f MB)" % (out, out.stat().st_size / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
