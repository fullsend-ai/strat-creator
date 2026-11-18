#!/usr/bin/env python3
"""Write a run's telemetry folder: one layout for every agentic pipeline.

Vendored byte-for-byte into strat-creator, strat-pipeline, epic-decomposer and
rfe-autofixer. Change it in all four (or move it to agentic-ci) so the results
repos keep one contract.

Layout (contract "agentic-telemetry/v1"), written to --out, which each pipeline
places at <run folder>/telemetry in its results repo:

    telemetry/
      usage.json                      always: normalized summary (read this)
      raw/claude-otel.jsonl.gz        Claude Code OTEL log, when one was captured
      raw/fullsend-metrics.json.gz    Fullsend metrics.json, for Fullsend runs
      raw/fullsend-telemetry.jsonl.gz Fullsend run-telemetry.jsonl, for Fullsend runs

usage.json:

    schema      "agentic-telemetry/v1"
    pipeline    producing pipeline (strat-pipeline, epic-decomposer, rfe-autofixer)
    run_id      the run folder's name, or null when the caller cannot know it
    ci          GitLab project, pipeline id, job id, job name, job URL (null outside CI)
    source      what the totals come from: "claude-otel", "fullsend-metrics" or null
    totals      cost_usd; input/output/cache_read/cache_creation/reasoning tokens;
                api_requests; tool_calls; turns (agent turns, Fullsend only);
                user_prompts (Claude Code OTEL only). null = the source does not
                record it; totals itself is null when no source had usage data.
    by_model    cost and token fields per model
    started_at, finished_at   first/last Claude Code event (claude-otel only)
    raw         per raw file: compressed bytes, uncompressed bytes and sha256 of
                the stored (cleaned) content, and how many attributes were removed

Raw files are stored from allowlists only, because git keeps them forever and
Claude Code can be configured (OTEL_LOG_USER_PROMPTS, OTEL_LOG_TOOL_DETAILS,
OTEL_LOG_TOOL_CONTENT) to put prompts, commands, file contents and tool output
in its log. For OTEL logs and span files, each record is rebuilt field by
field; nothing from the input is copied without its type and format checked:
  - containers (CONTAINER_FIELDS) are rebuilt recursively; structural scalars
    (SCALAR_FIELDS: ids, timestamps, enums, counts, measurements) are kept only
    with the type and format their rule allows; any other field is dropped
    (status messages, traceState, unknown and sibling fields included);
  - attributes are kept only when their key is in the known vocabulary
    (ATTRIBUTE_KEYS); any other key is dropped whatever its value. A kept
    attribute is rebuilt as {key, value: {kind: scalar}}: booleans and numbers
    as real types; strings only when the key is on SAFE_STRING_KEYS and the
    value is a plain identifier (SAFE_VALUE, no spaces or credentials), or the
    key is a measurement key (NUMBER_KEYS) and the value is a number;
  - names (span, event, metric, scope) and log bodies only when they are in
    the known vocabulary (SAFE_NAME), else replaced with "redacted".
metrics.json is stored as its usage fields only (METRICS_FIELDS); if it does
not parse, no raw copy is kept. CI fields in usage.json must match their
GitLab formats or are null. Every file is then checked against the CI secret
values in the environment (same rule as strat-creator's fullsend-redact.py): a
raw file with a match is not stored, and a usage.json with a match is not
written. Totals are computed from the original inputs; the strings they
carry into usage.json (model names, timestamps) must pass the same identifier
and timestamp checks, or the model is recorded as "unknown" and the timestamp
is ignored.

Totals prefer the Claude Code OTEL log (per-request api_request events), else
Fullsend's metrics.json. Output is deterministic for the same inputs (no
generation timestamp, gzip mtime 0), so re-publishing a run is a no-op. The
folder is built next to --out and moved into place only when complete. The
script never fails a publish: unreadable inputs are skipped with a warning.
Python 3.9+, standard library only.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

SCHEMA = "agentic-telemetry/v1"
CLAUDE_OTEL = "claude-otel.jsonl.gz"
FULLSEND_METRICS = "fullsend-metrics.json.gz"
FULLSEND_TELEMETRY = "fullsend-telemetry.jsonl.gz"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens", "reasoning_tokens")
TOTAL_KEYS = (("cost_usd",) + TOKEN_FIELDS
              + ("api_requests", "tool_calls", "turns", "user_prompts"))
MODEL_KEYS = ("cost_usd",) + TOKEN_FIELDS + ("api_requests",)
CI_ENV = (("project", "CI_PROJECT_PATH"), ("pipeline_id", "CI_PIPELINE_ID"),
          ("job_id", "CI_JOB_ID"), ("job_name", "CI_JOB_NAME"), ("job_url", "CI_JOB_URL"))

# String attributes kept in stored raw logs: identifiers, model names and enums.
# Anything else that is a string (prompts, commands, paths, file contents, tool
# input/output, error messages) is removed. Add a key only if its values can
# never carry free text.
SAFE_STRING_KEYS = frozenset({
    # Claude Code events, metrics and spans
    "event.name", "event.timestamp", "session.id", "prompt.id", "message.uuid",
    "request_id", "tool_use_id", "user.id", "terminal.type", "model", "final_model",
    "effort", "speed", "query_source", "agent_id", "agent.name", "agent.source",
    "parent_agent_id", "agent_type", "subagent_type", "skill.name", "skill.source", "command_name",
    "command_source", "invocation_trigger", "tool_name", "tool_source", "decision",
    "decision_source", "decision_type", "source", "success", "result", "error_type",
    "language", "type", "start_type", "permission_mode", "safe_mode", "managed_only",
    "used_default", "enabled_via", "hook_event", "hook_matcher", "hook_name",
    "hook_source", "hook_type", "plugin.name", "plugin.scope", "plugin_id_hash",
    "marketplace.name", "managed_settings.helper.applied", "managed_settings.helper.state",
    "managed_settings.source_behavior", "managed_settings.trigger", "span.type",
    "stop_reason", "llm_request.context",
    # resources
    "service.name", "service.version", "host.arch", "os.type", "os.version",
    # OTel GenAI conventions (identifiers only, never messages or tool arguments)
    "gen_ai.operation.name", "gen_ai.agent.name", "gen_ai.provider.name", "gen_ai.system",
    "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.id",
    "gen_ai.tool.name", "gen_ai.tool.call.id",
    # Fullsend spans
    "fullsend.agent", "fullsend.runtime", "fullsend.work_item_id",
    "fullsend.security_trace_id", "fullsend.harness.content_sha", "vcs.provider.name",
    "cicd.pipeline.run.url.full",
})
# Values allowed under SAFE_STRING_KEYS: plain identifiers (no spaces, no
# userinfo/query characters). The one URL key must be a plain https URL.
SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=,-]{0,199}$")
URL_KEYS = frozenset({"cicd.pipeline.run.url.full"})
SAFE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9_.~-]+)*/?$")
NUMERIC_STRING = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
# Measurement keys: their values are numbers (numeric strings included). A
# prompt or tool input that happens to be a number is still content, so a
# numeric string is kept only under these keys.
NUMBER_KEYS = frozenset({
    # Claude Code events and metrics
    "event.sequence", "cost_usd", "cost_usd_micros", "duration_ms", "ttft_ms",
    "total_duration_ms", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "cache_creation_1h_tokens", "cache_creation_5m_tokens",
    "reasoning_tokens", "total_tokens", "total_tool_uses", "tool_input_size_bytes",
    "tool_result_size_bytes", "prompt_length", "user_prompt_length", "response_length",
    "content_original_length", "exit_code", "attempt", "iteration",
    "interaction.duration_ms", "interaction.sequence", "agent_path_count",
    "command_path_count", "skill_path_count", "num_hooks", "num_success",
    "num_blocking", "num_non_blocking_error", "num_cancelled", "error_count",
    "period_days", "artifacts_deleted", "files_past_cutoff", "files_retained_fresh",
    "history_entries_pruned", "session_files_deleted", "transcripts_deleted",
    "transcripts_exempted_desktop",
    # OTel GenAI conventions
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.reasoning_tokens", "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    # Fullsend spans
    "fullsend.cost_usd", "fullsend.iterations", "fullsend.num_turns",
    "fullsend.tool_calls", "cicd.pipeline.run.id",
})
# Flags: their values are booleans.
BOOL_KEYS = frozenset({
    "success", "has_hooks", "has_mcp", "host_owned_mcp", "is_async", "is_built_in",
    "model_swapped", "content_truncated", "fullsend.prescript.skipped",
})
# Every attribute key that may be stored. A key carries text of its own, so an
# unknown key is dropped even when its value is a boolean or a number. Add a
# key here only if it is a fixed name, never derived from content.
ATTRIBUTE_KEYS = SAFE_STRING_KEYS | NUMBER_KEYS | BOOL_KEYS
# Structural OTLP fields kept when a stored record is rebuilt (plus the
# collector's {ts, path, payload} wrapper). Containers are rebuilt recursively;
# every scalar must pass its rule. Anything else is dropped.
CONTAINER_FIELDS = frozenset({
    "payload", "resourceLogs", "resourceMetrics", "resourceSpans", "resource", "scope",
    "scopeLogs", "scopeMetrics", "scopeSpans", "logRecords", "metrics", "spans",
    "events", "links", "status", "sum", "gauge", "histogram", "dataPoints",
})
HEX_ID = re.compile(r"^[0-9a-fA-F]{0,64}$")
UINT = re.compile(r"^\d{1,20}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?$")


def _is_int(value):
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and bool(UINT.match(value)))


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)) or (
        isinstance(value, str) and bool(NUMERIC_STRING.match(value)))


SCALAR_FIELDS = {
    "traceId": lambda v: isinstance(v, str) and bool(HEX_ID.match(v)),
    "spanId": lambda v: isinstance(v, str) and bool(HEX_ID.match(v)),
    "parentSpanId": lambda v: isinstance(v, str) and bool(HEX_ID.match(v)),
    "ts": lambda v: isinstance(v, str) and bool(TIMESTAMP.match(v)),
    "isMonotonic": lambda v: isinstance(v, bool),
    "bucketCounts": lambda v: isinstance(v, list) and all(_is_int(x) for x in v),
    "explicitBounds": lambda v: isinstance(v, list) and all(_is_number(x) for x in v),
}
for _field in ("timeUnixNano", "observedTimeUnixNano", "startTimeUnixNano", "endTimeUnixNano",
               "code", "kind", "flags", "severityNumber", "droppedAttributesCount",
               "droppedEventsCount", "droppedLinksCount", "aggregationTemporality", "count"):
    SCALAR_FIELDS[_field] = _is_int
for _field in ("asDouble", "asInt", "min", "max"):
    SCALAR_FIELDS[_field] = _is_number
# Span, event, metric and scope names, and log bodies, that may be stored.
SAFE_NAME = re.compile(
    r"^(?:"
    r"(?:claude_code|gen_ai|tool)(?:\.[a-z0-9_]+)+"          # claude_code.tool.execution
    r"|com\.anthropic\.claude_code(?:\.[a-z_]+)*"            # scopes
    r"|github\.com/fullsend-ai/fullsend(?:/[a-z0-9_]+)*"      # Fullsend scope
    r"|execute_tool [A-Za-z][A-Za-z0-9_]{0,63}"              # execute_tool Bash
    r"|agent|run|sandbox_create|create_agent|invoke_agent|chat"
    r")$"
)
PATH_VALUE = re.compile(r"^/v1/(?:logs|metrics|traces)$")
# The usage fields kept from Fullsend's metrics.json (nested: token_usage, per_model_usage).
METRICS_FIELDS = ("num_turns", "total_cost_usd", "tool_calls", "iterations", "model",
                  "requested_model", "runtime", "requested_runtime")
METRICS_TOKEN_FIELDS = ("input", "output", "reasoning", "cache_creation", "cache_read")
METRICS_MODEL_FIELDS = ("requests", "input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}$")
CI_FORMATS = {
    "project": re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,254}$"),
    "pipeline_id": re.compile(r"^\d{1,20}$"),
    "job_id": re.compile(r"^\d{1,20}$"),
    "job_name": re.compile(r"^[A-Za-z0-9_.:\[\], /-]{1,255}$"),
    "job_url": re.compile(r"^https://[A-Za-z0-9.-]+(?::\d+)?/[A-Za-z0-9_./-]+/-/jobs/\d+$"),
}


def warn(message):
    print(f"WARNING: telemetry: {message}", file=sys.stderr)


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        for kind in (int, float):
            try:
                return kind(value)
            except ValueError:
                continue
    return None


def _attr_value(value):
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _attributes(owner):
    result = {}
    for item in owner.get("attributes") or []:
        if isinstance(item, dict) and "key" in item:
            result[item["key"]] = _attr_value(item.get("value"))
    return result


def _model_name(value):
    """A model name for usage.json: a plain identifier, else "unknown"."""
    return value if isinstance(value, str) and IDENTIFIER.match(value) else "unknown"


def _empty_usage():
    usage = {"cost_usd": 0.0, "api_requests": 0}
    usage.update({field: 0 for field in TOKEN_FIELDS})
    return usage


def _round_usage(usage, keys=MODEL_KEYS):
    """Fixed key order (identical in every usage.json); cost rounded to 6 places."""
    result = {}
    for key in keys:
        value = usage.get(key)
        result[key] = round(value, 6) if key == "cost_usd" and value is not None else value
    return result


def summarize_claude_otel(path):
    """Totals from the api_request events of a collector log ({ts, path, payload} lines).

    Returns None when the log has no api_request event: no usage data, not $0."""
    totals, by_model = _empty_usage(), {}
    seen, seen_events, tool_calls, prompts, stamps, bad = set(), set(), 0, 0, [], 0
    with open(path, "rb") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                bad += 1
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                continue
            for resource in payload.get("resourceLogs") or []:
                for scope in resource.get("scopeLogs") or []:
                    for entry in scope.get("logRecords") or []:
                        attrs = _attributes(entry)
                        name = attrs.get("event.name")
                        # An exporter retry can resend a whole batch: count each
                        # event once, whatever its type.
                        key = (name, attrs.get("session.id"), attrs.get("event.sequence"),
                               attrs.get("request_id"), attrs.get("tool_use_id"),
                               attrs.get("prompt.id"), attrs.get("event.timestamp"))
                        if key in seen_events:
                            continue
                        seen_events.add(key)
                        stamp = attrs.get("event.timestamp")
                        if isinstance(stamp, str) and TIMESTAMP.match(stamp):
                            stamps.append(stamp)
                        if name == "tool_result":
                            tool_calls += 1
                        elif name == "user_prompt":
                            prompts += 1
                        if name != "api_request":
                            continue
                        seen.add(key)
                        model = _model_name(attrs.get("model"))
                        per_model = by_model.setdefault(model, _empty_usage())
                        values = {
                            "cost_usd": _number(attrs.get("cost_usd")) or 0,
                            "input_tokens": _number(attrs.get("input_tokens")) or 0,
                            "output_tokens": _number(attrs.get("output_tokens")) or 0,
                            "cache_read_tokens": _number(attrs.get("cache_read_tokens")) or 0,
                            "cache_creation_tokens": _number(attrs.get("cache_creation_tokens")) or 0,
                        }
                        for target in (totals, per_model):
                            target["api_requests"] += 1
                            for field, value in values.items():
                                target[field] += value
    if bad:
        warn(f"{path}: skipped {bad} line(s) that are not JSON")
    if not seen:
        warn(f"{path}: no api_request events, no usage data")
        return None
    for usage in [totals, *by_model.values()]:
        usage["reasoning_tokens"] = None  # Claude Code OTEL does not report it
    totals.update(tool_calls=tool_calls, turns=None, user_prompts=prompts)
    stamps.sort()
    return {
        "totals": _round_usage(totals, TOTAL_KEYS),
        "by_model": {model: _round_usage(usage) for model, usage in sorted(by_model.items())},
        "started_at": stamps[0] if stamps else None,
        "finished_at": stamps[-1] if stamps else None,
    }


def summarize_fullsend_metrics(path):
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)
    if not isinstance(metrics, dict):
        raise ValueError("metrics.json is not an object")
    tokens = metrics.get("token_usage") or {}
    totals = {
        "cost_usd": _number(metrics.get("total_cost_usd")),
        "input_tokens": _number(tokens.get("input")),
        "output_tokens": _number(tokens.get("output")),
        "cache_read_tokens": _number(tokens.get("cache_read")),
        "cache_creation_tokens": _number(tokens.get("cache_creation")),
        "reasoning_tokens": _number(tokens.get("reasoning")),
        "api_requests": None,
        "tool_calls": _number(metrics.get("tool_calls")),
        "turns": _number(metrics.get("num_turns")),
        "user_prompts": None,
    }
    if totals["cost_usd"] is None and not any(totals[field] for field in TOKEN_FIELDS):
        warn(f"{path}: no cost or token usage, no usage data")
        return None
    by_model = {}
    for model, usage in sorted((metrics.get("per_model_usage") or {}).items()):
        if not isinstance(usage, dict):
            continue
        if _model_name(model) != model:
            warn(f"{path}: a per-model entry has an invalid model name; not recorded")
            continue
        entry = {
            "cost_usd": _number(usage.get("cost_usd")),
            "input_tokens": _number(usage.get("input_tokens")),
            "output_tokens": _number(usage.get("output_tokens")),
            "cache_read_tokens": _number(usage.get("cache_read_input_tokens")),
            "cache_creation_tokens": _number(usage.get("cache_creation_input_tokens")),
            "reasoning_tokens": None,
            "api_requests": _number(usage.get("requests")),
        }
        if entry["api_requests"] == 0 and any(entry[field] for field in TOKEN_FIELDS):
            # Fullsend reports requests: 0 alongside real token usage (the count is
            # not filled in): unknown, not zero. A zero with no usage stays zero.
            entry["api_requests"] = None
        by_model[model] = _round_usage(entry)
    return {"totals": _round_usage(totals, TOTAL_KEYS), "by_model": by_model,
            "started_at": None, "finished_at": None}


def _rebuild_attribute(item):
    """A new {key, value: {kind: scalar}} from validated parts, or None to drop it."""
    if not isinstance(item, dict):
        return None
    key, value = item.get("key"), item.get("value")
    if key not in ATTRIBUTE_KEYS or not isinstance(value, dict) or len(value) != 1:
        return None  # unknown keys, array/kvlist/bytes values and malformed entries
    kind, scalar = next(iter(value.items()))
    if kind == "boolValue":
        keep = isinstance(scalar, bool)
    elif kind == "doubleValue":
        keep = isinstance(scalar, (int, float)) and not isinstance(scalar, bool)
    elif kind == "intValue":
        keep = _is_int(scalar)
    elif kind == "stringValue" and isinstance(scalar, str):
        if key in URL_KEYS:
            keep = bool(SAFE_URL.match(scalar))
        elif key in SAFE_STRING_KEYS:
            keep = bool(SAFE_VALUE.match(scalar))
        else:
            keep = key in NUMBER_KEYS and bool(NUMERIC_STRING.match(scalar))
    else:
        keep = False
    return {"key": key, "value": {kind: scalar}} if keep else None


def _clean(node, removed):
    """Rebuild a JSON object from the allowlists (see the module docstring)."""
    result = {}
    for key, value in node.items():
        if key == "attributes" and isinstance(value, list):
            kept = [a for a in (_rebuild_attribute(item) for item in value) if a is not None]
            removed[0] += len(value) - len(kept)
            result[key] = kept
        elif key == "name":
            ok = isinstance(value, str) and SAFE_NAME.match(value)
            result[key] = value if ok else "redacted"
            removed[0] += 0 if ok else 1
        elif key == "body":
            text = value.get("stringValue") if isinstance(value, dict) else None
            ok = isinstance(text, str) and SAFE_NAME.match(text)
            result[key] = {"stringValue": text if ok else "redacted"}
            removed[0] += 0 if ok and len(value) == 1 else 1
        elif key == "path":
            if isinstance(value, str) and PATH_VALUE.match(value):
                result[key] = value
            else:
                removed[0] += 1
        elif key in ("version", "severityText", "unit"):
            if isinstance(value, str) and IDENTIFIER.match(value):
                result[key] = value
            else:
                removed[0] += 1
        elif key in SCALAR_FIELDS:
            if SCALAR_FIELDS[key](value):
                result[key] = value
            else:
                removed[0] += 1
        elif key in CONTAINER_FIELDS and isinstance(value, dict):
            result[key] = _clean(value, removed)
        elif key in CONTAINER_FIELDS and isinstance(value, list):
            items = [_clean(item, removed) for item in value if isinstance(item, dict)]
            removed[0] += len(value) - len(items)
            result[key] = items
        else:
            removed[0] += 1
    return result


def _metrics_subset(path):
    """Fullsend metrics.json reduced to its usage fields (raises on unparsable input)."""
    with open(path, encoding="utf-8") as handle:
        metrics = json.load(handle)
    if not isinstance(metrics, dict):
        raise ValueError("metrics.json is not an object")

    def scalar(value):
        if isinstance(value, str):
            return value if IDENTIFIER.match(value) else None
        return _number(value)

    subset = {key: scalar(metrics.get(key)) for key in METRICS_FIELDS if key in metrics}
    tokens = metrics.get("token_usage")
    if isinstance(tokens, dict):
        subset["token_usage"] = {key: _number(tokens.get(key))
                                 for key in METRICS_TOKEN_FIELDS if key in tokens}
    models = metrics.get("per_model_usage")
    if isinstance(models, dict):
        subset["per_model_usage"] = {
            model: {key: _number(usage.get(key)) for key in METRICS_MODEL_FIELDS if key in usage}
            for model, usage in sorted(models.items())
            if isinstance(usage, dict) and isinstance(model, str) and IDENTIFIER.match(model)
        }
    return subset


SECRET_NAME = re.compile(r"TOKEN|PASSWORD|SECRET|CREDENTIALS|API_KEY|SERVICE_ACCOUNT_KEY")
KEY_FILE_VARS = ("GOOGLE_APPLICATION_CREDENTIALS", "AIPCC_CICD_GCP_SERVICE_ACCOUNT_KEY")
BASIC_AUTH_PAIRS = (("JIRA_USER", "JIRA_TOKEN"), ("RESULTS_GIT_USER", "RESULTS_PUSH_TOKEN"))


class SecretFound(ValueError):
    """A stored file would contain a CI secret value."""


def _secret_values(env=None):
    """CI secret values to keep out of git: the rule of strat-creator's fullsend-redact.py."""
    env = os.environ if env is None else env
    values = set()
    for name, value in env.items():
        if not value or not SECRET_NAME.search(name):
            continue
        if len(value) >= 8:
            values.add(value)
        if name in KEY_FILE_VARS:
            try:
                with open(value, encoding="utf-8") as handle:
                    contents = handle.read()
                data = json.loads(contents)
            except (OSError, ValueError):
                continue
            values.add(contents)
            private_key = data.get("private_key", "") if isinstance(data, dict) else ""
            values.update(line for line in private_key.splitlines() if len(line) >= 8)
            if private_key:
                values.add(private_key)
                values.add(json.dumps(private_key)[1:-1])
    for user_var, token_var in BASIC_AUTH_PAIRS:
        user = env.get(user_var) or ("oauth2" if user_var == "RESULTS_GIT_USER" else "")
        token = env.get(token_var, "")
        if user and token:
            values.add(base64.b64encode(f"{user}:{token}".encode()).decode())
    return sorted((v.encode() for v in values if v), key=len, reverse=True)


