"""
用项目真实的 FS_DUO 着色器离屏渲染玻璃效果序列
=================================================
之前视频里的"启用玻璃效果"是用 PIL 叠模糊**仿造**的，一看就假。
这里改成真正调用 `pc/duo_glass.py` 里的着色器（逆投影 + Vogel 盘 + mip LOD
+ 边缘覆盖率），通过 QOffscreenSurface + FBO 离屏渲染，产出不同倾角的效果图。

用法：
  .venv\\Scripts\\python.exe tools/render_glass_stills.py
  .venv\\Scripts\\python.exe tools/render_glass_stills.py --angles 0 20 40 60 80
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from OpenGL import GL
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtGui import QImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from duo_glass import FS_DUO, VS  # noqa: E402  复用被测的同一份着色器

FONT_DIR = Path(r"C:\Windows\Fonts")


# ---------------------------------------------------------------- 合成桌面
def make_desktop(w=1600, h=1000):
    """程序化合成一张"像样的桌面"：渐变壁纸 + 图标 + 窗口。

    为什么要自己合成：玻璃效果需要一张明亮、对比度足的底图才看得出变化。
    之前用深色聊天窗口截图，模糊后几乎一片黑，观众根本看不出效果。
    """
    # 壁纸：左上到右下的冷色渐变 + 光晕
    yy, xx = np.mgrid[0:h, 0:w]
    t = (xx / w * 0.6 + yy / h * 0.4)
    base = np.zeros((h, w, 3), np.float32)
    base[..., 0] = 24 + 60 * t
    base[..., 1] = 32 + 96 * t
    base[..., 2] = 52 + 140 * t
    # 右上光晕
    d = np.sqrt((xx - w * 0.78) ** 2 + (yy - h * 0.22) ** 2) / (w * 0.55)
    glow = np.clip(1.0 - d, 0, 1) ** 2
    base += glow[..., None] * np.array([120, 150, 190], np.float32)
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img, "RGBA")

    # 桌面图标网格
    def ico(size):
        return ImageFont.truetype(str(FONT_DIR / "segoeui.ttf"), size)

    names = ["此电脑", "回收站", "项目", "文档", "图片", "下载"]
    for i, nm in enumerate(names):
        cx = 70 + (i % 1) * 0
        cy = 90 + i * 118
        d.rounded_rectangle([cx - 30, cy - 30, cx + 30, cy + 30], radius=10,
                            fill=(255, 255, 255, 46), outline=(255, 255, 255, 70), width=2)
        d.rectangle([cx - 14, cy - 10, cx + 14, cy + 12], fill=(255, 255, 255, 150))
        d.rectangle([cx - 10, cy - 6, cx + 10, cy - 2], fill=(70, 130, 200, 230))
        d.text((cx, cy + 44), nm, font=ico(17), fill=(255, 255, 255, 235), anchor="mm")

    # 一个终端窗口
    wx, wy, ww, wh = 300, 150, 660, 420
    d.rounded_rectangle([wx + 6, wy + 10, wx + ww + 6, wy + wh + 10], radius=12,
                        fill=(0, 0, 0, 90))
    d.rounded_rectangle([wx, wy, wx + ww, wy + wh], radius=12, fill=(18, 20, 24, 246),
                        outline=(255, 255, 255, 40), width=2)
    d.rounded_rectangle([wx, wy, wx + ww, wy + 40], radius=12, fill=(34, 38, 44, 250))
    d.rectangle([wx, wy + 28, wx + ww, wy + 40], fill=(34, 38, 44, 250))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([wx + 18 + i * 26, wy + 14, wx + 32 + i * 26, wy + 28], fill=c)
    d.text((wx + ww / 2, wy + 20), "sui-winduo — zsh", font=ico(17),
           fill=(200, 205, 212, 235), anchor="mm")

    mono = ImageFont.truetype(str(FONT_DIR / "consola.ttf"), 19)
    lines = [
        ("$ python pc/duo_glass.py --port COM3", (200, 206, 214, 240)),
        ("[串口] 已连接  angle=+42.60  浓度=46.5%", (74, 246, 38, 245)),
        ("[GL] context = True 4 6", (120, 190, 255, 240)),
        ("[玻璃] 覆盖层已创建 accent=acrylic(4)", (120, 190, 255, 240)),
        ("", None),
        ("$ git log --oneline -3", (200, 206, 214, 240)),
        ("ce93873 docs: add how-it-works animation", (255, 189, 46, 240)),
        ("84f9b2d feat(gui): terminal console + acrylic", (255, 189, 46, 240)),
        ("1b0d273 chore: drop large temp screenshots", (255, 189, 46, 240)),
        ("", None),
        ("$ _", (150, 255, 150, 250)),
    ]
    for i, (s, col) in enumerate(lines):
        if col:
            d.text((wx + 26, wy + 70 + i * 30), s, font=mono, fill=col)

    # 一个图片预览窗口
    px, py, pw, ph = 1040, 430, 460, 330
    d.rounded_rectangle([px + 6, py + 10, px + pw + 6, py + ph + 10], radius=12,
                        fill=(0, 0, 0, 90))
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=12, fill=(240, 238, 232, 246),
                        outline=(255, 255, 255, 60), width=2)
    for i in range(5):
        d.rectangle([px + 26, py + 30 + i * 40, px + pw - 26 - (i * 34) % 140,
                     py + 54 + i * 40], fill=(120 + i * 18, 130 + i * 12, 150 + i * 10, 220))
    d.text((px + 20, py + ph - 34), "preview.png — 460x330", font=ico(16),
           fill=(70, 74, 82, 240))

    # 任务栏
    tb = 62
    d.rectangle([0, h - tb, w, h], fill=(20, 22, 26, 235))
    for i in range(7):
        bx = w / 2 - 210 + i * 60
        d.rounded_rectangle([bx, h - tb + 12, bx + 40, h - 12], radius=8,
                            fill=(255, 255, 255, 30 + i * 6),
                            outline=(255, 255, 255, 50), width=1)
    d.text((w - 130, h - tb / 2), "10:24  2026/10/2", font=ico(17),
           fill=(230, 235, 240, 240), anchor="mm")
    return img


def fit_cover(img, w, h):
    """等比缩放到"盖满"目标尺寸再居中裁剪 —— 绝不拉伸变形。

    直接用 resize 会改变纵横比，画面会显得被拉扁/拉长（这个错误在
    视频第一版里出现过：1600x1000 的序列被塞进 1360x850 的框）。
    """
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


# ---------------------------------------------------------------- 离屏渲染
class GlassRenderer:
    """用 FS_DUO 在 FBO 上渲染任意倾角的效果。"""

    def __init__(self, src_image, width, height):
        self.w, self.h = width, height
        # 等比裁切适配，不用 resize（避免纵横比失真）
        self.src = fit_cover(src_image.convert("RGB"), width, height)
        self._setup_qt()
        self._setup_gl()

    def _setup_qt(self):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(fmt)
        if not self.ctx.create():
            raise RuntimeError("无法创建 OpenGL 上下文")
        self.surface = QOffscreenSurface()
        self.surface.setFormat(fmt)
        self.surface.create()
        if not self.ctx.makeCurrent(self.surface):
            raise RuntimeError("无法 makeCurrent")

    def _setup_gl(self):
        from OpenGL import GL as gl
        self.fbo = gl.glGenFramebuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        self.tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, self.w, self.h, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, self.tex, 0)
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("FBO 不完整")

        # 源纹理（带 mipmap，着色器依赖 textureLod）
        self.src_tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.src_tex)
        raw = self.src.tobytes("raw", "RGB")
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB8, self.w, self.h, 0,
                        gl.GL_RGB, gl.GL_UNSIGNED_BYTE, raw)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                           gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)

        self.prog = QOpenGLShaderProgram()
        okv = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VS)
        okf = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FS_DUO)
        if not (okv and okf and self.prog.link()):
            raise RuntimeError("着色器编译失败:\n" + self.prog.log())
        self.prog.bind()
        self.u = {n: self.prog.uniformLocation(n) for n in
                  ("uTex", "uRes", "uTilt", "uEyeZ", "uSpread", "uDark", "uMaxTaps")}

    def render(self, tilt_deg, eye_h=2.0, spread=0.42, dark=0.001, taps=32):
        gl = GL
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, self.w, self.h)
        self.prog.bind()
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.src_tex)
        gl.glUniform1i(self.u["uTex"], 0)
        gl.glUniform2f(self.u["uRes"], float(self.w), float(self.h))
        gl.glUniform1f(self.u["uTilt"], np.radians(tilt_deg))
        gl.glUniform1f(self.u["uEyeZ"], eye_h * self.h)
        gl.glUniform1f(self.u["uSpread"], spread)
        gl.glUniform1f(self.u["uDark"], dark)
        gl.glUniform1i(self.u["uMaxTaps"], taps)
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0.0, 0.0); gl.glVertex2f(-1.0, -1.0)
        gl.glTexCoord2f(1.0, 0.0); gl.glVertex2f(1.0, -1.0)
        gl.glTexCoord2f(1.0, 1.0); gl.glVertex2f(1.0, 1.0)
        gl.glTexCoord2f(0.0, 1.0); gl.glVertex2f(-1.0, 1.0)
        gl.glEnd()
        gl.glFinish()

        buf = gl.glReadPixels(0, 0, self.w, self.h, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
        arr = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)[::-1]  # 翻转行序
        return Image.fromarray(arr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--angles", type=float, nargs="+",
                    default=[0, 10, 20, 30, 40, 50, 60, 70, 80, 88])
    ap.add_argument("--outdir", default=str(ROOT / "docs" / "glass_seq"))
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--desktop", default="", help="用真实截图替代程序化桌面")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.desktop and Path(args.desktop).exists():
        desk = Image.open(args.desktop).convert("RGB")
        print("桌面素材: %s %s" % (args.desktop, desk.size))
    else:
        desk = make_desktop(1600, 1000)
        print("桌面素材: 程序化合成 %s" % (desk.size,))
    desk.save(outdir / "desktop.png")

    print("初始化离屏渲染 %dx%d ..." % (args.width, args.height))
    r = GlassRenderer(desk, args.width, args.height)

    print("%-8s %s" % ("倾角", "输出"))
    for a in args.angles:
        img = r.render(a)
        p = outdir / ("tilt_%05.1f.png" % a)
        img.save(p)
        # 简单统计，便于判断效果强度是否单调递增
        arr = np.asarray(img.convert("L"), np.float32)
        print("%-8.1f %s  亮度均值=%.1f" % (a, p.name, arr.mean()))

    # 拼接一张对比条，方便一眼确认
    tiles = []
    for a in args.angles:
        im = Image.open(outdir / ("tilt_%05.1f.png" % a)).resize((410, 256), Image.LANCZOS)
        tiles.append((a, im))
    cols = 5
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 410, rows * 256), (0, 0, 0))
    for i, (a, im) in enumerate(tiles):
        sheet.paste(im, ((i % cols) * 410, (i // cols) * 256))
    sp = outdir / "sequence_sheet.png"
    sheet.save(sp)
    print("\n序列对比图: %s" % sp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
