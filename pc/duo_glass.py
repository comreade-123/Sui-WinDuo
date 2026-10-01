"""
iPhone Duo glass overlay - Windows port for WinDuo_EthanMaven firmware
=====================================================================
This is the Windows-side glass overlay of the open source project WindowsDuo,
ported to this project's hardware:

  - shader math kept intact: inverse projection + Vogel disk blur + mip LOD
    + edge coverage smoothing
  - serial protocol adapted to this firmware's JSON:
      {"angle":45.2,"status":"ok","mode":"default","author":"EthanMaven"}
  - angle mapping follows this firmware: 0 deg = baseline pose, 180 deg = widest

Credits (all MIT / portable):
  - WindowsDuo by KaedeharaKazuha1029 : https://github.com/KaedeharaKazuha1029/WindowsDuo
  - elijah-semyonov/DuoLikeAnimation   : inverse projection + Vogel disk blur
  - chuspeeism/iphone-duo              : mip LOD sampling + edge coverage
  - DhananjayBhosale/MacDuo            : laptop scene (hinge = screen bottom edge)
  - askmaddyy/FrostFold                : frosted glass look

Model: the desktop content stays fixed on a world-space plane; the screen is a
glass pane rotating about the hinge (screen bottom edge) toward the viewer.
For every pixel: eye -> glass pixel -> extended ray hits the interface plane =
sample point. Bigger gap -> bigger blur radius and more darkening; rays leaving
the interface -> pure black. Hence near the hinge everything stays sharp, and
the upper part of the screen blurs and disappears first.

Usage (use the venv python):
  .venv\\Scripts\\python.exe pc\\duo_glass.py                 # follow the sensor
  .venv\\Scripts\\python.exe pc\\duo_glass.py --manual        # keyboard only, no hardware
  .venv\\Scripts\\python.exe pc\\duo_glass.py --selftest      # no-window self test
  .venv\\Scripts\\python.exe pc\\duo_glass.py --smoke         # render, save PNG, quit

Keys (click this console first to give it focus):
  up/down = tune concentration  left = clear  right = full
  r = toggle auto/manual        Esc = quit
"""

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

import mss
from OpenGL import GL
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QApplication

# ------------------------------------------------- shaders (upstream math, unchanged)
VS = """#version 330 compatibility
varying vec2 vUV;
void main() {
    vUV = gl_MultiTexCoord0.xy;
    gl_Position = gl_ModelViewProjectionMatrix * gl_Vertex;
}
"""

FS_DUO = """#version 330 compatibility
uniform sampler2D uTex;
uniform vec2  uRes;      // capture size in px
uniform float uTilt;     // glass tilt (radians), 0 = flush with the interface
uniform float uEyeZ;     // eye distance to interface plane in px
uniform float uSpread;   // gap -> blur radius (tan of scatter half angle)
uniform float uDark;     // light loss per unit blur radius
uniform int   uMaxTaps;
varying vec2 vUV;

const float GOLDEN = 2.39996322972865332;
const float TWO_PI = 6.28318530717958648;

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
    // vUV: (0,0)=bottom-left (GL convention). hinge = screen bottom edge.
    vec2 p = vUV * uRes;                 // y up from the hinge
    float tilt = uTilt;
    vec2 uvFlat = vec2(vUV.x, 1.0 - vUV.y);   // flat sampling (capture rows are top-first)

    if (tilt < 1e-5) {
        gl_FragColor = vec4(texture(uTex, uvFlat).rgb, 1.0);
        return;
    }

    // glass pixel in 3D: rotate about the bottom hinge, upper part lifts toward the viewer
    float d = p.y;                                    // distance from this pixel to the hinge
    vec3 glass = vec3(p.x, d * cos(tilt), d * sin(tilt));
    vec3 eye   = vec3(uRes * 0.5, uEyeZ);             // eye right in front, above center

    // ray eye -> glass pixel, extended to hit the interface plane z=0
    float depth = eye.z - glass.z;
    if (depth <= 1e-3) { gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
    float t   = eye.z / depth;
    vec2 hit  = eye.xy + (glass.xy - eye.xy) * t;     // hit point on interface (px, y up)

    // gap between glass and interface -> blur radius
    float gap    = glass.z;
    float radius = uSpread * gap;

    // whole blur kernel outside the interface -> black
    if (hit.x < -radius || hit.x > uRes.x + radius ||
        hit.y < -radius || hit.y > uRes.y + radius) {
        gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return;
    }

    // frosted glass absorbs light: darken proportionally to scattering
    float att = max(1.0 - uDark * radius, 0.0);

    vec2 uvHit = vec2(hit.x / uRes.x, 1.0 - hit.y / uRes.y);

    if (radius < 0.5) {
        gl_FragColor = vec4(textureLod(uTex, uvHit, 0.0).rgb * att, 1.0);
        return;
    }

    // mip LOD: large radius drops to a lower mip first, then disk sampling
    float lod  = clamp(log2(max(radius, 1.0) / 16.0), 0.0, 6.0);
    float effR = radius / exp2(lod);

    // Vogel disk: sqrt for uniform area density + golden angle + per-pixel rotation
    int taps = int(clamp(effR * 2.0, 6.0, float(uMaxTaps)));
    float rot = hash21(gl_FragCoord.xy) * TWO_PI;

    // edge coverage: fade the part of the kernel outside the screen, no hard edge
    float footX = (radius + 1.0) / uRes.x;
    float footY = (radius + 1.0) / uRes.y;

    vec3 sum = vec3(0.0);
    for (int i = 0; i < taps; ++i) {
        float r = effR * sqrt((float(i) + 0.5) / float(taps));
        float a = float(i) * GOLDEN + rot;
        vec2 off = r * vec2(cos(a), sin(a));          // px, interface plane coords
        vec2 uv  = uvHit + vec2(off.x / uRes.x, -off.y / uRes.y);
        float cx = smoothstep(0.0, footX, uv.x) * (1.0 - smoothstep(1.0 - footX, 1.0, uv.x));
        float cy = smoothstep(0.0, footY, 1.0 - uv.y) * (1.0 - smoothstep(1.0 - footY, 1.0, 1.0 - uv.y));
        sum += textureLod(uTex, uv, lod).rgb * cx * cy;
    }
    vec3 c = sum / float(taps) * att;
    gl_FragColor = vec4(c, 1.0);
}
"""


