import json
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "frontmatter.py")
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")


def run_fm(tmp_path, *args, check=True):
    """Run frontmatter.py as a subprocess."""
    env = os.environ.copy()
    env["PYTHONPATH"] = SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, SCRIPT, *args],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"frontmatter.py {' '.join(args)} failed (rc={result.returncode}):\n"
            f"  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )
    return result


# ─── schema ───────────────────────────────────────────────────────────────────


class TestSchema:

    def test_schema_strat_task(self, tmp_path):
        result = run_fm(tmp_path, "schema", "strat-task")
        assert "required:" in result.stdout
        assert "optional:" in result.stdout
        assert "strat_id" in result.stdout
        assert "title" in result.stdout
        assert "source_rfe" in result.stdout
        assert "priority" in result.stdout
        assert "status" in result.stdout

    def test_schema_strat_review(self, tmp_path):
        result = run_fm(tmp_path, "schema", "strat-review")
        assert "required:" in result.stdout
        assert "scores" in result.stdout
        assert "reviewers" in result.stdout
        assert "recommendation" in result.stdout
        assert "feasibility" in result.stdout
        assert "testability" in result.stdout

    def test_schema_rfe_task(self, tmp_path):
        result = run_fm(tmp_path, "schema", "rfe-task")
        assert "rfe_id" in result.stdout

    def test_schema_rfe_review(self, tmp_path):
        result = run_fm(tmp_path, "schema", "rfe-review")
        assert "rfe_id" in result.stdout
        assert "score" in result.stdout

    def test_schema_shows_enums(self, tmp_path):
        result = run_fm(tmp_path, "schema", "strat-task")
        assert "Draft" in result.stdout
        assert "Refined" in result.stdout
        assert "Blocker" in result.stdout

    def test_schema_shows_patterns(self, tmp_path):
        result = run_fm(tmp_path, "schema", "strat-task")
        assert "pattern" in result.stdout


# ─── set ──────────────────────────────────────────────────────────────────────


class TestSet:

    def _strat_task_path(self, tmp_path, name="STRAT-001.md"):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / name)

    def _strat_review_path(self, tmp_path, name="STRAT-001-review.md"):
        d = tmp_path / "strat-reviews"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / name)

    def test_creates_valid_strat_task(self, tmp_path):
        path = self._strat_task_path(tmp_path)
        result = run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Enable GPU sharing",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
        )
        assert result.returncode == 0
        assert "OK:" in result.stdout
        assert os.path.exists(path)

    def test_creates_valid_strat_review(self, tmp_path):
        path = self._strat_review_path(tmp_path)
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "recommendation=approve",
            "needs_attention=false",
            "scores.feasibility=3",
            "scores.testability=3",
            "scores.scope=2",
            "scores.architecture=4",
            "scores.total=12",
            "reviewers.feasibility=approve",
            "reviewers.testability=approve",
            "reviewers.scope=approve",
            "reviewers.architecture=approve",
        )
        assert os.path.exists(path)

    def test_rejects_invalid_status(self, tmp_path):
        path = self._strat_task_path(tmp_path)
        result = run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Test",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Bogus",
            check=False,
        )
        assert result.returncode == 1
        assert "Error" in result.stderr

    def test_rejects_unknown_field(self, tmp_path):
        path = self._strat_task_path(tmp_path)
        result = run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Test",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
            "unknown_field=value",
            check=False,
        )
        assert result.returncode == 1
        assert "unknown field" in result.stderr.lower()

    def test_updates_existing_file(self, tmp_path):
        path = self._strat_task_path(tmp_path)
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Original",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
        )
        run_fm(tmp_path, "set", path, "status=Refined")
        result = run_fm(tmp_path, "read", path)
        data = json.loads(result.stdout)
        assert data["status"] == "Refined"
        assert data["title"] == "Original"


# ─── set with dot notation ───────────────────────────────────────────────────


