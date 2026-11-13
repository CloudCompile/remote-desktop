# Performance

Reproduce with one command (issue #37):

```sh
python3 scripts/benchmark.py --url http://127.0.0.1:8090 \
    --user admin --password ... --duration 10 --audio 5
```

Scenarios: `GET /api/health` (public, cheapest), `GET /core/rfb.js`
(static + ETag), `GET /api/stats` (includes a 0.25s CPU sample), and
`GET /api/audio.raw` (live PCM through ffmpeg).

## Baseline

Measured with `scripts/benchmark.py --duration 3 --audio 3`, 8 HTTP
workers, Python 3.12, on a 4-vCPU Azure instance (Nov 2026):

| Scenario | Throughput | p50 | p95 | p99 |
|---|---|---|---|---|
| health | ~1700 req/s | <1 ms | <1 ms | <1 ms |
| static (rfb.js, 105 KB) | ~1800 req/s (~600 MB/s) | <1 ms | <1 ms | <1 ms |
| stats | ~31 req/s | ~300 ms | ~300 ms | ~300 ms |
| audio.raw (1 stream) | ~165 KiB/s (= 44.1 kHz s16le stereo PCM rate) | — | — | — |

The dominant server overhead for interactive use is the JSON `stats`
sample (0.25 s busy-wait by design); the byte paths (static, websocket
proxy, audio) are effectively kernel-bound.

## Regression budget for future PRs

- `health` and `static` must stay above **1000 req/s** with 8 workers.
- `stats` p95 must stay under **400 ms** (the sample is 250 ms).
- Websocket proxy overhead: a 1 KiB round-trip through `/websockify`
  must stay under **5 ms** p95 (see the byte-fidelity integration test).
- Audio: one stream must sustain ≥ **140 KiB/s** (the PCM rate minus a
  small margin); 3 parallel streams must not starve HTTP scenarios.

Numbers are runner-dependent; compare against a run on the same machine,
not against this table.
