"""Shared test fixtures — jira-emulator server for integration tests."""
import base64
import json
import os
import socket
import tempfile
import threading
import time
import urllib.request

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")


def _find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _jira_request(base_url, method, path, body=None):
    """Make a request to the jira-emulator."""
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    creds = base64.b64encode(b"admin:admin").decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        if resp.status == 204:
            return None
        body_bytes = resp.read()
        return json.loads(body_bytes) if body_bytes else None


@pytest.fixture(scope="session")
def jira_emu():
    """Start a jira-emulator server for the test session."""
    port = _find_free_port()

    att_dir = tempfile.mkdtemp(prefix="jira-emu-attachments-")
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
    os.environ["AUTH_MODE"] = "none"
    os.environ["SEED_DATA"] = "true"
    os.environ["ATTACHMENT_DIR"] = att_dir
    os.environ["BASE_URL"] = f"http://127.0.0.1:{port}"

    from jira_emulator.config import get_settings
    get_settings.cache_clear()
    from jira_emulator.database import reset_engine
    reset_engine()
    import uvicorn
    from jira_emulator.app import create_app

    app = create_app()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base_url}/")
            break
        except Exception:
            time.sleep(0.05)

    yield base_url
    server.should_exit = True


@pytest.fixture
def jira(jira_emu):
    """Per-test fixture: resets emulator state and provides helpers."""
    from jira_emulator.services import seed_service
    _extra_link_types = [
        {"name": "Cloners",
         "inward_description": "is cloned by",
         "outward_description": "clones"},
        {"name": "Issue split",
         "inward_description": "is split from",
         "outward_description": "split to"},
    ]
    _orig = seed_service.LINK_TYPES
    seed_service.LINK_TYPES = _orig + [
        lt for lt in _extra_link_types
        if lt["name"] not in {x["name"] for x in _orig}
    ]

    # Target Version (customfield_10855) — multi-version picker, stored/returned
    # as a JSON array of {"name": ...} objects (emulated via multiselect type).
    _existing_cf = next(
        (cf for cf in seed_service.CUSTOM_FIELDS
         if cf["field_id"] == "customfield_10855"), None)
    if _existing_cf is None:
        seed_service.CUSTOM_FIELDS = [
            *seed_service.CUSTOM_FIELDS,
            {"field_id": "customfield_10855", "name": "Target Version",
             "field_type": "multiselect"},
        ]
    else:
        # Fail loud if a future emulator seeds this field incompatibly, rather
        # than silently reusing a definition these tests don't expect.
        assert _existing_cf["field_type"] == "multiselect", (
            "customfield_10855 already seeded with field_type "
            f"{_existing_cf['field_type']!r}; tests expect 'multiselect'")

    req = urllib.request.Request(
        f"{jira_emu}/api/admin/reset", method="POST", data=b"")
    urllib.request.urlopen(req)

    # Live Jira always holds versions for the active release trains, which
    # list-rfe-ids.py and prepare_run.py resolve at runtime; seed one
    # unreleased version per train config/pipeline-settings.yaml names.
    import yaml
    with open(os.path.join(SCRIPTS_DIR, "..", "config", "pipeline-settings.yaml")) as f:
        intake = yaml.safe_load(f)["jql"]
    for train in intake.get("target_version_trains") or []:
        _jira_request(jira_emu, "POST", "/rest/api/3/version",
                      {"project": intake["project"], "name": f"{train} GA RHOAI RELEASE"})

    class JiraHelper:
        url = jira_emu

        @staticmethod
        def create(key, summary, description, labels=None, components=None,
                   fix_versions=None, affects_versions=None,
                   target_versions=None, issue_type=None, parent_key=None,
                   status=None):
            """Import an issue with a specific key."""
            issue = {
                "key": key,
                "summary": summary,
                "project": key.split("-")[0],
                "issue_type": issue_type or "Feature Request",
                "description": description,
            }
            if labels:
                issue["labels"] = labels
            if components:
                issue["components"] = [{"name": c} for c in components]
            if fix_versions:
                issue["fix_versions"] = [{"name": v} for v in fix_versions]
            if affects_versions:
                issue["affects_versions"] = [{"name": v}
                                             for v in affects_versions]
            if parent_key:
                issue["epic_link"] = parent_key
            if status:
                issue["status"] = status
            _jira_request(jira_emu, "POST", "/api/admin/import",
                          {"issues": [issue]})
            # Target Version isn't part of the import field map; set it via a
            # REST update so it round-trips like real Jira's customfield_10855.
            if target_versions:
                # Accept plain names or full version objects ({"id"/"name": ...}).
                tv = [v if isinstance(v, dict) else {"name": v}
                      for v in target_versions]
                _jira_request(
                    jira_emu, "PUT", f"/rest/api/3/issue/{key}",
                    {"fields": {"customfield_10855": tv}})

        @staticmethod
        def get(key):
            """GET an issue, return parsed JSON."""
            return _jira_request(jira_emu, "GET",
                                 f"/rest/api/3/issue/{key}")

        @staticmethod
        def search(jql, fields="key,description,labels"):
            """JQL search, return list of issues."""
            from urllib.parse import quote
            path = (f"/rest/api/3/search/jql"
                    f"?jql={quote(jql, safe='')}&fields={fields}")
            data = _jira_request(jira_emu, "GET", path)
            return data.get("issues", [])

        @staticmethod
        def request(method, path, body=None):
            """Make an arbitrary API request to the emulator."""
            return _jira_request(jira_emu, method, path, body)

        @staticmethod
        def create_version(project, name, released=False):
            """Create a project version (GET /project/{key} lists it), idempotently."""
            return _jira_request(jira_emu, "POST", "/rest/api/3/version",
                                 {"project": project, "name": name, "released": released})

    return JiraHelper()


@pytest.fixture
def description_limit(jira):
    """Set the emulator's logical description limit for one test."""
    from jira_emulator.config import get_settings

    settings = get_settings()
    original_limit = settings.DESCRIPTION_MAX_LENGTH

    def set_limit(limit):
        settings.DESCRIPTION_MAX_LENGTH = limit

    yield set_limit
    settings.DESCRIPTION_MAX_LENGTH = original_limit


@pytest.fixture
def art_dir(tmp_path):
    """Create artifact directory structure in a temp dir and chdir there."""
    for subdir in ("strat-tasks", "strat-reviews", "strat-originals"):
        (tmp_path / "artifacts" / subdir).mkdir(parents=True)
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old_cwd)


@pytest.fixture
def scripts_dir():
    """Return the path to the scripts/ directory."""
    return os.path.abspath(SCRIPTS_DIR)
