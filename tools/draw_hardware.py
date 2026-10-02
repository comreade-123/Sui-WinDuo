"""
硬件模块矢量绘制（用于原理动画）
==================================
为什么自己画而不是用网上的照片：
  1. 网上能找到的 ESP32/MPU6050 图片基本都带水印或授权不明，不能进开源仓库；
  2. 静态照片在动画里"死"，无法做元件飞入、数据流动、引脚高亮这类动效；
  3. 矢量绘制可以统一美术风格，和视频的终端/科技配色保持一致。

风格：伪 3D（正面 + 顶面 + 阴影 + 高光），模块可以单独动。
坐标都是相对模块左上角，方便整体位移与缩放。
"""

import math
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = r"C:\Windows\Fonts"
_fonts = {}


def F(name, size):
    key = (name, size)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(FONT_DIR + "\\" + name, size)
        except Exception:
            _fonts[key] = ImageFont.load_default()
    return _fonts[key]


def _shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


# ---------------------------------------------------------------- 通用底座
def chip_body(d, box, top_color, side_color, radius=8, depth=10):
    """画一个带厚度的"芯片/模块"底座：顶面斜切 + 正面 + 侧面厚度。"""
    x0, y0, x1, y1 = box
    # 底部阴影
    d.rounded_rectangle([x0 + 6, y1 - depth + 8, x1 + 10, y1 + 14],
                        radius=radius, fill=(0, 0, 0, 90))
    # 侧面厚度
    d.rounded_rectangle([x0 + 4, y0 + depth, x1 + 4, y1],
                        radius=radius, fill=side_color)
    # 正面
    d.rounded_rectangle(box, radius=radius, fill=top_color,
                        outline=_shade(top_color, 0.7), width=2)
    return box


# ---------------------------------------------------------------- ESP32 开发板
def draw_esp32(img, x, y, scale=1.0, alpha=1.0, label=True):
    """ESP32-WROOM-32E 开发板：深蓝 PCB + 银色 USB + 屏蔽罩 + 排针。"""
    d = ImageDraw.Draw(img, "RGBA")
    w, h = int(360 * scale), int(230 * scale)
    def S(v):
        return int(v * scale)

    # 阴影
    d.rounded_rectangle([x + S(8), y + S(14), x + w + S(14), y + h + S(18)],
                        radius=S(12), fill=(0, 0, 0, int(110 * alpha)))
    # 板身（深蓝 PCB）
    d.rounded_rectangle([x, y, x + w, y + h], radius=S(10),
                        fill=(22, 46, 92, int(255 * alpha)),
                        outline=(60, 110, 180, int(255 * alpha)), width=S(2))
    # 左侧 USB 接口
    d.rounded_rectangle([x - S(30), y + S(78), x + S(16), y + S(148)], radius=S(5),
                        fill=(186, 192, 200, int(255 * alpha)),
                        outline=(120, 126, 134, int(255 * alpha)), width=S(2))
    d.rectangle([x - S(22), y + S(88), x + S(10), y + S(138)],
                fill=(120, 126, 134, int(255 * alpha)))
    # 右侧 micro-USB
    d.rounded_rectangle([x + w - S(16), y + S(84), x + w + S(24), y + S(146)], radius=S(4),
                        fill=(168, 174, 182, int(255 * alpha)))
    # 中央屏蔽罩（金属）
    mx0, my0 = x + S(96), y + S(38)
    mx1, my1 = x + S(266), y + S(176)
    d.rounded_rectangle([mx0, my0, mx1, my1], radius=S(6),
                        fill=(158, 164, 172, int(255 * alpha)),
                        outline=(104, 110, 118, int(255 * alpha)), width=S(2))
    # 屏蔽罩上的散热纹
    for i in range(5):
        yy = my0 + S(20) + i * S(26)
        d.line([(mx0 + S(12), yy), (mx1 - S(12), yy)],
               fill=(138, 144, 152, int(255 * alpha)), width=S(2))
    # 模块上的丝印
    if label:
        f = F("consola.ttf", max(9, S(15)))
        d.text((x + S(181), my1 + S(10)), "ESP32-WROOM-32E", font=f,
               fill=(170, 200, 240, int(240 * alpha)), anchor="ma")
    # 两侧排针
    pin = (222, 186, 78, int(255 * alpha))
    for i in range(9):
        py = y + S(30) + i * S(20)
        d.rectangle([x - S(4), py, x + S(8), py + S(10)], fill=pin)
        d.rectangle([x + w - S(8), py, x + w + S(4), py + S(10)], fill=pin)
    # 板载 LED
    d.ellipse([x + S(300), y + S(30), x + S(316), y + S(46)],
              fill=(60, 220, 90, int(255 * alpha)))
    d.ellipse([x + S(302), y + S(32), x + S(310), y + S(40)],
              fill=(180, 255, 190, int(200 * alpha)))
    return (x, y, w, h)


