"""Unit tests for the inline `check:` judges in eval/strat-refine.yaml.

These run the check source straight out of the config, so they fail if the YAML
drifts from what they assert.
"""
import json
import os

import pytest
import yaml

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVAL_YAML = os.path.join(PROJECT_ROOT, "eval", "strat-refine.yaml")

SESSION = "11111111-2222-3333-4444-555555555555"
OTHER_SESSION = "99999999-8888-7777-6666-555555555555"

# Long enough to clear MIN_RESULT_CHARS — stands in for a real architecture doc.
DOC = "RHOAI platform architecture. " * 20
# What an empty context directory actually returned in the 2026-08-11 run.
EMPTY_LISTING = "No files found"
# What empty-context Bash probes returned in the 2026-08-05/08-10 runs: long output
# that proves nothing — compound ls/find existence checks echoing absolute workspace
# paths and unrelated staged files, and the fetch script's cp-failure message.
WORKSPACE = "/tmp/agent-eval/strat-creator-eval/cases/RHAIRFE-0000-abcdef/workspace"
EMPTY_DIR_PROBE = (
    "total 0\n"
    "drwxr-xr-x  2 runner runner  40 Aug 10 12:00 .\n"
    "drwxr-xr-x  8 runner runner 160 Aug 10 12:00 ..\n"
    "---\n"
    + "\n".join(
        "{}/eval/.assets/assess-strat/rubric-{:02d}.md".format(WORKSPACE, i)
        for i in range(10)
    )
)
FETCH_FAILURE = (
    "cp: cannot stat '{}/local-arch/*': No such file or directory\n".format(WORKSPACE)
    * 6
)


def _load_check(name):
    with open(EVAL_YAML) as f:
        config = yaml.safe_load(f)
    judge = next(j for j in config["judges"] if j["name"] == name)
    body = "".join("    " + ln + "\n" for ln in judge["check"].splitlines())
    namespace = {}
    exec("def _check(outputs):\n" + body, namespace)
    return namespace["_check"]


def _tool_use(call_id, tool, tool_input, session=SESSION):
    return {
        "sessionId": session,
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": call_id, "name": tool, "input": tool_input,
        }]},
    }


def _tool_result(call_id, content, is_error=False, session=SESSION):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error:
        block["is_error"] = True
    return {"sessionId": session, "type": "user", "message": {"content": [block]}}


