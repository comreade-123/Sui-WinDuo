"""
Sui-WinDuo 工作原理动画 v2（真实素材 + 全程动效）
====================================================
相比 v1 的关键改动：
  · 效果演示不再用 PIL 叠模糊**仿造**，而是调用项目真实着色器 FS_DUO
    离屏渲染出各倾角画面（见 tools/render_glass_stills.py）
  · 底图用真实桌面截图（素材/桌面截图.png），亮度和对比度足够，效果看得清
  · 硬件用矢量图层（tools/draw_hardware.py 导出 RGBA），可以飞入/缩放/高亮
  · 每一帧都有动效：元件飞入、导线生长、粒子流动、角度数字滚动、
    JSON 逐字输入、滑块拖动、扫描光带、进度条推进

用法：
  .venv\\Scripts\\python.exe tools/make_video_v2.py
  .venv\\Scripts\\python.exe tools/make_video_v2.py --seconds 26 --out docs/how-it-works.mp4
"""

import argparse
import math
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    from draw_hardware import draw_button, draw_esp32, draw_mpu6050, draw_ssd1306
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from draw_hardware import draw_button, draw_esp32, draw_mpu6050, draw_ssd1306

W, H = 1920, 1080
FPS = 30
TOTAL = 27.0

ROOT = Path(__file__).resolve().parent.parent
SEQ = ROOT / "docs" / "glass_seq"
FFMPEG = [r"D:\ffmpeg\ffmpeg-6.1.1-essentials_build\bin\ffmpeg.exe", "ffmpeg"]

BG = (10, 12, 15)
PANEL = (17, 20, 24)
LINE = (42, 48, 56)
FG = (228, 233, 240)
DIM = (132, 142, 154)
GREEN = (74, 246, 38)
CYAN = (56, 189, 248)
AMBER = (255, 189, 46)
RED = (255, 95, 86)
BLUE = (10, 132, 255)
HILITE = (255, 255, 255)

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
    if kind == "outback":
        c1, c3 = 1.70158, 2.70158
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
    return t * t * (3 - 2 * t)


def seg(t, a, b, kind="inout"):
    if t <= a:
        return 0.0
    if t >= b:
        return 1.0
    return ease((t - a) / max(1e-6, b - a), kind)


