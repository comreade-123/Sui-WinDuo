#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_device.py -- WinDuo / EthanMaven 设备行为仿真器 (纯 Python 3, 无第三方依赖)

本文件是 firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino 中
**姿态解算 / 零漂校准 / 按键状态机 / 输出节流** 的可执行 Python 复刻 (不改动固件)。
:func:`FirmwareModel` 与 .ino 的 loop() 一一对应, 用 5ms 步长推进毫秒时间轴。

它证明什么 (无硬件可证):
  * updateAttitude() 的姿态链在数学上收敛: 角度恒在 [0,180], 无 NaN, 无越界;
  * 静止时互补滤波 + 基准锚定 (STILL_BASE_TRACK) 的噪声/漂移量级;
  * 校准窗口的静止判定 (CALIB_VAR_MAX / CALIB_GYRO_MAX_DPS) 真的会拒绝"边动边校准";
  * 长按 800ms 触发正好一次重校准; 短按 50~800ms 正好切一次模式;
  * 串口 JSON 是 20Hz、字段齐全、噪声行不会让解析器崩溃;
  * 仿真参数与 .ino 常量逐个对齐 (--check-firmware)。

它不能证明什么: 任何硬件事实 (I2C 实际应答、OLED 实际点亮、按键实际电平、
真实零漂量级)。详见 tools/README_verify.md。

    python tools/demo_device.py                 # 跑全部场景
    python tools/demo_device.py --list          # 列出场景
    python tools/demo_device.py --quick         # 缩短漂移窗口
    python tools/demo_device.py --check-firmware firmware/WindowsDuo_EthanMaven/WindowsDuo_EthanMaven.ino
    python tools/demo_device.py --json-out verification/demo_device_result.json

退出码: 0 = 全部断言通过, 1 = 有断言 FAIL, 2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from dataclasses import dataclass, field

# ==========================================================================
# 1. 与 .ino 对齐的常量 (改这里必须同步改固件, 反之亦然)
# ==========================================================================

RAD_TO_DEG_F = 57.29578
DEG_TO_RAD_F = 0.01745329

SERIAL_BAUD = 115200
OLED_UPDATE_INTERVAL = 200
SERIAL_SEND_INTERVAL = 50

COMP_FILTER_ALPHA = 0.98
ANGLE_MIN_DEG = 0.0
ANGLE_MAX_DEG = 180.0
ANGLE_LPF_ALPHA = 0.35

CALIB_WINDOW_MS = 3000
CALIB_TIMEOUT_MS = 8000
CALIB_GYRO_MAX_DPS = 6.0
CALIB_VAR_MAX = 900.0

STILL_BASE_TRACK = 0.004
ACCEL_TRUST_STILL = 0.995
ACCEL_TRUST_MOVING = 0.980
STILL_ABS_DPS = 1.2

BUTTON_DEBOUNCE_MS = 30
BUTTON_LONG_MS = 800
BUTTON_SHORTMIN_MS = 50

LOOP_DT_MS = 5          # sampleImu / updateAttitude 的节流间隔 (固件里的 `now - x < 5`)
WARMUP_MS = 200         # 每段统计前丢弃的收敛时间 (仅影响统计, 不影响断言/限幅检查)
TEXT_AUTHOR = "EthanMaven"
MODE_NAMES = ("default", "calibrate", "debug")
STATUS_NAMES = ("warming_up", "calibrating", "ok", "sensor_error")

# 传感器仿真参数 (只影响 PC 侧激励, 不影响固件逻辑)
GYRO_NOISE_DPS = 0.05
ACCEL_NOISE_DEG = 0.20
LID_SPAN_DEG = 90.0     # 物理 0->180 行程对应的俯仰变化量

STATIC, MOVING = "static", "moving"


class ScenarioError(RuntimeError):
    """场景配置错误。"""


# ==========================================================================
# 2. 固件姿态链的逐行复刻
# ==========================================================================


