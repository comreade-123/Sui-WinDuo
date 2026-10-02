"""
硬件模块矢量绘制 v2 —— 按实物照片重绘，比例与细节对齐真实形态
================================================================
修正 v1 的问题：
  · 硬件示意画错位、比例失调（用户反馈"很丑也画错位了"）
  · OLED 显示内容是我编的（"WinDuo / ANGLE +42.6"），与实际固件不符
    —— 实际布局见 firmware drawMainScreen()：
         WinDuo STD          (setCursor(0,0),  size 1)
         ─────────────       (drawFastHLine(0,10,128))
            42        deg    (setCursor(0,16) size 3 四位对齐 / setCursor(80,24) size 1)
         ┌────────────┐      (drawRect(0,44,128,8))
         └────────────┘
         S:ok  46%           (setCursor(0,55), size 1)

绘制原则：
  · 尺寸严格按真实模块比例（ESP32 开发板 ≈ 54x28mm，GY-521 ≈ 21x16mm，
    SSD1306 ≈ 27x27mm），这样拼在一起不会"错位"
  · 每个模块画在透明画布中央，导出 RGBA 供动画独立摆放
"""

import math
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = {}


def F(name, size):
    key = (name, size)
    if key not in FONTS:
        try:
            FONTS[key] = ImageFont.truetype(r"C:\Windows\Fonts\\" + name, size)
        except Exception:
            FONTS[key] = ImageFont.load_default()
    return FONTS[key]


def shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def pxr(v, s):
    """缩放并取整：PIL 的绘图 API 只接受整数，浮点会抛
    TypeError: 'float' object cannot be interpreted as an integer。"""
    return max(0, int(round(v * s)))


