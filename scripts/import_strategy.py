#!/usr/bin/env python3
"""Import the prepared strategy, including oversized attachment content."""
import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from artifact_utils import read_frontmatter_validated
from jira_utils import require_env
from pull_strategy import pull_strategy
from strat_pipeline_outcome import regular
from strat_pipeline_state import check_strat, load_input, load_progress
from strat_side_effects import atomic, locked

CONTRACT = "strat-import-v2"


def output_paths(key, rfe):
    required = {f"artifacts/strat-tasks/{key}.md", f"artifacts/strat-originals/{key}.md",
                f"artifacts/strat-originals/{rfe}.md"}
    optional = {f"artifacts/strat-originals/{rfe}-comments.md",
                f"artifacts/strat-reviews/{key}-review-summary.md", f"artifacts/strat-reviews/{key}-review.md"}
    return required, required | optional


def check_directories(root):
    # The pull helper writes several sibling artifacts. Refuse redirected paths
    # before any import writes, including originals, comments and prior reviews.
    for part in ("artifacts", "artifacts/strat-tasks", "artifacts/strat-originals",
                 "artifacts/strat-reviews", "artifacts/strat-comments"):
        directory = root / part
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("import output directory is not a regular directory")
        if directory.exists() and any(path.is_symlink() for path in directory.iterdir()):
            raise ValueError("import output contains a symlink")


def completed(root, receipt, identity, key, rfe):
    if not receipt.exists() and not receipt.is_symlink():
        return None
    record = json.loads(regular(root, str(receipt.relative_to(root))).read_text())
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in identity.items()):
        raise ValueError("import completion identity differs from this prepared run")
    required, allowed = output_paths(key, rfe)
    files = record.get("files")
    if (not isinstance(files, list) or not all(isinstance(name, str) for name in files)
            or not required <= set(files) <= allowed or len(files) != len(set(files))):
        raise ValueError("import completion has an invalid artifact inventory")
    for name in files:
        try:
            regular(root, name)
        except ValueError as error:
            raise ValueError("completed import has missing or unsafe context; preserve work and reconcile") from error
    return {"strat_key": key, "already_imported": True, "files": [str(root / name) for name in files]}


def publish_file(source, target):
    """Expose one complete downloaded file; never leave a truncated artifact."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".import-")
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def import_strategy(key, rfe):
    check_strat(key)
    data = load_input()
    if rfe not in data["acquired_keys"]:
        raise ValueError("RFE is not acquired by this prepared run")
    root = Path.cwd().resolve()
    reused = data.get("reused_push", {}).get(rfe)
    if reused and reused["key"] != key:
        raise ValueError("import key differs from the verified reusable strategy")
    with locked(root / f"tmp/strat-imports/{key}.lock"):
        return prepare_import(root, key, rfe, data)


def prepare_import(root, key, rfe, data):
    progress = load_progress(data["run_id"])
    check_directories(root)
    existing = root / "artifacts/strat-tasks" / f"{key}.md"
    if existing.exists():
        fm, _ = read_frontmatter_validated(existing, "strat-task")
        if fm.get("strat_id") != key or fm.get("source_rfe") != rfe:
            raise ValueError("existing import identity disagrees")
    receipt = root / f"tmp/strat-imports/{key}.json"
    identity = {"contract": CONTRACT, "run_id": data["run_id"], "strat_key": key, "rfe_key": rfe}
    result = completed(root, receipt, identity, key, rfe)
    if result is not None:
        return result
    if progress.get("review", {}).get(key) or (root / "tmp/review-composition" / key / "active.json").exists():
        raise ValueError("review already binds this task")
    # A task without completion can be an older partial import. Keep its body,
    # but fetch every context input before publishing any new artifact.
    with tempfile.TemporaryDirectory(prefix=f".strat-import-{key}-", dir=root / "tmp") as directory:
        stage = Path(directory)
        result = pull_strategy(*require_env(), key, str(stage), pipeline_rfe=rfe)
        files = {"artifacts/" + str(path.relative_to(stage)): path for path in stage.rglob("*") if path.is_file()}
        required, allowed = output_paths(key, rfe)
        if not required <= set(files) <= allowed:
            raise ValueError("downloaded import has an incomplete or unexpected artifact inventory")
        check_directories(root)
        comments = f"artifacts/strat-originals/{rfe}-comments.md"
        if comments not in files and (root / comments).is_file():
            content = (root / comments).read_bytes()
            lines = [line.strip() for line in content.decode("utf-8").splitlines() if line.strip()]
            # strategy-create writes this placeholder before Path A. The
            # successful remote read confirmed no comments; retain exact bytes.
            if lines == [f"# Comments: {rfe}", "No comments found."]:
                saved = stage / "strat-originals" / f"{rfe}-comments.md"
                saved.write_bytes(content)
                files[comments] = saved
        omitted = [name for name in sorted(allowed - set(files))
                   if (root / name).exists() or (root / name).is_symlink()]
        if omitted:
            # Without a completed import we cannot tell a partial download from
            # local review work. Preserve both and refuse a misleading receipt.
            raise ValueError("import has local optional context absent from Jira; preserve work and reconcile: "
                             + ", ".join(omitted))
        task = f"artifacts/strat-tasks/{key}.md"
        for name in sorted(files):
            if name == task:
                continue
            publish_file(files[name], root / name)
        if not existing.exists():
            publish_file(files[task], existing)
        atomic(receipt, {**identity, "files": sorted(files)})
        result["files"] = [str(root / "artifacts" / Path(name).relative_to(stage)) for name in result["files"]]
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key")
    parser.add_argument("--rfe", required=True)
    args = parser.parse_args()
    print(json.dumps(import_strategy(args.key, args.rfe)))


if __name__ == "__main__":
    main()