# ---------------------------------------------------------------- 素材
class Assets:
    def __init__(self):
        # 玻璃序列与桌面底图：优先读 .png，回退到压缩后的 .jpg
        # （仓库里存的是 JPEG，体积只有 PNG 的 1/7，而效果演示不需要无损）
        desk_png, desk_jpg = SEQ / "desktop.png", SEQ / "desktop.jpg"
        self.desktop = self._load(desk_png if desk_png.exists() else desk_jpg, (1280, 720))
        self.icon = None
        p = ROOT / "icon.png"
        if p.exists():
            self.icon = Image.open(p).convert("RGBA")
        # 玻璃序列（16 档，16:9）
        self.tilts, self.glass = [], []
        seen = set()
        for p in sorted(list(SEQ.glob("tilt_*.png")) + list(SEQ.glob("tilt_*.jpg"))):
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
        # 硬件图层
        self.hw = {}
        for name in ("esp32", "mpu6050", "ssd1306", "button"):
            p = SEQ / ("hw_%s.png" % name)
            self.hw[name] = Image.open(p).convert("RGBA") if p.exists() else None
        self.desktop_small = self.desktop.resize((900, 506), Image.LANCZOS)
        self.glass_small = [g.resize((900, 506), Image.LANCZOS) for g in self.glass]

    @staticmethod
    def _load(p, size):
        """等比裁切加载：绝不改变纵横比（直接 resize 会拉扁画面）。"""
        if not Path(p).exists():
            return Image.new("RGB", size, (30, 34, 42))
        img = Image.open(p).convert("RGB")
        w, h = size
        s = max(w / img.width, h / img.height)
        nw, nh = int(round(img.width * s)), int(round(img.height * s))
        img = img.resize((nw, nh), Image.LANCZOS)
        return img.crop(((nw - w) // 2, (nh - h) // 2,
                         (nw - w) // 2 + w, (nh - h) // 2 + h))

    def glass_at(self, tilt):
        """按倾角取最接近的预渲染帧（10 档线性插值不划算，档位已足够密）。"""
        if not self.tilts:
            return self.desktop
        i = min(range(len(self.tilts)), key=lambda k: abs(self.tilts[k] - tilt))
        return self.glass[i]

    def glass_small_at(self, tilt):
        if not self.tilts:
            return self.desktop_small
        i = min(range(len(self.tilts)), key=lambda k: abs(self.tilts[k] - tilt))
        return self.glass_small[i]


def paste_soft(img, layer, xy, scale=1.0, alpha=1.0):
    """按缩放/透明度贴一张 RGBA 图层。"""
    if layer is None or alpha <= 0.01:
        return
    if abs(scale - 1.0) > 0.01:
        layer = layer.resize((max(1, int(layer.width * scale)),
                              max(1, int(layer.height * scale))), Image.LANCZOS)
    if alpha < 0.99:
        layer = layer.copy()
        a = layer.split()[3].point(lambda v: int(v * alpha))
        layer.putalpha(a)
    img.paste(layer, (int(xy[0]), int(xy[1])), layer)


def glow_text(d, xy, s, f, color, anchor="la", glow=1.0):
    """带轻辉光的文字（增强科技感，纯绘制）。"""
    x, y = xy
    d.text((x, y), s, font=f, fill=tuple(int(c * 0.35 * glow) for c in color), anchor=anchor)
    d.text((x, y), s, font=f, fill=color, anchor=anchor)


def round_rect_panel(d, box, title=None, accent=LINE, r=10):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=r, fill=PANEL, outline=accent, width=1)
    if title:
        f = cn(21)
        s = " " + title + " "
        tw = d.textlength(s, font=f)
        d.rectangle([x0 + 16, y0 - 11, x0 + 16 + tw, y0 + 11], fill=PANEL)
        d.text((x0 + 16, y0), s, font=f, fill=accent, anchor="lm")


def flow_dots(d, p0, p1, t, color, n=3, r=4, speed=1.0):
    """沿线段流动的粒子，表达数据流动。"""
    for k in range(n):
        u = ((t * speed) + k / n) % 1.0
        x = p0[0] + (p1[0] - p0[0]) * u
        y = p0[1] + (p1[1] - p0[1]) * u
        fade = math.sin(u * math.pi)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=tuple(list(color) + [int(255 * fade)]))


# ---------------------------------------------------------------- 场景
def sc_title(img, d, t, T, A):
    """1) 片头 0-2.6s"""
    a = seg(t, 0.0, 0.7, "out")
    if A.icon and a > 0:
        s = 0.7 + 0.3 * ease(seg(t, 0.0, 1.0, "outback"))
        ic = A.icon.resize((int(120 * s), int(120 * s)), Image.LANCZOS)
        if a < 1:
            ic = ic.copy()
            ic.putalpha(ic.split()[3].point(lambda v: int(v * a)))
        img.paste(ic, (int(W / 2 - ic.width / 2), int(210 + (1 - a) * -30)), ic)

    y = seg(t, 0.25, 1.0, "out")
    if y > 0:
        f1 = mono(104)
        f2 = cn(34)
        f3 = mono(21)
        d.text((W / 2, 400 - (1 - y) * 24), "Sui-WinDuo", font=f1, fill=HILITE, anchor="mm")
        d.text((W / 2, 476 - (1 - y) * 18), "笔记本屏幕开合角 → 悬浮玻璃效果",
               font=f2, fill=DIM, anchor="mm")
        d.text((W / 2, 528 - (1 - y) * 12),
               "ESP32 + MPU6050 + SSD1306    串口 JSON    OpenGL 逆向投影着色器",
               font=f3, fill=GREEN, anchor="mm")

    # 底部推进线
    p = seg(t, 0.3, T * 0.95, "inout")
    d.rectangle([W / 2 - 260, 640, W / 2 - 260 + 520 * p, 644], fill=CYAN)