def _check_secrets(chunks, secrets, what):
    for chunk in chunks:
        for secret in secrets:
            if secret in chunk:
                raise SecretFound(f"{what} contains a CI secret value; not stored")


def _store(src, dest, mode, secrets=()):
    """Gzip src deterministically after cleaning it; returns the raw-file entry.

    mode: "otel" (rebuild every JSON line from the allowlists, drop non-JSON lines)
    or "metrics" (store the usage fields of metrics.json only)."""
    removed, dropped = [0], 0
    if mode == "metrics":
        lines = [(json.dumps(_metrics_subset(src), sort_keys=True) + "\n").encode()]
    else:
        lines = []
        with open(src, "rb") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except ValueError:
                    dropped += 1
                    continue
                if not isinstance(record, dict):
                    dropped += 1
                    continue
                cleaned = _clean(record, removed)
                lines.append((json.dumps(cleaned, separators=(",", ":")) + "\n").encode())
    _check_secrets(lines, secrets, os.path.basename(dest))
    digest, size = hashlib.sha256(), 0
    with open(dest, "wb") as sink:
        with gzip.GzipFile(filename="", mode="wb", fileobj=sink, mtime=0, compresslevel=9) as out:
            for line in lines:
                digest.update(line)
                size += len(line)
                out.write(line)
    entry = {"bytes": os.path.getsize(dest), "uncompressed_bytes": size,
             "sha256": digest.hexdigest()}
    if mode == "otel":
        entry["removed_attributes"] = removed[0] + dropped
    return entry


