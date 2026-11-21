"""Hands-free scrolling: nod or clench to advance, nod up to go back.

Reads the same UDP motion stream as the other tools in this folder:

    ./airpods.app/Contents/MacOS/airpods --stream-only
    python game/scroll.py                 # nod down = next, nod up = back
    python game/scroll.py --clench        # a jaw clench advances too
    python game/scroll.py --clench --no-nod

The gesture logic is a function of the motion samples alone, so it can be exercised
without AirPods or a Mac:

    python game/scroll.py --selftest     # synthetic nods and clenches, asserts the events
    python game/scroll.py --simulate     # print what it would do, no events sent
    python game/scroll.py --demo         # read the real stream, print, send nothing

Sending scroll events needs macOS and Accessibility permission for the terminal:

    pip install pyobjc-framework-Quartz
    System Settings -> Privacy & Security -> Accessibility
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys

from control import JAW_ACCEL_IMPULSE, JAW_GYRO_IMPULSE, JAW_STILL_DEG, JawClench
from stream import MotionStream, Sample

DEFAULT_NOD_DEG = 10.0
DEFAULT_CALIBRATE_S = 1.5
DEFAULT_REFRACTORY_S = 0.7
DEFAULT_RETURN_FRACTION = 0.45
SCROLL_LINES = 12

# Toggled by SIGUSR1 (the launcher's `--toggle`). Gestures are still detected while paused,
# they just stop producing events, so resuming does not replay whatever happened in between.
PAUSED = False


def _toggle_pause(signum, frame):
    global PAUSED
    PAUSED = not PAUSED
    print("paused" if PAUSED else "resumed", flush=True)


class NodDetector:
    """Turns a stream of pitch samples into +1 (next), -1 (back) or 0 (nothing).

    Neutral pitch is the mean of the first `calibrate_s` seconds, so the gesture is
    relative to however you happen to be sitting. A nod fires once, then the head has
    to come back within `return_fraction` of the way to neutral before it can fire again,
    and two gestures are never closer than `refractory_s`.
    """

    def __init__(
        self,
        nod_deg: float = DEFAULT_NOD_DEG,
        calibrate_s: float = DEFAULT_CALIBRATE_S,
        refractory_s: float = DEFAULT_REFRACTORY_S,
        return_fraction: float = DEFAULT_RETURN_FRACTION,
    ) -> None:
        self.nod = abs(nod_deg)
        self.calibrate_s = calibrate_s
        self.refractory_s = refractory_s
        self.return_deg = self.nod * return_fraction
        self.neutral: float | None = None
        self._calibrating_since: float | None = None
        self._sum = 0.0
        self._n = 0
        self._armed = True
        self._last = -math.inf

    @property
    def calibrated(self) -> bool:
        return self.neutral is not None

    def feed(self, pitch: float, t: float) -> int:
        if self.neutral is None:
            if self._calibrating_since is None:
                self._calibrating_since = t
            self._sum += pitch
            self._n += 1
            if t - self._calibrating_since >= self.calibrate_s:
                self.neutral = self._sum / self._n
            return 0

        tilt = math.degrees(pitch - self.neutral)  # positive = chin down

        if self._armed and t - self._last >= self.refractory_s:
            if tilt > self.nod:
                self._armed, self._last = False, t
                return 1
            if tilt < -self.nod:
                self._armed, self._last = False, t
                return -1
        elif not self._armed and abs(tilt) < self.return_deg:
            self._armed = True
        return 0


def post_scroll(lines: int) -> None:
    import Quartz  # imported here so the detector and --selftest run anywhere

    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def post_key(code: int) -> None:
    import Quartz

    for down in (True, False):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, code, down))


def act(direction: int, keys: bool, reverse: bool) -> None:
    step = SCROLL_LINES if reverse else -SCROLL_LINES
    if keys:
        post_key(125 if direction > 0 else 126)  # down / up arrow
    else:
        post_scroll(step * direction)


def read_stream(host: str, port: int):
    stream = MotionStream(host, port)
    while True:
        sample = stream.read()
        if sample is not None:
            yield sample


def simulate(step_s: float = 0.02, cycles: int = 3, clench: bool = True, turns: bool = True):
    """A head that holds still, nods down, returns, nods up, turns side to side, then clenches.

    Moves carry their rotation rate and a small jolt as they start, like the real sensor.
    """
    t = 0.0

    def move(axis: int, a: float, b: float, dur: float):
        """Rotate one axis (0 yaw, 1 pitch) from a to b degrees, with its rate and a starting jolt."""
        nonlocal t
        n = max(1, int(dur / step_s))
        rate = math.radians(b - a) / dur
        for i in range(n):
            angle = math.radians(a + (b - a) * (i + 1) / n)
            jolt = 0.05 if i == 0 else 0.0
            attitude = (angle, 0.0) if axis == 0 else (0.0, angle)
            accel = (0.0, jolt) if axis == 0 else (jolt, 0.0)
            gyro = (0.0, rate) if axis == 0 else (rate, 0.0)
            yield Sample(*attitude, 0.0, *accel, 0.0, t, gyro[0], 0.0, gyro[1])
            t += step_s

    def ramp(a: float, b: float, dur: float):
        return move(1, a, b, dur)

    def turn(a: float, b: float, dur: float):
        return move(0, a, b, dur)

    def hold(deg: float, dur: float, spike: float = 0.0):
        nonlocal t
        for _ in range(max(1, int(dur / step_s))):
            yield Sample(0.0, math.radians(deg), 0.0, spike, 0.0, 0.0, t, spike, 0.0, 0.0)
            t += step_s

    yield from hold(0.0, 2.0)            # calibration window
    for _ in range(cycles):
        yield from ramp(0.0, 22.0, 0.6)  # nod down
        yield from ramp(22.0, 0.0, 0.4)  # back to neutral
        yield from hold(0.0, 1.0)
        yield from ramp(0.0, -22.0, 0.6)  # nod up
        yield from ramp(-22.0, 0.0, 0.4)
        yield from hold(0.0, 1.0)
        if turns:
            for a, b, dur in ((0.0, 45.0, 0.5), (45.0, 0.0, 0.5), (0.0, -30.0, 1.0), (-30.0, 0.0, 0.3)):
                yield from turn(a, b, dur)  # fast and slow head turns: must not count as clenches
                yield from hold(0.0, 0.6)
        if clench:
            yield from hold(0.0, 0.06, spike=0.6)  # jaw clench: acceleration, no head movement
            yield from hold(0.0, 1.0)


def gestures(source, nod: "NodDetector | None", clench: "JawClench | None"):
    """Yield (direction, reason) for every gesture in a stream of samples.

    Detectors keep running while paused so their state stays current; only the events stop.
    """
    for s in source:
        direction, reason = 0, ""
        clenched = clench is not None and clench.update(s)  # every sample, so its history has no holes
        if nod is not None:
            direction = nod.feed(s.pitch, s.t)
            reason = "nod"
        if not direction and clenched:
            direction, reason = 1, "clench"
        if direction and not PAUSED:
            yield direction, reason


def selftest() -> int:
    events = list(gestures(simulate(), NodDetector(), JawClench()))
    nods = [d for d, why in events if why == "nod"]
    clenches = [d for d, why in events if why == "clench"]
    ok = nods == [1, -1, 1, -1, 1, -1] and clenches == [1, 1, 1]
    print(f"selftest: nods {nods}, clenches {clenches} — {'ok' if ok else 'FAILED'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Hands-free scrolling from the AirPods motion stream.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--keys", action="store_true", help="send arrow keys instead of scroll events")
    ap.add_argument("--reverse", action="store_true", help="flip the scroll direction")
    ap.add_argument("--nod", type=float, default=DEFAULT_NOD_DEG, help="degrees of tilt that count as a nod")
    ap.add_argument("--clench", action="store_true", help="a jaw clench advances the feed too")
    ap.add_argument("--no-nod", action="store_true", help="ignore head nods (use with --clench)")
    ap.add_argument("--clench-impulse", type=float, default=JAW_ACCEL_IMPULSE,
                    help="acceleration spike that counts as a clench (the Swift app's default)")
    ap.add_argument("--clench-gyro", type=float, default=JAW_GYRO_IMPULSE,
                    help="rotation-rate spike that counts as a clench (the Swift app's default)")
    ap.add_argument("--clench-still", type=float, default=JAW_STILL_DEG,
                    help="degrees the head may move during a clench before it counts as a turn")
    ap.add_argument("--clench-debug", action="store_true",
                    help="print every clench candidate and why it was kept or rejected")
    ap.add_argument("--demo", action="store_true", help="print gestures, send no events")
    ap.add_argument("--simulate", action="store_true", help="use a synthetic head, no AirPods needed")
    ap.add_argument("--selftest", action="store_true", help="check the detectors against synthetic motion")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    nod = None if args.no_nod else NodDetector(nod_deg=args.nod)
    clench = (
        JawClench(
            accel_impulse=args.clench_impulse,
            gyro_impulse=args.clench_gyro,
            still_deg=args.clench_still,
            debug=args.clench_debug,
        )
        if args.clench
        else None
    )
    if nod is None and clench is None:
        ap.error("nothing to detect: drop --no-nod or add --clench")

    signal.signal(signal.SIGUSR1, _toggle_pause)
    source = simulate(clench=args.clench) if args.simulate else read_stream(args.host, args.port)
    print("calibrating: hold still" if not args.simulate else "simulating a head", flush=True)

    for direction, why in gestures(source, nod, clench):
        print(f"{'next' if direction > 0 else 'back'} ({why})", flush=True)
        if not (args.demo or args.simulate):
            act(direction, args.keys, args.reverse)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
