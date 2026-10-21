#!/usr/bin/env python3
"""Validate CI inputs and prepare trusted source/context; never invoke a model."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTEXT_REPO = "https://github.com/opendatahub-io/architecture-context.git"


def git(*args, cwd=None, binary=False):
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True, capture_output=True, text=not binary, timeout=180
    ).stdout


def versions(root=ROOT):
    return dict(
        line.split("=", 1)
        for line in (root / ".fullsend/ci/fullsend-versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )


def validate(env):
    if env.get("CI_PIPELINE_SOURCE", "web") not in {"web", "api"}:
        raise ValueError("select Fullsend only on a manual web/API pipeline; schedules keep the existing jobs")
    mode = env.get("FULLSEND_MODE", "single")
    if mode not in {"single", "batch"}:
        raise ValueError("FULLSEND_MODE must be single or batch")
    for name in ("CONFIG_FILE", "RFE_FILTER", "STRAT_KEY"):
        if env.get(name):
            raise ValueError(f"{name} is supported by the existing jobs, not the Fullsend trial")
    key = env.get("RFE_KEY", "")
    if mode == "single" and not re.fullmatch(r"RHAIRFE-[1-9][0-9]*", key):
        raise ValueError("single mode requires RFE_KEY=RHAIRFE-N")
    if mode == "batch" and key:
        raise ValueError("batch mode discovers tickets; leave RFE_KEY empty")
    for name, default, minimum in (("BATCH_SIZE", "10", 1), ("BATCH_OFFSET", "0", 0)):
        value = env.get(name, default)
        if not re.fullmatch(r"[0-9]+", value) or int(value) < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    for name, default in (("STRAT_DRY_RUN", "1"), ("FULLSEND_PUBLISH", "0")):
        if env.get(name, default) not in {"0", "1"}:
            raise ValueError(f"{name} must be 0 or 1")
    if env.get("FULLSEND_PUBLISH", "0") == "1" and env.get("STRAT_DRY_RUN", "1") != "0":
        raise ValueError("dry-run results must not update production publication")
    return {
        "mode": mode,
        "rfe_key": key,
        "dry_run": env.get("STRAT_DRY_RUN", "1"),
        "publish": env.get("FULLSEND_PUBLISH", "0"),
    }


def verify_consumer(consumer, env, source=ROOT):
    """Bind the remote registrations and every companion to one source tree."""
    revision = env.get("STRAT_CREATOR_REF", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("STRAT_CREATOR_REF must be a full source commit")
    if git("rev-parse", "HEAD", cwd=source).strip() != revision:
        raise ValueError("CI source does not match STRAT_CREATOR_REF")
    # Bootstrap checks HEAD before executing this helper; also reject tracked
    # edits before packaging runtime companions from that checkout.
    if git("status", "--porcelain", "--untracked-files=all", "--", ".fullsend", cwd=source).strip():
        raise ValueError("CI source package has uncommitted changes")
    repository = env.get("STRAT_CREATOR_REPO", "")
    if not re.fullmatch(r"https://github[.]com/[A-Za-z0-9_.-]+/strat-creator[.]git", repository):
        raise ValueError("STRAT_CREATOR_REPO must name a public GitHub strat-creator repository")
    prefix = repository.removesuffix(".git").replace("https://github.com/", "https://raw.githubusercontent.com/")
    branch = env.get("STRAT_CREATOR_BRANCH", "")
    if branch:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch):
            raise ValueError("STRAT_CREATOR_BRANCH must name a branch")
        try:
            git("check-ref-format", "refs/heads/" + branch)
        except subprocess.CalledProcessError as error:
            raise ValueError("STRAT_CREATOR_BRANCH must name a valid branch") from error
    config = yaml.safe_load((consumer / ".fullsend/config.yaml").read_text())
    entries = config.get("agents", [])
    if len(entries) != 2 or {entry.get("name") for entry in entries} != {"strat-single", "strat-batch"}:
        raise ValueError("register exactly strat-single and strat-batch")
    hashes = {}
    for entry in entries:
        name = entry["name"]
        path = f".fullsend/{name}/{name}.yaml"
        digest = hashlib.sha256((source / path).read_bytes()).hexdigest()
        expected = f"{prefix}/{branch}/{path}" if branch else f"{prefix}/{revision}/{path}#sha256={digest}"
        if entry.get("source") != expected:
            raise ValueError("agent registration must match the configured repository, branch/commit and harness")
        hashes[name] = digest
        harness = yaml.safe_load((source / path).read_text())
        if harness["validation_loop"]["max_iterations"] != 1:
            raise ValueError("trial jobs require one validation iteration")
    record = {"repository": repository, "commit": revision, "harness_sha256": hashes}
    if branch:
        record["branch"] = branch
    return record


def materialize_runtime(consumer, destination, env, source=ROOT):
    """Expand one resolved source snapshot into private runtime state.

    Released Fullsend 0.47.1 resolves some remote companions against the
    consumer directory. Supply those companions from the verified checkout,
    Branch registrations are bound to the already checked-out commit here,
    so later dependency resolution never follows the moving branch again.
    """
    record = verify_consumer(consumer, env, source)
    if destination.exists():
        raise ValueError("runtime destination already exists; preserve the previous run")
    shutil.copytree(
        source / ".fullsend",
        destination,
        ignore=shutil.ignore_patterns(
            "ci",
            "__pycache__",
            ".fullsend-cache",
            "config.yaml",
            "lock.yaml",
            "fullsend.lock",
            "README.md",
            "fleet-entry.example.yaml",
        ),
    )
    if "branch" not in record:
        shutil.copyfile(consumer / ".fullsend/config.yaml", destination / "config.yaml")
    else:
        config = yaml.safe_load((consumer / ".fullsend/config.yaml").read_text())
        prefix = record["repository"].removesuffix(".git").replace("https://github.com/", "https://raw.githubusercontent.com/")
        for entry in config["agents"]:
            name = entry["name"]
            digest = record["harness_sha256"][name]
            entry["source"] = f"{prefix}/{record['commit']}/.fullsend/{name}/{name}.yaml#sha256={digest}"
        (destination / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))


def checkout_candidate(destination, receipt, env, root=ROOT):
    record = verify_consumer(Path(env["CI_PROJECT_DIR"]), env, root)
    if destination.exists():
        raise ValueError("source destination already exists; preserve the previous run")
    destination.mkdir(parents=True)
    git("init", "--quiet", destination)
    git("remote", "add", "origin", record["repository"], cwd=destination)
    git("fetch", "--quiet", "--depth=1", root, record["commit"], cwd=destination)
    git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=destination)
    actual = git("rev-parse", "HEAD", cwd=destination).strip()
    if actual != record["commit"]:
        raise ValueError("candidate checkout does not match the configured source")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                **record,
                "fullsend_version": versions(root)["FULLSEND_VERSION"],
                "model": "claude-opus-4-6",
                "effort": "high",
                "concurrency": 10,
            },
            indent=2,
        )
        + "\n"
    )


def tree_entries(repository, tree):
    entries = []
    for record in git("ls-tree", "-z", tree, cwd=repository).split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, kind, _ = metadata.split()
            entries.append((mode, kind, name))
    return entries


def fetch_context(destination, receipt, repository=CONTEXT_REPO):
    """Resolve current remote HEAD once, then extract one consistent subset.

    Directory entries only: cone checkout includes sibling aliases whose
    targets may be excluded. Do not materialize those aliases at all.
    """
    if destination.exists():
        raise ValueError("architecture destination already exists; do not refresh a prepared run")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="strat-context-") as temporary:
        bare = Path(temporary) / "repository"
        git("init", "--quiet", "--bare", bare)
        # HEAD is intentional. There is no context revision input or fixed SHA.
        git("fetch", "--quiet", "--depth=1", "--filter=blob:none", repository, "HEAD", cwd=bare)
        commit = git("rev-parse", "FETCH_HEAD", cwd=bare).strip()
        releases = sorted(
            name
            for mode, kind, name in tree_entries(bare, commit + ":architecture")
            if mode == "040000" and kind == "tree" and re.fullmatch(r"rhoai-[A-Za-z0-9._-]+", name)
        )
        if not releases:
            raise ValueError("current architecture repository contains no RHOAI release directory")
        release = releases[-1]
        paths = [name for mode, kind, name in tree_entries(bare, commit) if kind == "blob" and mode != "120000"]
        paths.append(f"architecture/{release}")
        if any(name == "overlays" and kind == "tree" for _, kind, name in tree_entries(bare, commit)):
            paths.append("overlays")
        archive = Path(temporary) / "context.tar"
        archive.write_bytes(git("archive", "--format=tar", commit, "--", *paths, cwd=bare, binary=True))
        destination.mkdir()
        try:
            with tarfile.open(archive) as stream:
                stream.extractall(destination, filter="data")
            for path in destination.rglob("*"):
                if path.is_symlink() and not path.resolve(strict=True).is_relative_to(destination.resolve()):
                    raise ValueError("architecture contains an escaping symlink")
            if not (destination / "architecture" / release / "PLATFORM.md").is_file():
                raise ValueError("selected architecture release has no PLATFORM.md")
            (destination / "LATEST_VERSION").write_text(release + "\n")
        except BaseException:
            shutil.rmtree(destination)
            raise
    receipt.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "repository": repository,
        "resolved_commit": commit,
        "release": release,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "selection": "current remote HEAD, selected release directory and overlays",
    }
    receipt.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("validate").add_argument("consumer", type=Path)
    sub.add_parser("runtime").add_argument("destination", type=Path)
    for operation in ("source", "context"):
        command = sub.add_parser(operation)
        command.add_argument("destination", type=Path)
        command.add_argument("receipt", type=Path)
    args = parser.parse_args()
    if args.operation == "validate":
        print(json.dumps({**validate(os.environ), **verify_consumer(args.consumer, os.environ)}, indent=2))
    elif args.operation == "runtime":
        materialize_runtime(Path(os.environ["CI_PROJECT_DIR"]), args.destination, os.environ)
    elif args.operation == "source":
        checkout_candidate(args.destination, args.receipt, os.environ)
    else:
        print(json.dumps(fetch_context(args.destination, args.receipt), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Fullsend preparation failed: {error}")
