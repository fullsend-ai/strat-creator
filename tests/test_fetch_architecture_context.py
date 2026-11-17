"""fetch-architecture-context.sh's per-type mode (--repo/--path/--dest [--ref]): a type whose docs live in
its application's repository (react-shopping-cart-strategy) gets a sparse clone under .context/."""
import os
import subprocess

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


def test_refuses_incomplete_or_escaping_arguments(tmp_path):
    for args in (["--repo", "x", "--path", "docs"],
                 ["--repo", "x", "--path", "docs", "--dest", "elsewhere"],
                 ["--repo", "x", "--path", "../docs", "--dest", ".context/a"],
                 ["--bogus", "1"]):
        result = _fetch(tmp_path, *args)
        assert result.returncode == 2, (args, result.stdout, result.stderr)
