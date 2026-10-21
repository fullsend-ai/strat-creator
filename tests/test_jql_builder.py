import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from jira_utils import build_jql_from_config

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestBuildJqlFromConfig:

    def test_default_settings_exclude_rework_needed_label(self):
        config = os.path.join(PROJECT_ROOT, "config", "pipeline-settings.yaml")

        jql = build_jql_from_config(config)

        assert ('(labels NOT IN ("strat-creator-processing", '
                '"strat-creator-rework-needed") OR labels IS EMPTY)') in jql

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
