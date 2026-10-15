"""Static checks of the fullsend harnesses, and the resume trigger through `fullsend dispatch`."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FULLSEND = ROOT / ".fullsend"
EVENTS = ROOT / "tests" / "fixtures" / "fullsend-events"
AGENTS = ("strat-single", "strat-batch", "strat-resume")
IMAGE = re.compile(r"^ghcr[.]io/fullsend-ai/fullsend-sandbox@sha256:[0-9a-f]{64}$")
PINNED = re.compile(r"^https://raw[.]githubusercontent[.]com/fullsend-ai/agents/[0-9a-f]{40}/\S+#sha256=[0-9a-f]{64}$")


def _harness(name):
    return yaml.safe_load((FULLSEND / name / f"{name}.yaml").read_text())


def test_config_registers_every_harness():
    config = yaml.safe_load((FULLSEND / "config.yaml").read_text())
    assert [a["source"] for a in config["agents"]] == [f"{a}/{a}.yaml" for a in AGENTS]


@pytest.mark.parametrize("name", AGENTS)
def test_harness_shape(name):
    h = _harness(name)
    assert IMAGE.match(h["image"])
    assert h["plugins"] == ["plugins/strat-pipeline"]
    for field in (h["agent"], h["policy"], h["pre_script"], h["post_script"],
                  h["validation_loop"]["script"], h["validation_loop"]["schema"]):
        assert (FULLSEND / field).is_file(), field
    assert h["validation_loop"]["max_iterations"] == 2
    assert h["validation_loop"]["feedback_mode"] == "append"
    assert all(PINNED.match(u) for u in h["openshell"]["profiles"] + h["providers"])
    runner = h["env"]["runner"]
    assert runner["STRAT_MODE"] == name.removeprefix("strat-")
    assert "STRAT_STATE_DIR" in runner and "STRAT_STATE_DIR" not in h["env"]["sandbox"]
    assert "STRAT_CREATOR_REF" in runner
    prompt = (FULLSEND / h["agent"]).read_text()
    # The agent body must keep the Workflow tool: no tools allowlist.
    assert "tools:" not in prompt.split("---")[1]
    assert "strat-pipeline:strat-pipeline" in prompt


def test_every_harness_pins_the_same_image():
    assert len({_harness(n)["image"] for n in AGENTS}) == 1


def test_timeouts():
    assert [_harness(n)["timeout_minutes"] for n in AGENTS] == [90, 295, 90]


def test_only_resume_has_a_trigger():
    assert "trigger" not in _harness("strat-single") and "trigger" not in _harness("strat-batch")
    assert "comment_added" in _harness("strat-resume")["trigger"]


@pytest.mark.skipif(shutil.which("fullsend") is None, reason="fullsend CLI not installed")
@pytest.mark.parametrize("event,expected", [
    ("jira-comment-match.json", ["strat-resume"]),
    ("jira-comment-other-command.json", []),
    ("jira-comment-bot.json", []),
    ("jira-comment-read-role.json", []),
    ("jira-comment-no-command.json", []),
])
def test_resume_trigger(event, expected, tmp_path):
    config = tmp_path / ".fullsend"
    shutil.copytree(FULLSEND, config, ignore=shutil.ignore_patterns(".fullsend-cache"))
    proc = subprocess.run(
        ["fullsend", "dispatch", "--config-dir", str(config), "--input-driver", "json",
         "--input-file", str(EVENTS / event), "--output-driver", "json",
         "--repo", "opendatahub-io/strat-creator"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    matrix = json.loads(proc.stdout) or []
    assert [m["agent"] for m in matrix] == expected