def angle_to_concentration(angle, angle_open, deadband, neg_scale=0.0):
    """Signed angle -> concentration in [0, 1].

    Design (revised after field testing):
      * |angle| <= deadband -> 0 (hysteresis right at zero, avoids flicker)
      * positive angle      -> ramp from 0 up to 1 at angle_open
      * negative angle      -> same ramp shape, multiplied by neg_scale
        neg_scale = 0.0 keeps the old behaviour: negative means fully clear.
        A small non-zero value (e.g. 0.15) makes the response *continuous*
        through zero, so a hinge spring-back no longer looks like the effect
        snapping in from nowhere.
    """
    if angle is None:
        return 0.0
    mag = abs(angle)
    if mag <= deadband:
        return 0.0
    span = angle_open if angle_open > 0.0 else 90.0
    if span <= deadband:
        return 1.0
    ratio = (mag - deadband) / (span - deadband)
    ratio = max(0.0, min(1.0, ratio))
    if angle < 0.0:
        return ratio * max(0.0, min(1.0, neg_scale))
    return ratio


# ------------------------------------------------- serial reader thread
class AngleReader(threading.Thread):
    """Reads this firmware's JSON lines and extracts angle / status / mode."""

    def __init__(self, port, baud):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.lock = threading.Lock()
        self.angle = None
        self.status = "connecting"
        self.fps = 0.0
        self.mode = ""
        self._stop = False

    def get(self):
        with self.lock:
            return self.angle, self.fps, self.status, self.mode

    def run(self):
        import serial
        pat = re.compile(r"\{[^}]*\}")
        n, t0 = 0, time.time()
        while not self._stop:
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    self._set("connected")
                    buf = b""
                    while not self._stop:
                        buf += ser.readline()
                        if b"}" not in buf:
                            buf = buf[-128:] if len(buf) > 512 else buf
                            continue
                        line, buf = buf.rsplit(b"}", 1)
                        line = (line + b"}").decode("utf-8", "ignore")
                        m = pat.search(line)
                        if not m:
                            continue
                        try:
                            d = json.loads(m.group(0))
                        except ValueError:
                            continue
                        if "angle" not in d:
                            continue
                        with self.lock:
                            self.angle = float(d["angle"])
                            self.status = str(d.get("status", "ok"))
                            self.mode = str(d.get("mode", ""))
                        n += 1
                        now = time.time()
                        if now - t0 >= 1:
                            with self.lock:
                                self.fps = n / (now - t0)
                            n, t0 = 0, now
            except Exception:
                self._set("waiting " + self.port)
                time.sleep(2)

    def _set(self, s):
        with self.lock:
            self.status = s


