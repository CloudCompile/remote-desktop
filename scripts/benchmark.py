#!/usr/bin/env python3
"""Benchmark the remote-desktop server (#37): HTTP, JSON API, static files
and an audio stream — stdlib only.

Usage:
  python3 scripts/benchmark.py --url http://127.0.0.1:8090 --duration 10
  python3 scripts/benchmark.py --user admin --password secret --audio 3

Scenarios:
  health   GET /api/health     (public liveness, cheapest)
  static   GET /core/rfb.js    (vendored asset, ETag path)
  stats    GET /api/stats      (authed; includes a 0.25s CPU sample)
  audio    GET /api/audio.raw  (N parallel ffmpeg PCM streams, --audio secs)

Prints req/s and latency percentiles (p50/p95/p99) per scenario.
"""

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def _login(base, user, password):
    if not user:
        return None
    req = urllib.request.Request(
        f"{base}/api/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.headers.get("Set-Cookie", "").split(";", 1)[0] or None
    except Exception as e:
        print(f"login failed ({e}); authed scenarios will 401")
        return None


class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = []
        self.errors = 0
        self.bytes_rx = 0

    def add(self, dt, n=0, err=False):
        with self.lock:
            if err:
                self.errors += 1
            else:
                self.latencies.append(dt)
            self.bytes_rx += n

    def report(self, name, wall):
        lat = sorted(self.latencies)
        n = len(lat)
        rq = n / wall if wall > 0 else 0.0
        p50 = _percentile(lat, 50)
        p95 = _percentile(lat, 95)
        p99 = _percentile(lat, 99)
        print(
            f"  {name:<8} {n:>7} reqs  {rq:8.1f} req/s   "
            f"p50={p50:7.1f}ms p95={p95:7.1f}ms p99={p99:7.1f}ms "
            f"errors={self.errors}"
        )
        return {
            "name": name,
            "reqs": n,
            "req_per_s": round(rq, 2),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "errors": self.errors,
            "bytes": self.bytes_rx,
        }


def http_scenario(name, base, cookie, path, duration, workers, meter, stop):
    def worker():
        req = urllib.request.Request(f"{base}{path}", headers={"Cookie": cookie} if cookie else {})
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = r.read()
                meter.add(time.perf_counter() - t0, len(body))
            except Exception:
                meter.add(time.perf_counter() - t0, err=True)
                time.sleep(0.05)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    stop.wait(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    return meter.report(name, time.perf_counter() - t0)


def audio_scenario(name, base, cookie, path, duration, workers, meter, stop):
    """Hold `workers` audio streams open, reading until the deadline."""

    def worker():
        while not stop.is_set():
            req = urllib.request.Request(f"{base}{path}", headers={"Cookie": cookie} if cookie else {})
            try:
                with urllib.request.urlopen(req, timeout=duration + 10) as r:
                    while not stop.is_set():
                        chunk = r.read1(4096)
                        if not chunk:
                            break
                        meter.add(0, len(chunk))
            except Exception:
                meter.add(0, err=True)
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    stop.wait(duration)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    wall = time.perf_counter() - t0
    kbps = meter.bytes_rx / 1024 / wall if wall else 0.0
    print(f"  {name:<8} {meter.bytes_rx:>7} bytes in {wall:6.1f}s  {kbps:8.0f} KiB/s errors={meter.errors}")
    return {
        "name": name,
        "bytes": meter.bytes_rx,
        "seconds": round(wall, 1),
        "kib_per_s": round(kbps),
        "errors": meter.errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--user", default=os.environ.get("RD_USER", ""))
    ap.add_argument("--password", default=os.environ.get("RD_PASSWORD", ""))
    ap.add_argument("--duration", type=float, default=10.0, help="seconds per scenario")
    ap.add_argument("--workers", type=int, default=8, help="HTTP concurrency")
    ap.add_argument("--audio", type=float, default=0.0, help="seconds to hold an audio.raw stream (0 = skip)")
    ap.add_argument("--audio-workers", type=int, default=1)
    ap.add_argument("--static", default="/core/rfb.js")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    print(f"benchmark: {base}  duration={args.duration}s workers={args.workers}")
    cookie = _login(base, args.user, args.password)
    results = []
    for name, path in (("health", "/api/health"), ("static", args.static), ("stats", "/api/stats")):
        stop = threading.Event()
        results.append(http_scenario(name, base, cookie, path, args.duration, args.workers, Meter(), stop))
    if args.audio > 0:
        stop = threading.Event()
        results.append(
            audio_scenario("audio.raw", base, cookie, "/api/audio.raw", args.audio, args.audio_workers, Meter(), stop)
        )
    print("\nJSON:")
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
