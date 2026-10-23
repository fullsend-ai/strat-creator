"""Host validation must notice review effects changed after record-review."""
import importlib.util
import json
import urllib.request
from pathlib import Path

import pytest
from test_fullsend_host_integration import SCHEMA, validated_run  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_readback_validator", ROOT / ".fullsend/shared/validate_result.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


@pytest.mark.parametrize("key", ["RHAISTRAT-8101", "RHAISTRAT-8102"])
@pytest.mark.parametrize("changed,expected", [
    ("missing-comment", "exact summary comment"),
    ("altered-comment", "exact summary comment"),
    ("missing-attachment", "exact review attachment"),
    ("altered-attachment", "exact review attachment"),
    ("conflicting-label", "conflicting labels"),
])
def test_host_rechecks_recorded_review_without_republishing(
    request, jira, monkeypatch, tmp_path, key, changed, expected
):
    import jira_utils

    repo, run_path, env = request.getfixturevalue("validated_run")
    result = repo / "output/agent-result.json"
    original_progress = (repo / "tmp/strat-progress.json").read_bytes()
    assert key in json.loads(original_progress)["review"]
    if changed.endswith("comment"):
        original = jira_utils.get_comments

        def comments(server, user, token, issue):
            rows = original(server, user, token, issue)
            if issue != key:
                return rows
            # The emulator has no comment edit/delete API. Alter only its
            # readback here; publication/recording above used the real server.
            return [] if changed == "missing-comment" else [
                {**row, "body": jira_utils.markdown_to_adf("Changed review summary.")} for row in rows]

        monkeypatch.setattr(jira_utils, "get_comments", comments)
    elif changed == "missing-attachment":
        attachment, = jira.get(key)["fields"]["attachment"]
        jira.request("DELETE", f"/rest/api/3/attachment/{attachment['id']}")
    elif changed == "altered-attachment":
        upload = tmp_path / "changed-review.md"
        upload.write_text("Changed review bytes, superseding the original.\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("STRAT_EFFECTS_DIR", str(tmp_path / "external-effects"))
        monkeypatch.setenv("STRAT_RUN_ID", "external-revision")
        jira_utils.add_attachment(jira.url, "admin", "admin", key, str(upload), filename=f"{key}-review.md")
    else:
        other = "strat-creator-needs-attention" if key.endswith("8101") else "strat-creator-rubric-pass"
        jira.request("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": other}]}})

    reads = []

    def observe(open_request):
        def read_only(request, *args, **kwargs):
            assert request.get_method() == "GET", "Host readback attempted a mutation"
            assert request.full_url.startswith(jira.url + "/"), "Readback left the isolated emulator"
            reads.append(request.full_url)
            return open_request(request, *args, **kwargs)
        return read_only

    monkeypatch.setattr(urllib.request, "urlopen", observe(urllib.request.urlopen))
    monkeypatch.setattr(jira_utils._redirect_opener, "open", observe(jira_utils._redirect_opener.open))
    failures = validator.validate(result, repo, SCHEMA, run_path, env)
    assert any(key in error and expected in error for error in failures), failures
    assert reads
    assert (repo / "tmp/strat-progress.json").read_bytes() == original_progress