def clamp_float(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def wrap_deg180(deg: float) -> float:
    while deg >= 180.0:
        deg -= 360.0
    while deg < -180.0:
        deg += 360.0
    return deg


def pitch_from_accel(ax_g: float, ay_g: float, az_g: float) -> float:
    """对应 .ino 的 pitchFromAccel(): atan2(-gx, sqrt(gy^2+gz^2))."""
    horizontal = math.sqrt(ay_g * ay_g + az_g * az_g)
    return math.atan2(-ax_g, horizontal) * RAD_TO_DEG_F


class SensorSim:
    """MPU6050 数值模型: 真实俯仰 + 白噪声 + 可注入零漂。"""

    def __init__(self, seed: int = 20240617) -> None:
        self.rng = random.Random(seed)
        self.true_pitch_deg = 0.0
        self.gyro_bias_dps = 0.0

    def set_pitch(self, deg: float) -> None:
        """把装置直接搬到指定俯仰 (用于设置"开机时笔记本所处的姿态")。

        注意 true_pitch_deg 会一直保持到下一次 step 累加, 因此跨段有效。
        """
        self.true_pitch_deg = deg

    def step(self, dt_s: float, pitch_rate_dps: float):
        """返回 (gyro_y_dps, ax_g, ay_g, az_g)。

        物理模型: 屏幕绕转轴转动, 传感器俯仰随之变化。开合 180 度 = 传感器俯仰变化
        180 度, 因此传感器俯仰会走出 -90..+90 的量程之外; 此时 pitchFromAccel 会
        **折返** (theta=+135 读到 +45), 而陀螺仪仍然读得到真实角速度 —— 这正是
        端点奇异性的来源, 由 :func:`pitch_from_accel` 如实复刻, 不做美化。
        """
        self.true_pitch_deg += pitch_rate_dps * dt_s
        # 陀螺读数与加速度计同向翻转: 两者是同一颗芯片的同一根轴, 符号约定必须一致
        gyro_y = -pitch_rate_dps + self.gyro_bias_dps + self.rng.gauss(0.0, GYRO_NOISE_DPS)
        theta = -(self.true_pitch_deg / RAD_TO_DEG_F) + self.rng.gauss(0.0, ACCEL_NOISE_DEG / RAD_TO_DEG_F)
        ax = -math.sin(theta)
        horizontal = math.cos(theta)
        return gyro_y, ax, 0.0, horizontal


@dataclass
class CalibrationEvent:
    at_ms: int
    reason: str
    samples: int = 0
    accepted: bool = False
    variance: float = 0.0
    mean_abs_gyro: float = 0.0
    baseline_before: float = 0.0
    baseline_after: float = 0.0
    bias_after: float = 0.0


@dataclass
class ButtonEvent:
    at_ms: int
    kind: str          # short / long
    held_ms: int


class FirmwareModel:
    """WindowsDuo_EthanMaven.ino 的运行期状态机 (loop() 逐 tick 复刻)。

    :param strict_5ms_throttle: True = 按固件的 `now - x < 5` 节流逐 tick 推进。
    """

    def __init__(self, sensor: SensorSim, strict_5ms_throttle: bool = True) -> None:
        self.sensor = sensor
        self.strict = strict_5ms_throttle
        self.now = 0

        # 姿态解算
        self.pitch_deg = 0.0
        self.virtual_angle = 0.0
        self.angle_deg = 0.0
        self.baseline_pitch_deg = 0.0
        self.gyro_rate_dps = 0.0

        # 零漂校准 (单轴模型: 只保留开合轴 = 固件的 gyroBiasY)
        self.gyro_bias_dps = 0.0
        self.calib_active = False
        self.calib_start_ms = 0
        self.calib_last_ms = 0
        self.calib_sample_count = 0
        self.calib_sum_gx = 0.0
        self.calib_sum_gx_sq = 0.0
        self.calib_sum_pitch = 0.0
        self.calib_sum_gz = 0.0
        self.calib_sum_gz_sq = 0.0

        # 状态 / 模式 / 按键
        self.sys_status = "warming_up"
        self.work_mode = "default"
        self.btn_state = 0            # 0 idle,1 debounce_down,2 pressed,3 debounce_up
        self.btn_edge_ms = 0
        self.btn_pressed_ms = 0
        self.raw_pressed = False
        self.btn_long_latched = False   # 对应固件 btnLongLatched (已修复)
        self.short_press_count = 0
        self.long_press_count = 0

        # 节流时间戳
        self.last_oled_ms = 0
        self.last_send_ms = 0
        self.last_imu_sample_ms = 0
        self.last_attitude_ms = 0

        # 观测
        self.angle_trace: list[tuple[int, float]] = []
        self.mode_trace: list[tuple[int, str]] = []
        self.calibrations: list[CalibrationEvent] = []
        self.button_events: list[ButtonEvent] = []
        self.serial_lines: list[str] = []
        self.oled_frames: list[tuple[int, str]] = []
        self.nan_samples = 0
        self.last_accel = (0.0, 0.0, 1.0)

    # ---------------- 校准 ----------------

    def start_calibration(self, reason: str) -> None:
        self.calib_active = True
        self.calib_start_ms = self.now
        self.calib_last_ms = self.calib_start_ms
        self.last_imu_sample_ms = 0
        self.calib_sample_count = 0
        self.calib_sum_gx = self.calib_sum_gx_sq = self.calib_sum_pitch = 0.0
        self.calib_sum_gz = self.calib_sum_gz_sq = 0.0
        self.calibrations.append(
            CalibrationEvent(at_ms=self.now, reason=reason, baseline_before=self.baseline_pitch_deg)
        )
        self.refresh_status()

    def finish_calibration(self, accepted: bool) -> None:
        ev = self.calibrations[-1] if self.calibrations else None
        if ev is not None:
            ev.samples = self.calib_sample_count
            ev.accepted = accepted
        if accepted and self.calib_sample_count > 0:
            n = float(self.calib_sample_count)
            self.gyro_bias_dps = self.calib_sum_gx / n     # 对应固件的 gyroBiasY
            self.baseline_pitch_deg = self.calib_sum_pitch / n
            self.pitch_deg = self.baseline_pitch_deg
            self.virtual_angle = 0.0
            self.angle_deg = 0.0
        if ev is not None:
            ev.baseline_after = self.baseline_pitch_deg
            ev.bias_after = self.gyro_bias_dps
        self.calib_active = False
        self.refresh_status()

    def update_calibration(self) -> None:
        if not self.calib_active:
            return
        dt_ms = self.now - self.calib_last_ms
        if dt_ms >= 5:
            self.calib_last_ms = self.now
            self.calib_sample_count += 1
            g = self.gyro_rate_raw
            self.calib_sum_gx += g
            self.calib_sum_gx_sq += g * g
            self.calib_sum_gz += 0.0
            self.calib_sum_gz_sq += 0.0
            self.calib_sum_pitch += self.pitch_accel

        if self.now - self.calib_start_ms >= CALIB_WINDOW_MS and self.calib_sample_count >= 100:
            n = float(self.calib_sample_count)
            mean = self.calib_sum_gx / n
            var = (self.calib_sum_gx_sq / n) - mean * mean
            mean_abs = abs(mean)
            accepted = (var <= CALIB_VAR_MAX) and (mean_abs <= CALIB_GYRO_MAX_DPS)
            if self.calibrations:
                self.calibrations[-1].variance = var
                self.calibrations[-1].mean_abs_gyro = mean_abs
            self.finish_calibration(accepted)
            return
        if self.now - self.calib_start_ms >= CALIB_TIMEOUT_MS:
            self.finish_calibration(False)

    # ---------------- 状态 ----------------

    def refresh_status(self) -> None:
        if self.calib_active:
            self.sys_status = "calibrating"
        elif self.sys_status in ("sensor_error", "calibrating"):
            self.sys_status = "ok"
        # warming_up -> ok 的迁移由 finishCalibration/首个采样驱动

    # ---------------- 姿态 ----------------

    def update_attitude(self) -> None:
        elapsed_ms = self.now - self.last_attitude_ms
        if elapsed_ms < 5:
            return
        self.last_attitude_ms = self.now
        dt_sec = elapsed_ms / 1000.0

        gx = self.gyro_rate_raw - self.gyro_bias_dps
        gy = self.gyro_rate_raw - self.gyro_bias_dps
        self.gyro_rate_dps = gy

        self.pitch_deg += self.gyro_rate_dps * dt_sec

        ax, ay, az = self.last_accel
        accel_magnitude = math.sqrt(ax * ax + ay * ay + az * az)
        accel_valid = 0.75 < accel_magnitude < 1.25
        still_now = (abs(gx) + abs(gy) + abs(0.0)) < STILL_ABS_DPS

        if accel_valid:
            accel_pitch = pitch_from_accel(ax, ay, az)
            alpha = ACCEL_TRUST_STILL if still_now else ACCEL_TRUST_MOVING
            self.pitch_deg = alpha * self.pitch_deg + (1.0 - alpha) * accel_pitch
        self.pitch_deg = wrap_deg180(self.pitch_deg)

        relative = wrap_deg180(self.pitch_deg - self.baseline_pitch_deg)
        if relative < 0.0:
            relative = -relative
        relative = clamp_float(relative, ANGLE_MIN_DEG, ANGLE_MAX_DEG)
        self.virtual_angle = relative

        if (not self.calib_active) and still_now and accel_valid and self.sys_status == "ok":
            accel_relative = clamp_float(
                abs(wrap_deg180(self.pitch_deg - self.baseline_pitch_deg)),
                ANGLE_MIN_DEG, ANGLE_MAX_DEG)
            rad_a = self.virtual_angle * DEG_TO_RAD_F
            rad_b = accel_relative * DEG_TO_RAD_F
            sin_diff = math.sin(rad_a) * math.cos(rad_b) - math.cos(rad_a) * math.sin(rad_b)
            cos_diff = math.cos(rad_a) * math.cos(rad_b) + math.sin(rad_a) * math.sin(rad_b)
            diff_deg = math.atan2(sin_diff, cos_diff) * RAD_TO_DEG_F
            if abs(diff_deg) > 1.5:
                self.baseline_pitch_deg = wrap_deg180(
                    self.baseline_pitch_deg + STILL_BASE_TRACK * diff_deg)

        self.angle_deg += ANGLE_LPF_ALPHA * (self.virtual_angle - self.angle_deg)
        self.angle_deg = clamp_float(self.angle_deg, ANGLE_MIN_DEG, ANGLE_MAX_DEG)

    # ---------------- 按键 ----------------

    def _next_mode(self) -> None:
        """对应固件 nextMode()。注意 default->calibrate 会顺带启动一次校准。"""
        if self.work_mode == "default":
            self.work_mode = "calibrate"
            self.start_calibration("short press enter calibrate mode")
            self.mode_trace.append((self.now, self.work_mode))
        elif self.work_mode == "calibrate":
            self.work_mode = "debug"
            self.mode_trace.append((self.now, self.work_mode))
        else:
            self.work_mode = "default"
            self.mode_trace.append((self.now, self.work_mode))

    def update_button(self) -> None:
        """对应固件 updateButton() (已含 btnLongLatched 修复, 见 .ino:650-681)。"""
        pressed = self.raw_pressed
        if self.btn_state == 0:                                    # BTN_IDLE
            if pressed:
                self.btn_edge_ms = self.now
                self.btn_state = 1
        elif self.btn_state == 1:                                  # BTN_DEBOUNCE_DOWN
            if not pressed:
                self.btn_state = 0
            elif self.now - self.btn_edge_ms >= BUTTON_DEBOUNCE_MS:
                self.btn_pressed_ms = self.now
                self.btn_state = 2
        elif self.btn_state == 2:                                  # BTN_PRESSED
            if self.now - self.btn_pressed_ms >= BUTTON_LONG_MS and not self.btn_long_latched:
                self.btn_long_latched = True
                self.long_press_count += 1
                self.button_events.append(
                    ButtonEvent(self.now, "long", self.now - self.btn_pressed_ms))
                self.start_calibration("long press")
                self.work_mode = "calibrate"
                self.mode_trace.append((self.now, self.work_mode))
                self.btn_state = 3
                self.btn_edge_ms = self.now
            elif not pressed:
                self.btn_state = 3
                self.btn_edge_ms = self.now
        elif self.btn_state == 3:                                  # BTN_DEBOUNCE_UP
            if pressed:
                self.btn_state = 2                                 # 抖动, 回到按下态
            elif self.now - self.btn_edge_ms >= BUTTON_DEBOUNCE_MS:
                held = self.btn_edge_ms - self.btn_pressed_ms
                if (not self.btn_long_latched) and BUTTON_SHORTMIN_MS <= held < BUTTON_LONG_MS:
                    self.short_press_count += 1
                    self.button_events.append(ButtonEvent(self.now, "short", held))
                    self._next_mode()
                self.btn_long_latched = False                      # 真正松手才解除闩锁
                self.btn_state = 0

    # ---------------- 输出 ----------------

    @staticmethod
    def _quantize(value: float, scale: float) -> float:
        """复刻固件"先量化成整数再除回来"的写法 (.ino:855-868), 保证是 JSON 数值。"""
        half = 0.5 if value >= 0 else -0.5
        return int(value * scale + half) / scale

    def send_json_line(self) -> None:
        doc = {
            "angle": self._quantize(self.angle_deg, 10.0),      # 1 位小数, 数值类型
            "status": self.sys_status,
            "mode": self.work_mode,
            "author": TEXT_AUTHOR,
        }
        if self.work_mode == "debug":
            doc["gyro"] = self._quantize(self.gyro_rate_dps, 100.0)
            doc["bias"] = self._quantize(self.gyro_bias_dps, 1000.0)
            doc["base"] = self._quantize(self.baseline_pitch_deg, 100.0)
            doc["sp"] = self.short_press_count
            doc["lp"] = self.long_press_count
        if self.calib_active:
            elapsed = min(self.now - self.calib_start_ms, CALIB_WINDOW_MS)
            doc["progress"] = int(elapsed / CALIB_WINDOW_MS * 100)
        self.serial_lines.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))

    def update_oled(self, force: bool = False) -> None:
        if not force and (self.now - self.last_oled_ms < OLED_UPDATE_INTERVAL):
            return
        self.last_oled_ms = self.now
        if self.calib_active:
            elapsed = min(self.now - self.calib_start_ms, CALIB_WINDOW_MS)
            pct = int(elapsed / CALIB_WINDOW_MS * 100)
            self.oled_frames.append((self.now, f"CALIBRATING {pct}%"))
            return
        angle_int = int(self.angle_deg + 0.5)
        bar_pct = int(self.angle_deg / ANGLE_MAX_DEG * 100.0 + 0.5)
        self.oled_frames.append(
            (self.now, f"{self.work_mode}/{self.sys_status} {angle_int}deg {bar_pct}%"))

    # ---------------- loop() ----------------

    def tick(self, pitch_rate_dps: float) -> None:
        self.now += LOOP_DT_MS

        # sampleImu(): 节流 5ms
        if (not self.strict) or (self.now - self.last_imu_sample_ms >= 5):
            self.last_imu_sample_ms = self.now
            gyro_y, ax, ay, az = self.sensor.step(LOOP_DT_MS / 1000.0, pitch_rate_dps)
            self.gyro_rate_raw = gyro_y
            self.last_accel = (ax, ay, az)
            self.pitch_accel = pitch_from_accel(ax, ay, az)
            if self.sys_status == "warming_up" and not self.calib_active:
                self.sys_status = "ok"

        self.update_calibration()
        self.update_attitude()
        self.update_button()
        self.update_oled(False)

        if self.now - self.last_send_ms >= SERIAL_SEND_INTERVAL:
            self.last_send_ms = self.now
            self.send_json_line()

        self.angle_trace.append((self.now, self.angle_deg))
        if math.isnan(self.angle_deg) or math.isnan(self.pitch_deg):
            self.nan_samples += 1

    def button_down(self) -> None:
        self.raw_pressed = True

    def button_up(self) -> None:
        self.raw_pressed = False


