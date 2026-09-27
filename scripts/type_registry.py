#!/usr/bin/env python3
"""Type registry: enumerate ``types/<name>/type.yaml`` descriptors and expose their values.

rfe-creator's ``scripts/type_registry.py`` public surface, copied rather than imported (the
stations share configuration vocabulary, not code): ``load()``,
``TypeRegistry.names() / get() / detect()``, ``Descriptor.get(dotted)`` and the projections the
pin tests and the first consumers read (``tracker``, ``key_prefixes``, ``local_prefix``,
``local_id_pattern``, ``id_field``, ``owns()``, ``dirs()``, ``labels``, ``score_fields``).
A run's type comes from rfe-creator's resolution ladder, under its names: ``resolve()`` (rung by
rung: ``--type``, the artifact's frontmatter ``type:``, the artifact's directory, the ids'
prefixes, the default ``rfe-strategy``), ``candidates()``, ``Resolution.line()`` (``TYPE
RESOLVED: <type> (<rung>)``) and ``is_headless()`` (a headless/CI run never guesses). Two
differences are deliberate: the id rung also matches a type's INPUT prefixes
(``inputs[].jira.key_prefixes``), because a strategy run usually starts from its input's id
(``RHAIRFE-1`` -> ``rfe-strategy``); and rfe-creator's batch ``type:`` rung, deployment binding
overrides (``*_BINDING_*`` env, the workspace file) and ``launch-vars`` are left out — they arrive,
under the same names, with the consumer that needs them. Smaller differences, kept from strat's
first registry: ``detect()`` refuses a tie where rfe-creator returns the first match;
``candidates()`` returns ``(rung, descriptors)`` instead of a ``Candidates`` object; ``names()``
lists the primary root, then each extra root; the CLI has no ``candidates`` verb, no
``--extra-roots`` and no ``list --json``.

Status: ``scripts/jira_utils.py`` is the first consumer; it reads the default type.
``tests/test_type_registry_pins.py`` pins every literal a script still carries to its descriptor
projection — source of truth BY TEST before BY IMPORT.

CLI (rfe-creator's verbs, a subset)::

    python3 scripts/type_registry.py list
    python3 scripts/type_registry.py show <type> [--json]
    python3 scripts/type_registry.py get <type> <dotted.path> [--json]
    python3 scripts/type_registry.py resolve [--type T] [--artifact PATH] [--headless] [--json] [ID ...]

Exit codes (rfe-creator's): 0 ok; 1 unknown type or field, a broken descriptor, conflicting or
invalid resolve input; 2 usage; 3 ``resolve`` is ambiguous (``TYPE AMBIGUOUS: a, b - pass --type``).
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = PLUGIN_ROOT / "types"
DESCRIPTOR_FILENAME = "type.yaml"
# Dev/test drop-in roots (os.pathsep-separated directories each holding <name>/type.yaml), the
# same seam as rfe-creator's RFE_CREATOR_EXTRA_TYPES.
# For now: no CI allowlist for drop-ins (rfe-creator's RFE_CREATOR_EXTRA_TYPES_ALLOWLIST); add it
# before a headless job sets this variable.
EXTRA_ROOTS_ENV = "STRAT_CREATOR_EXTRA_TYPES"
ARTIFACTS_DIR = "artifacts"
DIR_FORMS = ("artifacts", "bare")
# A headless/CI run never guesses a type (is_headless).
HEADLESS_MARKER_VARS = ("STRAT_CREATOR_HEADLESS", "CI", "GITHUB_ACTIONS")
_FALSE_VALUES = {"", "0", "false", "no", "off"}
# candidates() rungs, most specific first. rfe-creator's last rung is a provisional Jira key grammar
# (every Jira-bound type a candidate); here it is the input prefix instead.
# For now: no provisional rung, so an id no type owns is no signal (an error when headless); add it
# with binding overrides, when a key from an overridden project must still find its type.
CANDIDATE_RUNGS = ("local_id_pattern", "key_prefix", "local_prefix", "input_key_prefix")
# resolve() rungs, strongest first. The last one is the default when nothing else decides; entry
# scripts print no TYPE RESOLVED line for it, so a run without --type looks exactly as before.
LEGACY_DEFAULT_RUNG = "legacy default"
RESOLVE_RUNGS = ("--type", "frontmatter type", "artifact dir", "id grammar", LEGACY_DEFAULT_RUNG)
LEGACY_DEFAULT_TYPE = "rfe-strategy"
EXIT_AMBIGUOUS = 3

MISSING = object()
_INDEX_RE = re.compile(r"^\d+$")


class RegistryError(ValueError):
    """A descriptor root or file is malformed (exit 1 at the CLI)."""


class ResolveError(RegistryError):
    """``resolve`` could not decide: unknown type, conflicting signals or invalid artifact input
    (``exit_code`` 1) or an ambiguous headless run (``exit_code`` 3)."""

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


def is_headless(env, flag=False):
    """True when ``flag`` is set (``--headless``) or ``env`` carries ``STRAT_CREATOR_HEADLESS``,
    ``CI`` or ``GITHUB_ACTIONS`` set to anything but an empty/false value."""
    if flag:
        return True
    return any(str(env.get(var, "")).strip().lower() not in _FALSE_VALUES for var in HEADLESS_MARKER_VARS)


class Descriptor:
    """Thin wrapper over one parsed ``type.yaml`` dict; ``data`` is the raw mapping."""

    def __init__(self, name, data, path=None):
        self.name = name
        self.data = data
        self.path = Path(path) if path is not None else None

    def __repr__(self):
        return f"Descriptor({self.name!r}, path={str(self.path) if self.path else None!r})"

    def get(self, dotted, default=MISSING):
        """Value at a dotted path (``"conventions.labels.rubric_pass"``); integer segments index
        lists (``"pipeline.dimensions.0.name"``). ``KeyError`` when absent and no default."""
        node = self.data
        for segment in dotted.split("."):
            if isinstance(node, dict) and segment in node:
                node = node[segment]
            elif isinstance(node, list) and _INDEX_RE.fullmatch(segment):
                try:
                    node = node[int(segment)]
                except IndexError:
                    node = MISSING
            else:
                node = MISSING
            if node is MISSING:
                if default is MISSING:
                    raise KeyError(f"{self.name}: no such descriptor field {dotted!r}")
                return default
        return node

    # -- identity ------------------------------------------------------------------------

    @property
    def tracker(self):
        return self.get("identity.tracker")

    def _tracker_block(self):
        return self.get(f"identity.{self.tracker}", {}) or {}

    @property
    def key_prefixes(self):
        """Tracker key prefixes; [0] is the write prefix, every entry a read prefix."""
        return list(self._tracker_block().get("key_prefixes") or [])

    @property
    def write_prefix(self):
        prefixes = self.key_prefixes
        return prefixes[0] if prefixes else None

    @property
    def local_prefix(self):
        return self.get("identity.local_prefix")

    @property
    def local_id_pattern(self):
        return self.get("identity.local_id_pattern")

    @property
    def id_field(self):
        return self.get("identity.id_field")

    @property
    def tracker_key_field(self):
        return self.get("identity.tracker_key_field", None)

    @property
    def input_key_prefixes(self):
        """Every ``inputs[].jira.key_prefixes`` entry: the ids a run of this type starts from."""
        return [p for inp in self.get("inputs", []) or [] for p in (inp.get("jira") or {}).get("key_prefixes") or []]

    def owns(self, item_id):
        """True when ``item_id`` is one of this type's ids: a full match of ``local_id_pattern``
        (most specific), else a ``key_prefixes`` prefix, else a ``local_prefix`` prefix."""
        if not isinstance(item_id, str) or not item_id:
            return False
        return bool(
            re.fullmatch(self.local_id_pattern, item_id)
            or any(item_id.startswith(p) for p in self.key_prefixes)
            or item_id.startswith(self.local_prefix)
        )

    # -- layout / conventions / schema ------------------------------------------------------

    def dirs(self, form="artifacts"):
        """``dirs`` as declared (``artifacts/<dir>``) or with the ``artifacts/`` root stripped."""
        if form not in DIR_FORMS:
            raise ValueError(f"form must be one of {DIR_FORMS}, got {form!r}")
        declared = dict(self.get("dirs"))
        if form == "artifacts":
            return declared
        prefix = ARTIFACTS_DIR + "/"
        return {k: (v[len(prefix):] if v.startswith(prefix) else v) for k, v in declared.items()}

    @property
    def labels(self):
        return dict(self.get("conventions.labels"))

    @property
    def score_fields(self):
        return list(self.get("schema.review.score_fields"))

    @property
    def stages(self):
        return list(self.get("pipeline.stages"))


class TypeRegistry:
    """Static enumeration of ``<root>/<name>/type.yaml`` across one or more roots."""

    def __init__(self, root=None, extra_roots=None, env=None):
        self.env = os.environ if env is None else env
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        if extra_roots is None:
            raw = self.env.get(EXTRA_ROOTS_ENV, "")
            extra_roots = [p for p in raw.split(os.pathsep) if p]
        self.extra_roots = [Path(r) for r in extra_roots]
        self.roots = [self.root] + self.extra_roots
        self._types = {}
        for candidate in self.roots:
            self._scan(candidate)

    def _scan(self, root):
        if not root.is_dir():
            if root == self.root:
                raise RegistryError(f"descriptor root {root} is not a directory")
            return
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            descriptor = entry / DESCRIPTOR_FILENAME
            if not descriptor.is_file():
                raise RegistryError(f"{entry}: missing {DESCRIPTOR_FILENAME}")
            if entry.name in self._types:
                raise RegistryError(
                    f"duplicate type {entry.name!r}: {self._types[entry.name].path} and {descriptor}"
                )
            data = _read_descriptor(descriptor, entry.name)
            self._types[entry.name] = Descriptor(entry.name, data, descriptor)

    def names(self):
        return list(self._types)

    def choices(self):
        """Alias of ``names()`` — the value for argparse ``choices=``."""
        return self.names()

    def get(self, name):
        try:
            return self._types[name]
        except KeyError:
            raise KeyError(f"unknown type {name!r}; registered: {', '.join(self.names())}") from None

    def __iter__(self):
        return iter(self._types.values())

    def __len__(self):
        return len(self._types)

    def __contains__(self, name):
        return name in self._types

    def detect(self, item_id):
        """The one type that owns ``item_id`` (``local_id_pattern`` full match first, then key
        prefix, then local prefix); ``None`` when nothing matches; ``RegistryError`` when two do."""
        if not isinstance(item_id, str) or not item_id:
            return None
        for rung in (
            lambda d: re.fullmatch(d.local_id_pattern, item_id),
            lambda d: any(item_id.startswith(p) for p in d.key_prefixes),
            lambda d: item_id.startswith(d.local_prefix),
        ):
            matches = [d for d in self if rung(d)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RegistryError(
                    f"{item_id!r} is claimed by {', '.join(d.name for d in matches)}"
                )
        return None

    def candidates(self, item_id):
        """``(rung, descriptors)``: the first of ``CANDIDATE_RUNGS`` with a match and EVERY type
        matching there; ``(None, [])`` when none does. ``detect()`` is the single-answer form."""
        if not isinstance(item_id, str) or not item_id:
            return None, []
        tests = {
            "local_id_pattern": lambda d: re.fullmatch(d.local_id_pattern, item_id),
            "key_prefix": lambda d: any(item_id.startswith(p) for p in d.key_prefixes),
            "local_prefix": lambda d: item_id.startswith(d.local_prefix),
            "input_key_prefix": lambda d: any(item_id.startswith(p) for p in d.input_key_prefixes),
        }
        for rung in CANDIDATE_RUNGS:
            matches = [d for d in self if tests[rung](d)]
            if matches:
                return rung, matches
        return None, []


def _read_descriptor(path, expected_name):
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise RegistryError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: descriptor must be a mapping, got {type(data).__name__}")
    declared = data.get("type")
    if declared != expected_name:
        raise RegistryError(
            f"{path}: 'type: {declared}' does not match its directory name {expected_name!r}"
        )
    return data


def load(root=None, extra_roots=None, env=None):
    """Load a fresh registry (never cached — callers that want one instance keep it)."""
    return TypeRegistry(root=root, extra_roots=extra_roots, env=env)


# -- resolution (rfe-creator's design §5 ladder) -----------------------------------------------


@dataclass
class Resolution:
    """Outcome of ``resolve``: the type (``None`` when ambiguous), its descriptor, the deciding
    rung and, when ambiguous, the candidate type names."""

    type_name: "str | None"
    desc: "Descriptor | None"
    rung: "str | None"
    candidates: list = field(default_factory=list)

    @property
    def ambiguous(self):
        return self.type_name is None

    def line(self):
        """``TYPE RESOLVED: <type> (<rung>)`` or ``TYPE AMBIGUOUS: <a>, <b> - pass --type``."""
        if self.type_name is None:
            return f"TYPE AMBIGUOUS: {', '.join(self.candidates)} - pass --type"
        return f"TYPE RESOLVED: {self.type_name} ({self.rung})"

    def as_dict(self):
        return {"type": self.type_name, "rung": self.rung, "candidates": list(self.candidates),
                "line": self.line()}


def resolve(registry, *, explicit_type=None, artifact=None, ids=(), env=None, headless=None):
    """The type of a run, rung by rung (rfe-creator's ``resolve``, minus the batch rung):

    1. ``--type`` — ``explicit_type``; an unregistered name is a ``ResolveError``.
    2. ``frontmatter type`` — ``artifact`` (a markdown path) carries a string ``type:``.
    3. ``artifact dir`` — else the artifact's directory against every type's ``dirs``; its stem
       then joins the ids, so a stem another type owns is a conflict, not a silent dir win.
    4. ``id grammar`` — every id through ``candidates()``.
    5. ``legacy default`` — no signal at all: ``rfe-strategy``.

    The signals of rungs 2-4 must agree (a run is single-typed); with ``--type`` they are checked
    only for a conflict. Several types left and nothing to decide: headless (``is_headless``)
    raises ``ResolveError`` with ``exit_code`` 3, interactive returns an ambiguous
    ``Resolution``. An id no type owns is no signal, except in a headless run without
    ``--type``, where it is an error: a headless run never guesses.
    """
    if env is None:
        env = registry.env
    headless_run = is_headless(env, bool(headless))
    registered = registry.names()
    registered_text = ", ".join(registered) or "(none)"

    def known(name, where):
        if name not in registry:
            raise ResolveError(f"unknown type {name!r} ({where}); registered types: {registered_text}")

    if explicit_type is not None:
        known(explicit_type, "--type")

    # (id, strict): the caller's ids are strict; an artifact stem is not (it may be any file name).
    id_list = [(i, True) for i in ids if isinstance(i, str) and i]
    deterministic = []  # (rung, label, type names)
    if artifact is not None:
        fm_type, dir_name, stem = _artifact_signal(artifact)
        if fm_type is not None:
            known(fm_type, f"{artifact} frontmatter type:")
            deterministic.append(("frontmatter type", f"{artifact} (type: {fm_type})", [fm_type]))
        else:
            owners = [d.name for d in registry if dir_name and dir_name in _artifact_dirs(d)]
            if owners:
                deterministic.append(("artifact dir", f"{artifact} (dir {dir_name})", owners))
            id_list.append((stem, False))
    for item_id, strict in id_list:
        _, matches = registry.candidates(item_id)
        if not matches:
            if strict and headless_run and explicit_type is None:
                raise ResolveError(
                    f"no registered type owns id {item_id!r} (registered types: {registered_text}); "
                    f"a headless run never guesses — fix the id or pass --type"
                )
            continue
        deterministic.append(("id grammar", item_id, [d.name for d in matches]))

    if explicit_type is not None:
        conflicts = [(label, names) for _, label, names in deterministic if explicit_type not in names]
        if conflicts:
            raise ResolveError(
                f"conflicting type signals: --type {explicit_type} vs {_render_pairs(conflicts)}; "
                f"a run is single-typed — split the input by type"
            )
        return Resolution(explicit_type, registry.get(explicit_type), "--type")

    if not deterministic:
        if LEGACY_DEFAULT_TYPE in registry:
            return Resolution(LEGACY_DEFAULT_TYPE, registry.get(LEGACY_DEFAULT_TYPE), LEGACY_DEFAULT_RUNG)
        raise ResolveError(
            f"no type signal and the default type {LEGACY_DEFAULT_TYPE!r} is not registered; "
            f"pass --type (registered types: {registered_text})"
        )
    common = set(deterministic[0][2])
    for _, _, names in deterministic[1:]:
        common &= set(names)
    if not common:
        pairs = [(label, names) for _, label, names in deterministic]
        raise ResolveError(
            f"conflicting type signals: {_render_pairs(pairs)}; a run is single-typed — split the "
            f"input by type or pass --type"
        )
    if len(common) == 1:
        (name,) = common
        rung = min((r for r, _, names in deterministic if name in names), key=RESOLVE_RUNGS.index)
        return Resolution(name, registry.get(name), rung)
    ambiguous = [n for n in registered if n in common]
    if headless_run:
        labels = ", ".join(label for _, label, _ in deterministic)
        raise ResolveError(
            f"ambiguous type for {labels}: candidates {', '.join(ambiguous)} — pass --type",
            exit_code=EXIT_AMBIGUOUS,
        )
    return Resolution(None, None, None, ambiguous)


def _render_pairs(pairs):
    return ", ".join(f"{label} -> {'/'.join(names)}" for label, names in pairs)


def _artifact_signal(path):
    """``(frontmatter type or None, parent dir name, file stem)`` for an artifact path."""
    file = Path(path)
    if not file.is_file():
        raise ResolveError(f"{path}: artifact file not found")
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResolveError(f"{path}: cannot read artifact: {exc}") from exc
    fm_type = _parse_frontmatter(text, path).get("type")
    if fm_type is not None:
        if not isinstance(fm_type, str) or not fm_type.strip():
            raise ResolveError(f"{path}: frontmatter 'type' must be a non-empty string, got {fm_type!r}")
        fm_type = fm_type.strip()
    return fm_type, file.parent.name, file.stem


def _parse_frontmatter(text, path):
    """The YAML mapping between the first two ``---`` lines (the first must be line 1), or ``{}``.
    Deliberately local: this module imports no repo helper."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return {}
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise ResolveError(f"{path}: invalid frontmatter YAML: {exc}") from exc
            return data if isinstance(data, dict) else {}
    return {}


def _artifact_dirs(desc):
    try:
        dirs = desc.dirs("bare")
    except KeyError:
        return []
    return [value for value in dirs.values() if isinstance(value, str)]


# -- CLI ------------------------------------------------------------------------------------


def _emit(value, as_json):
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=False))
    elif isinstance(value, (dict, list)):
        print(yaml.safe_dump(value, sort_keys=False).rstrip())
    else:
        print(value)