# ================================================================ SSD1306
# 真实比例：模块 27x27mm，玻璃可视区 128x64 像素（约 21.7x11mm）
#
# 绘制模型：先在"逻辑画布"上用固件那套 128x64 坐标摆放元素，字号也按
# SSD1306 的语义（字号 1 = 6x8 像素、字号 3 = 18x24 像素），最后整体乘 K
# 放大到图层像素。这样位置与字号天然等比，不会出现文字挤在顶边或糊掉。
def draw_ssd1306(img, x, y, s=1.0, alpha=1.0, screen="main", angle=42, pct=46,
                 status="ok", mode="STD", calib_pct=63, glow=1.0):
    """SSD1306 0.96" OLED（I2C，0x3C）。

    screen: "main"  主界面 —— 按固件 drawMainScreen() 的坐标
            "calib" 校准界面 —— 按固件 drawCalibrationScreen() 的坐标
    """
    K = 2.1 * s                        # 逻辑像素 -> 图层像素
    def L(v):
        return max(0, int(round(v * K)))

    d = ImageDraw.Draw(img, "RGBA")
    def A(v):
        return int(255 * v)

    # 逻辑画布 240x240，玻璃区 200x100（2:1）
    PW, PH = L(240), L(240)
    gx0, gy0 = L(20), L(84)
    gw, gh = L(200), L(100)
    gx1, gy1 = gx0 + gw, gy0 + gh
    sc = gw / 128.0                    # 固件逻辑像素 -> 图层像素

    # 模块 PCB
    d.rounded_rectangle([x, y, x + PW, y + PH], radius=L(6),
                        fill=(18, 44, 92, A(alpha)),
                        outline=(58, 108, 176, A(alpha)), width=max(1, L(1.4)))
    # 玻璃
    d.rounded_rectangle([gx0, gy0, gx1, gy1], radius=L(3),
                        fill=(5, 7, 10, A(alpha)),
                        outline=(40, 44, 52, A(alpha)), width=max(1, L(1)))

    if glow > 0.02:
        g = min(1.0, glow)
        ON = (176, 238, 255, A(g))
        DIMC = (116, 196, 228, A(g * 0.8))

        def put(lx, ly, text, size, color):
            """lx/ly 用固件的 128x64 逻辑坐标；size 用固件字号语义。"""
            fpt = max(7, int(size * 8 * sc))
            d.text((gx0 + lx * sc, gy0 + ly * sc), text,
                   font=F("consola.ttf", fpt), fill=color)

        if screen == "main":
            put(0, 0, "WinDuo %s" % mode, 1, ON)
            d.line([(gx0, gy0 + 10 * sc), (gx0 + 128 * sc, gy0 + 10 * sc)],
                   fill=DIMC, width=max(1, int(sc)))
            atxt = str(int(round(angle)))
            if len(atxt) < 4:
                atxt = " " * (4 - len(atxt)) + atxt
            put(0, 14, atxt, 3, ON)
            put(82, 24, "deg", 1, DIMC)
            bx0, by0 = gx0, gy0 + 44 * sc
            bx1, by1 = gx0 + 128 * sc, gy0 + 52 * sc
            d.rectangle([bx0, by0, bx1, by1], outline=DIMC, width=max(1, int(sc)))
            fw = (bx1 - bx0) * max(0.0, min(1.0, pct / 100.0))
            if fw > 2:
                d.rectangle([bx0 + 2 * sc, by0 + 2 * sc,
                             bx0 + fw, by1 - 2 * sc], fill=ON)
            put(0, 55, "S:%s  %d%%" % (status, pct), 1, ON)
        else:
            put(0, 0, "Calibrating gyro...", 1, ON)
            put(0, 12, "Keep device still", 1, DIMC)
            put(22, 26, str(int(calib_pct)), 3, ON)
            put(96, 34, "%", 1, ON)
            bx0, by0 = gx0, gy0 + 52 * sc
            bx1, by1 = gx0 + 128 * sc, gy0 + 60 * sc
            d.rectangle([bx0, by0, bx1, by1], outline=DIMC, width=max(1, int(sc)))
            fw = (bx1 - bx0) * max(0.0, min(1.0, calib_pct / 100.0))
            if fw > 2:
                d.rectangle([bx0 + 2 * sc, by0 + 2 * sc,
                             bx0 + fw, by1 - 2 * sc], fill=ON)

    # 四角定位孔
    for cx, cy in ((L(12), L(12)), (PW - L(12), L(12)),
                   (L(12), PH - L(12)), (PW - L(12), PH - L(12))):
        d.ellipse([x + cx - L(5), y + cy - L(5), x + cx + L(5), y + cy + L(5)],
                  outline=(150, 158, 168, A(alpha)), width=max(1, L(1.4)))

    # 上沿四针 GND VCC SCL SDA
    pin_c = (222, 186, 78, A(alpha))
    for i, lab in enumerate(["GND", "VCC", "SCL", "SDA"]):
        bx = x + L(52) + i * L(34)
        d.rounded_rectangle([bx, y - L(11), bx + L(20), y + L(6)],
                            radius=L(2), fill=pin_c,
                            outline=(150, 126, 52, A(alpha)), width=1)
        d.text((bx + L(10), y - L(17)), lab,
               font=F("consola.ttf", max(9, L(10))),
               fill=(196, 204, 214, A(alpha)), anchor="ms")
    return (PW, PH)


