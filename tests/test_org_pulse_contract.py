"""Completed feature export preserves numeric verdicts, dissent and real skips."""
import copy
import importlib.util
import json
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "org_pulse_publisher", ROOT / ".fullsend/shared/publish/push-to-org-pulse.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def feature():
    return json.loads((ROOT / "tests/fixtures/org-pulse-feature-contract.json").read_text())["features"][0]


def strategy():
    value = feature()
    value["strat_id"] = value.pop("key")
    return value


def test_completed_export_matches_consumer_fixture_without_hiding_dissent_or_skip():
    assert publisher.build_features([strategy()]) == [feature()]


@pytest.mark.parametrize("role", ["feasibility", "testability", "scope", "architecture"])
def test_pending_assessment_is_not_exported_as_a_completed_review(role, capsys):
    provisional = strategy()
    provisional["reviewers"][role] = "pending"
    original = copy.deepcopy(provisional)
    assert publisher.build_features([provisional, strategy()]) == [feature()]
    assert provisional == original
    assert "provisional review" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [True, False])
def test_all_provisional_cli_exits_without_a_sample_or_api_call(tmp_path, monkeypatch, capsys, dry_run):
    provisional = strategy()
    provisional["reviewers"]["scope"] = "pending"
    directory = tmp_path / "RHAISTRAT"
    directory.mkdir()
    (directory / "summary-production.json").write_text(json.dumps({"summary": {"strategies": [provisional]}}))
    monkeypatch.setenv("ORG_PULSE_API_TOKEN", "isolated-placeholder")
    args = ["push-to-org-pulse.py", "--results-dir", str(tmp_path), "--api-url", "https://orgpulse.invalid"]
    monkeypatch.setattr(sys, "argv", args + (["--dry-run"] if dry_run else []))

    def unexpected(*args):
        pytest.fail("provisional-only export must not call the API")

    monkeypatch.setattr(publisher, "push_to_org_pulse", unexpected)
    with pytest.raises(SystemExit) as stopped:
        publisher.main()
    assert stopped.value.code == 0
    output = capsys.readouterr()
    assert "No completed" in output.out and "Sample entry" not in output.out


@pytest.mark.skipif(shutil.which("openssl") is None, reason="local TLS fixture requires openssl")
def test_org_pulse_upload_accepts_untrusted_service_certificate(tmp_path, monkeypatch):
    """Exercise the temporary compatibility exception with real local HTTPS."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=untrusted.example.test", "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"created": 1, "updated": 0, "unchanged": 0}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    url = f"https://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.URLError) as rejected:
            urllib.request.urlopen(url, context=ssl.create_default_context(), timeout=5)
        assert isinstance(rejected.value.reason, ssl.SSLCertVerificationError)
        ok, response = publisher.push_batch(url, "isolated-placeholder", [feature()], 1, 1)
        assert ok is True, response
        assert response == {"created": 1, "updated": 0, "unchanged": 0}
        assert received == [{
            "path": "/api/modules/ai-impact/features/bulk",
            "authorization": "Bearer isolated-placeholder",
            "body": {"features": [feature()]},
        }]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