def sc_effect(img, d, t, T, A):
    """2) 真实效果序列 2.6-9.0s —— 连续扫过所有倾角"""
    # 底：真实桌面，居中带边框（尺寸与序列一致，等比不变形）
    bw, bh = 1280, 720
    bx, by = (W - bw) // 2, 210
    p = seg(t, 0.0, 0.55, "out")
    if p <= 0:
        return

    # 当前倾角：0 → 88 往返
    cycle = (t / max(1e-6, T)) * 1.0
    tilt = 88.0 * (0.5 - 0.5 * math.cos(cycle * math.pi * 1.4))
    frame = A.glass_at(tilt)
    fr = frame.resize((bw, bh), Image.LANCZOS)
    fr_full = fr.copy()          # 全尺寸副本，供扫描光带合成使用

    # 桌面窗口投影
    shadow = Image.new("RGB", (bw, bh), (0, 0, 0))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [bx + 8, by + 14, bx + bw + 8, by + bh + 14], radius=12, fill=(0, 0, 0, 130))
    sh = sh.filter(ImageFilter.GaussianBlur(18))
    img.paste(sh, (0, 0), sh)

    if p < 1.0:
        # 入场：从上往下"展开"。注意保留全尺寸帧用于后面的扫描光带合成，
        # 否则 alpha_composite 会因为尺寸不一致而报 "images do not match"。
        hh = int(bh * ease(p, "out"))
        fr = fr.crop((0, 0, bw, max(1, hh)))
    img.paste(fr, (bx, by))
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=12, outline=(70, 78, 90), width=2)

    # 扫描光带（在完整帧上合成，再按展开进度裁剪贴回）
    sw = (t * 0.55) % 1.0
    sx = bx + sw * bw
    ov = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(70):
        xx = int(sx - bx - i)
        if 0 <= xx < bw:
            od.line([(xx, 0), (xx, bh)], fill=(255, 255, 255, int(60 * (1 - i / 70))))
    banded = Image.alpha_composite(fr_full.convert("RGBA"), ov).convert("RGB")
    if p < 1.0:
        banded = banded.crop((0, 0, bw, max(1, int(bh * ease(p, "out")))))
    img.paste(banded, (bx, by))

    # 左上：倾角读数（滚动数字）—— 放在桌面图左侧留白处，不遮挡画面
    round_rect_panel(d, (40, 60, 300, 240), "玻璃倾角", CYAN)
    d.text((66, 104), "%.1f" % tilt, font=mono(74), fill=GREEN)
    d.text((232, 156), "deg", font=mono(24), fill=DIM)
    barw = 200
    d.rectangle([66, 200, 66 + barw, 210], outline=LINE, width=1)
    d.rectangle([67, 201, 67 + int((barw - 2) * clamp(tilt / 88.0)), 209], fill=GREEN)

    # 右侧：说明 + 亮度曲线，竖排放在桌面图右侧留白
    round_rect_panel(d, (W - 296, 60, W - 40, 330), "着色器输出", GREEN)
    lines = [
        "逆投影",
        "Vogel 盘采样",
        "mip LOD",
        "边缘覆盖率",
    ]
    for i, s in enumerate(lines):
        a = seg(t, 0.15 + i * 0.1, 0.45 + i * 0.1)
        if a > 0:
            d.text((W - 276, 100 + i * 46), s, font=cn(20), fill=FG)

    round_rect_panel(d, (W - 296, 350, W - 40, 640), "亮度随倾角衰减", AMBER)
    gx, gy, gw, gh = W - 276, 400, 216, 210
    pts = []
    for i in range(41):
        ti = 88 * i / 40
        b = A.glass_at(ti)
        bri = np.asarray(b.convert("L")).mean()
        pts.append((gx + i / 40 * gw, gy + gh - (bri / 50) * gh))
    d.line(pts, fill=AMBER, width=3)
    bri = np.asarray(frame.convert("L")).mean()
    cur = (gx + clamp(tilt / 88.0) * gw, gy + gh - (bri / 50) * gh)
    d.ellipse([cur[0] - 6, cur[1] - 6, cur[0] + 6, cur[1] + 6], fill=GREEN)
    d.text((W - 276, 620), "倾角越大越暗", font=cn(17), fill=DIM)

    # 顶部说明（避免观众以为是特效合成的假图）
    d.text((W / 2, 78), "底图：真实桌面截图    画面：项目着色器 FS_DUO 离屏渲染的真实输出",
           font=cn(20), fill=(116, 126, 138), anchor="ma")