# ================================================================ MPU6050
# 真实比例：GY-521 21x16mm（板子略长，带四孔）
def draw_mpu6050(img, x, y, s=1.0, alpha=1.0, highlight=False):
    """GY-521 / MPU6050 模块 —— 实物是黑色小板，中央小芯片，四角定位孔。"""
    d = ImageDraw.Draw(img, "RGBA")
    A = lambda v: int(255 * v)                      # noqa: E731
    px = lambda v: pxr(v, s)                         # noqa: E731

    W, H = px(300), px(200)
    # 板身：实物偏黑（不是蓝色）
    d.rounded_rectangle([x + px(4), y + px(6), x + W + px(4), y + H + px(6)],
                        radius=px(6), fill=(0, 0, 0, A(alpha * 0.5)))
    d.rounded_rectangle([x, y, x + W, y + H], radius=px(5),
                        fill=(22, 24, 28, A(alpha)),
                        outline=(58, 62, 70, A(alpha)), width=max(1, px(2)))
    # 中央 QFN 芯片
    cx0, cy0 = x + px(104), y + px(58)
    cx1, cy1 = x + px(176), y + px(130)
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=px(3),
                        fill=(40, 42, 48, A(alpha)),
                        outline=(80, 84, 92, A(alpha)), width=max(1, px(2)))
    if s >= 1.0:
        f = F("consola.ttf", max(7, int(px(13))))
        d.text(((cx0 + cx1) / 2, cy0 + px(6)), "MPU", font=f,
               fill=(120, 126, 136, A(alpha)), anchor="ma")
        d.text(((cx0 + cx1) / 2, cy0 + px(24)), "6050", font=f,
               fill=(120, 126, 136, A(alpha)), anchor="ma")
    # 芯片引脚
    for i in range(6):
        lx = cx0 + px(6) + i * px(11)
        d.rectangle([lx, cy0 - px(5), lx + px(6), cy0], fill=(170, 174, 182, A(alpha)))
        d.rectangle([lx, cy1, lx + px(6), cy1 + px(5)], fill=(170, 174, 182, A(alpha)))
    for i in range(5):
        ly = cy0 + px(8) + i * px(11)
        d.rectangle([cx0 - px(5), ly, cx0, ly + px(6)], fill=(170, 174, 182, A(alpha)))
        d.rectangle([cx1, ly, cx1 + px(5), ly + px(6)], fill=(170, 174, 182, A(alpha)))
    # 四个定位孔
    for hx, hy in ((x + px(28), y + px(40)), (x + W - px(28), y + px(40)),
                   (x + px(28), y + H - px(40)), (x + W - px(28), y + H - px(40))):
        d.ellipse([hx - px(11), hy - px(11), hx + px(11), hy + px(11)],
                  outline=(120, 126, 134, A(alpha)), width=max(1, px(2)))
        d.ellipse([hx - px(4), hy - px(4), hx + px(4), hy + px(4)],
                  fill=(10, 11, 13, A(alpha)))
    # 丝印小元件
    for i, (ex, ey) in enumerate([(x + px(56), y + px(40)), (x + px(228), y + px(40)),
                                  (x + px(56), y + px(146)), (x + px(228), y + px(146))]):
        d.rounded_rectangle([ex - px(12), ey - px(8), ex + px(12), ey + px(8)],
                            radius=px(2), fill=(196, 178, 130, A(alpha)))
    # 右侧 J4 排针（实物：SCL/SDA/+/− 四针，线材从右侧引出）
    pin_c = (28, 30, 34, A(alpha))
    for i in range(4):
        py_ = y + px(56) + i * px(26)
        d.rounded_rectangle([x + W - px(6), py_ - px(10), x + W + px(22), py_ + px(10)],
                            radius=px(2), fill=pin_c,
                            outline=(90, 94, 102, A(alpha)), width=1)
    if s >= 1.0:
        f = F("consola.ttf", max(7, int(px(12))))
        for i, lab in enumerate(["-", "+", "SCL", "SDA"]):
            py_ = y + px(56) + i * px(26)
            d.text((x + W + px(30), py_), lab, font=f,
                   fill=(180, 186, 196, A(alpha)), anchor="lm")
    if highlight:
        d.rounded_rectangle([x - px(6), y - px(6), x + W + px(6), y + H + px(6)],
                            radius=px(8), outline=(74, 246, 38, A(alpha)), width=max(1, px(3)))
    return (W, H)


