"""
Sui-WinDuo 工作原理动画 —— 纯代码生成（PIL + numpy 渲染，ffmpeg 编码 MP4）
============================================================================
不依赖任何录屏或剪辑软件：每一帧都用 Pillow 画出来，再用 ffmpeg 的 libx264
编码成 H.264 MP4。

内容按整个项目的真实实现来编排（数字全部来自项目实测/源码，不是编的）：
  1  片头：标题
  2  硬件：ESP32 + MPU6050 + SSD1306 的 I2C 拓扑
  3  采样：I2C 帧时序 + 角速度积分与加速度计参考
  4  解算：分区融合（近零区加速度计校准 / 深负区纯陀螺仪）+ 实测漂移数据
  5  输出：115200 串口 JSON 行协议
  6  渲染：逆投影几何（眼睛 -> 玻璃像素 -> 界面平面），Vogel 盘模糊
  7  对比：未启用 vs 启用玻璃效果
  8  控制台：pc/sui_winduo_app.py 截图
  9  片尾：署名

用法：
  .venv\\Scripts\\python.exe tools/make_intro_video.py
  .venv\\Scripts\\python.exe tools/make_intro_video.py --out docs/how-it-works.mp4 --fps 30
"""

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- 基本设置
W, H = 1920, 1080
FPS = 30
TOTAL_SEC = 28.0

ROOT = Path(__file__).resolve().parent.parent
FFMPEG_CANDIDATES = [
    r"D:\ffmpeg\ffmpeg-6.1.1-essentials_build\bin\ffmpeg.exe",
    "ffmpeg",
]

# 配色（与项目 GUI / 终端风格一致）
BG = (10, 12, 14)
FG = (226, 232, 240)
DIM = (139, 148, 158)
GREEN = (74, 246, 38)
CYAN = (56, 189, 248)
AMBER = (255, 189, 46)
RED = (255, 95, 86)
PANEL = (20, 23, 27)
LINE = (44, 50, 58)
WHITE = (255, 255, 255)

FONT_DIR = Path(r"C:\Windows\Fonts")
MONO_BOLD = FONT_DIR / "CascadiaMono.ttf"
CN_REG = FONT_DIR / "msyh.ttc"
CN_BOLD = FONT_DIR / "msyhbd.ttc"

# ---------------------------------------------------------------- 小工具
_cache = {}


def font(path, size):
    key = (str(path), size)
    if key not in _cache:
        try:
            _cache[key] = ImageFont.truetype(str(path), size)
        except Exception:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def ease(t, kind="inout"):
    """缓动函数，t 归一到 0..1。"""
    t = max(0.0, min(1.0, t))
    if kind == "out":
        return 1 - (1 - t) ** 3
    if kind == "in":
        return t ** 3
    if kind == "outback":
        c1, c3 = 1.70158, 2.70158
        return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
    return t * t * (3 - 2 * t)


def clamp01(v):
    return max(0.0, min(1.0, v))


def seg(t, start, end, kind="inout"):
    """把全局时间映射到某个片段内的 0..1 进度。"""
    if t <= start:
        return 0.0
    if t >= end:
        return 1.0
    return ease((t - start) / max(1e-6, end - start), kind)


def text(d, xy, s, f, color, anchor="la"):
    d.text(xy, s, font=f, fill=color, anchor=anchor)


def fit(d, s, path, max_w, start=110):
    """自动缩字号，保证一行放得下。"""
    size = start
    while size > 10:
        f = font(path, size)
        if d.textlength(s, font=f) <= max_w:
            return f
        size -= 2
    return font(path, 10)


def mono(size):
    return font(MONO_BOLD, size)


def cn(size, bold=False):
    return font(CN_BOLD if bold else CN_REG, size)


def panel(d, box, title=None, accent=LINE, radius=8):
    """终端风格面板：淡背景 + 圆角描边；有标题时标题压在边框线上。"""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=radius, fill=PANEL, outline=accent, width=1)
    if title:
        f = cn(22)
        label = f"─ {title} ─"
        tw = d.textlength(label, font=f)
        # 先用背景色盖住一段边框线，再把标题写上去（避免被线穿透）
        d.rectangle([x0 + 14, y0 - 12, x0 + 14 + tw + 8, y0 + 12], fill=PANEL)
        d.text((x0 + 18, y0), label, font=f, fill=accent, anchor="lm")
    return box