def _ci():
    """GitLab job identity; a value that does not match its GitLab format is null."""
    values = {}
    for key, var in CI_ENV:
        value = os.environ.get(var) or None
        if value is not None and not CI_FORMATS[key].match(value):
            warn(f"{var} does not look like a GitLab value; not recorded")
            value = None
        values[key] = value
    return values if any(values.values()) else None


def _build(out, pipeline, run_id, sources):
    raw_dir = os.path.join(out, "raw")
    os.makedirs(raw_dir)
    secrets = _secret_values()
    raw, summary, source = {}, None, None
    for kind, src, name, mode in sources:
        target = os.path.join(raw_dir, name)
        try:
            raw["raw/" + name] = _store(src, target, mode, secrets)
        except (OSError, ValueError, AttributeError, TypeError) as error:
            # Only the raw copy is lost: totals are still read from the input.
            warn(f"cannot store {src}: {error}")
            if os.path.exists(target):
                os.remove(target)
        if summary is not None or kind is None:
            continue
        try:
            summary = (summarize_claude_otel(src) if kind == "claude-otel"
                       else summarize_fullsend_metrics(src))
        except (OSError, ValueError, AttributeError, TypeError) as error:
            warn(f"cannot summarize {src}: {error}")
        if summary is not None:
            source = kind

    summary = summary or {"totals": None, "by_model": {}, "started_at": None, "finished_at": None}
    usage = {
        "schema": SCHEMA,
        "pipeline": pipeline,
        "run_id": run_id or None,
        "ci": _ci(),
        "source": source,
        "totals": summary["totals"],
        "by_model": summary["by_model"],
        "started_at": summary["started_at"],
        "finished_at": summary["finished_at"],
        "raw": dict(sorted(raw.items())),
    }
    text = json.dumps(usage, indent=2) + "\n"
    _check_secrets([text.encode()], secrets, "usage.json")  # raises: no folder at all
    with open(os.path.join(out, "usage.json"), "w", encoding="utf-8") as handle:
        handle.write(text)
    if not os.listdir(raw_dir):
        os.rmdir(raw_dir)  # git does not keep empty directories anyway
    return usage