# ==========================================================================
# 3. PC 侧解析 (与 pc/ 端同规则: 角度是字符串, 噪声行必须跳过)
# ==========================================================================

KEY_RE = re.compile(r'"(angle|status|mode|author|gyro|bias|base|sp|lp|progress)"\s*:\s*'
                    r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?)')


def parse_device_line(line: str) -> dict | None:
    """解析一行串口输出。噪声/残缺行返回 None, 绝不抛异常。

    注意: 固件把 angle 序列化成 JSON 字符串 ("angle":"45.3"), 因此这里显式做
    字符串->float 归一化, 两种形态 (字符串/数字) 都能吃。
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except Exception:
        found = dict(KEY_RE.findall(line))
        if "angle" not in found:
            return None
        obj = {}
        for k, v in found.items():
            if v.startswith('"'):
                obj[k] = v[1:-1]
            else:
                try:
                    obj[k] = float(v)
                except ValueError:
                    return None
    if not isinstance(obj, dict) or "angle" not in obj:
        return None
    try:
        obj["angle"] = float(obj["angle"])
    except (TypeError, ValueError):
        return None
    return obj


def make_noise_lines() -> list[str]:
    """串口噪声: 固件启动日志(# 开头)、截断行、乱码、溢出数字、半行。"""
    return [
        "",
        "   ",
        "# calibration started: boot",
        "# calibration done. gyro_bias(dps)=0.123,-0.045,0.010 baseline_pitch=1.234",
        "garbage!!!",
        '{"angle":"12.3","status":',
        '{"angle":"1e999999","status":"ok","mode":"default","author":"x"}',
        '{"angle":"12.3" "status":"ok"}',
        "AT+RST",
        "x" * 300,
        '{"angle":45.0,"status":"ok","mode":"default"}',
        '{"angle":null}',
        '{"angle":"not-a-number"}',
    ]


# ==========================================================================
# 4. 场景
# ==========================================================================


@dataclass
class Segment:
    name: str
    duration_ms: int
    pitch_rate_dps: float = 0.0
    jitter_amp_deg: float = 0.0
    jitter_hz: float = 0.0
    events: list[tuple[int, str]] = field(default_factory=list)
    motion: bool = False
    gyro_bias_dps: float = 0.0
    pitch_offset_deg: float = 0.0    # 段开始时把真实俯仰直接搬到这个数值


@dataclass
class Scenario:
    name: str
    title: str
    seed: int
    segments: list[Segment]

    @property
    def duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments)


