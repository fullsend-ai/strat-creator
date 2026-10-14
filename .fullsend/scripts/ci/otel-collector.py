#!/usr/bin/env python3
"""Lightweight OTLP HTTP/JSON receiver that writes payloads to a log file.

Listens on localhost:4318 and accepts OTLP HTTP/JSON exports for metrics
and logs, writing them to a structured log file for later analysis.

Also tracks token usage over time and writes a rate file for live
tokens/sec display.
"""
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

LOG_FILE = os.environ.get("OTEL_LOG_FILE", "/tmp/claude-otel.jsonl")
RATE_FILE = os.environ.get("OTEL_RATE_FILE", "/tmp/claude-otel-rate.json")

# Rolling window for tokens/sec calculation
_token_samples = []  # [(timestamp, total_tokens)]
_WINDOW_SECS = 60


class OTLPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", errors="replace")}

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "path": self.path,
            "payload": payload,
        }
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Track token usage for rate calculation
        if "/v1/metrics" in self.path:
            _update_token_rate(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"partialSuccess":{}}')

    def log_message(self, format, *args):
        pass  # suppress request logging


def _update_token_rate(payload):
    """Extract token totals from metrics payload and update rate file."""
    global _token_samples
    now = time.monotonic()
    total = 0
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                if metric.get("name") == "claude_code.token.usage":
                    data = metric.get("sum", metric.get("gauge", {}))
                    for dp in data.get("dataPoints", []):
                        total += dp.get("asDouble", dp.get("asInt", 0))
    if total <= 0:
        return

    _token_samples.append((now, total))
    # Trim window
    cutoff = now - _WINDOW_SECS
    _token_samples = [(t, v) for t, v in _token_samples if t >= cutoff]

    # Compute rate from window
    rate = 0.0
    if len(_token_samples) >= 2:
        dt = _token_samples[-1][0] - _token_samples[0][0]
        dv = _token_samples[-1][1] - _token_samples[0][1]
        if dt > 0:
            rate = dv / dt

    # Atomic write
    tmp = RATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"total": total, "rate": rate, "ts": time.time()}, f)
    os.replace(tmp, RATE_FILE)


def main():
    port = int(os.environ.get("OTEL_COLLECTOR_PORT", "4318"))
    server = HTTPServer(("127.0.0.1", port), OTLPHandler)
    # Clean exit on SIGTERM
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"OTLP collector listening on 127.0.0.1:{port}, writing to {LOG_FILE}",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
