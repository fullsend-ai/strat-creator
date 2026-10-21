"""Unit tests for skill directory structure — symlinks, script references."""
import os
import py_compile
import re

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKILLS_DIR = os.path.join(PROJECT_ROOT, ".claude", "skills")
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")

SCRIPT_REF_PATTERN = re.compile(
    r'(?:python3|bash)\s+\$\{CLAUDE_SKILL_DIR\}/scripts/(\S+)')
SYSPATH_PATTERN = re.compile(
    r"sys\.path\.insert\(0,\s*'\$\{CLAUDE_SKILL_DIR\}/scripts'\)")
IMPORT_PATTERN = re.compile(
    r'from\s+(\w+)\s+import')
ENV_SKILL_DIR_PATTERN = re.compile(
    r"os\.environ\[.CLAUDE_SKILL_DIR.\]")


def _discover_skills():
    """Find all skill dirs that contain a SKILL.md."""
    results = []
    for name in sorted(os.listdir(SKILLS_DIR)):
        skill_path = os.path.join(SKILLS_DIR, name)
        skill_md = os.path.join(skill_path, "SKILL.md")
        if os.path.isdir(skill_path) and os.path.isfile(skill_md):
            with open(skill_md) as f:
                content = f.read()
            results.append((name, skill_path, content))
    return results


def _skills_referencing_scripts():
    """Skills whose SKILL.md references ${CLAUDE_SKILL_DIR}/scripts/."""
    return [(name, path, content) for name, path, content
            in _discover_skills()
            if "${CLAUDE_SKILL_DIR}/scripts/" in content]


def _extract_script_refs(content):
    """Extract script filenames from SKILL.md content."""
    return list(set(SCRIPT_REF_PATTERN.findall(content)))


def _extract_module_refs(content):
    """Extract module names from trusted CLAUDE_SKILL_DIR import patterns."""
    modules = set()
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if SYSPATH_PATTERN.search(line) or ENV_SKILL_DIR_PATTERN.search(line):
            for j in range(i, min(i + 8, len(lines))):
                m = IMPORT_PATTERN.search(lines[j])
                if m:
                    modules.add(m.group(1))
    return list(modules)


CWD_SYSPATH_PATTERN = re.compile(
    r"""sys\s*\.\s*path\s*\.\s*insert\s*\(\s*0\s*,\s*
        (['"])(?:\./)?scripts\1\s*\)""",
    re.VERBOSE,
)

ALL_SKILLS = _discover_skills()
ALL_SKILLS_WITH_SCRIPT_REFS = _skills_referencing_scripts()


# ─── No CWD-based Imports ──────────────────────────────────────────────────


class TestNoCwdImports:

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS,
                             ids=[s[0] for s in ALL_SKILLS])
    def test_no_cwd_based_sys_path_insert(self, skill_name, skill_path,
                                           content):
        matches = CWD_SYSPATH_PATTERN.findall(content)
        assert not matches, (
            f"Skill '{skill_name}' uses CWD-based "
            f"sys.path.insert(0, 'scripts') ({len(matches)} occurrence(s)). "
            f"Use sys.path.insert(0, '${{CLAUDE_SKILL_DIR}}/scripts') instead "
            f"to avoid CWE-427 untrusted search path.")


# ─── Symlink Presence ────────────────────────────────────────────────────────


class TestSymlinkPresence:

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS_WITH_SCRIPT_REFS,
                             ids=[s[0] for s in ALL_SKILLS_WITH_SCRIPT_REFS])
    def test_skill_has_scripts_symlink(self, skill_name, skill_path, content):
        scripts_link = os.path.join(skill_path, "scripts")
        assert os.path.islink(scripts_link), (
            f"Skill '{skill_name}' references scripts via CLAUDE_SKILL_DIR "
            f"but has no scripts symlink. "
            f"Add: ln -s ../strategy-common/scripts "
            f".claude/skills/{skill_name}/scripts")


# ─── Symlink Chain Resolution ────────────────────────────────────────────────


