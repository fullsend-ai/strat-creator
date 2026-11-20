import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import jira_utils  # noqa: E402
from jira_utils import TargetVersionError, build_jql_from_config, resolve_target_versions  # noqa: E402


def _versions(*names, **flags):
    return [{"name": n, **flags} for n in names]


class TestResolveTargetVersions:
    """jql.target_version_trains resolves to RHAIRFE's live, unreleased version names."""

    TRAINS = {"project": "RHAIRFE", "target_version_trains": ["3.7", "3.8"]}

    def test_deterministic_order_train_then_event_then_product(self):
        live = _versions("3.8 GA RHAII RELEASE", "3.7 GA RHELAI RELEASE", "3.7 EA2 RHOAI RELEASE",
                         "3.7 EA1 RHELAI RELEASE", "3.7 GA RHAII RELEASE", "3.7 EA RHOAI RELEASE",
                         "3.7 EA1 RHAII RELEASE", "3.8 EA1 RHOAI RELEASE")
        assert resolve_target_versions(self.TRAINS, live) == [
            "3.7 EA RHOAI RELEASE", "3.7 EA1 RHAII RELEASE", "3.7 EA1 RHELAI RELEASE",
            "3.7 EA2 RHOAI RELEASE", "3.7 GA RHAII RELEASE", "3.7 GA RHELAI RELEASE",
            "3.8 EA1 RHOAI RELEASE", "3.8 GA RHAII RELEASE"]
        cfg = {**self.TRAINS, "target_version_trains": ["3.8", "3.7"]}
        assert resolve_target_versions(cfg, live)[:2] == ["3.8 EA1 RHOAI RELEASE", "3.8 GA RHAII RELEASE"]

    def test_released_and_archived_versions_are_excluded(self):
        live = (_versions("3.7 EA1 RHOAI RELEASE", released=True)
                + _versions("3.7 EA2 RHOAI RELEASE", archived=True)
                + _versions("3.7 GA RHOAI RELEASE", released=False, archived=False)
                + _versions("3.8 GA RHOAI RELEASE"))
        assert resolve_target_versions(self.TRAINS, live) == ["3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE"]

    def test_a_train_with_no_live_version_fails_loudly(self):
        live = _versions("3.7 GA RHOAI RELEASE") + _versions("3.8 GA RHOAI RELEASE", released=True)
        with pytest.raises(TargetVersionError, match=r"train 3\.8 matched no unreleased versions in RHAIRFE"):
            resolve_target_versions(self.TRAINS, live)

    def test_only_off_pattern_versions_fail_loudly_too(self, capsys):
        live = _versions("3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE - [SUMMIT]")
        with pytest.raises(TargetVersionError, match=r"train 3\.8"):
            resolve_target_versions(self.TRAINS, live)
        assert "'3.8 GA RHOAI RELEASE - [SUMMIT]'" in capsys.readouterr().err

    def test_off_pattern_train_versions_are_skipped_with_a_warning(self, capsys):
        live = _versions("3.7 [Fast-1] RHAII RELEASE", "3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE",
                         "3.8 GA RHOAI RELEASE - [SUMMIT]", "3.6 [Fast-1] RHAII RELEASE",
                         "rhai-3.7", "3.70 GA RHOAI RELEASE")
        live += _versions("3.7 [Fast-2] RHAII RELEASE", released=True)
        assert resolve_target_versions(self.TRAINS, live) == ["3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE"]
        err = capsys.readouterr().err
        assert "warning: skipping target version '3.7 [Fast-1] RHAII RELEASE' of train 3.7" in err
        assert "warning: skipping target version '3.8 GA RHOAI RELEASE - [SUMMIT]' of train 3.8" in err
        # Other trains, released versions and other first tokens are not this config's drift.
        assert "3.6 [Fast-1]" not in err and "Fast-2" not in err and "rhai-3.7" not in err and "3.70" not in err

    def test_whitespace_drift_resolves_to_the_live_name(self):
        live = _versions("3.7 GA  RHAII RELEASE", "3.8 GA RHOAI RELEASE")
        assert resolve_target_versions(self.TRAINS, live) == ["3.7 GA  RHAII RELEASE", "3.8 GA RHOAI RELEASE"]

    def test_extra_target_versions_are_appended_and_deduped(self):
        cfg = {**self.TRAINS, "extra_target_versions": ["3.7 EA RHELAI RELEASE", "3.7 GA RHOAI RELEASE",
                                                        "rhai-3.7", "rhai-3.7"]}
        live = _versions("3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE")
        assert resolve_target_versions(cfg, live) == [
            "3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE", "3.7 EA RHELAI RELEASE", "rhai-3.7"]

    def test_extra_target_versions_alone_need_no_jira(self, monkeypatch):
        monkeypatch.setattr(jira_utils, "get_project_versions", lambda *a: pytest.fail("no train to resolve"))
        assert resolve_target_versions({"extra_target_versions": ["a", "a", "b"]}) == ["a", "b"]

    def test_static_target_versions_override_wins(self, monkeypatch):
        monkeypatch.setattr(jira_utils, "get_project_versions", lambda *a: pytest.fail("the override needs no Jira"))
        cfg = {**self.TRAINS, "target_versions": ["3.7 GA  RHAII RELEASE", "rhoai-3.6"],
               "extra_target_versions": ["x"]}
        assert resolve_target_versions(cfg) == ["3.7 GA  RHAII RELEASE", "rhoai-3.6"]

    def test_no_version_settings_resolve_to_nothing(self):
        assert resolve_target_versions({}) == []
        assert resolve_target_versions({"target_versions": [], "target_version_trains": []}) == []

    def test_an_unquoted_train_is_refused(self):
        # YAML reads an unquoted 3.10 as the float 3.1: resolving "3.1" would be the wrong train.
        with pytest.raises(TargetVersionError, match="must be a quoted string"):
            resolve_target_versions({"target_version_trains": [3.1]}, [])

    def test_trains_fetch_the_project_versions(self, monkeypatch):
        monkeypatch.setattr(jira_utils, "require_env", lambda: ("https://jira", "u", "t"))
        calls = []
        monkeypatch.setattr(jira_utils, "get_project_versions",
                            lambda s, u, t, project: calls.append((s, project)) or _versions(
                                "3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE"))
        assert resolve_target_versions(self.TRAINS) == ["3.7 GA RHOAI RELEASE", "3.8 GA RHOAI RELEASE"]
        assert calls == [("https://jira", "RHAIRFE")]

    def test_trains_without_credentials_fail_loudly(self, monkeypatch):
        monkeypatch.setattr(jira_utils, "require_env", lambda: (None, None, None))
        with pytest.raises(TargetVersionError, match="JIRA_SERVER, JIRA_USER, JIRA_TOKEN"):
            resolve_target_versions(self.TRAINS)


