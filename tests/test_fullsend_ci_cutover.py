"""Offline input checks for the regular GitLab Workflow jobs."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cutover_config", ROOT / ".fullsend/ci/fullsend-config.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


@pytest.mark.parametrize("dry_run,publish", [("1", "0"), ("0", "1")])
def test_scheduled_batches_accept_the_same_publication_contract(dry_run, publish):
    result = config.validate({
        "CI_PIPELINE_SOURCE": "schedule", "FULLSEND_MODE": "batch",
        "STRAT_DRY_RUN": dry_run, "FULLSEND_PUBLISH": publish,
    })
    assert result == {"mode": "batch", "rfe_key": "", "dry_run": dry_run, "publish": publish}


@pytest.mark.parametrize("source", ["push", "merge_request_event", "parent_pipeline", "trigger"])
def test_non_execution_pipeline_sources_are_rejected(source):
    with pytest.raises(ValueError, match="web, API, or scheduled"):
        config.validate({"CI_PIPELINE_SOURCE": source, "FULLSEND_MODE": "batch"})


def test_a_schedule_cannot_silently_turn_into_a_single_ticket_run():
    with pytest.raises(ValueError, match="scheduled pipelines require batch"):
        config.validate({"CI_PIPELINE_SOURCE": "schedule", "RFE_KEY": "RHAIRFE-1"})


@pytest.mark.parametrize("env,mode", [
    ({}, "batch"), ({"RFE_KEY": "RHAIRFE-7"}, "single"),
    ({"CONFIG_FILE": "config/batch.yaml", "RFE_FILTER": "--baseline"}, "config"),
    ({"STRAT_KEY": "RHAISTRAT-7"}, "reprocess"),
])
def test_regular_selectors_infer_the_correct_operation(env, mode):
    assert config.validate(env)["mode"] == mode


@pytest.mark.parametrize("env", [
    {"RFE_KEY": "RHAIRFE-7", "STRAT_KEY": "RHAISTRAT-7"},
    {"CONFIG_FILE": "config/batch.yaml", "STRAT_KEY": "RHAISTRAT-7"},
    {"CONFIG_FILE": "config/batch.yaml", "RFE_KEY": "RHAIRFE-7"},
    {"FULLSEND_MODE": "batch", "CONFIG_FILE": "config/batch.yaml"},
    {"FULLSEND_MODE": "reprocess", "RFE_KEY": "RHAIRFE-7"},
    {"STRAT_KEY": "RHAISTRAT-7; echo unsafe"},
    {"CONFIG_FILE": "../batch.yaml"}, {"CONFIG_FILE": "/tmp/batch.yaml"},
    {"CONFIG_FILE": "config/batch.txt"}, {"RFE_FILTER": "--baseline"},
    {"CONFIG_FILE": "config/batch.yaml", "RFE_FILTER": "--baseline --no-baseline"},
    {"CONFIG_FILE": "config/batch.yaml", "RFE_FILTER": "--include-processed"},
    {"CI_PIPELINE_SOURCE": "schedule", "CONFIG_FILE": "config/batch.yaml"},
    {"CI_PIPELINE_SOURCE": "schedule", "STRAT_KEY": "RHAISTRAT-7"},
])
def test_ambiguous_or_unsupported_requests_fail_before_any_model(env):
    with pytest.raises(ValueError):
        config.validate(env)
