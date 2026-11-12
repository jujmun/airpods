"""Record a labeled stretch of AirPods motion to CSV."""

from __future__ import annotations

import argparse
import csv
import time
from pathlib import Path

from stream import HOST, PORT, open_stream


def main() -> None:
    parser = argparse.ArgumentParser(description="Record AirPods motion to CSV.")
    parser.add_argument("--label", required=True, help="row label, such as clench or rest")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--output", "-o", required=True, help="CSV path, such as data/clench.csv")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    stream = open_stream(args.host, args.port)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"recording {args.label!r} for {args.seconds:g}s -> {path}", flush=True)

    count = 0
    start: float | None = None
    try:
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["t", "yaw", "pitch", "roll", "ax", "ay", "az", "rx", "ry", "rz", "label"])
            while True:
                samples = stream.drain()
                if not samples:
                    time.sleep(0.01)
                    if start is not None and time.monotonic() - start >= args.seconds:
                        break
                    continue
                for sample in samples:
                    if start is None:
                        start = sample.t
                    elapsed = sample.t - start
                    writer.writerow(
                        [
                            f"{elapsed:.4f}",
                            f"{sample.yaw:.6f}",
                            f"{sample.pitch:.6f}",
                            f"{sample.roll:.6f}",
                            f"{sample.ax:.6f}",
                            f"{sample.ay:.6f}",
                            f"{sample.az:.6f}",
                            f"{sample.rx:.6f}",
                            f"{sample.ry:.6f}",
                            f"{sample.rz:.6f}",
                            args.label,
                        ]
                    )
                    count += 1
                    if elapsed >= args.seconds:
                        break
                handle.flush()
                if start is not None and time.monotonic() - start >= args.seconds:
                    break
    finally:
        stream.close()
    print(f"wrote {count} samples", flush=True)


if __name__ == "__main__":
    main()
