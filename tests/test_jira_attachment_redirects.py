"""Exercise attachment redirects and credential boundaries without external APIs."""

import base64
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jira_utils  # noqa: E402


@pytest.fixture
def attachment_servers():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.server.server_port, self.path, self.headers.get("Authorization")))
            if self.path == "/media-redirect":
                self.send_response(303)
                self.send_header("Location", media_url + "/review.md?signed=synthetic")
            elif self.path == "/same-origin":
                self.send_response(302)
                self.send_header("Location", "/review.md")
            else:
                self.send_response(200)
            self.end_headers()
            if "redirect" not in self.path and self.path != "/same-origin":
                self.wfile.write(b"Exact review bytes.\n")

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    jira_url, media_url = (f"http://127.0.0.1:{server.server_port}" for server in servers)
    for thread in threads:
        thread.start()
    try:
        yield jira_url, media_url, seen
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


@pytest.mark.parametrize("path", ["/media-redirect", "/same-origin", "direct-media"])
def test_attachment_reads_preserve_bytes_and_limit_credentials(attachment_servers, tmp_path, path):
    jira_url, media_url, seen = attachment_servers
    content_url = media_url + "/review.md" if path == "direct-media" else jira_url + path
    destination = tmp_path / "review.md"
    jira_utils.download_attachment(jira_url, "user", "synthetic-token", content_url, destination)
    assert destination.read_bytes() == b"Exact review bytes.\n"
    authorization = "Basic " + base64.b64encode(b"user:synthetic-token").decode()
    for port, _, auth in seen:
        expected = authorization if str(port) == jira_url.rsplit(":", 1)[-1] else None
        assert auth == expected
    assert len(seen) == (1 if path == "direct-media" else 2)


def test_https_attachment_does_not_follow_downgrade(monkeypatch, tmp_path):
    requests = []

    def redirect(request, **kwargs):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 303, "redirect",
                                     {"Location": "http://media.invalid/review"}, None)

    monkeypatch.setattr(jira_utils._redirect_opener, "open", redirect)
    with pytest.raises(ValueError, match="downgrade"):
        jira_utils.download_attachment("https://jira.invalid", "user", "secret",
                                       "https://jira.invalid/attachment", tmp_path / "review")
    assert requests == ["https://jira.invalid/attachment"]
    assert not (tmp_path / "review").exists()


@pytest.mark.parametrize("url", ["file:///tmp/review", "https://user:secret@media.invalid/review"])
def test_unsafe_attachment_urls_stop_before_network(monkeypatch, tmp_path, url):
    def unexpected(*args, **kwargs):
        pytest.fail("unsafe attachment URL reached the network")

    monkeypatch.setattr(jira_utils._redirect_opener, "open", unexpected)
    with pytest.raises(ValueError):
        jira_utils.download_attachment("https://jira.invalid", "user", "secret", url, tmp_path / "review")


def test_attachment_redirect_loop_is_bounded(monkeypatch, tmp_path):
    requests = []

    def redirect(request, **kwargs):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 303, "redirect", {"Location": "/again"}, None)

    monkeypatch.setattr(jira_utils._redirect_opener, "open", redirect)
    with pytest.raises(RuntimeError, match="too many"):
        jira_utils.download_attachment("https://jira.invalid", "user", "secret",
                                       "https://jira.invalid/attachment", tmp_path / "review")
    assert len(requests) == jira_utils._MAX_REDIRECTS + 1


def test_shared_policy_limits_jira_and_media_to_python_tls():
    policy = yaml.safe_load((ROOT / ".fullsend/shared/policy.yaml").read_text())
    jira = policy["network_policies"]["jira"]
    assert jira["binaries"] == [{"path": "**/python3*"}]
    assert jira["endpoints"] == [
        {"host": "redhat.atlassian.net", "port": 443, "tls": "skip"},
        {"host": "api.media.atlassian.com", "port": 443, "tls": "skip"},
    ]