# ------------------------------------------------- keyboard control
class ManualControl(threading.Thread):
    """Arrow keys tune concentration; r toggles auto; Esc quits."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.target = 0.0
        self.last_key = ""
        self.quit_flag = False
        self.auto = True          # True = follow the sensor angle

    def get(self):
        with self.lock:
            return self.target, self.last_key, self.auto

    def run(self):
        import msvcrt
        while not self.quit_flag:
            try:
                ch = msvcrt.getwch()
            except Exception:
                time.sleep(0.2)
                continue
            if ch in ("\xe0", "\x00"):
                k = msvcrt.getwch()
                mapping = {"H": 0.03, "P": -0.03, "M": 1.0, "K": 0.0}
                delta = mapping.get(k)
                key = {"H": "up", "P": "down", "M": "right", "K": "left"}.get(k, k)
            else:
                if ch == "\x1b":
                    self.quit_flag = True
                    break
                if ch.lower() == "r":
                    with self.lock:
                        self.auto = not self.auto
                        self.last_key = "auto" if self.auto else "manual"
                    continue
                mapping = {"w": 0.03, "s": -0.03, "d": 1.0, "a": 0.0}
                delta = mapping.get(ch.lower())
                key = ch
            if delta is not None:
                with self.lock:
                    self.auto = False
                    self.target = (max(0.0, min(1.0, self.target + delta))
                                   if abs(delta) < 0.5 else delta)
                    self.last_key = key


# ------------------------------------------------- capture thread
class CaptureWorker(threading.Thread):
    def __init__(self, region):
        super().__init__(daemon=True)
        self.region = region
        self.request = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.frame = None          # (bytes, w, h, seq)
        self.busy = False

    def latest(self):
        with self.lock:
            return self.frame

    def kick(self):
        if not self.busy:
            self.request.set()

    def run(self):
        seq = 0
        with mss.MSS() as sct:
            while True:
                self.request.wait()
                self.request.clear()
                self.done.clear()
                self.busy = True
                try:
                    shot = sct.grab(self.region)
                    raw = shot.raw                     # BGRA
                    seq += 1
                    with self.lock:
                        self.frame = (raw, shot.width, shot.height, seq)
                except Exception as e:
                    print("[capture] error:", e)
                finally:
                    self.busy = False
                    self.done.set()


# ------------------------------------------------- GL window
class GlassGLWidget(QOpenGLWidget):
    def __init__(self, screen, reader, capturer, manual, cfg):
        super().__init__()
        self.reader = reader
        self.capturer = capturer
        self.manual = manual
        self.cfg = cfg
        self.g = 0.0
        self.shown = False
        self._uploaded_seq = -1
        self._last_kick = 0.0
        self._last_print = 0.0
        self._locked = False

        self.angle_open = float(cfg.angle_open)
        self.deadband = float(cfg.deadband)
        self.neg_scale = float(cfg.neg_scale)
        self.refresh_hz = float(cfg.refresh_hz)
        self.max_tilt = float(cfg.max_tilt_deg) * 3.14159265 / 180.0
        self.eye_h = float(cfg.eye_dist_h)
        self.spread = float(cfg.blur_spread)
        self.dark = float(cfg.darkening)
        self.max_taps = int(cfg.max_taps)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.setWindowTitle("duo-glass")

    def showEvent(self, _ev):
        # Critical: exclude ourselves from screen capture, otherwise the capture
        # grabs our own last frame and converges to a flat color after a few
        # frames (the documented root cause of the "white screen" bug upstream).
        if not getattr(self, "_no_exclude", False):
            try:
                import ctypes
                WDA_EXCLUDEFROMCAPTURE = 0x11
                r = ctypes.windll.user32.SetWindowDisplayAffinity(
                    int(self.winId()), WDA_EXCLUDEFROMCAPTURE)
                if not r:
                    print("[warn] SetWindowDisplayAffinity failed, capture may include self")
            except Exception as e:
                print("[warn] display affinity exception:", e)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def initializeGL(self):
        print("[GL] context =", self.context().isValid(),
              self.context().format().majorVersion(),
              self.context().format().minorVersion())
        self.prog = QOpenGLShaderProgram(self)
        ok_v = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VS)
        ok_f = self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FS_DUO)
        if not (ok_v and ok_f and self.prog.link()):
            print("[GL] shader build failed:\n", self.prog.log())
        self.prog.bind()

        self.cap_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        self._gl_ready = True

    def paintGL(self):
        if not getattr(self, "_gl_ready", False):
            return
        dpr = self.devicePixelRatioF()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))

        frame = self.capturer.latest()
        if frame and frame[3] != self._uploaded_seq:
            raw, fw, fh, seq = frame
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, fw, fh, 0,
                            GL.GL_BGRA, GL.GL_UNSIGNED_BYTE, raw)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self._uploaded_seq = seq

        if self._uploaded_seq == -1:
            GL.glClearColor(0, 0, 0, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return

        self.prog.bind()
        GL.glViewport(0, 0, w, h)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
        GL.glUniform1i(self.prog.uniformLocation("uTex"), 0)
        GL.glUniform2f(self.prog.uniformLocation("uRes"), float(frame[1]), float(frame[2]))
        GL.glUniform1f(self.prog.uniformLocation("uTilt"), self.g * self.max_tilt)
        GL.glUniform1f(self.prog.uniformLocation("uEyeZ"), self.eye_h * frame[2])
        GL.glUniform1f(self.prog.uniformLocation("uSpread"), self.spread)
        GL.glUniform1f(self.prog.uniformLocation("uDark"), self.dark)
        GL.glUniform1i(self.prog.uniformLocation("uMaxTaps"), self.max_taps)
        self._draw_quad()

    def _draw_quad(self):
        GL.glBegin(GL.GL_QUADS)
        GL.glTexCoord2f(0.0, 0.0); GL.glVertex2f(-1.0, -1.0)
        GL.glTexCoord2f(1.0, 0.0); GL.glVertex2f(1.0, -1.0)
        GL.glTexCoord2f(1.0, 1.0); GL.glVertex2f(1.0, 1.0)
        GL.glTexCoord2f(0.0, 1.0); GL.glVertex2f(-1.0, 1.0)
        GL.glEnd()

    # ---------- main loop ----------
    def tick(self):
        target_m, key, auto = (0.0, "", False)
        if self.manual is not None:
            target_m, key, auto = self.manual.get()
            if self.manual.quit_flag:
                QApplication.quit()

        angle = None
        fps, status, mode = 0.0, "manual", ""
        if self.reader is not None:
            angle, fps, status, mode = self.reader.get()

        # Signed angle -> concentration:
        #   positive angle 0..uAngleOpen  -> perspective stretch (0 = none, max = 1)
        #   negative angle                -> no stretch at all (stays clear)
        #   |angle| <= deadband           -> no stretch (avoid flicker around zero)
        if self.cfg.demo >= 0.0:
            target = float(self.cfg.demo)
            disp = "demo"
        elif auto and angle is not None:
            target = angle_to_concentration(angle, self.angle_open, self.deadband,
                                            self.neg_scale)
            disp = "auto+" if angle >= 0.0 else "auto-"
        else:
            target = target_m
            disp = "manual"
        self.g += (target - self.g) * 0.22

        if not self.shown:
            self.capturer.kick()
        else:
            now = time.time()
            if self.refresh_hz > 0 and now - self._last_kick >= 1.0 / self.refresh_hz:
                self._last_kick = now
                self.capturer.kick()
            self.update()
            if self.cfg.lock_at_close and self.g > 0.985 and not self._locked:
                self._locked = True
                import ctypes
                ctypes.windll.user32.LockWorkStation()
            if self.g < 0.9:
                self._locked = False

        if time.time() - self._last_print > 0.1:
            self._last_print = time.time()
            a = angle if angle is not None else float("nan")
            if getattr(self.cfg, "trace", False):
                # 逐项打印角度链路，定位「某个角度之后浓度突然回 0」这类问题
                if angle is None:
                    print("\n[trace] angle=None (no sample yet)")
                else:
                    print("\n[trace] angle=%8.2f -> target=%.4f  g=%.4f  deadband=%.2f  "
                          "angle_open=%.1f  %s"
                          % (angle, target, self.g, self.deadband, self.angle_open,
                             "NEGATIVE -> forced 0" if angle < 0.0 else
                             ("in deadband -> 0" if angle <= self.deadband else "stretch")),
                          flush=True)
            print("\r[%s] angle=%7.2f  concentration=%5.1f%%  [%s %s %3.0fHz] "
                  "arrows=tune r=auto/manual Esc=quit   "
                  % (disp, a, self.g * 100, status, mode, fps),
                  end="", flush=True)


# ------------------------------------------------- entry point
def build_cfg():
    p = argparse.ArgumentParser(description="iPhone Duo glass overlay (WinDuo_EthanMaven)")
    p.add_argument("--port", default="COM3", help="serial port (default COM3)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--manual", action="store_true", help="keyboard only, no serial")
    p.add_argument("--angle-open", type=float, default=90.0,
                   help="positive angle that maps to concentration 100%% (default 90)")
    p.add_argument("--deadband", type=float, default=1.0,
                   help="angles with |angle| below this stay fully clear (default 1.0)")
    p.add_argument("--neg-scale", type=float, default=0.0,
                   help="how much a NEGATIVE angle contributes, 0..1 (default 0.0 = "
                        "fully clear; try 0.15 to make the response continuous through zero)")
    p.add_argument("--refresh-hz", type=float, default=3.0, help="screen capture rate (default 3Hz)")
    p.add_argument("--max-tilt-deg", type=float, default=88.0, help="max glass tilt (default 88)")
    p.add_argument("--eye-dist-h", type=float, default=2.0, help="eye distance in screen heights")
    p.add_argument("--blur-spread", type=float, default=0.42, help="blur spread (smaller = clearer)")
    p.add_argument("--darkening", type=float, default=0.001, help="darkening per blur radius")
    p.add_argument("--max-taps", type=int, default=32, help="max samples per pixel")
    p.add_argument("--lock-at-close", action="store_true", help="lock workstation at full concentration")
    p.add_argument("--selftest", action="store_true", help="no-window self test")
    p.add_argument("--smoke", action="store_true", help="render then save PNG and quit")
    p.add_argument("--demo", type=float, default=-1.0,
                   help="hold a fixed concentration 0..1 (for screenshots / demos)")
    p.add_argument("--trace", action="store_true",
                   help="print raw angle / deadband / concentration once per second")
    return p.parse_args()


def main():
    cfg = build_cfg()

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    screen = app.primaryScreen()
    dpr = screen.devicePixelRatio()
    geom = screen.geometry()
    region = {"left": geom.x(), "top": geom.y(),
              "width": int(geom.width() * dpr), "height": int(geom.height() * dpr)}

    kb = ManualControl()
    kb.start()
    if cfg.manual:
        reader = None
        print("[manual] serial disabled")
    else:
        reader = AngleReader(cfg.port, cfg.baud)
        reader.start()
    capturer = CaptureWorker(region)
    capturer.start()

    print("=" * 66)
    print("iPhone Duo glass overlay - Windows (WinDuo_EthanMaven port)")
    print("  hinge = screen bottom edge | max tilt %.0f deg | eye %.1fx screen height"
          % (cfg.max_tilt_deg, cfg.eye_dist_h))
    print("  blur_spread=%.2f  darkening=%.4f  taps<=%d"
          % (cfg.blur_spread, cfg.darkening, cfg.max_taps))
    print("  angle map: positive 0..%.0f deg -> stretch 0..100%% | negative -> clear"
          % cfg.angle_open)
    print("  deadband: |angle| <= %.1f deg stays clear" % cfg.deadband)
    print("-" * 66)
    print("  click this console first, then use keys:")
    print("  up/down = tune  left = clear  right = full  r = auto/manual  Esc = quit")
    print("=" * 66)

    if cfg.selftest:
        time.sleep(4)
        angle, fps, st, mode = reader.get() if reader else (None, 0.0, "manual", "")
        capturer.kick()
        capturer.done.wait(timeout=3)
        frame = capturer.latest()
        ok = (frame is not None) and (cfg.manual or angle is not None)
        print("\n[selftest] serial: %s angle=%s fps=%.0f | capture: %s  => %s"
              % (st, angle, fps,
                 ("OK %dx%d" % (frame[1], frame[2])) if frame else "FAIL",
                 "PASS" if ok else "FAIL"))
        return 0 if ok else 1

    widget = GlassGLWidget(screen, reader, capturer, kb, cfg)
    widget.show()
    widget.shown = True
    capturer.kick()

    if cfg.smoke:
        def dump_and_quit():
            try:
                img = widget.grabFramebuffer()
                out1 = str(Path(__file__).with_name("smoke_widget.png"))
                img.save(out1)
                print("\n[smoke] rendered frame: %s (%dx%d)" % (out1, img.width(), img.height()))
            except Exception as e:
                print("\n[smoke] grabFramebuffer failed:", e)
            app.quit()
        widget.g = 0.85
        QTimer.singleShot(2500, dump_and_quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
