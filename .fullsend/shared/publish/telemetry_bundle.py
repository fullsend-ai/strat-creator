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

Raw OTEL logs are cleaned before they are stored, because git keeps them forever
and Claude Code can be configured (OTEL_LOG_USER_PROMPTS, OTEL_LOG_TOOL_DETAILS,
OTEL_LOG_TOOL_CONTENT) to put prompts, commands, file contents and tool output
in them. Kept: numbers, booleans, numeric strings, and string attributes whose
key is on SAFE_STRING_KEYS (ids, model, timings, enums). Every other string
attribute is removed, and so are span status messages; a log body is kept only
when it is a claude_code.* event name. Totals are computed from the original log, so they are not affected.

Totals prefer the Claude Code OTEL log (per-request api_request events), else
Fullsend's metrics.json. Output is deterministic for the same inputs (no
generation timestamp, gzip mtime 0), so re-publishing a run is a no-op. The
folder is built next to --out and moved into place only when complete. The
script never fails a publish: unreadable inputs are skipped with a warning.
Python 3.9+, standard library only.
"""

import argparse
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
    "agent_type", "subagent_type", "skill.name", "skill.source", "command_name",
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
NUMERIC_STRING = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
EVENT_BODY = re.compile(r"^claude_code\.[A-Za-z0-9_.]+$")


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
    seen, tool_calls, prompts, stamps, bad = set(), 0, 0, [], 0
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
                        if attrs.get("event.timestamp"):
                            stamps.append(str(attrs["event.timestamp"]))
                        if name == "tool_result":
                            tool_calls += 1
                        elif name == "user_prompt":
                            prompts += 1
                        if name != "api_request":
                            continue
                        key = (attrs.get("session.id"), attrs.get("event.sequence"),
                               attrs.get("request_id"), attrs.get("event.timestamp"))
                        if key in seen:  # an exporter retry can resend a batch
                            continue
                        seen.add(key)
                        model = str(attrs.get("model") or "unknown")
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
        by_model[model] = _round_usage({
            "cost_usd": _number(usage.get("cost_usd")),
            "input_tokens": _number(usage.get("input_tokens")),
            "output_tokens": _number(usage.get("output_tokens")),
            "cache_read_tokens": _number(usage.get("cache_read_input_tokens")),
            "cache_creation_tokens": _number(usage.get("cache_creation_input_tokens")),
            "reasoning_tokens": None,
            "api_requests": _number(usage.get("requests")) or None,
        })
    return {"totals": _round_usage(totals, TOTAL_KEYS), "by_model": by_model,
            "started_at": None, "finished_at": None}


def _clean(node, removed):
    """Drop free-text string attributes (and non-event log bodies) in place."""
    if isinstance(node, list):
        for item in node:
            _clean(item, removed)
        return
    if not isinstance(node, dict):
        return
    attributes = node.get("attributes")
    if isinstance(attributes, list):
        kept = []
        for item in attributes:
            value = _attr_value(item.get("value")) if isinstance(item, dict) else None
            if (isinstance(value, (bool, int, float))
                    or (isinstance(value, str) and (NUMERIC_STRING.match(value)
                                                    or item.get("key") in SAFE_STRING_KEYS))):
                kept.append(item)
            else:
                removed[0] += 1
        node["attributes"] = kept
    body = node.get("body")
    if isinstance(body, dict) and not EVENT_BODY.match(str(body.get("stringValue", ""))):
        del node["body"]
        removed[0] += 1
    status = node.get("status")
    if isinstance(status, dict) and status.get("message"):  # span error text
        del status["message"]
        removed[0] += 1
    if isinstance(node.get("raw"), str):  # collector fallback for a non-JSON export
        del node["raw"]
        removed[0] += 1
    for key, value in node.items():
        if key != "attributes":
            _clean(value, removed)


def _store(src, dest, clean):
    """Gzip src (cleaned line by line when clean) deterministically; the raw-file entry."""
    digest, size, removed, dropped = hashlib.sha256(), 0, [0], 0
    with open(src, "rb") as source, open(dest, "wb") as sink:
        with gzip.GzipFile(filename="", mode="wb", fileobj=sink, mtime=0, compresslevel=9) as out:
            for line in source:
                if clean:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        dropped += 1
                        continue
                    _clean(record, removed)
                    line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
                digest.update(line)
                size += len(line)
                out.write(line)
    entry = {"bytes": os.path.getsize(dest), "uncompressed_bytes": size,
             "sha256": digest.hexdigest()}
    if clean:
        entry["removed_attributes"] = removed[0] + dropped
    return entry


def _ci():
    values = {key: os.environ.get(var) or None for key, var in CI_ENV}
    return values if any(values.values()) else None


def _build(out, pipeline, run_id, sources):
    raw_dir = os.path.join(out, "raw")
    os.makedirs(raw_dir)
    raw, summary, source = {}, None, None
    for kind, src, name, clean in sources:
        try:
            raw["raw/" + name] = _store(src, os.path.join(raw_dir, name), clean)
        except OSError as error:
            warn(f"cannot store {src}: {error}")
            continue
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
    with open(os.path.join(out, "usage.json"), "w", encoding="utf-8") as handle:
        json.dump(usage, handle, indent=2)
        handle.write("\n")
    if not os.listdir(raw_dir):
        os.rmdir(raw_dir)  # git does not keep empty directories anyway
    return usage


def write_bundle(out, pipeline, run_id=None, otel_log=None, fullsend_run_dir=None):
    """Write <out>/usage.json and <out>/raw/*.gz, replacing <out> only once complete."""
    sources = []
    if otel_log:
        if os.path.isfile(otel_log):
            sources.append(("claude-otel", otel_log, CLAUDE_OTEL, True))
        else:
            warn(f"Claude OTEL log {otel_log} not found")
    if fullsend_run_dir:
        metrics = os.path.join(fullsend_run_dir, "metrics.json")
        spans = os.path.join(fullsend_run_dir, "run-telemetry.jsonl")
        if os.path.isfile(metrics):
            sources.append(("fullsend-metrics", metrics, FULLSEND_METRICS, False))
        else:
            warn(f"Fullsend metrics {metrics} not found")
        if os.path.isfile(spans):
            sources.append((None, spans, FULLSEND_TELEMETRY, True))

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
