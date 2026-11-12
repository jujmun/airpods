"""Neutral pose, dead-zone steer, and jaw-clench detection."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from stream import Sample

DEFAULT_THRESHOLD = 0.02
DEFAULT_DEAD_ZONE = math.radians(5.0)
DEFAULT_WINDOW_S = 0.15
DEFAULT_REFRACTORY_S = 0.40
DEFAULT_ATTITUDE_GATE = math.radians(8.0)
DEFAULT_CALIBRATION_S = 0.5
DEFAULT_SENSITIVITY = 1.0
MIN_CALIBRATION_SAMPLES = 3


def acceleration_magnitude(ax: float, ay: float, az: float) -> float:
    return math.sqrt(ax * ax + ay * ay + az * az)


def wrap_angle(angle: float) -> float:
    """Shortest signed angle, in radians, wrapped into [-pi, pi]."""
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def dead_zone_velocity(theta: float, dead_zone: float, sensitivity: float) -> float:
    """Velocity control: 0 inside the dead zone, then k times the excess angle."""
    if abs(theta) < dead_zone:
        return 0.0
    return sensitivity * (theta - math.copysign(dead_zone, theta))


@dataclass(frozen=True)
class ControlState:
    calibrating: bool
    yaw: float
    pitch: float
    roll: float
    vx: float
    vy: float
    ax: float
    ay: float
    az: float
    magnitude: float
    clench: bool


class Controller:
    """Calibrate a neutral pose, then map yaw/pitch and detect jaw clenches.

    A clench is a short acceleration spike whose attitude barely changes.
    Head turns move yaw, pitch, or roll and are rejected.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        sensitivity: float = DEFAULT_SENSITIVITY,
        dead_zone: float = DEFAULT_DEAD_ZONE,
        window_s: float = DEFAULT_WINDOW_S,
        refractory_s: float = DEFAULT_REFRACTORY_S,
        attitude_gate: float = DEFAULT_ATTITUDE_GATE,
        calibration_s: float = DEFAULT_CALIBRATION_S,
    ) -> None:
        self.threshold = threshold
        self.sensitivity = sensitivity
        self.dead_zone = dead_zone
        self.window_s = window_s
        self.refractory_s = refractory_s
        self.attitude_gate = attitude_gate
        self.calibration_s = calibration_s
        self._cal_samples: list[Sample] = []
        self._cal_start: float | None = None
        self._yaw0 = 0.0
        self._pitch0 = 0.0
        self._roll0 = 0.0
        self._calibrated = False
        self._window: deque[Sample] = deque()
        self._armed = False
        self._prev_t: float | None = None
        self._last_fire = -1e9

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def update(self, sample: Sample) -> ControlState:
        magnitude = acceleration_magnitude(sample.ax, sample.ay, sample.az)
        if self._cal_start is None:
            self._cal_start = sample.t
        if not self._calibrated:
            self._cal_samples.append(sample)
            elapsed = sample.t - self._cal_start
            if elapsed >= self.calibration_s and len(self._cal_samples) >= MIN_CALIBRATION_SAMPLES:
                count = len(self._cal_samples)
                self._yaw0 = sum(item.yaw for item in self._cal_samples) / count
                self._pitch0 = sum(item.pitch for item in self._cal_samples) / count
                self._roll0 = sum(item.roll for item in self._cal_samples) / count
                self._calibrated = True
                self._window.clear()
                self._armed = False
                self._prev_t = None
            else:
                return self._state(sample, magnitude, 0.0, 0.0, 0.0, 0.0, 0.0, False)

        yaw = wrap_angle(sample.yaw - self._yaw0)
        pitch = wrap_angle(sample.pitch - self._pitch0)
        roll = wrap_angle(sample.roll - self._roll0)
        vx = dead_zone_velocity(yaw, self.dead_zone, self.sensitivity)
        vy = dead_zone_velocity(pitch, self.dead_zone, self.sensitivity)
        clench = self._detect_clench(sample)
        return self._state(sample, magnitude, yaw, pitch, roll, vx, vy, clench)

    def _detect_clench(self, sample: Sample) -> bool:
        if self._prev_t is not None and sample.t - self._prev_t > self.window_s:
            self._armed = False
        self._prev_t = sample.t
        self._window.append(sample)
        cutoff = sample.t - self.window_s
        while self._window and self._window[0].t < cutoff:
            self._window.popleft()

        score = max(
            acceleration_magnitude(item.ax, item.ay, item.az) for item in self._window
        )
        attitude_ok = (
            _span(item.yaw for item in self._window) < self.attitude_gate
            and _span(item.pitch for item in self._window) < self.attitude_gate
            and _span(item.roll for item in self._window) < self.attitude_gate
        )
        armed = score >= self.threshold and attitude_ok
        clench = (
            armed
            and not self._armed
            and (sample.t - self._last_fire) >= self.refractory_s
        )
        if clench:
            self._last_fire = sample.t
        self._armed = armed
        return clench

    def _state(
        self,
        sample: Sample,
        magnitude: float,
        yaw: float,
        pitch: float,
        roll: float,
        vx: float,
        vy: float,
        clench: bool,
    ) -> ControlState:
        return ControlState(
            calibrating=not self._calibrated,
            yaw=yaw,
            pitch=pitch,
            roll=roll,
            vx=vx,
            vy=vy,
            ax=sample.ax,
            ay=sample.ay,
            az=sample.az,
            magnitude=magnitude,
            clench=clench,
        )