# ---------------------------------------------------------------- MPU6050
def draw_mpu6050(img, x, y, scale=1.0, alpha=1.0, label=True):
    """GY-521 / MPU6050 模块：蓝色小板 + 中央芯片 + 排针。"""
    d = ImageDraw.Draw(img, "RGBA")
    w, h = int(220 * scale), int(160 * scale)
    def S(v):
        return int(v * scale)

    d.rounded_rectangle([x + S(6), y + S(10), x + w + S(10), y + h + S(12)],
                        radius=S(8), fill=(0, 0, 0, int(100 * alpha)))
    d.rounded_rectangle([x, y, x + w, y + h], radius=S(6),
                        fill=(20, 52, 104, int(255 * alpha)),
                        outline=(64, 116, 186, int(255 * alpha)), width=S(2))
    # 中央芯片
    cx0, cy0 = x + S(74), y + S(46)
    cx1, cy1 = x + S(146), y + S(114)
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=S(3),
                        fill=(30, 32, 36, int(255 * alpha)),
                        outline=(70, 74, 80, int(255 * alpha)), width=S(2))
    # 芯片引脚
    for i in range(7):
        px = cx0 + S(6) + i * S(9)
        d.rectangle([px, cy0 - S(4), px + S(4), cy0], fill=(190, 194, 200, int(255 * alpha)))
        d.rectangle([px, cy1, px + S(4), cy1 + S(4)], fill=(190, 194, 200, int(255 * alpha)))
    # 丝印
    if label:
        f = F("consola.ttf", max(9, S(15)))
        d.text((x + w / 2, cy0 - S(20)), "MPU6050", font=f,
               fill=(180, 210, 250, int(240 * alpha)), anchor="ma")
        d.text((x + w / 2, cy0 - S(4)), "0x68", font=f,
               fill=(255, 200, 90, int(240 * alpha)), anchor="ma")
    # 排针（下沿）
    pin = (222, 186, 78, int(255 * alpha))
    for i in range(8):
        px = x + S(20) + i * S(24)
        d.rectangle([px, y + h - S(14), px + S(10), y + h - S(2)], fill=pin)
    return (x, y, w, h)


# ---------------------------------------------------------------- SSD1306
def draw_ssd1306(img, x, y, scale=1.0, alpha=1.0, label=True, screen_text=None,
                 glow=0.0):
    """SSD1306 OLED：黑色玻璃屏 + 内嵌显示内容 + 排针。

    glow: 0~1，屏幕内容的亮度（用于做"上电点亮"动效）。
    """
    d = ImageDraw.Draw(img, "RGBA")
    w, h = int(240 * scale), int(150 * scale)
    def S(v):
        return int(v * scale)

    d.rounded_rectangle([x + S(6), y + S(10), x + w + S(10), y + h + S(12)],
                        radius=S(8), fill=(0, 0, 0, int(100 * alpha)))
    # 模块底
    d.rounded_rectangle([x, y, x + w, y + h], radius=S(6),
                        fill=(16, 34, 66, int(255 * alpha)),
                        outline=(58, 108, 176, int(255 * alpha)), width=S(2))
    # 屏幕玻璃（深色）
    sx0, sy0 = x + S(30), y + S(20)
    sx1, sy1 = x + w - S(30), y + S(104)
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=S(4),
                        fill=(8, 10, 14, int(255 * alpha)),
                        outline=(40, 44, 52, int(255 * alpha)), width=S(2))
    # 屏幕内容（自发光）
    if glow > 0.01:
        g = int(255 * min(1.0, glow))
        f = F("consola.ttf", max(9, S(20)))
        fs = F("consola.ttf", max(8, S(13)))
        if screen_text is None:
            screen_text = "WinDuo"
        d.text((sx0 + S(12), sy0 + S(10)), screen_text, font=f,
               fill=(120, 255, 160, g), anchor="la")
        d.text((sx0 + S(12), sy0 + S(40)), "ANGLE +42.6", font=fs,
               fill=(120, 255, 160, int(g * 0.85)), anchor="la")
        # 进度条
        bw = (sx1 - sx0) - S(24)
        d.rectangle([sx0 + S(12), sy0 + S(60), sx0 + S(12) + bw, sy0 + S(70)],
                    outline=(120, 255, 160, int(g * 0.8)), width=S(1))
        d.rectangle([sx0 + S(13), sy0 + S(62), sx0 + S(13) + int(bw * 0.42), sy0 + S(68)],
                    fill=(120, 255, 160, int(g * 0.9)))
    if label:
        f2 = F("consola.ttf", max(8, S(13)))
        d.text((x + w / 2, y + h - S(26)), "SSD1306 128x64  0x3C", font=f2,
               fill=(170, 205, 245, int(240 * alpha)), anchor="ma")
    # 排针（上沿）
    pin = (222, 186, 78, int(255 * alpha))
    for i in range(4):
        px = x + S(60) + i * S(30)
        d.rectangle([px, y + S(2), px + S(12), y + S(12)], fill=pin)
    return (x, y, w, h)