class TestTrainRenames:
    """The 2026-11-18 EA -> EA1 rename against a fixture: picked up, or a red run; never silent."""

    CONFIG = """\
jql:
  project: RHAIRFE
  required_labels:
    - strat-creator-3.7
  target_version_trains: ["3.7"]
  quality_labels:
    - tech-reviewed
  order_by: key ASC
"""

    def _config(self, tmp_path):
        path = tmp_path / "pipeline-settings.yaml"
        path.write_text(self.CONFIG)
        return str(path)

    def test_a_rename_inside_the_pattern_is_picked_up(self, tmp_path, capsys):
        before = build_jql_from_config(self._config(tmp_path), _versions("3.7 EA RHOAI RELEASE"))
        after = build_jql_from_config(self._config(tmp_path), _versions("3.7 EA1 RHOAI RELEASE"))
        assert before == ('project = RHAIRFE AND (labels = "strat-creator-3.7" OR cf[10855] in '
                          '("3.7 EA RHOAI RELEASE")) AND (labels = "tech-reviewed") ORDER BY key ASC')
        assert after == before.replace("3.7 EA RHOAI", "3.7 EA1 RHOAI")
        err = capsys.readouterr().err
        assert "resolved target versions (1): 3.7 EA RHOAI RELEASE\n" in err
        assert "resolved target versions (1): 3.7 EA1 RHOAI RELEASE\n" in err

    def test_a_rename_outside_the_pattern_fails_the_run(self, tmp_path):
        with pytest.raises(TargetVersionError, match=r"train 3\.7 matched no unreleased versions"):
            build_jql_from_config(self._config(tmp_path), _versions("3.7 Early Access RHOAI RELEASE"))


