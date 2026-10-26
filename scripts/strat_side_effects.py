#!/usr/bin/env python3
"""Durable intent and read-back for S1 writes, independent of agent journals.

The ledger survives same-workspace retries. An uncertain write is never sent
again just because a read did not find it: Jira search may lag a committed
write. Keep the ledger when recovering a run; absence is not proof of failure.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.error
from pathlib import Path

CONTRACT = "strat-side-effects-v1"
OWNER_PREFIX = "strat-creator-owner-"
HUMAN_GATES = {"strat-creator-human-sign-off", "strat-creator-needs-attention"}


class _StandaloneContext(dict):
    """Only context() can select the explicit standalone journal, never JSON."""


class UncertainWrite(RuntimeError):
    """An operator must reconcile the external effect before work continues."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def owner_label(owner):
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", owner):
        raise ValueError("invalid lock owner")
    return OWNER_PREFIX + digest(owner)[:32]


def atomic(path, value):
    """Publish and fsync a JSON record before any external mutation."""
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("side-effect state must not use symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".effect-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def context():
    """Only S1 runs (or explicit isolated fixtures) use a persistent ledger."""
    path = Path("tmp/strat-input.json")
    if path.is_symlink():
        raise ValueError("symlinked S1 input")
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("S1 input must be a JSON object with a valid lock owner")
        if data.get("dry_run"):
            raise ValueError("Jira writes are forbidden in a dry run")
        owner_label(data.get("run_id"))
        owner_label(data.get("lock_owner"))
        return Path("tmp/strat-effects"), data
    if os.environ.get("STRAT_MANAGED_RUN") == "1":
        raise ValueError("managed run is missing tmp/strat-input.json; preserve state and stop")
    directory = os.environ.get("STRAT_EFFECTS_DIR")
    if directory:
        run = os.environ.get("STRAT_RUN_ID")
        owner_label(run)
        return Path(directory), _StandaloneContext(run_id=run)
    return None, None


@contextlib.contextmanager
def locked(path):
    if any(p.is_symlink() for p in path.parents):
        raise ValueError("symlinked side-effect lock directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("side-effect lock must be a regular file")
    with os.fdopen(fd, "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def perform(server, kind, target, payload, read, write, guard=None, slot=None):
    """Reconcile first; journal intent before sending; do not replay uncertainty.

    read returns None for absent, otherwise the verified operation result.
    A successful local record is also rechecked remotely, never trusted alone.
    HTTP 4xx except timeout/rate-limit is a definite rejection, not a commit.
    Model/Workflow restarts call this same function; HTTP retries do not append.
    """
    directory, data = context()
    if data and Path("tmp/strat-input.json").exists():
        from strat_freshness import verify_progress
        root = Path.cwd()
        progress_path = root / "tmp/strat-progress.json"
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
        verify_progress(root, data, progress)
    identity = {"server_hash": digest(server.rstrip("/")), "kind": kind, "target": target,
                "payload_hash": digest(payload)}
    name = identity if slot is None else {"server_hash": identity["server_hash"], "kind": kind,
                                          "target": target, "slot": slot}
    path = directory / (digest(name) + ".json") if directory else None
    manager = locked(directory / ".lock") if directory else contextlib.nullcontext()
    with manager:
        if guard:
            guard(data)
        if path and path.is_symlink():
            raise UncertainWrite("symlinked side-effect ledger")
        record = json.loads(path.read_text()) if path and path.exists() else None
        if record and (record.get("identity") != identity or record.get("run_id") != data["run_id"]
                       or record.get("contract") != CONTRACT):
            raise UncertainWrite("side-effect ledger identity mismatch")
        found = read()
        if found is not None:
            if path:
                atomic(path, {"contract": CONTRACT, "run_id": data["run_id"], "identity": identity,
                              "status": "verified", "result": found})
            return found
        if record and record.get("status") != "rejected":
            raise UncertainWrite(f"{kind} {target}: previous write is not visible; reconcile before repeating")
        if path:
            atomic(path, {"contract": CONTRACT, "run_id": data["run_id"], "identity": identity,
                          "status": "pending"})
        try:
            write()
        except Exception as exc:
            found = read()  # Lost response can conceal a successful commit.
            if found is not None:
                if path:
                    atomic(path, {"contract": CONTRACT, "run_id": data["run_id"], "identity": identity,
                                  "status": "verified", "result": found})
                return found
            if path and isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500 \
                    and exc.code not in (408, 429):
                atomic(path, {"contract": CONTRACT, "run_id": data["run_id"], "identity": identity,
                              "status": "rejected", "http_status": exc.code})
            raise
        found = read()
        if found is None:
            raise UncertainWrite(f"{kind} {target}: server acknowledged but read-back is absent")
        if path:
            atomic(path, {"contract": CONTRACT, "run_id": data["run_id"], "identity": identity,
                          "status": "verified", "result": found})
        return found


def checked_labels(labels):
    """A missing or malformed requested Jira field is not an empty label set."""
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ValueError("Jira response labels must be present as a list of strings")
    return labels


def check_human_gates(server, target, labels, data, *, root=None):
    """Stop late human intervention; reconcile only our exact verdict intent.

    Pending is needed when Jira committed a label before the process died.
    This journal is recovery evidence, not a tamper-proof authority or Jira CAS.
    """
    gates = set(checked_labels(labels)) & HUMAN_GATES
    if gates == {"strat-creator-needs-attention"} and isinstance(data, dict) and data.get("run_id"):
        identity = {"server_hash": digest(server.rstrip("/")), "kind": "add-labels", "target": target,
                    "payload_hash": digest(["strat-creator-needs-attention"])}
        path = (root or Path.cwd()) / "tmp/strat-effects" / (digest(identity) + ".json")
        if path.is_file() and not path.is_symlink() and not any(p.is_symlink() for p in path.parents):
            effect = json.loads(path.read_text())
            if (isinstance(effect, dict) and effect.get("contract") == CONTRACT
                    and effect.get("run_id") == data["run_id"] and effect.get("identity") == identity
                    and effect.get("status") in ("pending", "verified")):
                gates.clear()
    if gates:
        raise ValueError(f"freshness: human gate stops strategy writes: {target}: {', '.join(sorted(gates))}")


def check_owner(server, user, token, target, data):
    """Recheck S1 ownership at each write; never adopt a foreign/orphaned claim.

    Jira labels do not provide compare-and-swap. This is conflict detection and
    recovery ownership, not a distributed lease or a cross-run exclusivity proof.
    """
    if data is None or isinstance(data, _StandaloneContext):
        path = Path("tmp/strat-input.json")
        if path.exists() or path.is_symlink() or os.environ.get("STRAT_MANAGED_RUN") == "1":
            raise ValueError("managed write requires a valid lock owner")
        return
    if not isinstance(data, dict):
        raise ValueError("managed write requires a valid lock owner")
    owner_label(data.get("lock_owner"))
    from jira_utils import get_issue
    if target.startswith("RHAISTRAT-"):
        fields = get_issue(server, user, token, target, fields=["issuelinks", "labels"])["fields"]
        check_human_gates(server, target, fields.get("labels"), data)
        links = fields.get("issuelinks", [])
        rfes = {link.get(side, {}).get("key") for link in links if link.get("type", {}).get("name") == "Cloners"
                for side in ("inwardIssue", "outwardIssue")
                if link.get(side, {}).get("key", "").startswith("RHAIRFE-")}
        if len(rfes) != 1:
            raise UncertainWrite("strategy write has no unique source RFE")
        target = rfes.pop()
    if target not in data.get("acquired_keys", []):
        raise UncertainWrite("write target is outside acquired RFEs")
    labels = set(checked_labels(get_issue(server, user, token, target, fields=["labels"])["fields"].get("labels")))
    owners = {x for x in labels if x.startswith(OWNER_PREFIX)}
    if owners != {owner_label(data["lock_owner"])} or "strat-creator-processing" not in labels:
        raise UncertainWrite("missing or conflicting Jira lock ownership")