def _write_case(tmp_path, results, session=SESSION, tool="Read", extra_events=None):
    """Build a case dir whose refine subagent made one read per entry in results.

    Each entry is (content, is_error). extra_events appends raw transcript events.
    """
    case_dir = tmp_path / "case"
    step_dir = case_dir / "steps" / "refine"
    step_dir.mkdir(parents=True)
    (step_dir / "stdout.log").write_text(
        json.dumps({"type": "system", "session_id": session}) + "\n")

    if tool == "Bash":
        tool_input = {"command": "ls -la .context/architecture-context"}
    else:
        tool_input = {"file_path": ".context/architecture-context/PLATFORM.md"}
    events = [{"sessionId": session, "type": "user", "message": {"content": []}}]
    for index, (content, is_error) in enumerate(results):
        call_id = "toolu_{}".format(index)
        events.append(_tool_use(call_id, tool, tool_input, session=session))
        events.append(_tool_result(call_id, content, is_error, session=session))
    events.extend(extra_events or [])

    subagents = case_dir / "subagents"
    subagents.mkdir()
    with open(subagents / "refine.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return case_dir


@pytest.fixture
def check():
    return _load_check("architecture_context_used")


def test_passes_when_a_read_returns_real_documentation(check, tmp_path):
    case_dir = _write_case(tmp_path, [(DOC, False)])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is True
    assert "1 of 1" in rationale


def test_fails_when_every_read_comes_back_empty(check, tmp_path):
    """The 2026-08-11 failure: six reads, all attempted, none returned anything.

    Counting tool_use blocks scored this 100%. Counting results scores it 0.
    """
    case_dir = _write_case(tmp_path, [(EMPTY_LISTING, False)] * 6)

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "0 of 6" in rationale


def test_errored_reads_do_not_count(check, tmp_path):
    case_dir = _write_case(tmp_path, [(DOC, True)])

    value, _ = check({"case_dir": str(case_dir)})

    assert value is False


def test_mixed_results_pass_on_the_substantive_one(check, tmp_path):
    case_dir = _write_case(
        tmp_path, [(EMPTY_LISTING, False), (DOC, False), (DOC, True)])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is True
    assert "1 of 3" in rationale


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_structured_read_tools_count(check, tmp_path, tool):
    case_dir = _write_case(tmp_path, [(DOC, False)], tool=tool)

    value, _ = check({"case_dir": str(case_dir)})

    assert value is True


def test_bash_output_never_counts_as_documentation(check, tmp_path):
    """Bash length proves nothing: in the 2026-08-05/08-10 runs, empty-context
    probes returned 242-6187 chars of paths and cp errors, past any threshold.
    Bash still counts as an attempt, so the rationale reflects the looking.
    """
    case_dir = _write_case(
        tmp_path,
        [(EMPTY_DIR_PROBE, False), (FETCH_FAILURE, False), (DOC, False)],
        tool="Bash")

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "0 of 3" in rationale


def test_threshold_boundary(check, tmp_path):
    """Pins MIN_RESULT_CHARS = 200 and the >= comparison."""
    at_threshold = _write_case(tmp_path / "at", [("x" * 200, False)])
    below = _write_case(tmp_path / "below", [("x" * 199, False)])

    value_at, _ = check({"case_dir": str(at_threshold)})
    value_below, _ = check({"case_dir": str(below)})

    assert value_at is True
    assert value_below is False


def test_reading_the_fetch_script_is_not_documentation(check, tmp_path):
    """scripts/fetch-architecture-context.sh qualifies by filename alone; its
    1875-char source was the sole 'hit' in several empty-context cases in the
    2026-08-05/08-10 runs. Only results from under .context/architecture-context/
    evidence documentation; the script read still counts as an attempt."""
    script = ("#!/bin/bash\n# Fetches the latest RHOAI architecture context.\n"
              + "echo fetching...\n" * 40)
    case_dir = _write_case(tmp_path, [(EMPTY_LISTING, False)], extra_events=[
        _tool_use("s1", "Read", {"file_path": "scripts/fetch-architecture-context.sh"}),
        _tool_result("s1", script),
    ])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "0 of 2" in rationale


def test_sibling_directory_paths_are_not_credited(check, tmp_path):
    """.context/architecture-context-backup/ shares the prefix but is not the
    staged docs dir; a long read from it stays an attempt, never a hit."""
    case_dir = _write_case(tmp_path, [(EMPTY_LISTING, False)], extra_events=[
        _tool_use("b1", "Read",
                  {"file_path": ".context/architecture-context-backup/README.md"}),
        _tool_result("b1", DOC),
    ])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "0 of 2" in rationale


def test_grep_of_the_docs_dir_itself_counts(check, tmp_path):
    """Grep/Glob address the docs dir as a path with NO trailing slash (34 of
    the 291 qualifying structured reads in the stored runs); the directory
    boundary check must keep these eligible."""
    case_dir = _write_case(tmp_path, [], extra_events=[
        _tool_use("g1", "Grep",
                  {"pattern": "KServe", "path": ".context/architecture-context"}),
        _tool_result("g1", DOC),
    ])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is True
    assert "1 of 1" in rationale


def test_unrelated_reads_and_tools_are_not_credited(check, tmp_path):
    """Long results count only for architecture-context reads: not reads of other
    files (refine always reads the strategy itself), not non-read tools, and not
    results that match no qualifying call."""
    case_dir = _write_case(tmp_path, [(EMPTY_LISTING, False)], extra_events=[
        _tool_use("d1", "Read", {"file_path": "artifacts/strat-tasks/STRAT-001.md"}),
        _tool_result("d1", DOC),
        _tool_use("d2", "Write",
                  {"file_path": "notes/architecture-context.md", "content": DOC}),
        _tool_result("d2", DOC),
        _tool_result("dangling", DOC),
    ])

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "0 of 1" in rationale


def test_list_shaped_content_counts_by_serialized_length(check, tmp_path):
    """Captures may carry tool_result content as [{'type': 'text', ...}] blocks."""
    hit = _write_case(tmp_path / "hit", [([{"type": "text", "text": DOC}], False)])
    miss = _write_case(
        tmp_path / "miss", [([{"type": "text", "text": EMPTY_LISTING}], False)])

    value_hit, _ = check({"case_dir": str(hit)})
    value_miss, _ = check({"case_dir": str(miss)})

    assert value_hit is True
    assert value_miss is False


def test_malformed_transcript_lines_do_not_error_the_judge(check, tmp_path):
    """A corrupted capture must not raise: an errored check drops the case from
    the min_pass_rate denominator, failing open at the run level."""
    case_dir = _write_case(tmp_path, [(DOC, False)])
    with open(case_dir / "subagents" / "refine.jsonl", "a") as f:
        f.write("null\n")
        f.write('"compacted"\n')
        f.write("123\n")
        f.write(json.dumps(
            {"sessionId": SESSION, "type": "summary", "message": "compacted"}) + "\n")

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is True
    assert "1 of 1" in rationale


def test_reads_from_another_session_are_not_credited(check, tmp_path):
    """Reviewer transcripts land in the same directory; only refine's count."""
    case_dir = _write_case(tmp_path, [(DOC, False)], session=OTHER_SESSION)
    step_log = case_dir / "steps" / "refine" / "stdout.log"
    step_log.write_text(json.dumps({"type": "system", "session_id": SESSION}) + "\n")

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "no subagent transcript belongs to the refine session" in rationale


def test_fails_closed_without_a_step_log(check, tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "stdout.log" in rationale


def test_fails_closed_without_subagent_transcripts(check, tmp_path):
    case_dir = _write_case(tmp_path, [(DOC, False)])
    os.remove(case_dir / "subagents" / "refine.jsonl")

    value, rationale = check({"case_dir": str(case_dir)})

    assert value is False
    assert "no subagent transcripts" in rationale


def test_the_initiative_eval_differs_only_in_its_type_lines():
    """eval/strat-refine-initiative.yaml is eval/strat-refine.yaml run with --type initiative-strategy on
    the Initiative dataset: judges, thresholds, permissions, hooks and models stay the RFE eval's."""
    with open(EVAL_YAML, encoding="utf-8") as fh:
        rfe = yaml.safe_load(fh)
    with open(os.path.join(PROJECT_ROOT, "eval", "strat-refine-initiative.yaml"), encoding="utf-8") as fh:
        initiative = yaml.safe_load(fh)
    assert initiative["name"] == "strat-creator-eval-initiative"
    assert initiative["dataset"]["path"] == "dataset/initiative-cases"
    for rfe_step, step in zip(rfe["execution"]["steps"], initiative["execution"]["steps"], strict=True):
        assert step["arguments"] == rfe_step["arguments"].replace(
            "{{ input.strat_id }}", "{{ input.strat_id }} --type initiative-strategy")
    for config in (rfe, initiative):
        del config["name"], config["description"], config["dataset"]["path"], config["dataset"]["schema"]
        config["outputs"][0].pop("schema")
        for step in config["execution"]["steps"]:
            del step["arguments"]
    assert initiative == rfe