class TestBuildJqlFromConfig:

    def _write_config(self, tmp_path, content):
        path = tmp_path / "pipeline-settings.yaml"
        path.write_text(content)
        return str(path)

    def test_full_config(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - strat-creator-3.5
  quality_labels:
    - rfe-creator-autofix-rubric-pass
    - tech-reviewed
  excluded_statuses:
    - Closed
    - Resolved
    - Draft
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert jql.startswith("project = RHAIRFE")
        assert 'labels = "strat-creator-3.5"' in jql
        assert 'labels = "rfe-creator-autofix-rubric-pass"' in jql
        assert 'labels = "tech-reviewed"' in jql
        assert " OR " in jql
        assert 'status NOT IN ("Closed", "Resolved", "Draft")' in jql
        assert jql.endswith("ORDER BY key ASC")

    def test_clause_ordering(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - label-a
  quality_labels:
    - label-b
  excluded_statuses:
    - Closed
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        proj_pos = jql.index("project = RHAIRFE")
        req_pos = jql.index('labels = "label-a"')
        qual_pos = jql.index('labels = "label-b"')
        status_pos = jql.index("status NOT IN")
        order_pos = jql.index("ORDER BY")
        assert proj_pos < req_pos < qual_pos < status_pos < order_pos

    def test_empty_quality_labels(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: MYPROJ
  required_labels:
    - required-one
  quality_labels: []
  excluded_statuses:
    - Closed
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert "project = MYPROJ" in jql
        assert 'labels = "required-one"' in jql
        assert 'status NOT IN ("Closed")' in jql

    def test_empty_excluded_statuses(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: TESTPROJ
  required_labels:
    - some-label
  quality_labels:
    - quality-one
  excluded_statuses: []
  order_by: created DESC
""")
        jql = build_jql_from_config(config)
        assert "project = TESTPROJ" in jql
        assert "status NOT IN" not in jql
        assert jql.endswith("ORDER BY created DESC")

    def test_missing_fields_use_defaults(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql: {}
""")
        jql = build_jql_from_config(config)
        assert "project = RHAIRFE" in jql
        assert "ORDER BY key ASC" in jql
        assert " OR " not in jql
        assert "status NOT IN" not in jql

    def test_missing_jql_key_uses_defaults(self, tmp_path):
        config = self._write_config(tmp_path, """\
batch_size: 5
""")
        jql = build_jql_from_config(config)
        assert "project = RHAIRFE" in jql
        assert "ORDER BY key ASC" in jql

    def test_multiple_required_labels_are_ored(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - label-alpha
    - label-beta
  quality_labels: []
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert 'labels = "label-alpha" OR labels = "label-beta"' in jql

    def test_quality_labels_grouped_with_or(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  quality_labels:
    - q1
    - q2
    - q3
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert '(labels = "q1" OR labels = "q2" OR labels = "q3")' in jql

    def test_single_quality_label(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  quality_labels:
    - single-q
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert '(labels = "single-q")' in jql

    def test_no_order_by(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  quality_labels: []
  excluded_statuses: []
  order_by: ""
""")
        jql = build_jql_from_config(config)
        assert "ORDER BY" not in jql

    def test_exact_format_without_target_versions(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - strat-creator-3.5
  quality_labels:
    - rfe-creator-autofix-rubric-pass
    - tech-reviewed
  excluded_statuses:
    - Closed
    - Resolved
    - Draft
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        expected = (
            'project = RHAIRFE'
            ' AND labels = "strat-creator-3.5"'
            ' AND (labels = "rfe-creator-autofix-rubric-pass"'
            ' OR labels = "tech-reviewed")'
            ' AND status NOT IN ("Closed", "Resolved", "Draft")'
            ' ORDER BY key ASC'
        )
        assert jql == expected

    def test_exact_format_with_target_versions(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - strat-creator-3.5
    - strat-creator-3.6
  target_versions:
    - rhoai-3.5
    - rhoai-3.6
  quality_labels:
    - rfe-creator-autofix-rubric-pass
    - tech-reviewed
  excluded_statuses:
    - Closed
    - Resolved
    - Draft
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        expected = (
            'project = RHAIRFE'
            ' AND (labels = "strat-creator-3.5" OR labels = "strat-creator-3.6"'
            ' OR cf[10855] in ("rhoai-3.5", "rhoai-3.6"))'
            ' AND (labels = "rfe-creator-autofix-rubric-pass"'
            ' OR labels = "tech-reviewed")'
            ' AND status NOT IN ("Closed", "Resolved", "Draft")'
            ' ORDER BY key ASC'
        )
        assert jql == expected

    def test_target_versions_only_no_required_labels(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  target_versions:
    - rhoai-3.5
  quality_labels:
    - tech-reviewed
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert 'cf[10855] in ("rhoai-3.5")' in jql
        assert "labels = " not in jql.split("AND")[1] or "tech-reviewed" in jql

    def test_excluded_labels(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  quality_labels: []
  excluded_labels:
    - strat-creator-processing
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert ('(labels NOT IN ("strat-creator-processing")'
                ' OR labels IS EMPTY)') in jql

    def test_multiple_excluded_labels(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels: []
  quality_labels: []
  excluded_labels:
    - lock-a
    - lock-b
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert ('(labels NOT IN ("lock-a", "lock-b")'
                ' OR labels IS EMPTY)') in jql

    def test_excluded_labels_omitted(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - some-label
  quality_labels: []
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert "labels NOT IN" not in jql

    def test_excluded_labels_clause_ordering(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - label-a
  quality_labels:
    - label-b
  excluded_labels:
    - strat-creator-processing
  excluded_statuses:
    - Closed
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        qual_pos = jql.index('labels = "label-b"')
        lock_pos = jql.index("labels NOT IN")
        status_pos = jql.index("status NOT IN")
        assert qual_pos < lock_pos < status_pos

    def test_target_versions_empty_list(self, tmp_path):
        config = self._write_config(tmp_path, """\
jql:
  project: RHAIRFE
  required_labels:
    - strat-creator-3.5
  target_versions: []
  quality_labels:
    - tech-reviewed
  excluded_statuses: []
  order_by: key ASC
""")
        jql = build_jql_from_config(config)
        assert 'labels = "strat-creator-3.5"' in jql
        assert "cf[10855]" not in jql