def _span(values: Iterable[float]) -> float:
    numbers = list(values)
    return max(numbers) - min(numbers)


# Spike thresholds are the same as JawClenchDetector in Sources/airpods/jaw.swift.
JAW_WINDOW_S = 0.12
JAW_REFRACTORY_S = 0.4
JAW_ACCEL_IMPULSE = 0.008
JAW_GYRO_IMPULSE = 0.45
JAW_SETTLE_S = 0.10
JAW_STILL_DEG = 2.0
JAW_LOOKBACK_S = 0.30
# Samples are timed on UDP arrival, so allow this much slack before treating a delay as a gap.
JAW_GAP_S = 0.15
JAW_MIN_SAMPLES = 3


def vector_norm(x: float, y: float, z: float) -> float:
    return math.sqrt(x * x + y * y + z * z)


def _impulse(values: list[float]) -> float:
    """Peak minus the median of the window. A smooth head turn scores near zero."""
    ordered = sorted(values)
    return ordered[-1] - ordered[len(ordered) // 2]


class JawClench:
    """Jaw-clench detector: the Swift app's spike test, plus a check that the head stayed put.

    A spike in acceleration or rotation rate above the rest of a short window starts a
    candidate. Starting or stopping a head turn makes the same spike, so the candidate only
    fires if yaw, pitch and roll each stayed within `still_deg` from `lookback_s` before the
    spike until `settle_s` after it. A clench is a jolt with a still head on both sides.
    If the stream has a gap around the candidate, there is nothing to check, so it is dropped.
    """

    def __init__(
        self,
        accel_impulse: float = JAW_ACCEL_IMPULSE,
        gyro_impulse: float = JAW_GYRO_IMPULSE,
        window_s: float = JAW_WINDOW_S,
        refractory_s: float = JAW_REFRACTORY_S,
        settle_s: float = JAW_SETTLE_S,
        still_deg: float = JAW_STILL_DEG,
        lookback_s: float = JAW_LOOKBACK_S,
        debug: bool = False,
    ) -> None:
        self.accel_impulse = accel_impulse
        self.gyro_impulse = gyro_impulse
        self.window_s = window_s
        self.refractory_s = refractory_s
        self.settle_s = settle_s
        self.still = math.radians(still_deg)
        self.lookback_s = lookback_s
        self.debug = debug
        self._window: deque[tuple[float, float, float]] = deque()
        self._history: deque[Sample] = deque()
        self._armed = False
        self._last_fire = -1e9
        self._pending: float | None = None
        self._pending_why = ""

    def update(self, sample: Sample) -> bool:
        if self._history and sample.t - self._history[-1].t > JAW_GAP_S:
            self._drop_pending("stream gap")
            self._window.clear()
            self._history.clear()
            self._armed = False

        self._window.append(
            (sample.t, vector_norm(sample.ax, sample.ay, sample.az), vector_norm(sample.rx, sample.ry, sample.rz))
        )
        cutoff = sample.t - self.window_s
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        self._history.append(sample)
        oldest = self._pending if self._pending is not None else sample.t
        keep = oldest - self.lookback_s
        while self._history and self._history[0].t < keep:
            self._history.popleft()

        fired = False
        if self._pending is not None and sample.t - self._pending >= self.settle_s:
            fired = self._confirm()
            if not fired:
                self._armed = False  # let a spike that is still going start a fresh candidate

        if len(self._window) < JAW_MIN_SAMPLES:
            return fired
        accel_score = _impulse([a for _, a, _ in self._window])
        gyro_score = _impulse([g for _, _, g in self._window])
        hit = accel_score >= self.accel_impulse or gyro_score >= self.gyro_impulse
        if (
            hit
            and not self._armed
            and self._pending is None
            and (sample.t - self._last_fire) >= self.refractory_s
        ):
            self._pending = sample.t
            self._pending_why = f"accel {accel_score:.3f} gyro {gyro_score:.2f}"
        self._armed = hit
        return fired

    def _confirm(self) -> bool:
        assert self._pending is not None
        since = self._pending - self.lookback_s
        span = [s for s in self._history if s.t >= since]
        if not span or span[0].t - since > JAW_GAP_S:
            self._drop_pending("not enough history before the spike")
            return False
        first = span[0]
        moved = max(
            _span(wrap_angle(item.yaw - first.yaw) for item in span),
            _span(wrap_angle(item.pitch - first.pitch) for item in span),
            _span(wrap_angle(item.roll - first.roll) for item in span),
        )
        ok = moved < self.still
        if ok:
            self._last_fire = self._pending  # refractory runs from the spike, as in jaw.swift
        self._log(f"head moved {math.degrees(moved):.1f} deg -> {'clench' if ok else 'rejected (head moved)'}")
        self._pending = None
        return ok

    def _drop_pending(self, reason: str) -> None:
        if self._pending is not None:
            self._log(f"rejected ({reason})")
        self._pending = None

    def _log(self, verdict: str) -> None:
        if self.debug:
            print(f"  spike {self._pending_why}, {verdict}", flush=True)