def write_bundle(out, pipeline, run_id=None, otel_log=None, fullsend_run_dir=None):
    """Write <out>/usage.json and <out>/raw/*.gz, replacing <out> only once complete."""
    sources = []
    if otel_log:
        if os.path.isfile(otel_log):
            sources.append(("claude-otel", otel_log, CLAUDE_OTEL, "otel"))
        else:
            warn(f"Claude OTEL log {otel_log} not found")
    if fullsend_run_dir:
        metrics = os.path.join(fullsend_run_dir, "metrics.json")
        spans = os.path.join(fullsend_run_dir, "run-telemetry.jsonl")
        if os.path.isfile(metrics):
            sources.append(("fullsend-metrics", metrics, FULLSEND_METRICS, "metrics"))
        else:
            warn(f"Fullsend metrics {metrics} not found")
        if os.path.isfile(spans):
            sources.append((None, spans, FULLSEND_TELEMETRY, "otel"))

    out = os.path.abspath(out)
    parent = os.path.dirname(out)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".telemetry-", dir=parent)
    try:
        build = os.path.join(staging, "telemetry")
        usage = _build(build, pipeline, run_id, sources)
        if os.path.lexists(out):
            if os.path.isdir(out) and not os.path.islink(out):
                shutil.rmtree(out)
            else:
                os.remove(out)
        os.replace(build, out)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    total = (usage["totals"] or {}).get("cost_usd")
    print(f"telemetry: wrote {out} (source={usage['source']}, cost_usd={total}, "
          f"raw={len(usage['raw'])})")
    return usage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", required=True, help="telemetry folder to (re)write")
    parser.add_argument("--pipeline", required=True, help="producing pipeline name")
    parser.add_argument("--run-id", default=None, help="name of the run's results folder")
    parser.add_argument("--otel-log", default=None, help="Claude Code OTEL collector log")
    parser.add_argument("--fullsend-run-dir", default=None,
                        help="Fullsend run directory holding metrics.json / run-telemetry.jsonl")
    args = parser.parse_args(argv)
    write_bundle(args.out, args.pipeline, args.run_id, args.otel_log, args.fullsend_run_dir)


if __name__ == "__main__":
    main()
