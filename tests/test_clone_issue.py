"""Integration tests for clone_issue.py against jira-emulator."""
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "clone_issue.py")


def _env(jira):
    return {
        **os.environ,
        "JIRA_SERVER": jira.url,
        "JIRA_USER": "admin",
        "JIRA_TOKEN": "admin",
    }


def _run(jira, args, env_override=None):
    env = env_override if env_override is not None else _env(jira)
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, env=env,
    )


class TestCloneIssue:

    def test_clones_issue_with_summary_priority_and_link(self, jira):
        jira.create("RHAIRFE-1000", "GPU sharing for notebooks",
                     "Enable time-sliced GPU sharing.")

        result = _run(jira, ["RHAIRFE-1000", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        assert new_key.startswith("RHAISTRAT-"), f"unexpected key: {new_key}"

        clone = jira.get(new_key)
        assert clone["fields"]["summary"] == "[DRAFT] GPU sharing for notebooks"

        source = jira.get("RHAIRFE-1000")
        source_priority = source["fields"].get("priority")
        clone_priority = clone["fields"].get("priority")
        if isinstance(source_priority, dict) and isinstance(clone_priority, dict):
            assert clone_priority["name"] == source_priority["name"]

        links = clone["fields"].get("issuelinks", [])
        cloner_links = [
            lk for lk in links
            if lk.get("type", {}).get("name") == "Cloners"
        ]
        assert len(cloner_links) >= 1
        linked_keys = []
        for lk in cloner_links:
            for direction in ("inwardIssue", "outwardIssue"):
                issue = lk.get(direction)
                if issue:
                    linked_keys.append(issue["key"])
        assert "RHAIRFE-1000" in linked_keys

    def test_labels_are_copied_to_clone(self, jira):
        jira.create("RHAIRFE-1001", "Model serving autoscaler",
                     "Autoscale model serving pods.",
                     labels=["strat-creator-3.5", "tech-reviewed"])

        result = _run(jira, ["RHAIRFE-1001", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        clone_labels = clone["fields"].get("labels", [])
        assert "strat-creator-3.5" in clone_labels
        assert "tech-reviewed" in clone_labels

    def test_rfe_creator_and_processing_labels_are_not_copied(self, jira):
        jira.create("RHAIRFE-1010", "Flagged RFE", "Description.",
                     labels=["rfe-creator-needs-attention",
                             "rfe-creator-autofix-rubric-pass",
                             "strat-creator-processing", "foo"])

        result = _run(jira, ["RHAIRFE-1010", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        clone = jira.get(result.stdout.strip())
        clone_labels = [label for label in clone["fields"].get("labels", [])
                        if not label.startswith("strat-creator-clone-")]
        assert clone_labels == ["foo"]

    def test_components_and_affects_versions_are_copied(self, jira):
        jira.create("RHAIRFE-1002", "Pipeline orchestration",
                     "Orchestrate ML pipelines.",
                     components=["Dashboard", "Model Registry"],
                     fix_versions=["2.12", "2.13"],
                     affects_versions=["2.10"])

        result = _run(jira, ["RHAIRFE-1002", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        clone_fields = clone["fields"]

        comp_names = sorted(c["name"] for c in clone_fields.get("components", []))
        assert comp_names == ["Dashboard", "Model Registry"]

        assert clone_fields.get("fixVersions", []) == []

        aff_names = [v["name"] for v in clone_fields.get("versions", [])]
        assert aff_names == ["2.10"]

    def test_target_version_is_copied_to_clone(self, jira):
        # Seed versions into RHAISTRAT so they can be resolved.
        jira.create("RHAISTRAT-599", "Version seed",
                     "Seeds versions into RHAISTRAT.",
                     issue_type="Feature",
                     fix_versions=["3.6 EA1 RHOAI RELEASE",
                                   "3.6 GA RHOAI RELEASE"])

        jira.create("RHAIRFE-1003", "Model registry GA",
                     "Promote model registry to GA.",
                     target_versions=["3.6 EA1 RHOAI RELEASE",
                                      "3.6 GA RHOAI RELEASE"])

        result = _run(jira, ["RHAIRFE-1003", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        target = clone["fields"].get("customfield_10855") or []
        # Versions are resolved by id against RHAISTRAT; verify both present.
        project = jira.request("GET", "/rest/api/3/project/RHAISTRAT")
        tp_ids = {v["name"]: v["id"] for v in project["versions"]}
        clone_ids = sorted(v["id"] for v in target)
        expected_ids = sorted([tp_ids["3.6 EA1 RHOAI RELEASE"],
                               tp_ids["3.6 GA RHOAI RELEASE"]])
        assert clone_ids == expected_ids

    def test_target_version_resolves_by_name_in_target_project(self, jira):
        # Seed "3.6 GA RHOAI RELEASE" into RHAISTRAT as a project version.
        jira.create("RHAISTRAT-600", "Version seed",
                     "Exists only to seed a version into RHAISTRAT.",
                     issue_type="Feature",
                     fix_versions=["3.6 GA RHOAI RELEASE"])

        # Fetch the target-project version id so we can assert against it.
        project = jira.request("GET",
                               "/rest/api/3/project/RHAISTRAT")
        tp_ids = {v["name"]: v["id"] for v in project["versions"]}
        expected_id = tp_ids["3.6 GA RHOAI RELEASE"]

        # Source RFE has two target versions; only one exists in RHAISTRAT.
        jira.create("RHAIRFE-1004", "Serving GA",
                     "Promote serving to GA.",
                     target_versions=[
                         {"id": "107606", "name": "3.6 GA RHAII RELEASE"},
                         {"id": "107607", "name": "3.6 GA RHOAI RELEASE"}])

        result = _run(jira, ["RHAIRFE-1004", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        target = clone["fields"].get("customfield_10855") or []
        assert len(target) == 1
        assert target[0]["id"] == expected_id
        assert "3.6 GA RHAII RELEASE" not in [v.get("name", "") for v in target]
        assert "omitting" in result.stderr.lower()

    def test_target_version_all_unmatched_omits_field(self, jira):
        # No versions seeded into RHAISTRAT for this test — both should be
        # omitted, and customfield_10855 should not be set.
        jira.create("RHAIRFE-1006", "No matching versions",
                     "Neither source version exists in RHAISTRAT.",
                     target_versions=[
                         {"id": "999998", "name": "NONEXISTENT-A"},
                         {"id": "999999", "name": "NONEXISTENT-B"}])

        result = _run(jira, ["RHAIRFE-1006", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        target = clone["fields"].get("customfield_10855") or []
        assert target == []

    def test_target_version_without_name_is_omitted(self, jira):
        jira.create("RHAIRFE-1007", "Nameless version test",
                     "Version object with only id, no name.",
                     target_versions=[
                         {"id": "999999"}])

        result = _run(jira, ["RHAIRFE-1007", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        target = clone["fields"].get("customfield_10855") or []
        assert target == []

    def test_clone_inherits_parent_outcome_from_rfe(self, jira):
        jira.create("RHAISTRAT-500", "AI Hub Delivery",
                     "Top-level Outcome.", issue_type="Outcome")
        jira.create("RHAIRFE-1003", "Agent catalog search",
                     "Search for agents in the catalog.",
                     parent_key="RHAISTRAT-500")

        result = _run(jira, ["RHAIRFE-1003", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        assert clone["fields"]["parent"]["key"] == "RHAISTRAT-500"
        assert "Setting parent Outcome: RHAISTRAT-500" in result.stderr

    def test_clone_skips_closed_parent_outcome(self, jira):
        jira.create("RHAISTRAT-501", "Old Outcome",
                     "Deprecated.", issue_type="Outcome", status="Closed")
        jira.create("RHAIRFE-1004", "Legacy feature",
                     "Should not inherit closed parent.",
                     parent_key="RHAISTRAT-501")

        result = _run(jira, ["RHAIRFE-1004", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        assert clone["fields"].get("parent") is None
        assert "Closed" in result.stderr

    def test_clone_no_parent_on_rfe(self, jira):
        jira.create("RHAIRFE-1005", "Standalone feature",
                     "No parent Outcome on the RFE.")

        result = _run(jira, ["RHAIRFE-1005", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        assert clone["fields"].get("parent") is None
        assert "No parent Outcome found" in result.stderr

    def test_does_not_double_draft_prefix(self, jira):
        jira.create("RHAIRFE-1008", "[DRAFT] Already prefixed feature",
                     "Source RFE already has [DRAFT] in summary.")

        result = _run(jira, ["RHAIRFE-1008", "--target-project", "RHAISTRAT"])
        assert result.returncode == 0, f"stderr: {result.stderr}"

        new_key = result.stdout.strip()
        clone = jira.get(new_key)
        assert clone["fields"]["summary"] == "[DRAFT] Already prefixed feature"

    def test_missing_env_vars_exits_with_code_2(self, jira):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")}

        result = _run(jira, ["RHAIRFE-1000", "--target-project", "RHAISTRAT"],
                       env_override=env)
        assert result.returncode == 2
