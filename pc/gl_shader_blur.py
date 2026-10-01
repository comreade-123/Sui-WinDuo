# -*- coding: utf-8 -*-
"""WinDuo OpenGL 玻璃着色器的角度 uniform 适配层。

把串口来的开合角 (0..180) 变成 GPU 能用的 uniform 值 (0..1)，并适配三种后端：

======================  ==============================================
后端                     写法
======================  ==============================================
裸 OpenGL / PyOpenGL     glUniform1f(glGetUniformLocation(prog, name), v)
PyQt6                    program.setUniformValue(name, v)
                         （或 QOpenGLFunctions.glUniform1f(loc, v)）
moderngl                 program[name].value = v
======================  ==============================================

三个 uniform 的语义（建议直接在原项目里沿用这三个名字）：
    u_hingeAngle    0..1  铰链开合角归一化值 = angle/180。0=完全闭合，1=完全展开。
                          语义是“几何量”，用于顶点位移、转轴朝向、边缘倾斜量。
    u_blurStrength  0..1  玻璃模糊/磨砂强度。0=完全通透（几乎无模糊），1=最浓。
                          语义是“采样强度”，用于模糊采样半径、mip LOD、混合权重。
    u_glassEdge     0..1  玻璃边缘高光带强度。0=无高光，1=最宽最亮。
                          语义是“光照量”，用于 fresnel 高光带宽/亮度。

**最短路径（接进原项目 render loop）**：
    loc = glGetUniformLocation(program, "u_hingeAngle")
    glUniform1f(loc, angle / 180.0)
PyQt6 等价写法：
    program.setUniformValue("u_hingeAngle", angle / 180.0)

依赖全部懒加载：模块导入时不 import PyQt6/OpenGL/moderngl，
三者都不存在时 backend="none"，只计算数值并打印一次提示，不抛异常。
"""

from __future__ import annotations

import importlib.util
import threading
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from winduo_protocol import ANGLE_MAX, AngleSmoother, clamp, normalize_angle
except ImportError:  # 作为包导入
    from .winduo_protocol import (  # type: ignore
        ANGLE_MAX,
        AngleSmoother,
        clamp,
        normalize_angle,
    )

__all__ = [
    "UNIFORM_HINGE_ANGLE",
    "UNIFORM_BLUR_STRENGTH",
    "UNIFORM_GLASS_EDGE",
    "UNIFORM_NAMES",
    "UNIFORM_SEMANTICS",
    "GLASS_EDGE_FRAGMENT_SNIPPET",
    "GLASS_EDGE_VERTEX_SNIPPET",
    "MINIMAL_INTEGRATION_SNIPPET",
    "angle_to_uniform",
    "angle_to_uniforms",
    "available_backends",
    "resolve_backend",
    "AngleUniformAdapter",
]

UNIFORM_HINGE_ANGLE = "u_hingeAngle"
UNIFORM_BLUR_STRENGTH = "u_blurStrength"
UNIFORM_GLASS_EDGE = "u_glassEdge"

UNIFORM_NAMES: Tuple[str, str, str] = (
    UNIFORM_HINGE_ANGLE,
    UNIFORM_BLUR_STRENGTH,
    UNIFORM_GLASS_EDGE,
)

UNIFORM_SEMANTICS: Dict[str, str] = {
    UNIFORM_HINGE_ANGLE: (
        "铰链开合角归一化值 0..1（= angle/180）。0=完全闭合，1=完全展开。"
        "语义：几何量，驱动顶点位移/转轴朝向/边缘倾斜。"
    ),
    UNIFORM_BLUR_STRENGTH: (
        "玻璃模糊（磨砂）强度 0..1。0=完全通透几乎无模糊，1=最浓。"
        "语义：采样强度，驱动模糊采样半径/mip LOD/混合权重。"
    ),
    UNIFORM_GLASS_EDGE: (
        "玻璃边缘高光带强度 0..1。0=无高光，1=最宽最亮。"
        "语义：光照量，驱动 fresnel 高光带宽与亮度。"
    ),
}