def build_scenarios(quick: bool = False) -> dict[str, Scenario]:
    """场景库。

    物理量程说明 (重要, 与固件 pitchFromAccel 有关):
      * 固件用 atan2(-ax, sqrt(ay^2+az^2)) 求俯仰, 该式的值域是 **-90..+90 度**。
        超过 ±90 度时重力矢量折返, 同一个读数对应两个姿态 (已验证: theta=+135 与
        theta=+45 都读到 +45)。
      * 固件输出 = |wrap180(pitch - baseline)|, 是 **1:1** 的, 没有倍数缩放。
        因此输出要走到 180, 需要传感器俯仰相对基准变化 180 度 —— 超出 atan2 量程,
        物理上不可达 (除非用陀螺积分硬撑, 而静止锚定会把角度拉回加速度计真值)。
      * 结论: "0..180" 是**输出标称范围**, 实际可达上限取决于转轴/安装比。
        若笔记本合盖->展开的机械行程是 90 度, 那么传感器摆幅 90 度, 输出就是 0..90;
        基准放在行程中点时, 输出 45 对应半开。**这是必须实物标定的项**,
        见 tools/README_verify.md 的"必须实物验证"。
      * 本文件的场景按"物理俯仰摆幅 90 度、基准居中"来设计, 保证激励落在传感器
        有效量程内, 不制造出物理上不可达的期望值。

    符号约定: 合盖方向 = 俯仰向正 (+90 对应完全合盖), 完全展开 = 俯仰 0。
    """
    drift_ms = 4000 if quick else 30000
    #: 基准(半开)相对完全展开的俯仰; 也是合盖方向剩余的行程
    MID_SPAN = LID_SPAN_DEG / 2.0

    return {
        "static": Scenario(
            "static", f"静止开机校准 + {drift_ms}ms 静置 (噪声/漂移量级)", 1001,
            [Segment("校准+静置", CALIB_WINDOW_MS + 1000 + drift_ms)]),
        "close": Scenario(
            "close", "半开基准开机校准 -> 合盖到机械极限 -> 保持", 1002,
            [
                Segment("开机校准(半开)", CALIB_WINDOW_MS + 500, pitch_offset_deg=MID_SPAN),
                Segment("合盖中", 1500, pitch_rate_dps=+MID_SPAN / 1.5, motion=True),
                Segment("合盖静止", 5000),
            ]),
        "open_back": Scenario(
            "open_back", "基准 -> 完全展开(输出0) -> 合盖(输出=摆幅) 双向可重复", 1003,
            [
                # 基准放在"完全展开"姿态: 加速度计读数恰好是 0, 方向可分辨
                Segment("开机校准(全展开)", CALIB_WINDOW_MS + 500),
                Segment("合盖中", 6000, pitch_rate_dps=+LID_SPAN_DEG / 6.0, motion=True),
                Segment("完全合盖静止", 6000),
                Segment("展开回程", 6000, pitch_rate_dps=-LID_SPAN_DEG / 6.0, motion=True),
                Segment("回到全展开静止", 6000),
            ]),
        "full_180": Scenario(
            "full_180", "全行程 180 度: 完全合盖(-90) -> 完全展开(+90), 暴露加速度计端点奇异性", 1016,
            [
                # 基准在"完全合盖": 传感器俯仰 -90 (加速度计折返读数 +90); 展开 180 度到 +90
                Segment("合盖基准校准", CALIB_WINDOW_MS + 500, pitch_offset_deg=-LID_SPAN_DEG),
                Segment("展开中", 12000, pitch_rate_dps=+LID_SPAN_DEG / 6.0, motion=True),
                Segment("完全展开静止", 8000),
                Segment("合盖回程", 12000, pitch_rate_dps=-LID_SPAN_DEG / 6.0, motion=True),
                Segment("回到合盖静止", 6000),
            ]),
        "jitter": Scenario(
            "jitter", "半开处注入 15Hz±8deg 抖动 vs 前后静置 (滤波对比)", 1004,
            [
                Segment("开机校准(全展开)", CALIB_WINDOW_MS + 500),
                Segment("过渡到半开", 3000, pitch_rate_dps=+MID_SPAN / 3.0, motion=True),
                Segment("静置基线", 4000),
                Segment("抖动", 5000, jitter_amp_deg=8.0, jitter_hz=15.0, motion=True),
                Segment("抖动后静置", 4000),
            ]),
        "jitter_low": Scenario(
            "jitter_low", "半开处 15Hz±45deg 大幅抖动: 验证限幅把角度夹在 [0,180]", 1015,
            [
                # 基准在全展开(0), 摆到半开(45 度)后 ±45 度抖动 => 恰好触及 0 与 90 两端
                Segment("开机校准(全展开)", CALIB_WINDOW_MS + 500),
                Segment("过渡到半开", 3000, pitch_rate_dps=+MID_SPAN / 3.0, motion=True),
                Segment("静置基线", 3000),
                Segment("大幅抖动", 4000, jitter_amp_deg=45.0, jitter_hz=15.0, motion=True),
                Segment("抖动后静置", 3000),
            ]),
        "longpress": Scenario(
            "longpress", "长按 1.2s 只触发一次重校准 (闩锁语义)", 1005,
            [
                Segment("静止", 3500),
                Segment("长按中", 1200, events=[(0, "button_down")]),
                Segment("松开后静止", 4000, events=[(0, "button_up")]),
            ]),
        "longpress_3s": Scenario(
            "longpress_3s", "长按 3.0s 仍然只触发一次 (会被无限重复触发的边界)", 1011,
            [
                Segment("静止", 3500),
                Segment("长按中", 3000, events=[(0, "button_down")]),
                Segment("松开后静止", 2000, events=[(0, "button_up")]),
            ]),
        "shortpress": Scenario(
            "shortpress", "短按 300ms x3: default->calibrate->debug->default", 1006,
            [
                Segment("静止", 3500),
                Segment("按压1", 300, events=[(0, "button_down")]),
                Segment("间隔1", 200, events=[(0, "button_up")]),
                Segment("按压2", 300, events=[(0, "button_down")]),
                Segment("间隔2", 200, events=[(0, "button_up")]),
                Segment("按压3", 300, events=[(0, "button_down")]),
                Segment("间隔3", 4000, events=[(0, "button_up")]),
            ]),
        "calib_motion": Scenario(
            "calib_motion", "校准窗口内合盖: 必须被静止判定拒绝 (保留旧零偏)", 1007,
            [
                Segment("校准中合盖", CALIB_WINDOW_MS + 200, pitch_rate_dps=+LID_SPAN_DEG / 3.0,
                        motion=True),
                Segment("未停止", 2000, pitch_rate_dps=+LID_SPAN_DEG / 3.0, motion=True),
                Segment("停止后静止", 3000),
            ]),
        "drift": Scenario(
            "drift", "注入 2.0dps 陀螺零漂静止 20s: 静止锚定/积分漂移量级 (不重校准)", 1009,
            [
                Segment("开机校准", CALIB_WINDOW_MS + 500),
                Segment("零漂静置", 20000, gyro_bias_dps=2.0),
            ]),
        "drift_recal": Scenario(
            "drift_recal", "注入 2.0dps 零漂后长按重校准: 漂移必须被消除", 1012,
            [
                Segment("开机校准", CALIB_WINDOW_MS + 500),
                Segment("零漂静置", 8000, gyro_bias_dps=2.0),
                Segment("长按重校准", 1500, events=[(0, "button_down")], gyro_bias_dps=2.0),
                Segment("重校准后静置", 8000, events=[(0, "button_up")], gyro_bias_dps=2.0),
            ]),
        "noise": Scenario(
            "noise", "串口噪声行注入: 解析器不得崩溃", 1010,
            [Segment("静止", 3000)]),
        "json_schema": Scenario(
            "json_schema", "三种模式下的 JSON 键集合与字段类型 (ok/debug/calibrating)", 1013,
            [
                Segment("静止", 3500),
                Segment("短按->calibrate", 300, events=[(0, "button_down")]),
                Segment("校准中", 600, events=[(0, "button_up")]),
                Segment("短按->debug", 300, events=[(0, "button_down")]),
                Segment("debug 中", 2000, events=[(0, "button_up")]),
                Segment("短按->default", 300, events=[(0, "button_down")]),
                Segment("default 中", 1500, events=[(0, "button_up")]),
            ]),
        "longpress_double": Scenario(
            "longpress_double", "两次长按 (各 1.0s, 中间松开 0.5s) 必须触发 2 次", 1014,
            [
                Segment("静止", 3500),
                Segment("长按1", 1000, events=[(0, "button_down")]),
                Segment("松开", 500, events=[(0, "button_up")]),
                Segment("长按2", 1000, events=[(0, "button_down")]),
                Segment("松开后静置", 3000, events=[(0, "button_up")]),
            ]),
    }