# ================================================================ ESP32
# 真实比例：ESP32-WROOM-32E 开发板 ≈ 54x28mm
def draw_esp32(img, x, y, s=1.0, alpha=1.0):
    """ESP32-WROOM-32E 开发板 —— 实物为黑色 PCB，银色 USB，金属屏蔽罩，
    两侧两排排针，板载红色电源 LED。"""
    d = ImageDraw.Draw(img, "RGBA")
    A = lambda v: int(255 * v)                      # noqa: E731
    px = lambda v: pxr(v, s)                         # noqa: E731

    W, H = px(560), px(290)
    d.rounded_rectangle([x + px(6), y + px(10), x + W + px(6), y + H + px(10)],
                        radius=px(8), fill=(0, 0, 0, A(alpha * 0.45)))
    # 板身：实物黑板
    d.rounded_rectangle([x, y, x + W, y + H], radius=px(7),
                        fill=(20, 22, 26, A(alpha)),
                        outline=(52, 56, 64, A(alpha)), width=max(1, px(2)))
    # 左侧 micro-USB（实物在短边）
    d.rounded_rectangle([x - px(34), y + px(96), x + px(10), y + px(194)],
                        radius=px(4), fill=(178, 184, 192, A(alpha)),
                        outline=(120, 126, 134, A(alpha)), width=max(1, px(2)))
    d.rectangle([x - px(26), y + px(108), x + px(4), y + px(182)],
                fill=(122, 128, 136, A(alpha)))
    # 金属屏蔽罩
    mx0, my0 = x + px(190), y + px(46)
    mx1, my1 = x + px(430), y + px(238)
    d.rounded_rectangle([mx0, my0, mx1, my1], radius=px(5),
                        fill=(150, 156, 164, A(alpha)),
                        outline=(102, 108, 116, A(alpha)), width=max(1, px(2)))
    if s >= 1.0:
        f = F("consola.ttf", max(8, int(px(17))))
        d.text(((mx0 + mx1) / 2, my0 + px(18)), "ESP32-WROOM-32E", font=f,
               fill=(96, 102, 110, A(alpha)), anchor="ma")
        d.text(((mx0 + mx1) / 2, my0 + px(42)), "8MB Flash", font=F("consola.ttf", max(7, int(px(13)))),
               fill=(112, 118, 126, A(alpha)), anchor="ma")
    # 屏蔽罩四角焊盘
    for sx, sy in ((mx0 + px(10), my0 + px(10)), (mx1 - px(10), my0 + px(10)),
                   (mx0 + px(10), my1 - px(10)), (mx1 - px(10), my1 - px(10))):
        d.ellipse([sx - px(6), sy - px(6), sx + px(6), sy + px(6)],
                  fill=(126, 132, 140, A(alpha)))
    # 右侧 CP2102 芯片
    d.rounded_rectangle([x + px(452), y + px(56), x + px(516), y + px(120)],
                        radius=px(3), fill=(34, 36, 42, A(alpha)),
                        outline=(74, 78, 86, A(alpha)), width=1)
    # 两侧排针（实物各 15 针）
    pin_c = (222, 186, 78, A(alpha))
    for i in range(15):
        py_ = y + px(26) + i * px(16)
        d.rounded_rectangle([x - px(8), py_, x + px(8), py_ + px(10)],
                            radius=px(2), fill=pin_c)
        d.rounded_rectangle([x + W - px(8), py_, x + W + px(8), py_ + px(10)],
                            radius=px(2), fill=pin_c)
    # 板载 LED（实物红色电源灯）
    d.ellipse([x + px(60), y + px(44), x + px(84), y + px(68)],
              fill=(196, 48, 48, A(alpha)))
    d.ellipse([x + px(66), y + px(50), x + px(76), y + px(60)],
              fill=(255, 150, 140, int(A(alpha) * 0.8)))
    # 两颗电容（银色圆柱）
    for i, cxp in enumerate((x + px(96), x + px(146))):
        cy0 = y + px(60) + i * px(4)
        d.rounded_rectangle([cxp, cy0, cxp + px(34), cy0 + px(52)], radius=px(3),
                            fill=(172, 178, 186, A(alpha)),
                            outline=(118, 124, 132, A(alpha)), width=1)
    # 复位键
    d.rounded_rectangle([x + px(490), y + px(150), x + px(534), y + px(194)],
                        radius=px(4), fill=(196, 200, 206, A(alpha)),
                        outline=(140, 144, 152, A(alpha)), width=1)
    return (W, H)


# ================================================================ 按键
def draw_button(img, x, y, s=1.0, alpha=1.0, pressed=False):
    """实物：红色大圆帽按键（装在独立小板上）。"""
    d = ImageDraw.Draw(img, "RGBA")
    A = lambda v: int(255 * v)                      # noqa: E731
    px = lambda v: pxr(v, s)                         # noqa: E731
    W = H = px(200)
    d.rounded_rectangle([x, y, x + W, y + H], radius=px(6),
                        fill=(22, 24, 28, A(alpha)),
                        outline=(64, 68, 76, A(alpha)), width=max(1, px(2)))
    r = px(74) if not pressed else px(70)
    cx, cy = x + W / 2, y + H / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(178, 38, 38, A(alpha)))
    d.ellipse([cx - r, cy - r, cx + r * 0.2, cy + r * 0.2],
              fill=(224, 72, 72, A(alpha)))
    d.ellipse([cx - r * 0.7, cy - r * 0.8, cx + r * 0.1, cy - r * 0.2],
              fill=(255, 150, 150, int(A(alpha) * 0.55)))
    if s >= 0.8:
        f = F("msyh.ttc", max(8, int(px(20))))
        d.text((cx, y + H - px(18)), "按键模块", font=f,
               fill=(150, 156, 166, A(alpha)), anchor="mm")
    return (W, H)


