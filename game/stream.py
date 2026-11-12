"""Read AirPods motion datagrams from the Swift bridge."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

HOST = "127.0.0.1"
PORT = 9999


@dataclass(frozen=True)
class Sample:
    yaw: float
    pitch: float
    roll: float
    ax: float
    ay: float
    az: float
    t: float
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0


def parse(payload: bytes, t: float | None = None) -> Sample | None:
    """Parse one `yaw,pitch,roll,ax,ay,az[,rx,ry,rz]` datagram. Return None if it is malformed.

    Rotation rate is optional so older bridges that send six fields still work.
    """
    try:
        parts = payload.decode("utf-8").strip().split(",")
        if len(parts) not in (6, 9):
            return None
        values = [float(part) for part in parts]
    except (UnicodeDecodeError, ValueError):
        return None
    yaw, pitch, roll, ax, ay, az = values[:6]
    rx, ry, rz = values[6:] if len(values) == 9 else (0.0, 0.0, 0.0)
    return Sample(yaw, pitch, roll, ax, ay, az, time.monotonic() if t is None else t, rx, ry, rz)


class MotionStream:
    """Non-blocking UDP reader. One `read` call never waits on the bridge."""

    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            self.sock.bind((host, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.setblocking(False)
        self.host = host
        self.port = self.sock.getsockname()[1]

    def read(self) -> Sample | None:
        """Return the next valid sample, skipping malformed packets."""
        while True:
            try:
                data, _addr = self.sock.recvfrom(2048)
            except BlockingIOError:
                return None
            except (InterruptedError, ConnectionResetError):
                return None
            sample = parse(data)
            if sample is not None:
                return sample

    def drain(self) -> list[Sample]:
        samples: list[Sample] = []
        while True:
            sample = self.read()
            if sample is None:
                return samples
            samples.append(sample)

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> MotionStream:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def open_stream(host: str = HOST, port: int = PORT) -> MotionStream:
    try:
        stream = MotionStream(host, port)
    except OSError as exc:
        raise SystemExit(
            f"Cannot listen on {host}:{port} ({exc}). Stop anything else bound to that port."
        ) from exc
    print(f"listening on {host}:{stream.port}", flush=True)
    return stream