def sc_hardware(img, d, t, T, A):
    """3) 硬件飞入 9.0-14.0s"""
    round_rect_panel(d, (60, 50, W - 60, H - 60), "硬件组成 / I2C 总线", LINE)

    ex, ey = 300, 300
    mx, my = 1120, 220
    sx, sy = 1120, 560

    # 三块模块依次飞入
    a1 = seg(t, 0.0, 0.45, "outback")
    a2 = seg(t, 0.25, 0.7, "outback")
    a3 = seg(t, 0.5, 0.95, "outback")
    paste_soft(img, A.hw["esp32"], (ex + (1 - a1) * -220, ey), 1.0, a1)
    paste_soft(img, A.hw["mpu6050"], (mx + (1 - a2) * 260, my), 1.0, a2)
    paste_soft(img, A.hw["ssd1306"], (sx + (1 - a3) * 260, sy), 1.0, a3)

    # 总线：从 ESP32 右侧出发，分支到两块模块
    if a1 > 0.6:
        bp = seg(t, 0.6, 1.1, "out")
        x_bus = 780
        y_top, y_bot = 300, 640
        d.line([(700, 420), (x_bus, 420)], fill=CYAN, width=4)
        d.line([(x_bus, y_top), (x_bus, y_bot)], fill=CYAN, width=4)
        for yy in (y_top, y_bot):
            d.line([(x_bus, yy), (x_bus + int(300 * bp), yy)], fill=CYAN, width=4)
        if bp > 0.95:
            # 数据粒子流动
            flow_dots(d, (700, 420), (x_bus, 420), t, GREEN, 3, 5, 1.6)
            flow_dots(d, (x_bus, y_top), (1080, y_top), t, GREEN, 3, 5, 1.8)
            flow_dots(d, (x_bus, y_bot), (1080, y_bot), t, GREEN, 3, 5, 1.4)
            d.text((x_bus + 14, y_top - 40), "SCL / SDA  400kHz", font=mono(20), fill=CYAN)
            d.text((x_bus + 14, y_bot + 16), "0x68 / 0x3C", font=mono(20), fill=CYAN)

    # 引脚说明逐个出现
    rows = [
        ("ESP32-WROOM-32E", "主控 · 200Hz 采样 · 无 delay() 非阻塞", GREEN),
        ("MPU6050", "六轴 IMU · I2C 0x68 · AD0 接 GND", CYAN),
        ("SSD1306", "128x64 OLED · I2C 0x3C · 200ms 刷新", AMBER),
        ("按键 GPIO5", "短按切模式 · 长按重新校准 · 悬空自动禁用", BLUE),
    ]
    for i, (k, v, c) in enumerate(rows):
        a = seg(t, 1.1 + i * 0.15, 1.5 + i * 0.15)
        if a <= 0:
            continue
        yy = H - 300 + i * 52
        d.text((110, yy), "●", font=mono(22), fill=c)
        d.text((150, yy), k, font=mono(22), fill=c)
        d.text((520, yy), v, font=cn(21), fill=DIM)

    # 配线图上的引脚高亮脉冲
    if t > 1.6:
        for k, (px, py) in enumerate([(700, 420), (x_bus, y_top), (x_bus, y_bot)]):
            r = 6 + 5 * abs(math.sin(t * 3 + k))
            d.ellipse([px - r, py - r, px + r, py + r], outline=GREEN, width=2)


