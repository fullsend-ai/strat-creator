#!/usr/bin/env python3
"""Gate 1 for types/: every types/<name>/type.yaml validates against types/_schema/type.schema.json.

The shape of rfe-creator's scripts/validate_types.py (its JSON-Schema gate, the data-only rule, the
directory-name rule and the per-tracker binding uniqueness lint), copied rather than imported: the
stations share configuration vocabulary, not code. rfe-creator's further
lints (launcher tokens, typed-file existence, rubric checkout) arrive here with the consumers that
need them.

Usage:
    python3 scripts/validate_types.py            # gate 1 over types/
    python3 scripts/validate_types.py --root DIR # another descriptor root (tests)

Exit codes: 0 clean, 1 findings, 2 usage or missing dependency.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "types"
SCHEMA_RELPATH = Path("_schema") / "type.schema.json"
DESCRIPTOR_FILENAME = "type.yaml"
# Nothing executable lives under types/ (rfe-creator types/README.md "Data only").
CODE_SUFFIXES = (".py", ".sh", ".bash", ".zsh")


class MissingDependencyError(RuntimeError):
    """jsonschema is a dev dependency; the gate cannot run without it."""


def _load_jsonschema():
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - exercised only on a broken install
        raise MissingDependencyError(
            "jsonschema is required by scripts/validate_types.py — run: uv sync"
        ) from exc
    return jsonschema


def load_schema(root):
    """Return (schema_dict, path); <root>/_schema first, then the shipped types/_schema."""
    for candidate in (Path(root) / SCHEMA_RELPATH, DEFAULT_ROOT / SCHEMA_RELPATH):
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh), candidate
    raise FileNotFoundError(f"no {SCHEMA_RELPATH} under {root} or {DEFAULT_ROOT}")


def descriptor_dirs(root):
    """Type directories in name order; `_`-prefixed directories (the schema) are skipped."""
    root = Path(root)
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _is_code_file(path):
    if path.suffix in CODE_SUFFIXES:
        return True
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def schema_messages(data, schema, rel):
    """JSON-Schema findings for one descriptor (Draft 2020-12), one line each."""
    jsonschema = _load_jsonschema()
    from jsonschema.exceptions import best_match

    validator = jsonschema.Draft202012Validator(schema)
    messages = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        cause = err
        if err.validator in ("oneOf", "anyOf") and err.context:
            cause = best_match(err.context)
        location = getattr(cause, "json_path", None) or "$." + ".".join(
            str(p) for p in cause.absolute_path
        )
        text = cause.message if len(cause.message) <= 200 else cause.message[:197] + "..."
        messages.append(f"{rel}: {location}: {text}")
    return messages


def validate_dir(type_dir, schema, root):
    """Findings for one types/<name>/ directory (empty list = clean)."""
    rel = type_dir.relative_to(root.parent) if type_dir.is_relative_to(root.parent) else type_dir
    findings = []
    for path in sorted(type_dir.rglob("*")):
        if path.is_file() and _is_code_file(path):
            findings.append(f"{rel}: code file under a descriptor root: {path.name}")
    descriptor = type_dir / DESCRIPTOR_FILENAME
    if not descriptor.is_file():
        findings.append(f"{rel}: missing {DESCRIPTOR_FILENAME}")
        return findings, None
    try:
        with open(descriptor, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        findings.append(f"{rel}/{DESCRIPTOR_FILENAME}: invalid YAML: {exc}")
        return findings, None
    if not isinstance(data, dict):
        findings.append(f"{rel}/{DESCRIPTOR_FILENAME}: descriptor must be a mapping")
        return findings, None
    if data.get("type") != type_dir.name:
        findings.append(
            f"{rel}/{DESCRIPTOR_FILENAME}: 'type: {data.get('type')}' does not match the "
            f"directory name {type_dir.name!r}"
        )
    findings.extend(schema_messages(data, schema, f"{rel}/{DESCRIPTOR_FILENAME}"))
    return findings, data


def cross_findings(descriptors):
    """Per-tracker binding uniqueness (rfe-creator design §3.3 rule 1): two types may not claim
    the same (project, issue_type) pair."""
    seen = {}
    findings = []
    for name, data in descriptors.items():
        identity = data.get("identity") or {}
        tracker = identity.get("tracker")
        block = identity.get(tracker) or {}
        pair = (tracker, block.get("project") or block.get("repo"), block.get("issue_type"))
        if None in pair:
            continue
        if pair in seen:
            findings.append(
                f"types/{name} and types/{seen[pair]} both claim the {tracker} binding "
                f"({pair[1]}, {pair[2]})"
            )
        seen.setdefault(pair, name)
    return findings


def validate_all(root=DEFAULT_ROOT):
    root = Path(root)
    schema, _ = load_schema(root)
    findings = []
    descriptors = {}
    dirs = descriptor_dirs(root)
    if not dirs:
        findings.append(f"{root}: no type directories")
    for type_dir in dirs:
        dir_findings, data = validate_dir(type_dir, schema, root)
        findings.extend(dir_findings)
        if data is not None and not dir_findings:
            descriptors[type_dir.name] = data
    findings.extend(cross_findings(descriptors))
    return findings, sorted(descriptors)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gate 1: validate every types/<name>/type.yaml against types/_schema/type.schema.json"
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="descriptor root (default: types/)")
    args = parser.parse_args(argv)
    try:
        findings, names = validate_all(args.root)
    except (FileNotFoundError, MissingDependencyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in findings:
        print(f"FAIL: {line}")
    if findings:
        print(f"{len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"OK: {len(names)} descriptor(s) valid: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