# ==========================================================================
# 5. 运行与统计
# ==========================================================================


@dataclass
class Result:
    scenario: str
    title: str
    final_mode: str
    final_status: str
    static_std: float
    moving_std: float
    drift_per_30s: float
    out_of_range: int
    nan_samples: int
    samples: int
    button_short: int
    button_long: int
    calibrations: int
    accepted_calibrations: int
    serial_lines: int
    oled_frames: int
    parse_ok: int
    parse_bad: int
    final_angle: float
    segment_stds: dict = field(default_factory=dict)
    segment_modes: dict = field(default_factory=dict)
    assertions: list = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.assertions.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def failed(self) -> list:
        return [a for a in self.assertions if not a["ok"]]

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["failed"] = len(self.failed)
        return d


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _slope_per_30s(pairs: list[tuple[int, float]]) -> float:
    n = len(pairs)
    if n < 10:
        return 0.0
    mt = sum(t for t, _ in pairs) / n
    ma = sum(a for _, a in pairs) / n
    den = sum((t - mt) ** 2 for t, _ in pairs)
    if den == 0:
        return 0.0
    return sum((t - mt) * (a - ma) for t, a in pairs) / den * 30000.0


def run_scenario(sc: Scenario) -> Result:
    sensor = SensorSim(seed=sc.seed)
    dev = FirmwareModel(sensor)
    dev.start_calibration("boot")     # setup() 里的开机校准

    seg_angles: dict[str, list[tuple[int, float]]] = {}
    seg_modes: dict[str, set] = {}

    for seg in sc.segments:
        seg_angles[seg.name] = []
        seg_modes[seg.name] = set()
        if seg.gyro_bias_dps:
            sensor.gyro_bias_dps = seg.gyro_bias_dps
        if seg.pitch_offset_deg:
            sensor.set_pitch(seg.pitch_offset_deg)
        pending = sorted(seg.events)
        ev_idx = 0
        steps = max(1, seg.duration_ms // LOOP_DT_MS)
        for i in range(steps):
            local_ms = i * LOOP_DT_MS
            while ev_idx < len(pending) and pending[ev_idx][0] <= local_ms:
                action = pending[ev_idx][1]
                if action == "button_down":
                    dev.button_down()
                elif action == "button_up":
                    dev.button_up()
                else:
                    raise ScenarioError(f"未知按键动作: {action}")
                ev_idx += 1
            rate = seg.pitch_rate_dps
            if seg.jitter_amp_deg:
                # 用 sin: 段开始时位移为 0 (从静置姿态平滑接入), 而不是从振幅峰值跳变
                rate += (seg.jitter_amp_deg * 2 * math.pi * seg.jitter_hz
                         * math.cos(2 * math.pi * seg.jitter_hz * (local_ms / 1000.0)))
            dev.tick(rate)
            # 每段开头 200ms 是姿态/LPF 的收敛过程 (角度从上一段末值过渡到新姿态),
            # 统计时丢弃, 否则收敛过程会被当成"噪声"污染标准差。
            if local_ms >= WARMUP_MS:
                seg_angles[seg.name].append((dev.now, dev.angle_deg))
                seg_modes[seg.name].add(dev.work_mode)

    all_angles = [a for _, a in dev.angle_trace]
    out_of_range = sum(1 for a in all_angles if a < ANGLE_MIN_DEG - 1e-9 or a > ANGLE_MAX_DEG + 1e-9)
    moving_vals = [a for s in sc.segments if s.motion for _, a in seg_angles[s.name]]

    # 静止段只用"最后一段静止"衡量噪声/漂移: 前面的静止段可能仍在收敛 (LPF 滞后),
    # 混在一起会把收敛过程当成噪声, 得到虚高的标准差。
    tail_static = [s.name for s in sc.segments if not s.motion]
    drift_pairs = seg_angles[tail_static[-1]] if tail_static else []
    static_vals = [a for _, a in drift_pairs]

    expect_lines = sc.duration_ms / SERIAL_SEND_INTERVAL
    expect_frames = sc.duration_ms / OLED_UPDATE_INTERVAL

    res = Result(
        scenario=sc.name, title=sc.title,
        final_mode=dev.work_mode, final_status=dev.sys_status,
        static_std=_std(static_vals), moving_std=_std(moving_vals),
        drift_per_30s=_slope_per_30s(drift_pairs),
        out_of_range=out_of_range, nan_samples=dev.nan_samples, samples=len(all_angles),
        button_short=dev.short_press_count, button_long=dev.long_press_count,
        calibrations=len(dev.calibrations),
        accepted_calibrations=sum(1 for c in dev.calibrations if c.accepted),
        serial_lines=len(dev.serial_lines), oled_frames=len(dev.oled_frames),
        parse_ok=0, parse_bad=0, final_angle=dev.angle_deg,
        segment_stds={n: round(_std([a for _, a in v]), 4) for n, v in seg_angles.items()},
        segment_modes={n: sorted(m) for n, m in seg_modes.items()},
    )

    # ---- 通用断言
    res.check("角度始终在 [0,180]", out_of_range == 0, f"越界={out_of_range}/{len(all_angles)}")
    res.check("无 NaN 样本", res.nan_samples == 0, f"nan={res.nan_samples}")
    res.check("模式名合法", dev.work_mode in MODE_NAMES and
              all(m in MODE_NAMES for _, m in dev.mode_trace),
              f"final={dev.work_mode} trace={dev.mode_trace}")
    res.check("状态名合法", dev.sys_status in STATUS_NAMES, f"status={dev.sys_status}")
    res.check("串口 20Hz (50ms)", abs(res.serial_lines - expect_lines) <= 2,
              f"lines={res.serial_lines} 期望≈{int(expect_lines)}")
    res.check("OLED 200ms 刷新节流", abs(res.oled_frames - expect_frames) <= 2,
              f"frames={res.oled_frames} 期望≈{int(expect_frames)}")

    # ---- 全场景通用: 每个 JSON 行都必须是合法 JSON 且字段类型正确
    schema_bad: list[str] = []
    type_bad: list[str] = []
    for line in dev.serial_lines:
        try:
            obj = json.loads(line)
        except Exception as exc:                       # noqa: BLE001
            schema_bad.append(f"非法JSON: {line[:60]} ({exc})")
            continue
        expected = {"angle", "status", "mode", "author"}
        if obj.get("mode") == "debug":
            expected |= {"gyro", "bias", "base", "sp", "lp"}
        if "progress" in obj:
            expected |= {"progress"}
        if set(obj) != expected:
            schema_bad.append(f"键集合={sorted(obj)} 期望={sorted(expected)} mode={obj.get('mode')}")
        if not isinstance(obj.get("angle"), float):
            type_bad.append(f"angle 不是数值: {type(obj.get('angle')).__name__} ({line[:60]})")
        for k in ("gyro", "bias", "base"):
            if k in obj and not isinstance(obj[k], float):
                type_bad.append(f"{k} 不是数值: {type(obj[k]).__name__}")
        for k in ("sp", "lp", "progress"):
            if k in obj and not isinstance(obj[k], int):
                type_bad.append(f"{k} 不是整数: {type(obj[k]).__name__}")
        if not isinstance(obj.get("status"), str) or not isinstance(obj.get("mode"), str):
            type_bad.append("status/mode 不是字符串")
    res.check("串口每行都是合法 JSON 且键集合与固件一致",
              not schema_bad, schema_bad[0] if schema_bad else f"{len(dev.serial_lines)} 行全部通过")
    res.check("angle/gyro/bias/base 是 JSON 数值 (非字符串)",
              not type_bad, type_bad[0] if type_bad else "全部为 float")
    if dev.serial_lines:
        res.check("angle 量化到 1 位小数",
                  all(round(json.loads(x)["angle"], 1) == json.loads(x)["angle"]
                      for x in dev.serial_lines),
                  f"样例: {dev.serial_lines[0]}")

    # ---- 场景专属断言
    ev = dev.calibrations
    if sc.name == "static":
        res.check("开机校准恰好 1 次且被接受", len(ev) == 1 and ev[0].accepted,
                  f"cals={len(ev)} accepted={ev[0].accepted if ev else None}")
        if ev:
            res.check("校准样本数 ≈600 (200Hz x 3s)", 500 <= ev[0].samples <= 660,
                      f"samples={ev[0].samples}")
        res.check("静止段标准差 < 1.0 deg", res.static_std < 1.0, f"std={res.static_std:.4f}")
        res.check("静止 30s 漂移估计 < 2.0 deg", abs(res.drift_per_30s) < 2.0,
                  f"drift30s={res.drift_per_30s:+.4f} deg")
        res.check("最终模式 default / 状态 ok",
                  res.final_mode == "default" and res.final_status == "ok",
                  f"mode={res.final_mode} status={res.final_status}")
    if sc.name == "close":
        closed = [a for _, a in seg_angles["合盖静止"]]
        # 输出角 = |传感器相对摆幅| (1:1), 半开基准 -> 完全合盖 的摆幅是 LID_SPAN/2
        res.check("合盖到机械极限后输出稳定在摆幅附近 (40~50 deg)",
                  bool(closed) and 40.0 < _mean(closed) < 50.0,
                  f"mean={_mean(closed):.2f} max={max(closed):.2f}" if closed else "无样本")
        res.check("合盖后无跳变/回卷",
                  all(b >= a - 2.0 for a, b in zip(closed, closed[1:])))
        res.check("校准被接受 (静止窗口)", bool(ev) and ev[0].accepted,
                  f"accepted={ev[0].accepted if ev else None} "
                  f"var={ev[0].variance if ev else 0:.2f} meanAbsGyro={ev[0].mean_abs_gyro if ev else 0:.2f}")
    if sc.name == "open_back":
        res.check("合盖段角度单调上升到摆幅上限",
                  _monotone_ratio([a for _, a in seg_angles["合盖中"]], +1) > 0.95,
                  f"ratio={_monotone_ratio([a for _, a in seg_angles['合盖中']], +1):.3f}")
        res.check("展开回程单调下降回 0",
                  _monotone_ratio([a for _, a in seg_angles["展开回程"]], -1) > 0.95,
                  f"ratio={_monotone_ratio([a for _, a in seg_angles['展开回程']], -1):.3f}")
        closed2 = [a for _, a in seg_angles["完全合盖静止"]]
        # 注意: 基准在"完全展开"(俯仰0) 时, 合盖方向只能走到俯仰+90,
        # 恰好落在加速度计可分辨区间的边界, 因此输出上限是 90 而非 180。
        # 这是 1:1 映射 + atan2±90 量程的必然结果, 见 tools/verify_pitch_range.py。
        res.check("完全合盖后输出到达 1:1 行程上限 (~90)",
                  bool(closed2) and 85.0 < _mean(closed2) < 95.0,
                  f"mean={_mean(closed2):.2f}" if closed2 else "无样本")
        opened = [a for _, a in seg_angles["回到全展开静止"]]
        res.check("回到完全展开后输出回到 0", bool(opened) and max(opened) < 1.0,
                  f"max={max(opened):.2f}" if opened else "无样本")
        res.check("双向行程无越界", res.out_of_range == 0, f"越界={res.out_of_range}")
        res.check("双向可重复 (两端极差 < 1 deg)",
                  bool(opened) and bool(closed2)
                  and (max(opened) - min(opened)) < 1.0
                  and (max(closed2) - min(closed2)) < 1.0,
                  f"opened 极差={max(opened) - min(opened):.3f} "
                  f"closed 极差={max(closed2) - min(closed2):.3f}")
    if sc.name == "full_180":
        opened = [a for _, a in seg_angles["完全展开静止"]]
        back = [a for _, a in seg_angles["回到合盖静止"]]
        res.check("全 180 度行程无越界/无 NaN", res.out_of_range == 0 and res.nan_samples == 0,
                  f"越界={res.out_of_range} nan={res.nan_samples}")
        res.check("展开到 +90 后输出达到 180 (1:1 行程闭合)",
                  bool(opened) and max(opened) > 175.0,
                  f"max={max(opened):.2f}" if opened else "无样本")
        res.check("回到合盖后输出回到 0",
                  bool(back) and min(back) < 5.0,
                  f"min={min(back):.2f}" if back else "无样本")
        res.check("全程单调不反向 (合盖方向单调不减, 回程单调不增)",
                  _monotone_ratio([a for _, a in seg_angles["展开中"]], +1) > 0.95
                  and _monotone_ratio([a for _, a in seg_angles["合盖回程"]], -1) > 0.95,
                  f"up={_monotone_ratio([a for _, a in seg_angles['展开中']], +1):.3f} "
                  f"down={_monotone_ratio([a for _, a in seg_angles['合盖回程']], -1):.3f}")
    if sc.name == "jitter":
        res.check("抖动段方差 >> 静止段方差", res.moving_std > res.static_std * 5.0,
                  f"moving={res.moving_std:.4f} static={res.static_std:.4f}")
        res.check("抖动段未被限幅削平 (8deg 幅值远小于到端点距离)",
                  res.out_of_range == 0 and min(a for _, a in seg_angles["抖动"]) > 20.0,
                  f"min={min(a for _, a in seg_angles['抖动']):.2f}")
        res.check("抖动后角度回复到静置基线的 5deg 内",
                  abs(_mean([a for _, a in seg_angles['抖动后静置']])
                      - _mean([a for _, a in seg_angles['静置基线']])) < 5.0,
                  f"after={_mean([a for _, a in seg_angles['抖动后静置']]):.3f} "
                  f"before={_mean([a for _, a in seg_angles['静置基线']]):.3f}")
    if sc.name == "jitter_low":
        res.check("大幅抖动下角度仍被夹在 [0,180]",
                  res.out_of_range == 0, f"越界={res.out_of_range}")
        jit = [a for _, a in seg_angles["大幅抖动"]]
        base_mean = _mean([a for _, a in seg_angles["静置基线"]])
        res.check("大幅抖动把角度推到接近两端 (触及 <=1/4 与 >=3/4 量程)",
                  bool(jit) and min(jit) < 22.5 and max(jit) > 67.5,
                  f"min={min(jit):.2f} max={max(jit):.2f} (基准均值={base_mean:.2f})")
        res.check("抖动结束后回到静置量级",
                  abs(_mean([a for _, a in seg_angles['抖动后静置']]) - base_mean) < 10.0,
                  f"after={_mean([a for _, a in seg_angles['抖动后静置']]):.2f} before={base_mean:.2f}")
    if sc.name == "longpress":
        res.check("长按事件恰好 1 次", res.button_long == 1, f"long={res.button_long}")
        res.check("长按未产生短按", res.button_short == 0, f"short={res.button_short}")
        res.check("长按触发 1 次重校准 (含开机共 2)",
                  len(ev) == 2 and ev[1].reason == "long press", f"cals={[c.reason for c in ev]}")
        res.check("长按后进入 calibrate 模式", ev and dev.work_mode in ("calibrate", "default"),
                  f"mode={dev.work_mode}")
    if sc.name == "shortpress":
        res.check("短按恰好 3 次", res.button_short == 3, f"short={res.button_short}")
        res.check("长按 0 次", res.button_long == 0, f"long={res.button_long}")
        modes = [m for _, m in dev.mode_trace]
        res.check("模式轮换 default->calibrate->debug->default",
                  modes[:3] == ["calibrate", "debug", "default"], f"trace={modes}")
        res.check("短按进校准 => 额外 1 次校准 (含开机共 2)", len(ev) == 2, f"cals={len(ev)}")
    if sc.name == "calib_motion":
        res.check("校准内的运动被拒绝", bool(ev) and not ev[0].accepted,
                  f"accepted={ev[0].accepted if ev else None} "
                  f"var={ev[0].variance if ev else 0:.1f} maxgyro={ev[0].mean_abs_gyro if ev else 0:.2f}")
        res.check("被拒绝后零偏保持 0 (未写入错误值)",
                  bool(ev) and abs(ev[0].bias_after) < 1e-6, f"bias={ev[0].bias_after if ev else None}")
        res.check("被拒绝后基准未被污染",
                  bool(ev) and abs(ev[0].baseline_after - ev[0].baseline_before) < 1e-6,
                  f"before={ev[0].baseline_before:.3f} after={ev[0].baseline_after:.3f}" if ev else "")
    if sc.name == "drift":
        res.check("注入 2.0dps 零漂后静止 20s 的角度漂移 < 3 deg (静止锚定在压)",
                  abs(res.drift_per_30s) < 3.0, f"drift30s={res.drift_per_30s:+.4f}")
        res.check("零漂期间无越界", res.out_of_range == 0, f"越界={res.out_of_range}")
    if sc.name == "drift_recal":
        ev_long = [c for c in ev if c.reason == "long press"]
        res.check("长按触发重校准", len(ev_long) == 1, f"long-press 校准={len(ev_long)}")
        res.check("重校准测得的零漂接近注入值 2.0 dps (误差<0.15)",
                  bool(ev_long) and abs(dev.gyro_bias_dps - 2.0) < 0.15,
                  f"gyro_bias={dev.gyro_bias_dps:.4f} dps (注入 2.0)")
        res.check("重校准后 30s 漂移估计收敛 (<5 deg)",
                  abs(res.drift_per_30s) < 5.0, f"drift30s={res.drift_per_30s:+.4f}")
        res.check("全程无越界", res.out_of_range == 0, f"越界={res.out_of_range}")
    if sc.name == "noise":
        ok = bad = 0
        for line in dev.serial_lines:
            p = parse_device_line(line)
            if p and "angle" in p:
                ok += 1
            else:
                bad += 1
        res.parse_ok, res.parse_bad = ok, bad
        res.check("全部合法 JSON 行解析成功", bad == 0, f"bad={bad}")
        noise = make_noise_lines()
        res.check("噪声行全部安全处理",
                  all((parse_device_line(n) is None) or isinstance(parse_device_line(n)["angle"], float)
                      for n in noise), f"noise={len(noise)}")
        res.check("固件 # 日志行被忽略", parse_device_line("# calibration started: boot") is None)
        res.check("angle 字符串形态可解析",
                  parse_device_line('{"angle":"45.3","status":"ok","mode":"default","author":"x"}')
                  == {"angle": 45.3, "status": "ok", "mode": "default", "author": "x"})
        res.check("angle 数字形态同样可解析",
                  parse_device_line('{"angle":45.3}') == {"angle": 45.3})
    if sc.name == "longpress_double":
        res.check("两次长按 -> 恰好 2 次", res.button_long == 2, f"long={res.button_long}")
        res.check("两次长按 -> 恰好 2 次校准 (含开机共 3)",
                  len(ev) == 3, f"cals={[c.reason for c in ev]}")
        res.check("长按不产生短按事件", res.button_short == 0, f"short={res.button_short}")
    if sc.name == "longpress_3s":
        res.check("长按 3.0s 仍然只触发 1 次 (闩锁生效)", res.button_long == 1,
                  f"long={res.button_long}")
        res.check("长按 3.0s 只启动 1 次校准 (含开机共 2)", len(ev) == 2,
                  f"cals={len(ev)}")
    if sc.name == "json_schema":
        modes_seen = {json.loads(x)["mode"] for x in dev.serial_lines}
        statuses_seen = {json.loads(x)["status"] for x in dev.serial_lines}
        res.check("三种模式都出现过", modes_seen >= {"default", "calibrate", "debug"},
                  f"modes={sorted(modes_seen)}")
        res.check("出现过 calibrating 状态",
                  "calibrating" in statuses_seen, f"statuses={sorted(statuses_seen)}")
        dbg = [json.loads(x) for x in dev.serial_lines if json.loads(x)["mode"] == "debug"]
        ok_lines = [json.loads(x) for x in dev.serial_lines
                    if json.loads(x)["mode"] == "default" and "progress" not in json.loads(x)]
        cal_lines = [json.loads(x) for x in dev.serial_lines if "progress" in json.loads(x)]
        dbg_ok = [d for d in dbg if "progress" not in d]
        res.check("debug 行键集合 = 4 基础 + gyro/bias/base/sp/lp (未在校准时)",
                  bool(dbg_ok) and all(
                      set(d) == {"angle", "status", "mode", "author",
                                 "gyro", "bias", "base", "sp", "lp"} for d in dbg_ok),
                  f"样例={dbg_ok[0] if dbg_ok else None}")
        res.check("default(ok) 行键集合 = 4 个基础键 (未在校准时)",
                  bool(ok_lines) and all(set(d) == {"angle", "status", "mode", "author"}
                                         for d in ok_lines),
                  f"样例={ok_lines[0] if ok_lines else None}")
        res.check("校准行进 progress 的同时 mode 仍合法",
                  bool(cal_lines) and all(d["mode"] in MODE_NAMES for d in cal_lines),
                  f"校准行数={len(cal_lines)} 样例={cal_lines[0] if cal_lines else None}")
        res.check("校准行带 progress 字段",
                  bool(cal_lines) and all(0 <= d["progress"] <= 100 for d in cal_lines),
                  f"样例={cal_lines[0] if cal_lines else None}")
        if ok_lines:
            res.check("REGRESSION: angle 是 float 而不是 str",
                      isinstance(ok_lines[0]["angle"], float),
                      f"angle={ok_lines[0]['angle']!r} ({type(ok_lines[0]['angle']).__name__})")
    return res


def _monotone_ratio(values: list[float], direction: int) -> float:
    """direction=+1 求上升比例, -1 求下降比例 (允许 0.5 度回退噪声)。"""
    if len(values) < 2:
        return 1.0
    good = sum(1 for a, b in zip(values, values[1:]) if (b - a) * direction >= -0.5)
    return good / (len(values) - 1)


# ==========================================================================
# 6. 与 .ino 常量交叉核对
# ==========================================================================

FIRMWARE_CONSTANTS = [
    ("COMP_FILTER_ALPHA", COMP_FILTER_ALPHA),
    ("OLED_UPDATE_INTERVAL", OLED_UPDATE_INTERVAL),
    ("SERIAL_SEND_INTERVAL", SERIAL_SEND_INTERVAL),
    ("CALIB_WINDOW_MS", CALIB_WINDOW_MS),
    ("CALIB_TIMEOUT_MS", CALIB_TIMEOUT_MS),
    ("CALIB_GYRO_MAX_DPS", CALIB_GYRO_MAX_DPS),
    ("CALIB_VAR_MAX", CALIB_VAR_MAX),
    ("STILL_BASE_TRACK", STILL_BASE_TRACK),
    ("ACCEL_TRUST_STILL", ACCEL_TRUST_STILL),
    ("ACCEL_TRUST_MOVING", ACCEL_TRUST_MOVING),
    ("STILL_ABS_DPS", STILL_ABS_DPS),
    ("BUTTON_DEBOUNCE_MS", BUTTON_DEBOUNCE_MS),
    ("BUTTON_LONG_MS", BUTTON_LONG_MS),
    ("BUTTON_SHORTMIN_MS", BUTTON_SHORTMIN_MS),
    ("ANGLE_MIN_DEG", ANGLE_MIN_DEG),
    ("ANGLE_MAX_DEG", ANGLE_MAX_DEG),
    ("ANGLE_LPF_ALPHA", ANGLE_LPF_ALPHA),
    ("SERIAL_BAUD", SERIAL_BAUD),
]


def strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释 (保留字符串字面量), 用于常量抽取。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_firmware_constants(path: str) -> tuple[list[tuple[str, bool, str]], bool]:
    out: list[tuple[str, bool, str]] = []
    if not os.path.isfile(path):
        out.append(("固件文件存在", False, f"未找到 {path}"))
        return out, False
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        out.append(("固件为无 BOM 的 UTF-8", False, "检测到 UTF-8 BOM"))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        out.append(("固件为 UTF-8 文本", False, f"解码失败: {exc}"))
        return out, True
    if not raw.startswith(b"\xef\xbb\xbf"):
        out.append(("固件为无 BOM 的 UTF-8", True, "UTF-8 无 BOM"))

    code = strip_comments(text)
    for name, expect in FIRMWARE_CONSTANTS:
        m = re.search(rf"\b{name}\s*=\s*([0-9.]+)f?\s*;", code)
        if not m:
            out.append((f"{name} 存在", False, "未找到该常量"))
            continue
        actual = float(m.group(1))
        out.append((f"{name} == {expect}", abs(actual - expect) < 1e-6,
                    f"固件={actual} 仿真={expect}"))
    modes = [m for m in MODE_NAMES if f'"{m}"' in code]
    out.append(("模式名 default/calibrate/debug 齐备", len(modes) == 3, f"找到={modes}"))
    out.append(("无 delay( 调用", not re.search(r"\bdelay\s*\(", code), "见 verify_firmware.ps1"))
    out.append(("JSON 走 serializeJson", "serializeJson" in code, "serializeJson"))
    return out, True


# ==========================================================================
# 7. 输出
# ==========================================================================


def print_result(res: Result) -> None:
    print("-" * 78)
    print(f"[{res.scenario}] {res.title}")
    print(f"  最终模式={res.final_mode} 状态={res.final_status} 最终角度={res.final_angle:.3f}")
    print(f"  样本={res.samples} 越界={res.out_of_range} NaN={res.nan_samples}")
    print(f"  静止段标准差={res.static_std:.4f} deg  运动段标准差={res.moving_std:.4f} deg")
    print(f"  末段静止 30s 漂移估计={res.drift_per_30s:+.4f} deg")
    print(f"  按键: 短按={res.button_short} 长按={res.button_long}  "
          f"校准={res.calibrations}(接受 {res.accepted_calibrations})")
    print(f"  串口行={res.serial_lines} OLED帧={res.oled_frames} "
          f"解析OK={res.parse_ok} 解析NG={res.parse_bad}")
    print("  分段标准差: " + ", ".join(f"{k}={v:.3f}" for k, v in res.segment_stds.items()))
    for a in res.assertions:
        flag = "PASS" if a["ok"] else "FAIL"
        detail = f"  ({a['detail']})" if a["detail"] else ""
        print(f"    [{flag}] {a['name']}{detail}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="WinDuo 设备行为仿真器 (固件逻辑复刻)")
    ap.add_argument("--scenario", default="all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--quick", action="store_true", help="缩短静置/漂移窗口")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--check-firmware", default=None)
    args = ap.parse_args()

    scenarios = build_scenarios(quick=args.quick)

    if args.list:
        for name, sc in scenarios.items():
            print(f"{name:14s} {sc.title}  时长={sc.duration_ms}ms")
        return 0

    if args.check_firmware:
        checks, exists = check_firmware_constants(args.check_firmware)
        print("=" * 78)
        print(f"固件常量交叉核对: {args.check_firmware} (存在={exists})")
        bad = 0
        for name, ok, detail in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}  -- {detail}")
            bad += 0 if ok else 1
        print(f"结果: {len(checks) - bad} PASS / {bad} FAIL")
        return 1 if bad else 0

    selected = list(scenarios) if args.scenario == "all" else [args.scenario]
    for name in selected:
        if name not in scenarios:
            print(f"未知场景: {name} (可用: {', '.join(scenarios)})", file=sys.stderr)
            return 2

    print("=" * 78)
    print("WinDuo / EthanMaven 设备行为仿真 (固件逻辑逐行复刻)")
    print(f"参数: alpha={COMP_FILTER_ALPHA} 采样/姿态节流={LOOP_DT_MS}ms 校准={CALIB_WINDOW_MS}ms "
          f"超时={CALIB_TIMEOUT_MS}ms 静止阈值={CALIB_GYRO_MAX_DPS}dps/var<={CALIB_VAR_MAX} "
          f"串口={SERIAL_SEND_INTERVAL}ms OLED={OLED_UPDATE_INTERVAL}ms "
          f"按键={BUTTON_SHORTMIN_MS}~{BUTTON_LONG_MS}ms 角度=[{ANGLE_MIN_DEG:.0f},{ANGLE_MAX_DEG:.0f}]")

    results = []
    total_fail = 0
    for name in selected:
        res = run_scenario(scenarios[name])
        print_result(res)
        results.append(res)
        total_fail += len(res.failed)

    print("=" * 78)
    print(f"汇总: {len(results)} 场景 / {sum(len(r.assertions) for r in results)} 断言 / "
          f"{total_fail} FAIL")
    if total_fail:
        for r in results:
            for a in r.failed:
                print(f"  FAIL [{r.scenario}] {a['name']} -- {a['detail']}")
    else:
        print("全部断言通过")

    if args.json_out:
        payload = {
            "firmware_constants": {k: v for k, v in FIRMWARE_CONSTANTS},
            "results": [r.as_dict() for r in results],
            "total_fail": total_fail,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"已写出 JSON: {args.json_out}")

    return 1 if total_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ScenarioError as exc:
        print(f"场景配置错误: {exc}", file=sys.stderr)
        sys.exit(2)