def arrow(d, p0, p1, color, width=3, head=14, dash=None):
    """画箭头；dash 给定时画虚线。"""
    x0, y0 = p0
    x1, y1 = p1
    if dash:
        total = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(total // (dash * 2)))
        for i in range(n + 1):
            t0 = (i * 2 * dash) / total
            t1 = min(1.0, (i * 2 * dash + dash) / total)
            d.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                    (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)],
                   fill=color, width=width)
    else:
        d.line([p0, p1], fill=color, width=width)
    ang = math.atan2(y1 - y0, x1 - x0)
    for s in (+1, -1):
        a = ang + s * math.radians(26)
        d.line([(x1, y1), (x1 - head * math.cos(a), y1 - head * math.sin(a))],
               fill=color, width=width)


def bar(d, x, y, w, h, ratio, fg=GREEN, bg=LINE):
    """字符风格进度条（用矩形模拟 █░）。"""
    d.rectangle([x, y, x + w, y + h], fill=bg)
    d.rectangle([x, y, x + int(w * clamp01(ratio)), y + h], fill=fg)


def chip(d, xy, label, color=GREEN, size=20, pad=(12, 7)):
    f = mono(size)
    w = d.textlength(label, font=f)
    x, y = xy
    d.rounded_rectangle([x, y, x + w + pad[0] * 2, y + size + pad[1] * 2],
                        radius=6, outline=color, width=2)
    d.text((x + pad[0], y + pad[1] - 1), label, font=f, fill=color)


# ---------------------------------------------------------------- 场景绘制
def scene_intro(d, t, T):
    """1) 片头"""
    a = seg(t, 0.15, 0.9, "out")
    if a <= 0:
        return
    # logo
    logo = ROOT / "icon.png"
    if logo.exists():
        im = Image.open(logo).convert("RGBA").resize((150, 150), Image.LANCZOS)
        inv = Image.new("RGBA", im.size, (0, 0, 0, 0))
        px = im.load()
        ip = inv.load()
        for yy in range(im.height):
            for xx in range(im.width):
                r, g, b, al = px[xx, yy]
                if r < 110 and g < 110 and b < 110:
                    ip[xx, yy] = (255, 255, 255, int(255 * a))
        d._image.paste(inv, (int(W / 2 - 75), 150), inv)

    f1 = fit(d, "Sui-WinDuo", MONO_BOLD, 1300, 128)
    f2 = cn(38)
    f3 = mono(24)
    y = 430
    d.text((W / 2, y), "Sui-WinDuo", font=f1, fill=WHITE, anchor="mm")
    d.text((W / 2, y + 105), "笔记本屏幕开合角  ·  悬浮玻璃效果", font=f2,
           fill=DIM, anchor="mm")
    d.text((W / 2, y + 165), "ESP32 + MPU6050 + SSD1306  ->  串口 JSON  ->  OpenGL 着色器",
           font=cn(23), fill=GREEN, anchor="mm")
    d.text((W / 2, H - 90), "EthanMaven  ·  github.com/comreade-123  ·  MIT", font=mono(20),
           fill=(90, 98, 108), anchor="mm")


def draw_board(d, x, y, w, h, label, sub, color):
    """一块板子/模块。"""
    d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=(24, 28, 33),
                        outline=color, width=2)
    f = mono(22)
    fs = mono(16)
    d.text((x + w / 2, y + h / 2 - 12), label, font=f, fill=color, anchor="mm")
    d.text((x + w / 2, y + h / 2 + 16), sub, font=fs, fill=DIM, anchor="mm")


