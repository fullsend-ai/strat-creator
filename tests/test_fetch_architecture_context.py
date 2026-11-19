"""fetch-architecture-context.sh's per-type mode (--repo/--path/--dest [--ref]): a type whose docs live in
its application's repository (react-shopping-cart-strategy) gets a sparse clone under .context/."""
import os
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "scripts", "fetch-architecture-context.sh")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    *args], cwd=cwd, check=True, capture_output=True)


def _app_repo(tmp_path):
    repo = tmp_path / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.ts").write_text("code\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "app")
    _git(repo, "checkout", "-q", "-b", "docs")
    (repo / "docs" / "architecture").mkdir(parents=True)
    (repo / "docs" / "architecture" / "ARCHITECTURE.md").write_text("# Store\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")
    _git(repo, "checkout", "-q", "main")  # the docs are only on an unmerged branch
    return repo


def _fetch(cwd, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("RFE_SKIP_BOOTSTRAP", "STRAT_ARCHITECTURE_CONTEXT_PATH")}
    return subprocess.run(["bash", SCRIPT, *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_clones_only_the_docs_path_of_the_named_branch(tmp_path):
    repo = _app_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    args = ["--repo", f"file://{repo}", "--path", "docs/architecture", "--dest", ".context/app-architecture"]

    missing = _fetch(work, *args)  # main has no docs yet
    assert missing.returncode == 1 and "has no docs/architecture" in missing.stdout, missing.stdout + missing.stderr

    result = _fetch(work, *args, "--ref", "docs")
    assert result.returncode == 0, result.stderr
    dest = work / ".context" / "app-architecture"
    assert (dest / "docs" / "architecture" / "ARCHITECTURE.md").read_text() == "# Store\n"
    assert not (dest / "src").exists()


@pytest.mark.parametrize("args, error", [
    pytest.param(["--repo", "x", "--path", "docs"], "go together", id="no-dest"),
    pytest.param(["--repo", "x", "--path", "docs", "--dest"], "--dest needs a value", id="flag-without-value"),
    pytest.param(["--repo", "x", "--path", "docs", "--dest", "elsewhere"], "under .context/", id="dest-outside"),
    pytest.param(["--repo", "x", "--path", "docs", "--dest", ".context/"], "name a directory", id="dest-is-context"),
    pytest.param(["--repo", "x", "--path", "docs", "--dest", ".context/architecture-context"], "shared",
                 id="dest-is-the-rhoai-checkout"),
    pytest.param(["--repo", "x", "--path", "../docs", "--dest", ".context/a"], "'..'", id="path-escapes"),
    pytest.param(["--repo", "--upload-pack=touch", "--path", "docs", "--dest", ".context/a"], "start with '-'",
                 id="repo-is-an-option"),
    pytest.param(["--bogus", "1"], "unknown argument", id="unknown-flag"),
])
def test_refuses_incomplete_or_unsafe_arguments(tmp_path, args, error):
    (tmp_path / ".context" / "keep").mkdir(parents=True)
    result = subprocess.run(["bash", SCRIPT, *args], cwd=tmp_path, capture_output=True, text=True, timeout=30,
                            env={k: v for k, v in os.environ.items() if k != "RFE_SKIP_BOOTSTRAP"})
    assert result.returncode == 2 and error in result.stderr, result.stderr
    assert (tmp_path / ".context" / "keep").is_dir()  # nothing under .context/ was removed


def test_refetch_replaces_an_existing_destination(tmp_path):
    repo = _app_repo(tmp_path)
    work = tmp_path / "work"
    stale = work / ".context" / "app-architecture" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old\n")
    result = _fetch(work, "--repo", f"file://{repo}", "--path", "docs/architecture",
                    "--dest", ".context/app-architecture", "--ref", "docs")
    assert result.returncode == 0, result.stderr
    assert not stale.exists() and (stale.parent / "docs" / "architecture" / "ARCHITECTURE.md").is_file()
