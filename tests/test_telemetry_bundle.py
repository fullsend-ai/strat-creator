"""telemetry_bundle.py: the agentic-telemetry/v1 folder every pipeline publishes.

Shared by strat-creator, strat-pipeline, epic-decomposer and rfe-autofixer; only
SCRIPT differs between the copies."""

import gzip
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / ".fullsend/shared/publish/telemetry_bundle.py"


def load():
    spec = importlib.util.spec_from_file_location("telemetry_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attr(key, value):
    kind = "doubleValue" if isinstance(value, float) else "stringValue"
    return {"key": key, "value": {kind: value if kind == "doubleValue" else str(value)}}


def log_record(name, seq, **attrs):
    attrs = {
        "event.name": name,
        "event.sequence": seq,
        "session.id": "s1",
        "event.timestamp": f"2026-11-13T10:00:{seq:02d}.000Z",
        **attrs,
    }
    return {"attributes": [attr(k, v) for k, v in attrs.items()]}


def otel_line(*records):
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}
    return json.dumps({"ts": "2026-11-13T10:00:00Z", "path": "/v1/logs", "payload": payload})


REQ_A = log_record(
    "api_request",
    1,
    model="claude-opus-5-5",
    cost_usd=0.5,
    input_tokens=10,
    output_tokens=20,
    cache_read_tokens=100,
    cache_creation_tokens=5,
    request_id="r1",
)
REQ_B = log_record(
    "api_request",
    3,
    model="claude-haiku-5-5",
    cost_usd=0.25,
    input_tokens=1,
    output_tokens=2,
    cache_read_tokens=3,
    cache_creation_tokens=4,
    request_id="r2",
)
OTEL_LOG = (
    "\n".join(
        [
            otel_line(log_record("user_prompt", 0), REQ_A, log_record("tool_result", 2)),
            json.dumps({"ts": "x", "path": "/v1/metrics", "payload": {"resourceMetrics": []}}),
            otel_line(REQ_B, log_record("tool_result", 4)),
            otel_line(REQ_A),  # exporter retry: same request again
            "not json",
        ]
    )
    + "\n"
)
FULLSEND_METRICS = {
    "num_turns": 4,
    "total_cost_usd": 7.7202538,
    "tool_calls": 6,
    "token_usage": {
        "input": 246,
        "output": 122385,
        "reasoning": 54968,
        "cache_creation": 788344,
        "cache_read": 6649249,
    },
    "per_model_usage": {
        "claude-opus-5-5": {
            "requests": 0,
            "input_tokens": 246,
            "output_tokens": 122385,
            "cache_creation_input_tokens": 788344,
            "cache_read_input_tokens": 6649249,
            "cost_usd": 7.7202538,
        }
    },
}
TOTAL_KEYS = [
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "api_requests",
    "tool_calls",
    "turns",
    "user_prompts",
]
USAGE_KEYS = [
    "schema",
    "pipeline",
    "run_id",
    "ci",
    "source",
    "totals",
    "by_model",
    "started_at",
    "finished_at",
    "raw",
]
CI_VARS = ("CI_PROJECT_PATH", "CI_PIPELINE_ID", "CI_JOB_ID", "CI_JOB_NAME", "CI_JOB_URL")


class TelemetryBundleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bundle = load()
        self.otel = self.tmp / "claude-otel.jsonl"
        self.otel.write_text(OTEL_LOG)
        self.run_dir = self.tmp / "fullsend-run"
        self.run_dir.mkdir()
        (self.run_dir / "metrics.json").write_text(json.dumps(FULLSEND_METRICS))
        (self.run_dir / "run-telemetry.jsonl").write_text('{"resourceSpans": []}\n')
        env = {k: v for k, v in os.environ.items() if k not in CI_VARS}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def usage(self, out):
        return json.loads((out / "usage.json").read_text())

    def test_claude_otel_totals_come_from_api_request_events(self):
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", run_id="20261113-100000", otel_log=str(self.otel))
        usage = self.usage(out)
        self.assertEqual(list(usage), USAGE_KEYS)
        self.assertEqual(usage["schema"], "agentic-telemetry/v1")
        self.assertEqual(
            (usage["pipeline"], usage["run_id"], usage["ci"]), ("p", "20261113-100000", None)
        )
        self.assertEqual(usage["source"], "claude-otel")
        self.assertEqual(list(usage["totals"]), TOTAL_KEYS)
        self.assertEqual(
            usage["totals"],
            {
                "cost_usd": 0.75,
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_tokens": 103,
                "cache_creation_tokens": 9,
                "reasoning_tokens": None,
                "api_requests": 2,
                "tool_calls": 2,
                "turns": None,
                "user_prompts": 1,
            },
        )
        self.assertEqual(sorted(usage["by_model"]), ["claude-haiku-5-5", "claude-opus-5-5"])
        self.assertEqual(usage["by_model"]["claude-opus-5-5"]["cost_usd"], 0.5)
        self.assertEqual(usage["started_at"], "2026-11-13T10:00:00.000Z")
        self.assertEqual(usage["finished_at"], "2026-11-13T10:00:04.000Z")
        with gzip.open(out / "raw" / "claude-otel.jsonl.gz", "rb") as handle:
            stored = handle.read()
        self.assertEqual(len(stored.splitlines()), 4)  # the non-JSON line is not stored
        entry = usage["raw"]["raw/claude-otel.jsonl.gz"]
        self.assertEqual(entry["uncompressed_bytes"], len(stored))
        self.assertEqual(entry["removed_attributes"], 1)  # the non-JSON line
        self.assertEqual(entry["bytes"], (out / "raw" / "claude-otel.jsonl.gz").stat().st_size)

    def test_fullsend_run_uses_metrics_and_keeps_both_raw_files(self):
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(
            str(out), "p", run_id="gitlab-job-1", fullsend_run_dir=str(self.run_dir)
        )
        usage = self.usage(out)
        self.assertEqual(usage["source"], "fullsend-metrics")
        self.assertEqual(list(usage["totals"]), TOTAL_KEYS)
        self.assertEqual(usage["totals"]["cost_usd"], 7.720254)
        self.assertEqual(usage["totals"]["reasoning_tokens"], 54968)
        self.assertEqual(usage["totals"]["turns"], 4)
        self.assertIsNone(usage["totals"]["user_prompts"])
        self.assertIsNone(usage["totals"]["api_requests"])
        self.assertEqual(
            sorted(usage["raw"]),
            ["raw/fullsend-metrics.json.gz", "raw/fullsend-telemetry.jsonl.gz"],
        )
        self.assertEqual(
            sorted(p.name for p in (out / "raw").iterdir()),
            ["fullsend-metrics.json.gz", "fullsend-telemetry.jsonl.gz"],
        )

    def test_claude_otel_wins_when_both_sources_exist(self):
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(
            str(out), "p", otel_log=str(self.otel), fullsend_run_dir=str(self.run_dir)
        )
        usage = self.usage(out)
        self.assertEqual(usage["source"], "claude-otel")
        self.assertEqual(len(usage["raw"]), 3)

    def test_no_source_still_writes_usage_json(self):
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", otel_log=str(self.tmp / "absent.jsonl"))
        usage = self.usage(out)
        self.assertEqual(list(usage), USAGE_KEYS)
        self.assertIsNone(usage["source"])
        self.assertIsNone(usage["totals"])
        self.assertEqual(usage["raw"], {})
        self.assertEqual([p.name for p in out.iterdir()], ["usage.json"])

    def test_output_is_deterministic_and_replaces_the_folder(self):
        first, second = self.tmp / "a", self.tmp / "b"
        self.bundle.write_bundle(str(first), "p", run_id="r", otel_log=str(self.otel))
        (second / "stale").mkdir(parents=True)
        self.bundle.write_bundle(str(second), "p", run_id="r", otel_log=str(self.otel))
        self.assertFalse((second / "stale").exists())
        for name in ("usage.json", "raw/claude-otel.jsonl.gz"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_ci_fields_come_from_gitlab_variables(self):
        os.environ.update(
            CI_PROJECT_PATH="g/p",
            CI_PIPELINE_ID="7",
            CI_JOB_ID="9",
            CI_JOB_NAME="job",
            CI_JOB_URL="https://gitlab.com/g/p/-/jobs/9",
        )
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", otel_log=str(self.otel))
        self.assertEqual(
            self.usage(out)["ci"],
            {
                "project": "g/p",
                "pipeline_id": "7",
                "job_id": "9",
                "job_name": "job",
                "job_url": "https://gitlab.com/g/p/-/jobs/9",
            },
        )

    def test_unparseable_metrics_keep_the_raw_file(self):
        (self.run_dir / "metrics.json").write_text("{broken")
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", fullsend_run_dir=str(self.run_dir))
        usage = self.usage(out)
        self.assertIsNone(usage["source"])
        self.assertIn("raw/fullsend-metrics.json.gz", usage["raw"])

    def test_cli(self):
        out = self.tmp / "telemetry"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--out",
                str(out),
                "--pipeline",
                "p",
                "--run-id",
                "r",
                "--otel-log",
                str(self.otel),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items()},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.usage(out)["run_id"], "r")

    def test_stored_logs_keep_numbers_and_ids_but_not_content(self):
        secret = "do-not-store-this-text"
        content = [
            attr("event.name", "tool_result"),
            attr("tool_name", "Bash"),
            attr("session.id", "s1"),
            attr("duration_ms", "92"),
            attr("tool_input", '{"command": "echo ' + secret + '"}'),
            attr("tool_parameters", secret),
            attr("prompt", secret),
            {"key": "success", "value": {"boolValue": True}},
            {"key": "anything_new", "value": {"stringValue": secret}},
        ]
        record = {"attributes": content, "body": {"stringValue": secret}}
        span = {
            "name": "tool",
            "attributes": [attr("full_command", secret), attr("gen_ai.tool.name", "Bash")],
            "events": [{"name": "tool.output", "attributes": [attr("output", secret)]}],
            "status": {"code": 2, "message": secret},
        }
        line = {
            "ts": "t",
            "path": "/v1/logs",
            "payload": {
                "resourceLogs": [{"scopeLogs": [{"logRecords": [record]}]}],
                "resourceSpans": [{"scopeSpans": [{"spans": [span]}]}],
            },
        }
        self.otel.write_text(json.dumps(line) + "\n" + otel_line(REQ_A) + "\n")
        (self.run_dir / "run-telemetry.jsonl").write_text(
            json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}) + "\n"
        )
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(
            str(out), "p", otel_log=str(self.otel), fullsend_run_dir=str(self.run_dir)
        )
        for name in ("claude-otel.jsonl.gz", "fullsend-telemetry.jsonl.gz"):
            with gzip.open(out / "raw" / name, "rt") as handle:
                stored = handle.read()
            self.assertNotIn(secret, stored, name)
        with gzip.open(out / "raw" / "claude-otel.jsonl.gz", "rt") as handle:
            first = json.loads(handle.readline())
        kept = first["payload"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(
            [a["key"] for a in kept["attributes"]],
            ["event.name", "tool_name", "session.id", "duration_ms", "success"],
        )
        usage = self.usage(out)
        self.assertEqual(usage["raw"]["raw/claude-otel.jsonl.gz"]["removed_attributes"], 8)
        self.assertEqual(usage["totals"]["cost_usd"], 0.5)  # totals use the original log

    def test_log_without_api_requests_has_no_usage_rather_than_zero(self):
        self.otel.write_text(json.dumps({"ts": "t", "path": "/v1/logs", "payload": {}}) + "\n")
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", otel_log=str(self.otel))
        usage = self.usage(out)
        self.assertIsNone(usage["source"])
        self.assertIsNone(usage["totals"])
        self.assertIn("raw/claude-otel.jsonl.gz", usage["raw"])

    def test_empty_otel_log_falls_back_to_fullsend_metrics(self):
        self.otel.write_text("")
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(
            str(out), "p", otel_log=str(self.otel), fullsend_run_dir=str(self.run_dir)
        )
        self.assertEqual(self.usage(out)["source"], "fullsend-metrics")

    def test_a_failed_write_keeps_the_previous_folder(self):
        out = self.tmp / "telemetry"
        self.bundle.write_bundle(str(out), "p", run_id="r", otel_log=str(self.otel))
        before = (out / "usage.json").read_bytes()
        failing = mock.patch.object(self.bundle, "_ci", side_effect=OSError("disk full"))
        with failing, self.assertRaises(OSError):
            self.bundle.write_bundle(str(out), "p", run_id="other", otel_log=str(self.otel))
        self.assertEqual((out / "usage.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(".")), [])

    def test_numbers_in_strings(self):
        self.assertEqual(self.bundle._number("42"), 42)
        self.assertEqual(self.bundle._number("1e-07"), 1e-07)
        self.assertEqual(self.bundle._number("0.5"), 0.5)
        self.assertIsNone(self.bundle._number("n/a"))
        self.assertIsNone(self.bundle._number(True))


if __name__ == "__main__":
    unittest.main()