def _cmd_list(reg, args):
    for name in reg.names():
        print(name)
    return 0


def _cmd_show(reg, args):
    _emit(reg.get(args.type).data, args.json)
    return 0


def _cmd_get(reg, args):
    _emit(reg.get(args.type).get(args.path), args.json)
    return 0


def _cmd_resolve(reg, args):
    result = resolve(reg, explicit_type=args.type, artifact=args.artifact, ids=args.ids,
                     headless=args.headless)
    if args.json:
        _emit(result.as_dict(), True)
    else:
        print(result.line())
    return EXIT_AMBIGUOUS if result.ambiguous else 0


def _build_parser():
    parser = argparse.ArgumentParser(description="Inspect the types/ registry.")
    parser.add_argument("--root", default=None, help=f"descriptor root (default: {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="registered type names").set_defaults(func=_cmd_list)
    show = sub.add_parser("show", help="one descriptor")
    show.add_argument("type")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_show)
    get = sub.add_parser("get", help="one descriptor value by dotted path")
    get.add_argument("type")
    get.add_argument("path")
    get.add_argument("--json", action="store_true")
    get.set_defaults(func=_cmd_get)
    resolve_ = sub.add_parser("resolve", help="resolve the type of a run and print the TYPE RESOLVED line")
    resolve_.add_argument("--type", default=None, help="explicit type (rung 1)")
    resolve_.add_argument("--artifact", default=None, metavar="PATH",
                          help="artifact markdown file (frontmatter type:, else its directory, else its stem)")
    resolve_.add_argument("--headless", action="store_true",
                          help=f"treat the run as headless (also implied by {'/'.join(HEADLESS_MARKER_VARS)})")
    resolve_.add_argument("--json", action="store_true")
    resolve_.add_argument("ids", nargs="*", metavar="ID", help="item ids (rung 4: their prefixes)")
    resolve_.set_defaults(func=_cmd_resolve)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        return args.func(load(root=args.root), args)
    except ResolveError as exc:
        print(f"ERROR: {exc.args[0]}", file=sys.stderr)
        return exc.exit_code
    except (RegistryError, KeyError) as exc:
        print(f"ERROR: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