class TestSymlinkChain:

    def test_strategy_common_resolves_to_project_scripts(self):
        common_scripts = os.path.join(SKILLS_DIR, "strategy-common", "scripts")
        assert os.path.islink(common_scripts)
        resolved = os.path.realpath(common_scripts)
        assert resolved == os.path.realpath(SCRIPTS_DIR)

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS_WITH_SCRIPT_REFS,
                             ids=[s[0] for s in ALL_SKILLS_WITH_SCRIPT_REFS])
    def test_symlink_resolves_to_project_scripts(self, skill_name,
                                                  skill_path, content):
        scripts_link = os.path.join(skill_path, "scripts")
        if not os.path.islink(scripts_link):
            pytest.skip("symlink missing (covered by TestSymlinkPresence)")
        resolved = os.path.realpath(scripts_link)
        assert resolved == os.path.realpath(SCRIPTS_DIR), (
            f"Skill '{skill_name}' scripts symlink resolves to {resolved}, "
            f"expected {os.path.realpath(SCRIPTS_DIR)}")

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS_WITH_SCRIPT_REFS,
                             ids=[s[0] for s in ALL_SKILLS_WITH_SCRIPT_REFS])
    def test_symlink_target_is_directory(self, skill_name, skill_path, content):
        scripts_link = os.path.join(skill_path, "scripts")
        if not os.path.islink(scripts_link):
            pytest.skip("symlink missing (covered by TestSymlinkPresence)")
        assert os.path.isdir(scripts_link), (
            f"Skill '{skill_name}' scripts symlink is dangling")


# ─── Referenced Scripts Exist ────────────────────────────────────────────────


class TestReferencedScripts:

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS_WITH_SCRIPT_REFS,
                             ids=[s[0] for s in ALL_SKILLS_WITH_SCRIPT_REFS])
    def test_all_referenced_scripts_exist(self, skill_name, skill_path,
                                           content):
        scripts = _extract_script_refs(content)
        for script in scripts:
            script_path = os.path.join(SCRIPTS_DIR, script)
            assert os.path.isfile(script_path), (
                f"Skill '{skill_name}' references {script} but it does not "
                f"exist at {script_path}")

    @pytest.mark.parametrize("skill_name,skill_path,content",
                             ALL_SKILLS_WITH_SCRIPT_REFS,
                             ids=[s[0] for s in ALL_SKILLS_WITH_SCRIPT_REFS])
    def test_all_referenced_modules_exist(self, skill_name, skill_path,
                                           content):
        modules = _extract_module_refs(content)
        for module in modules:
            module_path = os.path.join(SCRIPTS_DIR, f"{module}.py")
            assert os.path.isfile(module_path), (
                f"Skill '{skill_name}' imports {module} via CLAUDE_SKILL_DIR "
                f"but {module}.py does not exist at {SCRIPTS_DIR}")


# ─── Script Quality ──────────────────────────────────────────────────────────


def _all_referenced_py_scripts():
    """Collect unique .py scripts referenced across all skills."""
    scripts = set()
    for _, _, content in ALL_SKILLS_WITH_SCRIPT_REFS:
        for s in _extract_script_refs(content):
            if s.endswith(".py"):
                scripts.add(s)
    return sorted(scripts)


def _all_referenced_sh_scripts():
    """Collect unique .sh scripts referenced across all skills."""
    scripts = set()
    for _, _, content in ALL_SKILLS_WITH_SCRIPT_REFS:
        for s in _extract_script_refs(content):
            if s.endswith(".sh"):
                scripts.add(s)
    return sorted(scripts)


class TestScriptQuality:

    @pytest.mark.parametrize("script", _all_referenced_py_scripts())
    def test_python_scripts_have_valid_syntax(self, script):
        script_path = os.path.join(SCRIPTS_DIR, script)
        if not os.path.isfile(script_path):
            pytest.skip(f"{script} not found (covered by TestReferencedScripts)")
        py_compile.compile(script_path, doraise=True)

    @pytest.mark.parametrize("script", _all_referenced_sh_scripts())
    def test_shell_scripts_are_executable(self, script):
        script_path = os.path.join(SCRIPTS_DIR, script)
        if not os.path.isfile(script_path):
            pytest.skip(f"{script} not found (covered by TestReferencedScripts)")
        assert os.access(script_path, os.X_OK), (
            f"{script} is not executable. Run: chmod +x scripts/{script}")