def scene_hardware(d, t, T):
    """2) 硬件拓扑"""
    panel(d, (80, 90, W - 80, H - 150), "硬件接线 / I2C 总线拓扑", LINE)
    ax = seg(t, 0.0, 0.35, "out")
    ay = seg(t, 0.15, 0.5, "out")
    az = seg(t, 0.3, 0.65, "out")

    ex, ey, ew, eh = 170, 330, 300, 190
    if ax > 0:
        draw_board(d, ex, ey, ew, eh, "ESP32", "WROOM-32E  240MHz", GREEN)
    if ay > 0:
        draw_board(d, 900, 240, 320, 150, "MPU6050", "6 轴 IMU  0x68", CYAN)
    if az > 0:
        draw_board(d, 900, 470, 320, 150, "SSD1306", "128x64 OLED  0x3C", AMBER)

    # I2C 总线
    if ay > 0.6:
        p = seg(t, 0.5, 0.8, "out")
        bx = ex + ew + 30
        d.line([(bx, 300), (bx, 620)], fill=CYAN, width=3)
        n = int(40 * p)
        for i in range(6):
            yy = 300 + i * 60
            if yy > 300 + (620 - 300) * p:
                break
            d.line([(bx, yy), (900, yy)], fill=CYAN, width=2)
        d.text((bx + 8, 285), "SCL / SDA", font=mono(18), fill=CYAN)
        d.text((bx + 8, 640), "400 kHz", font=mono(16), fill=DIM)

    if az > 0.8:
        d.text((170, 620), "SDA -> GPIO21   SCL -> GPIO22（固件开机自动识别候选引脚）",
               font=cn(21), fill=FG)
        d.text((170, 660), "按键 -> GPIO5（INPUT_PULLUP）   3V3 / GND 共地",
               font=cn(21), fill=FG)
    if az > 0.95:
        chip(d, (170, 720), "[ 非阻塞 ]", GREEN, 20)
        chip(d, (390, 720), "[ 无 delay() ]", GREEN, 20)
        chip(d, (640, 720), "[ 200Hz 采样 ]", GREEN, 20)


def scene_sample(d, t, T):
    """3) 采样：I2C 时序 + 角速度积分"""
    panel(d, (80, 90, W - 80, H - 150), "采样与姿态解算 / 陀螺积分 + 加速度计参考", LINE)
    # 左：I2C 时钟示意
    gx, gy, gw, gh = 150, 230, 700, 200
    d.text((gx, gy - 40), "I2C 帧（200Hz）", font=cn(23, True), fill=CYAN)
    d.rectangle([gx, gy, gx + gw, gy + gh], outline=LINE, width=1)
    for i in range(14):
        x = gx + 20 + i * (gw - 40) / 13
        h = (gh - 60) * (0.25 + 0.5 * abs(math.sin(i * 1.1)))
        d.rectangle([x - 7, gy + gh / 2 - h / 2, x + 7, gy + gh / 2 + h / 2],
                    fill=(30, 40, 48))
    move = (t / T * 4) % 1.0
    mx = gx + 20 + move * (gw - 40)
    d.rectangle([mx - 3, gy + 10, mx + 3, gy + gh - 10], fill=GREEN)
    d.text((gx, gy + gh + 30), "0x3B 连续读 14 字节 → ax ay az gx gy gz", font=cn(20),
           fill=DIM)

    # 右：积分示意
    rx = 960
    d.text((rx, gy - 40), "角度 = 角速度对时间积分（累加，不做 ±180 取模）",
           font=cn(23, True), fill=GREEN)
    pts = []
    for i in range(101):
        tt = i / 100
        val = -70 * math.sin(tt * math.pi * 0.9)
        pts.append((rx + tt * 780, gy + gh / 2 - val * 1.1))
    prog = seg(t, 0.1, 0.85, "out")
    n = max(2, int(len(pts) * prog))
    d.line(pts[:n], fill=GREEN, width=4)
    d.line([(rx, gy + gh / 2), (rx + 780, gy + gh / 2)], fill=LINE, width=1)
    d.text((rx, gy + gh + 30), "可无限累积，越过 ±180 度也不会跳回反方向", font=cn(20),
           fill=DIM)

    if t > 1.2:
        y = 620
        d.text((150, y), "为什么要融合两个传感器：", font=cn(24, True), fill=FG)
        d.text((150, y + 46), "陀螺仪  ——  响应快、无折返，但有零偏，会缓慢漂移",
               font=cn(21), fill=CYAN)
        d.text((150, y + 84), "加速度计 ——  绝对参考、不漂移，但 ±90 度处 atan2 折返",
               font=cn(21), fill=AMBER)


