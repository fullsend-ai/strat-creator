"""Integration tests — script path resolution via CLAUDE_SKILL_DIR."""
import ast
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".claude", "skills")
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")

SCRIPT_REF_PATTERN = re.compile(
    r'(?:python3|bash)\s+\$\{CLAUDE_SKILL_DIR\}/scripts/(\S+)')
SYSPATH_PATTERN = re.compile(
    r"sys\.path\.insert\(0,\s*'\$\{CLAUDE_SKILL_DIR\}/scripts'\)")
IMPORT_PATTERN = re.compile(
    r'from\s+(\w+)\s+import\s+(.+)')
ENV_SKILL_DIR_PATTERN = re.compile(
    r"os\.environ\[.CLAUDE_SKILL_DIR.\]")


def _skills_with_script_refs():
    """Skills whose SKILL.md references ${CLAUDE_SKILL_DIR}/scripts/."""
    results = []
    for name in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, name)
        skill_md = os.path.join(skill_path, "SKILL.md")
        if os.path.isdir(skill_path) and os.path.isfile(skill_md):
            with open(skill_md) as f:
                content = f.read()
            if "${CLAUDE_SKILL_DIR}/scripts/" in content:
                results.append((name, skill_path, content))
    return results


def _collect_multiline_import(lines, start):
    """Collect a parenthesized import that spans multiple lines.

    Given the line index where 'from X import (' starts, joins all lines
    through the closing ')' into a single import-names string.
    """
    combined = lines[start]
    if '(' not in combined:
        return combined
    closed = False
    for k in range(start + 1, min(start + 10, len(lines))):
        combined += " " + lines[k].strip()
        if ')' in lines[k]:
            closed = True
            break
    if not closed:
        raise ValueError(
            f"multiline import at line {start + 1} has no closing parenthesis "
            f"within scan window")
    return combined


def _skills_with_inline_imports():
    """Skills using trusted CLAUDE_SKILL_DIR import patterns."""
    results = []
    for name, path, content in _skills_with_script_refs():
        lines = content.split("\n")
        imports = []
        for i, line in enumerate(lines):
            if SYSPATH_PATTERN.search(line) or \
               ENV_SKILL_DIR_PATTERN.search(line):
                for j in range(i, min(i + 10, len(lines))):
                    m = IMPORT_PATTERN.search(lines[j])
                    if m:
                        raw = m.group(2).strip()
                        if '(' in raw and ')' not in raw:
                            full = _collect_multiline_import(lines, j)
                            m2 = IMPORT_PATTERN.search(full)
                            if m2:
                                raw = m2.group(2).strip()
                        imports.append((m.group(1), raw))
        if imports:
            results.append((name, path, imports))
    return results


ALL_SKILLS = _skills_with_script_refs()
INLINE_IMPORT_SKILLS = _skills_with_inline_imports()


# ─── Script Resolution ───────────────────────────────────────────────────────


class TestScriptResolution:

    @pytest.mark.parametrize("skill_name,skill_path,content", ALL_SKILLS,
                             ids=[s[0] for s in ALL_SKILLS])
    def test_scripts_reachable_via_skill_dir(self, skill_name, skill_path,
                                              content):
        scripts = set(SCRIPT_REF_PATTERN.findall(content))
        scripts_dir = os.path.join(skill_path, "scripts")
        for script in scripts:
            resolved = os.path.join(scripts_dir, script)
            assert os.path.isfile(resolved), (
                f"Cannot reach {script} via "
                f"${{CLAUDE_SKILL_DIR}}/scripts/ for {skill_name}: "
                f"{resolved} does not exist")


# ─── Inline Import Resolution ────────────────────────────────────────────────


class TestInlineImports:

    @pytest.mark.parametrize("skill_name,skill_path,imports",
                             INLINE_IMPORT_SKILLS,
                             ids=[s[0] for s in INLINE_IMPORT_SKILLS])
    def test_inline_imports_succeed(self, skill_name, skill_path, imports):
        scripts_dir = os.path.join(skill_path, "scripts")
        for module_name, import_names in imports:
            cleaned = import_names.replace("(", "").replace(")", "")
            names = [n.strip().rstrip(",") for n in cleaned.split(",")]
            names = [n for n in names if n]
            import_stmt = ", ".join(names)

            stmt_text = f"from {module_name} import {import_stmt}"
            try:
                tree = ast.parse(stmt_text)
            except SyntaxError as e:
                pytest.fail(
                    f"Invalid import syntax in {skill_name}: "
                    f"{stmt_text} ({e})")
            assert len(tree.body) == 1 and isinstance(
                tree.body[0], ast.ImportFrom
            ), f"Expected single ImportFrom in {skill_name}: {stmt_text}"
            node = tree.body[0]
            assert node.module and all(
                part.isidentifier() for part in node.module.split(".")
            ), f"Invalid module name in {skill_name}: {node.module}"
            for alias in node.names:
                assert alias.name.isidentifier(), (
                    f"Invalid import name '{alias.name}' in {skill_name}")

            validated_module = node.module
            validated_names = [a.name for a in node.names]
            helper = (
                "import sys, importlib\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "mod = importlib.import_module(sys.argv[2])\n"
                "for name in sys.argv[3:]:\n"
                "    if not hasattr(mod, name):\n"
                "        print(f'MISSING: {name}', file=sys.stderr)\n"
                "        sys.exit(1)\n"
                "print('OK')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", helper, scripts_dir,
                 validated_module] + validated_names,
                capture_output=True, text=True,
                cwd=PROJECT_ROOT,
            )
            assert result.returncode == 0 and "OK" in result.stdout, (
                f"Inline import failed for {skill_name}: "
                f"from {validated_module} import "
                f"{', '.join(validated_names)}\n"
                f"stderr: {result.stderr}")


# ─── Lightweight E2E ─────────────────────────────────────────────────────────


class TestScriptInvocation:

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             [s for s in ALL_SKILLS
                              if "frontmatter.py" in s[2]],
                             ids=[s[0] for s in ALL_SKILLS
                                  if "frontmatter.py" in s[2]])
    def test_frontmatter_schema_via_skill_dir(self, skill_name, skill_path,
                                               content):
        script = os.path.join(skill_path, "scripts", "frontmatter.py")
        result = subprocess.run(
            [sys.executable, script, "schema", "strat-task"],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, (
            f"frontmatter.py schema failed via {skill_name}: {result.stderr}")
        assert "strat_id" in result.stdout

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             [s for s in ALL_SKILLS
                              if "state.py" in s[2]],
                             ids=[s[0] for s in ALL_SKILLS
                                  if "state.py" in s[2]])
    def test_state_timestamp_via_skill_dir(self, skill_name, skill_path,
                                            content):
        script = os.path.join(skill_path, "scripts", "state.py")
        result = subprocess.run(
            [sys.executable, script, "timestamp"],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT,
        )
        assert result.returncode == 0, (
            f"state.py timestamp failed via {skill_name}: {result.stderr}")
        assert re.match(r"\d{4}-\d{2}-\d{2}T", result.stdout.strip())
