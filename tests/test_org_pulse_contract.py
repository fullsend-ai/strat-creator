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


@pytest.fixture
def local_services(tmp_path, monkeypatch):
    if shutil.which("openssl") is None:
        pytest.skip("local TLS fixture requires openssl")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=untrusted.example.test", "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True)
    servers = []
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    def create(secure=True):
        state = {"received": [], "redirect": None}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                state["received"].append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                          "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])) )})
                if state["redirect"]:
                    status, location = state["redirect"]
                    self.send_response(status)
                    self.send_header("Location", location)
                    self.end_headers()
                else:
                    self.reply()

            def do_GET(self):
                state["received"].append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.reply()

            def reply(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"created": 1, "updated": 0, "unchanged": 0}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        if secure:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        servers.append((server, thread))
        state["url"] = f"{'https' if secure else 'http'}://127.0.0.1:{server.server_port}"
        return state

    try:
        yield create
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_org_pulse_upload_accepts_untrusted_service_certificate(local_services):
    """Exercise the temporary compatibility exception with real local HTTPS."""
    service = local_services()
    with pytest.raises(urllib.error.URLError) as rejected:
        urllib.request.urlopen(service["url"], context=ssl.create_default_context(), timeout=5)
    assert isinstance(rejected.value.reason, ssl.SSLCertVerificationError)
    ok, response = publisher.push_batch(service["url"], "isolated-placeholder", [feature()], 1, 1)
    assert ok is True, response
    assert response == {"created": 1, "updated": 0, "unchanged": 0}
    assert service["received"] == [{"path": "/api/modules/ai-impact/features/bulk",
                                    "authorization": "Bearer isolated-placeholder", "body": {"features": [feature()]}}]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("secure", [True, False])
def test_org_pulse_redirect_does_not_forward_credentials(local_services, status, secure):
    source, destination = local_services(), local_services(secure)
    source["redirect"] = (status, destination["url"] + "/unexpected")
    ok, response = publisher.push_batch(source["url"], "isolated-placeholder", [feature()], 1, 1)
    assert ok is False and f"HTTP {status}" in response
    assert len(source["received"]) == 1
    assert destination["received"] == []


@pytest.mark.parametrize("kind", ["http", "userinfo", "query", "fragment"])
def test_org_pulse_rejects_unsafe_base_url_before_sending(local_services, kind):
    service = local_services(secure=kind != "http")
    url = service["url"]
    if kind == "userinfo":
        url = url.replace("://", "://user:synthetic-password@")
    elif kind in {"query", "fragment"}:
        url += "?unexpected=yes" if kind == "query" else "#unexpected"
    ok, response = publisher.push_batch(url, "isolated-placeholder", [feature()], 1, 1)
    assert ok is False and "base URL" in response
    assert "synthetic-password" not in response
    assert service["received"] == []