def scene_fusion(d, t, T):
    """4) 分区融合 + 实测数据"""
    panel(d, (80, 90, W - 80, H - 150), "分区解算 / 近零区用加速度计校准，深负区用纯陀螺仪", LINE)
    # 分区示意条
    x0, x1 = 220, W - 220
    y = 250
    d.rectangle([x0, y, x1, y + 60], outline=LINE, width=2)
    split = x0 + (x1 - x0) * 0.34
    d.rectangle([split, y, x1, y + 60], fill=(18, 40, 24))
    d.text(((x0 + split) / 2, y + 30), "近零区（>= -8°）  加速度计校准", font=cn(24),
           fill=GREEN, anchor="mm")
    d.text(((split + x1) / 2, y + 30), "深负区（< -8°）  纯陀螺仪积分", font=cn(24),
           fill=CYAN, anchor="mm")
    d.line([(split, y - 30), (split, y + 90)], fill=AMBER, width=2)
    d.text((split, y + 110), "-8°", font=mono(20), fill=AMBER, anchor="mm")

    # 角标在条上移动
    move = (t / T * 2) % 1.0
    ax = x0 + (x1 - x0) * (0.65 - 0.5 * abs(math.sin(move * math.pi)))
    d.polygon([(ax, y - 22), (ax - 11, y - 40), (ax + 11, y - 40)], fill=GREEN)

    # 实测数据面板
    px = 220
    py = 430
    tw = 700
    panel(d, (px, py, px + tw, py + 330), "实测数据（真实测量）", LINE)
    rows = [
        ("静止 40 秒漂移", "0.00 度/分钟", GREEN),
        ("波动跨度", "0.60 度", GREEN),
        ("加速度计倾角信度 conf", "1.000", GREEN),
        ("GPU 着色器 taps=32", "1.22 ms/帧", CYAN),
        ("无意义重绘", "降低 98.7%", CYAN),
        ("空闲时叠加层", "自动隐藏，GPU 归零", CYAN),
    ]
    for i, (k, v, c) in enumerate(rows):
        yy = py + 60 + i * 44
        d.text((px + 30, yy), k, font=cn(21), fill=DIM)
        d.text((px + tw - 30, yy), v, font=mono(21), fill=c, anchor="ra")

    # 右侧修 bug 的说明
    qx = 1000
    panel(d, (qx, py, qx + 700, py + 330), "过程中修掉的真实缺陷", LINE)
    bugs = [
        "加速度计参考轴选错 -> conf 恒为 0",
        "  => 修正被永久冻结，退化成纯陀螺仪",
        "对累积角取 ±180 模 -> 越界跳回反号",
        "Thread._stop 被 bool 覆盖 -> join() 崩溃",
        "截屏降分辨率 -> 全屏被放大变糊",
    ]
    for i, s in enumerate(bugs):
        col = RED if s.startswith("  ") else AMBER
        d.text((qx + 30, py + 60 + i * 44), s, font=cn(20), fill=col)


