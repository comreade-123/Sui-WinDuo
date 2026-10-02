"""Measure the real per-frame GPU cost of the glass shader.

The overlay uses asynchronous GL calls, so timing paintGL() only measures
the CPU-side submission. This benchmark renders the shader offscreen at
the real screen resolution and forces a glFinish() to get true GPU time
for a range of sample counts (taps).

Usage:
  .venv\\Scripts\\python.exe tools/bench_glass_gpu.py
"""
import sys
import time

import mss
from OpenGL import GL
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, "pc")
from duo_glass import FS_DUO, VS  # reuse the exact shaders under test


class Bench(QOpenGLWidget):
    def __init__(self, tap_list):
        super().__init__()
        self.tap_list = tap_list
        self.results = []

    def initializeGL(self):
        self.prog = QOpenGLShaderProgram(self)
        okv = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VS)
        okf = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FS_DUO)
        assert okv and okf and self.prog.link(), self.prog.log()
        self.prog.bind()

        self.cap_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)

        with mss.MSS() as sct:
            mon = sct.monitors[1]
            t0 = time.perf_counter()
            shot = sct.grab(mon)
            self.cap_ms = (time.perf_counter() - t0) * 1000.0
            self.fw, self.fh = shot.width, shot.height
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, self.fw, self.fh, 0,
                            GL.GL_BGRA, GL.GL_UNSIGNED_BYTE, shot.raw)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self.u = {n: self.prog.uniformLocation(n) for n in
                  ("uTex", "uRes", "uTilt", "uEyeZ", "uSpread", "uDark", "uMaxTaps")}
        self._ready = True

    def run_bench(self):
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        vw, vh = int(w * dpr), int(h * dpr)
        for taps in self.tap_list:
            # warm up
            self._render(vw, vh, taps)
            GL.glFinish()
            n = 12
            t0 = time.perf_counter()
            for _ in range(n):
                self._render(vw, vh, taps)
                GL.glFinish()
            self.results.append((taps, (time.perf_counter() - t0) / n * 1000.0))

    def _render(self, w, h, taps):
        self.prog.bind()
        GL.glViewport(0, 0, w, h)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
        GL.glUniform1i(self.u["uTex"], 0)
        GL.glUniform2f(self.u["uRes"], float(self.fw), float(self.fh))
        GL.glUniform1f(self.u["uTilt"], 0.6)
        GL.glUniform1f(self.u["uEyeZ"], 2.0 * self.fh)
        GL.glUniform1f(self.u["uSpread"], 0.42)
        GL.glUniform1f(self.u["uDark"], 0.001)
        GL.glUniform1i(self.u["uMaxTaps"], taps)
        GL.glBegin(GL.GL_QUADS)
        GL.glTexCoord2f(0.0, 0.0); GL.glVertex2f(-1.0, -1.0)
        GL.glTexCoord2f(1.0, 0.0); GL.glVertex2f(1.0, -1.0)
        GL.glTexCoord2f(1.0, 1.0); GL.glVertex2f(1.0, 1.0)
        GL.glTexCoord2f(0.0, 1.0); GL.glVertex2f(-1.0, 1.0)
        GL.glEnd()

    def paintGL(self):
        pass


def main():
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    w = Bench([0, 6, 12, 16, 24, 32])
    w.resize(1280, 720)
    w.show()
    app.processEvents()
    w.initializeGL()
    w.run_bench()

    print("=" * 62)
    print("GPU cost of the glass shader")
    print("=" * 62)
    print("capture (mss grab of the desktop): %.1f ms" % w.cap_ms)
    print("viewport: %d x %d" % (int(w.width() * w.devicePixelRatioF()),
                                 int(w.height() * w.devicePixelRatioF())))
    print()
    print("%6s | %12s | %s" % ("taps", "ms/frame", "max fps"))
    print("-" * 62)
    for taps, ms in w.results:
        print("%6d | %12.2f | %6.0f" % (taps, ms, 1000.0 / ms if ms > 0 else 0))
    print()
    base = dict(w.results).get(32, 0.0)
    for taps, ms in w.results:
        if taps == 0:
            continue
        print("taps=%2d 是 taps=32 的 %.0f%% 成本" % (taps, ms / base * 100 if base else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