def sc_hinge(img, d, t, T, A):
    """4) 开合演示 14.0-19.0s

    按真实硬件形态绘制（参考实拍照片）：
      · MPU6050 贴在笔记本屏幕的顶部边缘，**随屏幕一起转动**
        —— 这是整个项目的核心：测的就是屏幕自身的倾角
      · ESP32 + SSD1306 + 按键装在透明亚克力盒里，放在桌面上
      · 两者之间用四芯排线连接（SCL / SDA / + / -）
        —— 所以每次开合屏幕，传感器就跟着转，角度随之变化
    """
    round_rect_panel(d, (60, 50, W - 60, H - 60), "端到端演示 / 屏幕开合 → 传感器随动 → 玻璃效果", LINE)

    lid_t = 88 * (0.5 - 0.5 * math.cos(t / T * math.pi * 1.1))

    # ---------------- 左侧：笔记本侧视 + 传感器随动 ----------------
    base_x, base_y = 400, 700          # 转轴（屏幕底边）
    base_w = 300
    lid_len = 260                      # 屏幕长度：保证 88° 平摊时不超出左区

    # 键盘底座
    d.rounded_rectangle([base_x - base_w * 0.5, base_y, base_x + base_w * 0.5, base_y + 24],
                        radius=5, fill=(46, 52, 62))
    d.text((base_x, base_y + 44), "键盘底座", font=cn(19), fill=DIM, anchor="ma")

    # 屏幕（绕底边转轴抬起）
    ang = math.radians(90 - lid_t)     # 0° = 竖立贴合，88° = 几乎平摊
    tip_x = base_x - lid_len * math.cos(ang)
    tip_y = base_y - lid_len * math.sin(ang)
    # 屏幕背面
    d.line([(base_x, base_y), (tip_x, tip_y)], fill=(58, 74, 98), width=18)
    # 屏幕正面（发光面）
    d.line([(base_x, base_y), (tip_x, tip_y)], fill=(96, 152, 226), width=11)
    d.line([(base_x, base_y), (tip_x, tip_y)], fill=(170, 215, 255), width=3)
    # 转轴
    d.ellipse([base_x - 9, base_y - 9, base_x + 9, base_y + 9], fill=AMBER)
    d.text((base_x + 14, base_y + 4), "转轴", font=cn(18), fill=AMBER)

    # 屏幕顶端的 MPU6050（关键：贴在屏幕边缘，随屏幕转动）
    nx = tip_x + 12 * math.sin(ang)
    ny = tip_y + 12 * math.cos(ang)
    paste_soft(img, A.hw["mpu6050"], (nx - 32, ny - 24), 0.26, 1.0)
    # 高亮圈出传感器
    pr = 26 + 5 * abs(math.sin(t * 3))
    d.ellipse([nx - pr, ny - pr, nx + pr, ny + pr], outline=GREEN, width=3)

    # 排线：从传感器下到桌面盒子（两股线，模拟四芯排线）
    box_x, box_y = 330, 830
    mid_x = (nx + box_x) / 2 + 10
    d.line([(nx, ny + 18), (nx, ny + 56), (mid_x, ny + 56),
            (mid_x, box_y - 12), (box_x, box_y - 12)], fill=(210, 90, 90), width=3)
    d.line([(nx + 6, ny + 22), (nx + 6, ny + 60), (mid_x + 7, ny + 60),
            (mid_x + 7, box_y - 7), (box_x + 7, box_y - 7)], fill=(240, 210, 90), width=3)
    d.text((mid_x + 16, ny + 24), "四芯排线", font=cn(18), fill=(214, 128, 128))

    # 标注（放在传感器右侧，留足空间）
    d.text((nx + 44, ny - 30), "MPU6050 贴在屏幕顶端", font=cn(21), fill=GREEN)
    d.text((nx + 44, ny - 2), "随屏幕一起转动", font=cn(21), fill=GREEN)

    # 角度读数
    d.text((base_x + 150, base_y - 250), "%.0f°" % lid_t, font=mono(40), fill=GREEN)

    # 桌面亚克力盒：ESP32 + OLED + 按键
    d.rounded_rectangle([box_x, box_y, box_x + 330, box_y + 148], radius=8,
                        outline=(180, 210, 240), width=2, fill=(255, 255, 255, 16))
    d.text((box_x + 165, box_y + 142), "亚克力外壳 · 桌面单元", font=cn(17),
           fill=(170, 195, 220), anchor="ma")
    paste_soft(img, A.hw["esp32"], (box_x + 12, box_y + 12), 0.34, 1.0)
    paste_soft(img, A.hw["ssd1306"], (box_x + 168, box_y + 14), 0.38, 1.0)
    paste_soft(img, A.hw["button"], (box_x + 296, box_y + 26), 0.28, 1.0)

    # ---------------- 右侧：真实桌面缩略图 + 效果 ----------------
    kw, kh = 900, 506
    kx, ky = 900, 250
    fr = A.glass_small_at(lid_t).resize((kw, kh), Image.LANCZOS)
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [kx + 8, ky + 12, kx + kw + 8, ky + kh + 12], radius=10, fill=(0, 0, 0, 130))
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    img.paste(sh, (0, 0), sh)
    img.paste(fr, (kx, ky))
    d.rounded_rectangle([kx, ky, kx + kw, ky + kh], radius=10, outline=(70, 78, 90), width=2)

    # 底部：串口数据滚动
    yj = ky + kh + 30
    round_rect_panel(d, (kx, yj, kx + kw, yj + 126), "COM3  115200", GREEN)
    hist = []
    for i in range(6):
        tt = t - i * 0.16
        if tt < 0:
            continue
        ang_i = 88 * (0.5 - 0.5 * math.cos(max(0.0, tt) / T * math.pi * 1.1))
        hist.append(ang_i)
    for i, ang_i in enumerate(hist[:3]):
        a = 1.0 - i * 0.32
        s = '  {"angle":%.1f,"status":"ok","mode":"default","author":"EthanMaven"}' % ang_i
        col = tuple(int(c * a) for c in GREEN)
        d.text((kx + 24, yj + 42 + i * 28), s, font=mono(18), fill=col)

    # 浓度条
    d.text((kx, yj + 140), "玻璃浓度", font=cn(20), fill=DIM)
    d.rectangle([kx + 130, yj + 142, kx + kw, yj + 158], outline=LINE, width=1)
    d.rectangle([kx + 131, yj + 143, kx + 131 + int((kw - 132) * clamp(lid_t / 88.0)), yj + 157],
                fill=GREEN)