# ================================================================ 导出图层
def export_layers(outdir):
    os.makedirs(outdir, exist_ok=True)
    # 每个模块单独一张，边距留足（引脚标注/排针需要空间）。
    # OLED 特意放大到 520x560：固件里字号 1 只有 6x8 像素，图层太小的话
    # 屏幕上 "WinDuo STD" 那行会糊成一团，观众根本认不出显示内容。
    jobs = [
        # (名称, 画布宽, 画布高, 绘制函数)
        # 画布尺寸按模块实际占用 + 边距给足：早期版本给小了，
        # OLED 屏幕和引脚标签被裁掉一半（用户看到的错位/不全就有这一份）。
        ("esp32", 780, 440, lambda im: draw_esp32(im, 90, 60, 1.0)),
        ("mpu6050", 470, 340, lambda im: draw_mpu6050(im, 50, 50, 1.0)),
        ("ssd1306_main", 620, 640,
         lambda im: draw_ssd1306(im, 40, 60, 2.0, screen="main", angle=42, pct=46)),
        ("ssd1306_calib", 620, 640,
         lambda im: draw_ssd1306(im, 40, 60, 2.0, screen="calib", calib_pct=63)),
        ("button", 280, 280, lambda im: draw_button(im, 35, 35, 1.0)),
    ]
    for name, w, h, fn in jobs:
        # 两遍绘制：先在足够大的过渡画布上画一次，量出真实内容边界；
        # 再按边界 + 边距开最终画布重画一次。
        # 这样无论模块尺寸/字号怎么改，都不会出现"内容被裁掉一半"
        # （用户反馈的"错位/不全"里就包含这个问题）。
        probe = Image.new("RGBA", (w + 700, h + 700), (0, 0, 0, 0))
        fn(probe)
        bb = probe.split()[3].getbbox() or (0, 0, 1, 1)
        pad = 26
        cw = bb[2] - bb[0] + pad * 2
        ch = bb[3] - bb[1] + pad * 2
        layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        layer.alpha_composite(
            probe.crop((bb[0] - pad, bb[1] - pad, bb[0] - pad + cw, bb[1] - pad + ch)))
        a = layer.split()[3].filter(ImageFilter.GaussianBlur(10))
        shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        shadow.putalpha(a.point(lambda v: int(v * 0.5)))
        out = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        out.alpha_composite(shadow, (0, 8))
        out.alpha_composite(layer, (0, 0))
        out.save(os.path.join(outdir, "hw_%s.png" % name))
        print("  %-16s -> hw_%s.png  %s" % (name, name, out.size))


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/glass_seq"
    export_layers(out)
    # 预览联系表，方便肉眼核对比例与屏幕内容
    strip = Image.new("RGB", (1600, 700), (14, 16, 20))
    e = Image.open(os.path.join(out, "hw_esp32.png")).convert("RGBA")
    m = Image.open(os.path.join(out, "hw_mpu6050.png")).convert("RGBA")
    o = Image.open(os.path.join(out, "hw_ssd1306_main.png")).convert("RGBA")
    oc = Image.open(os.path.join(out, "hw_ssd1306_calib.png")).convert("RGBA")
    strip.paste(e, (10, 200), e)
    strip.paste(m, (780, 260), m)
    strip.paste(o, (1200, 70), o)
    dd = ImageDraw.Draw(strip)
    dd.text((20, 20), "hardware layers v2  (proportions follow the real modules)",
            font=F("consola.ttf", 20), fill=(190, 198, 208))
    strip.save(os.path.join(out, "_hw_preview.png"))
    # OLED 特写：检查屏幕内容是否可读，以及两种界面
    big = Image.new("RGB", (o.size[0] * 2 + 60, o.size[1] + 70), (14, 16, 20))
    # OLED 特写：两种界面并排，确认屏幕文字清晰可读
    ow, oh = o.size
    big = Image.new("RGB", (ow * 2 + 60, oh + 70), (14, 16, 20))
    big.paste(o, (10, 10), o)
    big.paste(oc, (ow + 40, 10), oc)
    d2 = ImageDraw.Draw(big)
    d2.text((20, oh + 30), "主界面 main", font=F("msyh.ttc", 22), fill=(150, 158, 168))
    d2.text((ow + 50, oh + 30), "校准界面 calib", font=F("msyh.ttc", 22), fill=(150, 158, 168))
    big.save(os.path.join(out, "_oled_zoom.png"))
    print("  预览 -> %s/_hw_preview.png  %s/_oled_zoom.png" % (out, out))