def scene_protocol(d, t, T):
    """5) 串口协议"""
    panel(d, (80, 90, W - 80, H - 150), "串口协议 / 115200 8N1，每 50ms 一行 JSON", LINE)
    f = mono(26)
    base = '{"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}'
    ty = 240
    d.text((170, ty - 60), "固件输出（20Hz）", font=cn(24, True), fill=FG)

    # 逐字显示
    n = int(len(base) * seg(t, 0.05, 0.75, "out"))
    shown = base[:n]
    d.text((170, ty), shown, font=f, fill=GREEN)

    # 字段高亮说明
    if t > 1.0:
        items = [
            ("angle", "有符号累积转角，可超过 ±180", CYAN),
            ("status", "正常 / 校准中 / 预热中 / 传感器异常", AMBER),
            ("mode", "default / calibrate / debug", GREEN),
            ("author", "作者标识", DIM),
        ]
        for i, (k, v, c) in enumerate(items):
            yy = ty + 120 + i * 62
            d.text((170, yy), f'"{k}"', font=mono(24), fill=c)
            d.text((420, yy), v, font=cn(23), fill=FG)

    # 数据流
    if t > 1.6:
        y = H - 260
        d.text((170, y - 46), "PC 端读取链路", font=cn(24, True), fill=FG)
        boxes = [("USB-SERIAL", "CH340"), ("serial_reader_win", "ctypes 零依赖"),
                 ("parse_line", "JSON 容错解析"), ("angle_to_concentration", "映射浓度")]
        bw = 360
        for i, (a, b) in enumerate(boxes):
            bx = 170 + i * (bw + 40)
            vis = seg(t, 1.6 + i * 0.18, 2.0 + i * 0.18, "out")
            if vis <= 0:
                continue
            d.rounded_rectangle([bx, y, bx + bw, y + 110], radius=8, fill=(22, 26, 31),
                                outline=GREEN, width=2)
            d.text((bx + bw / 2, y + 38), a, font=mono(20), fill=GREEN, anchor="mm")
            d.text((bx + bw / 2, y + 76), b, font=cn(20), fill=DIM, anchor="mm")
            if i < len(boxes) - 1 and vis > 0.6:
                arrow(d, (bx + bw + 6, y + 55), (bx + bw + 34, y + 55), GREEN, 3, 12)


def scene_shader(d, t, T):
    """6) 逆投影几何"""
    panel(d, (80, 90, W - 80, H - 150), "渲染原理 / 空间化逆投影（不是普通全屏模糊）", LINE)

    # 坐标系：底部横线 = 界面平面，中间斜线 = 玻璃平面
    ox, oy = 300, 820          # 铰链位置
    plane_w = 900
    tilt = math.radians(38)
    # 界面平面（水平）
    d.line([(ox - 120, oy), (ox + plane_w + 160, oy)], fill=DIM, width=3)
    d.text((ox + plane_w + 170, oy), "桌面内容（世界平面）", font=cn(20), fill=DIM, anchor="lm")

    # 玻璃平面（绕铰链抬起）
    gx1 = ox + plane_w
    gy1 = oy - plane_w * math.tan(math.radians(24)) * 0.0
    # 用固定倾角画玻璃：从铰链向右上方
    gl_len = plane_w
    gxe = ox + gl_len * math.cos(tilt)
    gye = oy - gl_len * math.sin(tilt)
    d.line([(ox, oy), (gxe, gye)], fill=CYAN, width=4)
    d.text((gxe + 12, gye - 8), "玻璃平面（屏幕）", font=cn(20), fill=CYAN)

    # 铰链
    d.ellipse([ox - 9, oy - 9, ox + 9, oy + 9], fill=AMBER)
    d.text((ox - 20, oy + 24), "铰链 = 屏幕底边", font=cn(19), fill=AMBER)

    # 眼睛
    eye = (ox + 460, oy - 460)
    d.ellipse([eye[0] - 12, eye[1] - 12, eye[0] + 12, eye[1] + 12], fill=WHITE)
    d.text((eye[0] + 20, eye[1] - 10), "视点", font=cn(20), fill=WHITE)

    # 抽样几条光线：eye -> 玻璃上一点 -> 延长交界面
    prog = seg(t, 0.15, 0.9, "out")
    for i in range(5):
        u = 0.28 + i * 0.17
        px_ = ox + gl_len * math.cos(tilt) * u
        py_ = oy - gl_len * math.sin(tilt) * u
        # 延长 ray 到 y = oy 平面
        if py_ >= eye[1]:
            continue
        k = (oy - eye[1]) / (py_ - eye[1])
        hx = eye[0] + (px_ - eye[0]) * k
        hy = oy
        c = (40, 60, 70)
        d.line([eye, (px_, py_)], fill=c, width=2)
        # 玻璃到交点：向右偏移（透视拉伸体现在这里）
        seg_len = max(0.0, prog - i * 0.12)
        if seg_len > 0:
            ex = px_ + (hx - px_) * min(1.0, seg_len * 1.6)
            ey = py_ + (hy - py_) * min(1.0, seg_len * 1.6)
            d.line([(px_, py_), (ex, ey)], fill=GREEN, width=2)
        d.ellipse([px_ - 5, py_ - 5, px_ + 5, py_ + 5], fill=CYAN)

    # 说明（中文必须用中文字体：Cascadia Mono 没有中文字形，会渲染成方块）
    if t > 1.2:
        tx = 1180
        panel(d, (tx, 230, tx + 640, 760), None, LINE)
        fh = cn(23, True)
        fc = cn(21)
        fn = mono(21)
        d.text((tx + 26, 258), "着色器做了什么", font=fh, fill=FG)
        lines = [
            ("1", "每个像素放到玻璃平面上", CYAN),
            ("2", "从视点发射光线穿过该像素", CYAN),
            ("3", "延长光线，求与界面平面的交点", CYAN),
            ("4", "以交点为中心做采样", GREEN),
            ("5", "间隙越大 → 模糊半径越大", GREEN),
            ("6", "Vogel 盘旋采样 + mip LOD 分级", GREEN),
            ("7", "核出界 → 边缘覆盖率平滑", GREEN),
            ("8", "磨砂吸光 → 越远越暗", AMBER),
        ]
        for i, (num, s, c) in enumerate(lines):
            vis = seg(t, 1.2 + i * 0.1, 1.6 + i * 0.1, "out")
            if vis <= 0:
                continue
            yy = 306 + i * 54
            d.text((tx + 26, yy), num, font=fn, fill=c)
            d.text((tx + 58, yy), s, font=fc, fill=c)

    if t > 2.2:
        d.text((300, 920), "靠近铰链处始终清晰，远离铰链处先模糊、先变暗、最终消失",
               font=cn(23), fill=FG)