#: 可直接贴进原项目片段着色器的边缘高光实现
GLASS_EDGE_FRAGMENT_SNIPPET = """
// ---- WinDuo 玻璃铰链 uniform（声明一次即可） ----
uniform float u_hingeAngle;    // 0..1 铰链开合角  = angle / 180.0
uniform float u_blurStrength;  // 0..1 模糊强度
uniform float u_glassEdge;     // 0..1 边缘高光强度

// 玻璃边缘高光：fresnel 用铰链角调制（开得越大，边缘越"薄"越亮）
vec3 winDuoGlassEdge(vec3 baseColor, float fresnel) {
    float thin     = mix(1.25, 0.75, u_hingeAngle);         // 开合角越大，高光越锐
    float rim      = pow(clamp(fresnel, 0.0, 1.0), 2.0 * thin);
    float edgeGain = rim * u_glassEdge;
    vec3  frosted  = mix(baseColor, vec3(1.0), 0.15 * u_blurStrength);
    return frosted + vec3(edgeGain);
}
"""

#: 顶点着色器里可以怎么用 u_hingeAngle（示意）
GLASS_EDGE_VERTEX_SNIPPET = """
// ---- 顶点侧：用铰链角做几何变形示意 ----
uniform float u_hingeAngle;   // 0..1
// 沿法线方向按开合角抬升边缘：0(闭合) 不动，1(展开) 抬高 1 个单位
vec3 winDuoHingeOffset(vec3 position, vec3 normal) {
    return position + normal * (u_hingeAngle * 1.0);
}
"""

#: 最短集成路径（复制粘贴即用）
MINIMAL_INTEGRATION_SNIPPET = """# --- 裸 OpenGL / PyOpenGL：最短路径 ---
from OpenGL import GL
angle = sample["angle"]                       # 来自 SerialReader
GL.glUniform1f(GL.glGetUniformLocation(prog, "u_hingeAngle"), angle / 180.0)

# --- PyQt6：最短路径 ---
angle = sample["angle"]
prog.setUniformValue("u_hingeAngle", angle / 180.0)
# 自动化版本（含平滑与三个 uniform，未装 GL 时自动降级）：
#   adapter = AngleUniformAdapter(program=prog)
#   adapter.update_angle_uniform(angle)
"""


# ---------------------------------------------------------------------------
# 纯映射函数
# ---------------------------------------------------------------------------

def angle_to_uniform(angle: Any, curve: str = "linear") -> float:
    """角度 0..180 -> uniform 0..1（线性；curve 可选 smooth/gamma 做观感微调）。"""
    t = normalize_angle(angle)
    if curve == "smooth":
        t = t * t * (3.0 - 2.0 * t)
    elif curve == "gamma":
        t = t ** (1.0 / 2.2)
    return clamp(t, 0.0, 1.0)


def angle_to_uniforms(
    angle: Any,
    curve: str = "linear",
    blur_range: Tuple[float, float] = (0.0, 1.0),
    edge_range: Tuple[float, float] = (0.0, 1.0),
) -> Dict[str, float]:
    """角度 -> 三个 uniform 值；blur/edge 支持自定义值域（默认恒等 0..1）。

    默认映射（curve="linear"）:
        u_hingeAngle   = angle / 180
        u_blurStrength = angle / 180
        u_glassEdge    = angle / 180
    """
    t = angle_to_uniform(angle, curve)
    blur_lo, blur_hi = float(blur_range[0]), float(blur_range[1])
    edge_lo, edge_hi = float(edge_range[0]), float(edge_range[1])
    return {
        UNIFORM_HINGE_ANGLE: clamp(t, 0.0, 1.0),
        UNIFORM_BLUR_STRENGTH: clamp(blur_lo + (blur_hi - blur_lo) * t, 0.0, 1.0),
        UNIFORM_GLASS_EDGE: clamp(edge_lo + (edge_hi - edge_lo) * t, 0.0, 1.0),
    }


# ---------------------------------------------------------------------------
# 后端探测（find_spec 不真正 import，保持懒加载）
# ---------------------------------------------------------------------------

def available_backends() -> Dict[str, bool]:
    """探测本机可用的 GL 绑定；不导入模块本身，开销极小。"""
    result = {"pyqt6": False, "pyopengl": False, "moderngl": False}
    for key, module in (("pyqt6", "PyQt6"), ("pyopengl", "OpenGL"), ("moderngl", "moderngl")):
        try:
            result[key] = importlib.util.find_spec(module) is not None
        except Exception:
            result[key] = False
    return result