def sc_pipeline(img, d, t, T, A):
    """5) 原理链路 19.0-24.2s"""
    round_rect_panel(d, (60, 50, W - 60, H - 60), "工作原理链路", LINE)
    nodes = [
        ("MPU6050", "200Hz 采样", CYAN),
        ("分区融合", "近零区校准 / 深负区陀螺", GREEN),
        ("串口 JSON", "115200 · 20Hz", AMBER),
        ("逆投影着色器", "Vogel盘 + mip LOD", BLUE),
        ("悬浮玻璃", "全屏透视拉伸", HILITE),
    ]
    n = len(nodes)
    bw2, bh2 = 300, 130
    gap = (W - 160 - n * bw2) / (n - 1)
    for i, (title, sub, c) in enumerate(nodes):
        a = seg(t, i * 0.14, i * 0.14 + 0.42, "outback")
        if a <= 0:
            continue
        x = 80 + i * (bw2 + gap) - (1 - a) * 40
        y = 470
        box = [x, y, x + bw2, y + bh2]
        d.rounded_rectangle(box, radius=10, fill=(22, 26, 32), outline=c, width=2)
        d.text((x + bw2 / 2, y + 42), title, font=cn(25, True), fill=c, anchor="mm")
        d.text((x + bw2 / 2, y + 88), sub, font=cn(19), fill=DIM, anchor="mm")
        # 节点内脉冲
        pr = 8 + 5 * abs(math.sin(t * 3 + i))
        d.ellipse([x + 16 - pr / 2, y + 14 - pr / 2, x + 16 + pr / 2, y + 14 + pr / 2],
                  outline=c, width=2)
        if i < n - 1:
            ax0 = x + bw2 + 6
            ax1 = 80 + (i + 1) * (bw2 + gap) - 6
            ap = seg(t, i * 0.14 + 0.3, i * 0.14 + 0.6)
            if ap > 0:
                d.line([(ax0, y + bh2 / 2), (ax0 + (ax1 - ax0) * ap, y + bh2 / 2)],
                       fill=c, width=3)
                if ap > 0.9:
                    flow_dots(d, (ax0, y + bh2 / 2), (ax1, y + bh2 / 2), t, c, 2, 5, 1.8)

    # 底部：三层说明
    if t > 1.5:
        cards = [
            ("采样层", "陀螺积分累积转角\n加速度计在近零区校准", CYAN),
            ("协议层", "每 50ms 一行 JSON\n以 # 开头的行是日志", AMBER),
            ("渲染层", "逐像素从视点发射线\n求交后再做磨砂采样", BLUE),
        ]
        for i, (k, v, c) in enumerate(cards):
            a = seg(t, 1.5 + i * 0.18, 1.9 + i * 0.18)
            if a <= 0:
                continue
            x = 150 + i * 570
            y = 700
            d.rounded_rectangle([x, y, x + 510, y + 190], radius=10,
                                fill=(19, 23, 28), outline=c, width=1)
            d.text((x + 26, y + 24), k, font=cn(24, True), fill=c)
            for j, ln in enumerate(v.split("\n")):
                d.text((x + 26, y + 74 + j * 38), ln, font=cn(21), fill=FG)