def scene_compare(d, t, T, clear_img, blurred_img, gui_img):
    """7) 效果对比 + 控制台"""
    panel(d, (80, 90, W - 80, H - 150), "效果对比 / 未启用 与 启用玻璃", LINE)

    tw, th = 820, 470
    y = 200
    left_x, right_x = 160, 940

    d.text((left_x, y - 42), "未启用", font=cn(24, True), fill=DIM)
    d.text((right_x, y - 42), "启用玻璃效果", font=cn(24, True), fill=GREEN)

    d.image_paste = ImageDraw.Draw(d._image)
    d._image.paste(clear_img, (left_x, y))
    d._image.paste(blurred_img, (right_x, y))
    d.rectangle([left_x, y, left_x + tw, y + th], outline=LINE, width=2)
    d.rectangle([right_x, y, right_x + tw, y + th], outline=GREEN, width=2)

    # 右侧扫描高光
    sweep = (t / T * 1.2) % 1.0
    sx = right_x + sweep * tw
    overlay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for i in range(60):
        xx = int(sx - right_x - i)
        if 0 <= xx < tw:
            al = int(70 * (1 - i / 60))
            od.line([(xx, 0), (xx, th)], fill=(255, 255, 255, al))
    d._image.paste(Image.alpha_composite(
        d._image.crop((right_x, y, right_x + tw, y + th)).convert("RGBA"), overlay).convert("RGB"),
        (right_x, y))

    if t > 1.4:
        gy = 720
        gh = 240
        vis = seg(t, 1.4, 1.9, "out")
        if vis > 0:
            gi = gui_img.resize((int(gh * gui_img.width / gui_img.height), gh), Image.LANCZOS)
            d._image.paste(gi, (160, gy))
            d.rectangle([160, gy, 160 + gi.width, gy + gh], outline=CYAN, width=2)
            d.text((200 + gi.width, gy + 20), "pc/sui_winduo_app.py", font=mono(22), fill=CYAN)
            d.text((200 + gi.width, gy + 62), "终端风格控制台 + 毛玻璃外壳", font=cn(22), fill=FG)
            d.text((200 + gi.width, gy + 104), "串口选择 / 启动停止 / 参数实时微调", font=cn(22),
                   fill=DIM)
            d.text((200 + gi.width, gy + 146), "Windows 原生标题栏 · 全中文界面", font=cn(22),
                   fill=DIM)