class TestSetDotNotation:

    def test_scores_dot_notation(self, tmp_path):
        d = tmp_path / "strat-reviews"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001-review.md")
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "recommendation=approve",
            "needs_attention=false",
            "scores.feasibility=5",
            "scores.testability=4",
            "scores.scope=3",
            "scores.architecture=5",
            "scores.total=17",
            "reviewers.feasibility=approve",
            "reviewers.testability=approve",
            "reviewers.scope=revise",
            "reviewers.architecture=approve",
        )
        result = run_fm(tmp_path, "read", path)
        data = json.loads(result.stdout)
        assert data["scores"]["feasibility"] == 5
        assert data["scores"]["total"] == 17
        assert data["reviewers"]["scope"] == "revise"

    def test_invalid_nested_field_rejected(self, tmp_path):
        d = tmp_path / "strat-reviews"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-002-review.md")
        result = run_fm(
            tmp_path, "set", path,
            "scores.nonexistent=9",
            check=False,
        )
        assert result.returncode == 1
        assert "unknown field" in result.stderr.lower()

    def test_dot_notation_on_non_dict_field_rejected(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001.md")
        result = run_fm(
            tmp_path, "set", path,
            "title.sub=value",
            check=False,
        )
        assert result.returncode == 1
        assert "not a dict" in result.stderr.lower()


# ─── read ─────────────────────────────────────────────────────────────────────


class TestRead:

    def test_outputs_json(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001.md")
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Test Feature",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
        )
        result = run_fm(tmp_path, "read", path)
        data = json.loads(result.stdout)
        assert data["strat_id"] == "STRAT-001"
        assert data["title"] == "Test Feature"
        assert isinstance(data, dict)

    def test_validates_against_schema(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001.md")
        with open(path, "w") as f:
            f.write("---\nstrat_id: STRAT-001\n---\nBody\n")
        result = run_fm(tmp_path, "read", path, check=False)
        assert result.returncode == 1
        assert "Error" in result.stderr

    def test_missing_file(self, tmp_path):
        result = run_fm(tmp_path, "read", "nonexistent.md", check=False)
        assert result.returncode == 1
        assert "not found" in result.stderr

    def test_read_with_defaults_applied(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001.md")
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Test",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
        )
        result = run_fm(tmp_path, "read", path)
        data = json.loads(result.stdout)
        assert data["jira_key"] is None


# ─── batch-read ───────────────────────────────────────────────────────────────


class TestBatchRead:

    def test_reads_multiple_files(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)

        paths = []
        for i in range(1, 3):
            path = str(d / f"STRAT-00{i}.md")
            run_fm(
                tmp_path, "set", path,
                f"strat_id=STRAT-00{i}",
                f"title=Feature {i}",
                "source_rfe=RHAIRFE-100",
                "priority=Major",
                "status=Draft",
            )
            paths.append(path)

        result = run_fm(tmp_path, "batch-read", *paths)
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["strat_id"] == "STRAT-001"
        assert data[1]["strat_id"] == "STRAT-002"

    def test_batch_read_missing_file_returns_error_entry(self, tmp_path):
        result = run_fm(tmp_path, "batch-read", "nonexistent.md")
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert "_error" in data[0]
        assert "not found" in data[0]["_error"]

    def test_batch_read_includes_file_path(self, tmp_path):
        d = tmp_path / "strat-tasks"
        d.mkdir(parents=True, exist_ok=True)
        path = str(d / "STRAT-001.md")
        run_fm(
            tmp_path, "set", path,
            "strat_id=STRAT-001",
            "title=Test",
            "source_rfe=RHAIRFE-100",
            "priority=Major",
            "status=Draft",
        )
        result = run_fm(tmp_path, "batch-read", path)
        data = json.loads(result.stdout)
        assert data[0]["_file"] == path


# ─── per-file schemas ─────────────────────────────────────────────────────────


class TestPerTypeSchema:
    """A strategy file validates against its own type's schema: its frontmatter `type:`, else the id its
    file name starts with. rfe-strategy keeps strat-task / strat-review; initiative-strategy's are
    initiative-strategy-task / initiative-strategy-review."""

    TWIN_TASK = ["strat_id=RHOAIENG-1", "title=Initiative", "source_initiative=RHOAIENG-1",
                 "jira_key=RHOAIENG-1", "priority=Major", "status=Draft"]

    def test_initiative_task_files(self, tmp_path):
        path = "artifacts/strat-tasks/RHOAIENG-1.md"
        run_fm(tmp_path, "set", path, *self.TWIN_TASK, "type=initiative-strategy")
        run_fm(tmp_path, "set", path, "status=Refined", "refine_count=1")
        data = json.loads(run_fm(tmp_path, "read", path).stdout)
        assert (data["source_initiative"], data["status"], data["type"]) == (
            "RHOAIENG-1", "Refined", "initiative-strategy")
        # an unstamped local draft: its ISTRAT- id names the type
        run_fm(tmp_path, "set", "artifacts/strat-tasks/ISTRAT-2.md", "strat_id=ISTRAT-2", "title=Draft",
               "source_initiative=INIT-3", "priority=Major", "status=Draft")

    def test_the_stamp_decides_before_the_file_name(self, tmp_path):
        path = "artifacts/strat-tasks/draft.md"
        run_fm(tmp_path, "set", path, *self.TWIN_TASK, "type=initiative-strategy")
        # source_initiative is an initiative-only field: this set passes only if the stamp is read from disk
        run_fm(tmp_path, "set", path, "status=Ready", "source_initiative=RHOAIENG-2")
        data = json.loads(run_fm(tmp_path, "read", path).stdout)
        assert (data["status"], data["source_initiative"]) == ("Ready", "RHOAIENG-2")
        result = run_fm(tmp_path, "set", "artifacts/strat-tasks/RHOAIENG-2.md", *self.TWIN_TASK,
                        "type=rfe-strategy", check=False)
        assert result.returncode == 1
        assert "unknown field 'source_initiative' for schema 'strat-task'" in result.stderr

    def test_each_type_refuses_the_others_fields(self, tmp_path):
        result = run_fm(tmp_path, "set", "artifacts/strat-tasks/RHOAIENG-1.md", "source_rfe=RHAIRFE-1",
                        check=False)
        assert result.returncode == 1
        assert "unknown field 'source_rfe' for schema 'initiative-strategy-task'" in result.stderr
        result = run_fm(tmp_path, "set", "artifacts/strat-tasks/RHAISTRAT-5.md", "source_initiative=RHOAIENG-1",
                        check=False)
        assert result.returncode == 1
        assert "unknown field 'source_initiative' for schema 'strat-task'" in result.stderr

    def test_schema_prints_the_initiative_schemas(self, tmp_path):
        task = run_fm(tmp_path, "schema", "initiative-strategy-task").stdout
        assert "source_initiative" in task and "source_rfe" not in task
        assert r"^(ISTRAT-\d+|RHOAIENG-\d+)$" in task and "- initiative-strategy" in task
        review = run_fm(tmp_path, "schema", "initiative-strategy-review").stdout
        assert "reviewers" in review and r"^(ISTRAT-\d+|RHOAIENG-\d+)$" in review