def resolve_backend(backend: str = "auto") -> str:
    """确定实际使用的后端名：pyqt6 / pyopengl / moderngl / none。"""
    if backend and backend != "auto":
        return backend
    found = available_backends()
    if found.get("pyopengl"):
        return "pyopengl"
    if found.get("moderngl"):
        return "moderngl"
    if found.get("pyqt6"):
        return "pyqt6"
    return "none"


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

class AngleUniformAdapter:
    """角度 -> uniform 写入器，可直接挂进原项目 render loop。

    参数:
        program:  可选 GL program 对象（QOpenGLShaderProgram / moderngl.Program）。
                  None 时不写 GPU，只维护数值（降级模式）。
        backend:  "auto" | "pyqt6" | "pyopengl" | "moderngl" | "none"。
        smoother: 自定义 AngleSmoother；None 时用默认 alpha=0.25。
        curve:    映射曲线，默认 linear（0°->0.0, 180°->1.0）。
        gl_module: 可选注入的 GL 模块（测试用），支持 glUniform1f/glGetUniformLocation。

    典型用法:
        adapter = AngleUniformAdapter(program=prog)
        ...
        def on_sample(sample):          # 20Hz 回调
            adapter.update_angle_uniform(sample["angle"])
    """

    def __init__(
        self,
        program: Any = None,
        backend: str = "auto",
        smoother: Optional[AngleSmoother] = None,
        curve: str = "linear",
        blur_range: Tuple[float, float] = (0.0, 1.0),
        edge_range: Tuple[float, float] = (0.0, 1.0),
        gl_module: Any = None,
        verbose: bool = True,
    ) -> None:
        self.program = program
        self.backend = resolve_backend(backend)
        self.curve = curve
        self.blur_range = blur_range
        self.edge_range = edge_range
        self.smoother = smoother if smoother is not None else AngleSmoother()
        self.verbose = bool(verbose)

        self._gl = gl_module
        self._gl_ready = False
        self._locations: Dict[str, Any] = {}
        self._values: Dict[str, float] = {name: 0.0 for name in UNIFORM_NAMES}
        self._lock = threading.RLock()
        self._hinted = False
        self._warned = False
        self.write_count = 0

        if program is not None:
            self.bind_program(program)
        if self.backend == "none" and not self._gl and self.verbose:
            self._hint_once(
                "未检测到 PyQt6/PyOpenGL/moderngl，着色器 uniform 写入已降级："
                "只计算数值（angle/180.0），不影响串口与玻璃覆盖层。"
            )

    # -- program 绑定 -------------------------------------------------------

    def bind_program(self, program: Any) -> None:
        """绑定 GL program 并缓存 uniform location。"""
        with self._lock:
            self.program = program
            self._locations.clear()
            self._gl_ready = False
            if program is None:
                return
            if self.backend == "none":
                if self.verbose:
                    self._hint_once("后端不可用，program 已记录但不会写入 GPU。")
                return
            try:
                self._prepare_backend()
            except Exception as exc:
                self._hint_once("准备 GL 后端失败（%s），已降级为只计算数值。" % exc)
                self.backend = "none"

    def _prepare_backend(self) -> None:
        """按后端缓存 uniform location（懒加载 import）。"""
        program = self.program
        if program is None:
            return

        if self.backend == "pyopengl":
            if self._gl is None:
                from OpenGL import GL  # 懒加载
                self._gl = GL
            ok = hasattr(self._gl, "glGetUniformLocation") and hasattr(self._gl, "glUniform1f")
            if ok:
                for name in UNIFORM_NAMES:
                    try:
                        self._locations[name] = self._gl.glGetUniformLocation(program, name)
                    except Exception:
                        self._locations[name] = -1
                self._gl_ready = True
            return

        if self.backend == "moderngl":
            for name in UNIFORM_NAMES:
                self._locations[name] = name
            self._gl_ready = True
            return

        if self.backend == "pyqt6":
            # QOpenGLShaderProgram 支持按名字写入，无需 location
            for name in UNIFORM_NAMES:
                self._locations[name] = name
            self._gl_ready = True
            return

    # -- 写入 ---------------------------------------------------------------

    def update_angle_uniform(self, angle: Any) -> Dict[str, float]:
        """喂入角度（原始或已平滑），算值并写入 uniform；返回本次的 uniform 字典。"""
        smoothed = self.smoother.update(angle)
        values = angle_to_uniforms(
            smoothed, curve=self.curve, blur_range=self.blur_range, edge_range=self.edge_range
        )
        with self._lock:
            self._values = dict(values)
        self._apply(values)
        return dict(values)

    def update_from_sample(self, sample: Any) -> Optional[Dict[str, float]]:
        """直接消费 SerialReader 的样本字典（优先用已平滑的 angle）。"""
        try:
            if isinstance(sample, dict):
                angle = sample.get("angle")
            else:
                angle = sample
        except Exception:
            return None
        if angle is None:
            return None
        return self.update_angle_uniform(angle)

    def values(self) -> Dict[str, float]:
        """最近一次写入的 uniform 值（副本）。"""
        with self._lock:
            return dict(self._values)

    def uniform_locations(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._locations)

    @property
    def active(self) -> bool:
        """是否真的会写 GPU。"""
        return self._gl_ready and self.program is not None

    def _apply(self, values: Dict[str, float]) -> None:
        if not self._gl_ready or self.program is None:
            return
        for name, value in values.items():
            try:
                self._write_uniform(name, float(value))
                self.write_count += 1
            except Exception as exc:
                if not self._warned:
                    self._warned = True
                    print("[gl_shader_blur] uniform %s 写入失败（%s），后续静默忽略。" % (name, exc))
                self._gl_ready = False
                return

    def _write_uniform(self, name: str, value: float) -> None:
        """按后端写入单个 uniform。"""
        program = self.program

        if self.backend == "pyopengl":
            loc = self._locations.get(name)
            if loc is None or int(loc) < 0:
                raise RuntimeError("uniform %s 不存在或已被优化掉" % name)
            self._gl.glUniform1f(int(loc), value)
            return

        if self.backend == "moderngl":
            try:
                uniform = program[name]
                uniform.value = value
            except Exception:
                program[name] = value
            return

        if self.backend == "pyqt6":
            self._set_qt_uniform(program, name, value)
            return

        raise RuntimeError("后端 %s 不可写" % self.backend)

    def _set_qt_uniform(self, program: Any, name: str, value: float) -> None:
        """PyQt6 的 setUniformValue 重载在不同版本略有差异，这里做级联兼容。"""
        errors = []
        # 1) 最常见：直接传 float
        try:
            program.setUniformValue(name, float(value))
            return
        except Exception as exc:
            errors.append(exc)
        # 2) 传 ctypes.c_float（部分版本只接受 GLfloat 包装）
        try:
            import ctypes
            program.setUniformValue(name, ctypes.c_float(float(value)))
            return
        except Exception as exc:
            errors.append(exc)
        # 3) 先取 location 再写
        try:
            loc = program.uniformLocation(name)
            if int(loc) < 0:
                raise RuntimeError("uniform %s 未找到" % name)
            program.setUniformValue(int(loc), float(value))
            return
        except Exception as exc:
            errors.append(exc)
        raise RuntimeError("setUniformValue 全部重载失败: %s" % errors)

    # -- 提示 ---------------------------------------------------------------

    def _hint_once(self, message: str) -> None:
        if self._hinted:
            return
        self._hinted = True
        print("[gl_shader_blur] %s" % message)


# ---------------------------------------------------------------------------
# 命令行自检：python pc/gl_shader_blur.py
# ---------------------------------------------------------------------------

def _main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WinDuo GL uniform 自检")
    parser.add_argument("--angle", type=float, default=45.0, help="测试角度")
    args = parser.parse_args(argv)

    found = available_backends()
    print("可用后端: %s" % found)
    print("选用后端: %s" % resolve_backend())
    print("GLSL 声明:\n%s" % GLASS_EDGE_FRAGMENT_SNIPPET.strip()[:120] + " ...")
    adapter = AngleUniformAdapter(backend="none")
    print("angle=%.1f -> uniforms=%s" % (args.angle, adapter.update_angle_uniform(args.angle)))
    print("最小集成示例:\n%s" % MINIMAL_INTEGRATION_SNIPPET)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