def scene_outro(d, t, T):
    """9) 片尾"""
    a = seg(t, 0.05, 0.6, "out")
    if a <= 0:
        return
    f1 = fit(d, "Sui-WinDuo", MONO_BOLD, 1200, 100)
    d.text((W / 2, 380), "Sui-WinDuo", font=f1, fill=WHITE, anchor="mm")
    d.text((W / 2, 480), "二创自开源项目 WindowsDuo（MIT）", font=cn(30), fill=DIM, anchor="mm")
    d.text((W / 2, 540), "EthanMaven  ·  github.com/comreade-123", font=mono(26), fill=GREEN,
           anchor="mm")
    d.text((W / 2, 610), "github.com/comreade-123/Sui-WinDuo", font=mono(24), fill=CYAN,
           anchor="mm")
    d.text((W / 2, H - 110), "MIT License  ·  Built with ESP32 + Arduino IDE + PyQt6 + OpenGL",
           font=cn(19), fill=(90, 98, 108), anchor="mm")


# ---------------------------------------------------------------- 素材准备
def prepare_assets():
    """准备对比图与 GUI 截图（玻璃效果用真实着色器参数近似）。"""
    assets = {}

    # 找一张桌面截图作为"未启用"底图
    base = None
    for p in [ROOT / "pc" / "cmp_scale050.png", ROOT / "pc" / "smoke_widget.png"]:
        if p.exists():
            base = Image.open(p).convert("RGB")
            break
    if base is None:
        # 没有素材就用程序化的桌面样式图（避免依赖缺失文件）
        base = Image.new("RGB", (1280, 800), (28, 32, 40))
        dd = ImageDraw.Draw(base)
        for i in range(6):
            dd.rectangle([60, 90 + i * 110, 1220, 160 + i * 110],
                         fill=(44 + i * 6, 52 + i * 6, 66 + i * 6))
        dd.text((70, 40), "desktop sample", font=font(MONO_BOLD, 28), fill=(200, 210, 220))

    clear = base.resize((820, 470), Image.LANCZOS)
    assets["clear"] = clear

    # "启用"图：真实着色器的近似 —— 越远模糊越强 + 磨砂变暗 + 出界渐黑
    arr = np.asarray(clear).astype(np.float32)
    h, w, _ = arr.shape

    # 权重按行广播：形状必须是 (h, 1, 1) 才能乘到 (h, w, 3) 上。
    # 注意 (h, 1) 不行：numpy 会拿 470 去对齐倒数第二维 820 而报错
    # （广播是从最后一维往前比，不是"按行"）。
    yy = np.linspace(0, 1, h)[:, None, None]

    blur1 = clear.filter(ImageFilter.GaussianBlur(1.6))
    blur2 = clear.filter(ImageFilter.GaussianBlur(6.0))
    blur3 = clear.filter(ImageFilter.GaussianBlur(14.0))
    a1 = np.asarray(blur1).astype(np.float32)
    a2 = np.asarray(blur2).astype(np.float32)
    a3 = np.asarray(blur3).astype(np.float32)

    # 底部（铰链）清晰，顶部最糊
    w1 = np.clip(1.0 - yy * 2.0, 0, 1)
    w3 = np.clip(yy * 1.6 - 0.4, 0, 1)
    w2 = np.clip(1.0 - w1 - w3, 0, 1)
    mixed = a1 * w1 + a2 * w2 + a3 * w3

    # 磨砂变暗：越远越暗
    dark = 1.0 - 0.42 * np.clip(yy * 1.25 - 0.05, 0, 1)
    mixed = mixed * dark

    # 视线出界 -> 边缘渐黑（xx 为 (1, w, 1)，与 (h, 1, 1) 相加得到 (h, w, 1)）
    xx = np.linspace(0, 1, w)[None, :, None]
    edge = np.clip(1.0 - ((xx - 0.5) ** 2 * 3.4 + (yy - 0.2) ** 2 * 1.1) * 1.1, 0, 1)
    mixed = mixed * (0.25 + 0.75 * edge)
    assets["blurred"] = Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8))

    # GUI 截图
    gui = ROOT / "pc" / "app_ui.png"
    if gui.exists():
        assets["gui"] = Image.open(gui).convert("RGB")
    else:
        assets["gui"] = Image.new("RGB", (1200, 900), (16, 18, 20))
    return assets