# ---------------------------------------------------------------- 按键模块
def draw_button(img, x, y, scale=1.0, alpha=1.0, pressed=False):
    d = ImageDraw.Draw(img, "RGBA")
    w, h = int(110 * scale), int(110 * scale)
    def S(v):
        return int(v * scale)
    d.rounded_rectangle([x, y, x + w, y + h], radius=S(8),
                        fill=(28, 34, 44, int(255 * alpha)),
                        outline=(90, 100, 116, int(255 * alpha)), width=S(2))
    inset = S(16) if pressed else S(10)
    d.ellipse([x + inset, y + inset, x + w - inset, y + h - inset],
              fill=(200, 60, 60, int(255 * alpha)) if not pressed
              else (150, 40, 40, int(255 * alpha)),
              outline=(240, 120, 120, int(255 * alpha)), width=S(2))
    d.ellipse([x + inset + S(8), y + inset + S(6),
               x + w - inset - S(24), y + h - inset - S(30)],
              fill=(255, 150, 150, int(90 * alpha)))
    return (x, y, w, h)


# ---------------------------------------------------------------- 导线
def draw_wire(d, pts, color, width=4, alpha=255):
    """折线导线。"""
    d.line(pts, fill=tuple(list(color) + [alpha]), width=width, joint="curve")


def draw_solder_joint(d, xy, color=(222, 186, 78), r=5):
    d.ellipse([xy[0] - r, xy[1] - r, xy[0] + r, xy[1] + r], fill=color)


# ---------------------------------------------------------------- 导出 RGBA 图层
def export_layers(outdir):
    """把每个模块渲染到透明画布上，供动画独立缩放/位移。

    投影做法：先在透明底上画模块，用 alpha 通道做高斯模糊得到柔和阴影，
    再把模块本身叠在阴影上 —— 比直接画黑色圆角矩形自然。
    """
    import os
    os.makedirs(outdir, exist_ok=True)
    specs = [
        ("esp32", lambda im: draw_esp32(im, 60, 50, 1.6), 520, 460),
        ("mpu6050", lambda im: draw_mpu6050(im, 40, 40, 1.7), 460, 360),
        ("ssd1306", lambda im: draw_ssd1306(im, 40, 40, 1.7, glow=1.0), 500, 360),
        ("button", lambda im: draw_button(im, 30, 30, 1.6), 240, 240),
    ]
    for name, fn, w, h in specs:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fn(layer)
        # 阴影：取 alpha 做模糊 + 下移
        a = layer.split()[3].filter(ImageFilter.GaussianBlur(12))
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        shadow.putalpha(a.point(lambda v: int(v * 0.55)))
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.alpha_composite(shadow, (0, 10))
        out.alpha_composite(layer, (0, 0))
        out.save(os.path.join(outdir, "hw_%s.png" % name))
        print("  图层 %-9s -> hw_%s.png  %s" % (name, name, out.size))


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--layers":
        export_layers("docs/glass_seq")
    else:
        im = Image.new("RGB", (1400, 700), (14, 16, 20))
        d = ImageDraw.Draw(im)
        d.text((30, 20), "hardware module render test", font=F("consola.ttf", 22),
               fill=(200, 210, 220))
        draw_esp32(im, 120, 90, 1.6)
        draw_mpu6050(im, 700, 110, 1.7)
        draw_ssd1306(im, 700, 360, 1.7, glow=1.0)
        draw_button(im, 1080, 380, 1.6)
        im.save("docs/glass_seq/_hw_test.png")
        print("已生成 docs/glass_seq/_hw_test.png")
        export_layers("docs/glass_seq")