def sc_outro(img, d, t, T, A):
    """6) 片尾 24.2-27.0s"""
    a = seg(t, 0.0, 0.5, "out")
    if a <= 0:
        return
    if A.icon:
        ic = A.icon.resize((110, 110), Image.LANCZOS)
        if a < 1:
            ic = ic.copy()
            ic.putalpha(ic.split()[3].point(lambda v: int(v * a)))
        img.paste(ic, (int(W / 2 - 55), 250), ic)
    y = 400
    d.text((W / 2, y), "Sui-WinDuo", font=mono(76), fill=HILITE, anchor="mm")
    d.text((W / 2, y + 74), "二创自开源项目 WindowsDuo（MIT）", font=cn(27), fill=DIM, anchor="mm")
    d.text((W / 2, y + 132), "EthanMaven  ·  github.com/comreade-123", font=mono(24),
           fill=GREEN, anchor="mm")
    d.text((W / 2, y + 178), "github.com/comreade-123/Sui-WinDuo", font=mono(22),
           fill=CYAN, anchor="mm")
    d.text((W / 2, H - 90), "MIT License  ·  ESP32 + Arduino IDE + PyQt6 + OpenGL",
           font=cn(19), fill=(96, 104, 116), anchor="mm")


SCENES = [
    (sc_title, 0.0, 2.6),
    (sc_effect, 2.6, 9.0),
    (sc_hardware, 9.0, 14.0),
    (sc_hinge, 14.0, 19.0),
    (sc_pipeline, 19.0, 24.2),
    (sc_outro, 24.2, 27.0),
]


def render_frame(idx, A, total_frames):
    tg = idx / FPS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img, "RGBA")

    # 背景网格（轻微移动，避免"死"背景）
    off = (tg * 12) % 60
    for gx in range(-60, W + 60, 60):
        d.line([(gx + off, 0), (gx + off, H)], fill=(16, 19, 24), width=1)
    for gy in range(-60, H + 60, 60):
        d.line([(0, gy), (W, gy)], fill=(16, 19, 24), width=1)

    for fn, s, e in SCENES:
        if s <= tg < e:
            fn(img, ImageDraw.Draw(img, "RGBA"), tg - s, e - s, A)
            break

    # 全局淡入淡出
    fade = 1.0
    if tg < 0.4:
        fade = tg / 0.4
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
    args = ap.parse_args()

    ff = find_ffmpeg()
    if not ff:
        print("[错误] 找不到 ffmpeg")
        return 2

    print("=" * 66)
    print("Sui-WinDuo 工作原理动画 v2")
    print("=" * 66)
    print("分辨率 %dx%d @ %dfps   时长 %.1fs" % (W, H, args.fps, args.seconds))
    A = Assets()
    print("素材: 桌面 %s | 玻璃序列 %d 档 (%s) | 硬件图层 %d 个"
          % (A.desktop.size, len(A.tilts),
             ",".join("%.0f" % v for v in A.tilts),
             sum(1 for v in A.hw.values() if v)))

    total = int(args.seconds * args.fps)
    tmp = Path(tempfile.mkdtemp(prefix="winduo_v2_"))
    t0 = time.time()
    for i in range(total):
        render_frame(i, A, total).save(tmp / ("f%05d.png" % i), compress_level=1)
        if i % 90 == 0 or i == total - 1:
            el = time.time() - t0
            print("  %4d/%d 帧  (%.1fs, %.1f 帧/秒)" % (i + 1, total, el, (i + 1) / max(1e-6, el)),
                  flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(args.fps), "-i", str(tmp / "f%05d.png"),
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("[错误] ffmpeg:\n" + (r.stderr or "")[:1500])
        return 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n完成: %s  (%.2f MB)" % (out, out.stat().st_size / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