# ---------------------------------------------------------------- 时间轴
SCENES = [
    ("intro", 0.0, 3.2),
    ("hardware", 3.2, 7.4),
    ("sample", 7.4, 11.6),
    ("fusion", 11.6, 15.4),
    ("protocol", 15.4, 19.2),
    ("shader", 19.2, 23.2),
    ("compare", 23.2, 26.4),
    ("outro", 26.4, 28.0),
]


def render_frame(idx, total, assets, fade_in=0.35, fade_out=0.45):
    t_global = idx / FPS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d._image = img

    # 场景标题条
    for name, s, e in SCENES:
        if s <= t_global < e:
            local = t_global - s
            dur = e - s
            if name == "intro":
                scene_intro(d, local, dur)
            elif name == "hardware":
                scene_hardware(d, local, dur)
            elif name == "sample":
                scene_sample(d, local, dur)
            elif name == "fusion":
                scene_fusion(d, local, dur)
            elif name == "protocol":
                scene_protocol(d, local, dur)
            elif name == "shader":
                scene_shader(d, local, dur)
            elif name == "compare":
                scene_compare(d, local, dur, assets["clear"], assets["blurred"], assets["gui"])
            elif name == "outro":
                scene_outro(d, local, dur)
            break

    # 全局淡入淡出（仅首尾）
    fade = 1.0
    if t_global < fade_in:
        fade = t_global / fade_in
    elif t_global > TOTAL_SEC - fade_out:
        fade = max(0.0, (TOTAL_SEC - t_global) / fade_out)
    if fade < 1.0:
        black = Image.new("RGB", (W, H), (0, 0, 0))
        img = Image.blend(black, img, clamp01(fade))
    return img


def find_ffmpeg():
    for c in FFMPEG_CANDIDATES:
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
    ap.add_argument("--seconds", type=float, default=TOTAL_SEC)
    ap.add_argument("--keep-frames", action="store_true")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[错误] 找不到 ffmpeg，无法编码 MP4。")
        return 2

    total = int(args.seconds * args.fps)
    print("=" * 64)
    print("Sui-WinDuo 工作原理动画")
    print("=" * 64)
    print("分辨率 : %dx%d @ %dfps" % (W, H, args.fps))
    print("时长   : %.1f 秒（%d 帧）" % (args.seconds, total))
    print("输出   : %s" % args.out)
    print("ffmpeg : %s" % ffmpeg)
    print()

    print("[1/3] 准备素材 ...")
    assets = prepare_assets()
    print("      对比图 %s / GUI %s" % (assets["clear"].size, assets["gui"].size))

    tmp = Path(tempfile.mkdtemp(prefix="winduo_anim_"))
    print("[2/3] 渲染帧 -> %s" % tmp)
    import time
    t0 = time.time()
    for i in range(total):
        img = render_frame(i, total, assets)
        img.save(tmp / ("f%05d.png" % i), compress_level=1)
        if i % 60 == 0 or i == total - 1:
            el = time.time() - t0
            print("      %4d / %d 帧  (%.1fs, %.1f 帧/秒)"
                  % (i + 1, total, el, (i + 1) / max(1e-6, el)), flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print("[3/3] 编码 H.264 ...")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(args.fps), "-i", str(tmp / "f%05d.png"),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("[错误] ffmpeg 失败：\n%s" % (r.stderr or "")[:2000])
        return 1

    if not args.keep_frames:
        shutil.rmtree(tmp, ignore_errors=True)

    size = out.stat().st_size if out.exists() else 0
    print()
    print("完成：%s  (%.2f MB, %.1f 秒)" % (out, size / 1024 / 1024, args.seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
