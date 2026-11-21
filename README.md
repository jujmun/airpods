# airpods

A macOS pointer that follows where you look. Clench your jaw to left-click. Press `\` to pause and resume tracking.

AirPods supply the jaw signal through headphone motion. The cursor itself comes from the camera: Vision face landmarks, a short calibration, and a ridge fit of pupil position inside each eye plus face pose.

Requires macOS 14, AirPods that report headphone motion, and a camera.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/jujmun/airpods/main/get.sh | sh
```

That clones into `~/.airpods` and runs the installer below; run it again to update. Options pass
through, so `curl -fsSL ... | sh -s -- --uninstall` removes what it created.

Or from a clone, if you would rather see the scripts first:

```bash
git clone https://github.com/jujmun/airpods.git
cd airpods
./install.sh          # builds the app, installs the Python bits, adds `airpods-scroll`
airpods-scroll        # starts the motion stream if needed, then scrolls hands-free
```

`install.sh` needs macOS 14+, Xcode Command Line Tools and Python 3.10+. It keeps everything out of
the system: a venv under `~/.local/share/airpods-scroll`, one command in `~/.local/bin`. It asks
macOS for Accessibility, because sending scroll events needs it, and the app asks for Motion &
Fitness on first launch. `./install.sh --uninstall` removes both.

Options pass through: `airpods-scroll --clench --no-nod`.

While it runs, `airpods-scroll --toggle` pauses and resumes gestures without stopping the stream,
`airpods-scroll --stop` ends it, and Control-C does the same. The stream the launcher started is
stopped with it, and a pause never replays gestures that happened while you were paused.

## Run from a checkout

```bash
./build.sh
./airpods.app/Contents/MacOS/airpods
```

`build.sh` compiles a release binary, wraps it in `airpods.app`, and ad-hoc signs it. Each rebuild changes that signature, so macOS may ask for Camera, Motion & Fitness, and Accessibility again. Allow this app, not only the terminal.

On launch, look at each yellow dot until calibration finishes (25 dots). Stay in the same seat afterward. Control-C quits.

| Input | Action |
| --- | --- |
| Where you look | Moves the pointer |
| Jaw clench | Left click |
| `\` | Pause or resume tracking |

Clicks are ignored while tracking is paused, and until calibration finishes.

## Motion stream

Headphone motion is also sent as UDP text, `yaw,pitch,roll,ax,ay,az,rx,ry,rz`, to `127.0.0.1:9999`.
The last three are rotation rate in rad/s. Builds before this sent only the first six fields, and
`game/stream.py` reads either.

```bash
./airpods.app/Contents/MacOS/airpods --stream-only
./airpods.app/Contents/MacOS/airpods --stream-only 127.0.0.1 9999
```

`--stream-only` skips the camera and the pointer. The tools in `game/` read that stream:

```bash
pip install -r game/requirements.txt
python game/plot_live.py
python game/record.py --label clench --output data/clench.csv
```

## Hands-free scrolling

`game/scroll.py` turns head gestures into scroll events, so a feed advances without touching
anything: nod down for the next video, nod up to go back. It reads the same motion stream, so it
needs `--stream-only` running and nothing else from this app.

```bash
pip install pyobjc-framework-Quartz          # macOS only
./airpods.app/Contents/MacOS/airpods --stream-only &
python game/scroll.py
```

Hold still for the first 1.5 seconds: that is where "neutral" comes from, so the gesture is relative
to how you are actually sitting. Then nod. `--nod 7` makes it more sensitive, `--keys` sends arrow
keys instead of wheel events, `--reverse` flips the scroll direction.

A jaw clench can advance the feed too:

```bash
python game/scroll.py --clench --no-nod    # clench only, no head movement needed
python game/scroll.py --clench             # nods as before, plus clench to advance
```

The scroller uses the app's clench thresholds (`Sources/airpods/jaw.swift`): a spike above the
median of a 0.12 s window, in acceleration (`--clench-impulse 0.008`) or rotation rate
(`--clench-gyro 0.45`), with 0.4 s between clenches. Starting or stopping a head turn spikes too,
so a spike only counts if the head stayed within `--clench-still 2` degrees from 0.3 s before it to
0.1 s after it. Clenches register about 0.1 s late as a result. `--clench-debug` prints every spike
and why it was kept or rejected.

Nothing needs AirPods to try it:

```bash
python game/scroll.py --selftest    # synthetic nods, asserts the gestures it emits
python game/scroll.py --simulate    # watch a synthetic head drive it, sends no events
```

Sending events needs Accessibility permission for the terminal (System Settings -> Privacy &
Security -> Accessibility). It scrolls whatever window has focus, so it works on the web players
(TikTok, Reels, Shorts), not in the phone apps.
