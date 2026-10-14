#!/usr/bin/env python3
"""Parse Claude Code stream-json output into a human-readable CI log."""

import argparse
import json
import os
import signal
import sys
import time

parser = argparse.ArgumentParser(description="Parse Claude Code stream-json output")
parser.add_argument(
    "--wrap", type=int, default=0, metavar="COLS",
    help="Word-wrap output at COLS columns (0 = no wrapping)",
)
parser.add_argument(
    "--no-color", action="store_true",
    help="Disable ANSI color codes in output",
)
parser.add_argument(
    "--claude-pid", type=int, default=0,
    help="PID of Claude Code process to kill on completion",
)
args = parser.parse_args()

# ANSI colors (GitLab CI supports these)
if args.no_color:
    THINK_COLOR = TOOL_COLOR = CLAUDE_COLOR = RED = YELLOW = RESET = ""
else:
    THINK_COLOR = "\033[3;31m"    # italic red
    TOOL_COLOR = "\033[1;90m"    # bold gray
    CLAUDE_COLOR = ""            # normal
    RED = "\033[31m"
    YELLOW = "\033[33m"
    RESET = "\033[0m"

in_text = False
in_thinking = False
last_block_type = None
partial_message = False
tool_name = None
tool_json = ""
_line_buf = ""
_total_input_tokens = 0
_total_output_tokens = 0
_total_cache_read = 0
_total_cache_write = 0
_last_emitted_total = 0
_last_emitted_time = 0.0


def emit(text):
    """Buffer text and flush complete lines."""
    global _line_buf
    _line_buf += text
    while "\n" in _line_buf:
        line, _line_buf = _line_buf.split("\n", 1)
        if args.wrap and len(line) > args.wrap:
            # Word-boundary wrap
            wrapped = ""
            col = 0
            for word in line.split(" "):
                if col + len(word) > args.wrap and col > 0:
                    wrapped += "\n"
                    col = 0
                if col > 0:
                    wrapped += " "
                    col += 1
                wrapped += word
                col += len(word)
            print(wrapped, flush=True)
        else:
            print(line, flush=True)


def flush_emit():
    """Flush any remaining buffered text."""
    global _line_buf
    if _line_buf:
        print(_line_buf, flush=True)
        _line_buf = ""


def type_break(block_type):
    """Track block type changes (no separator)."""
    global last_block_type
    last_block_type = block_type


def _format_tool(name, params):
    """Return a compact one-line summary for known tools, or None."""
    if name == "Bash":
        cmd = params.get("command", "")
        desc = params.get("description", "")
        return f"$ {cmd}" + (f"  # {desc}" if desc else "")
    if name == "Read":
        path = params.get("file_path", "")
        parts = [path]
        if "offset" in params:
            parts.append(f"L{params['offset']}")
        if "limit" in params:
            parts.append(f"+{params['limit']}")
        return " ".join(parts)
    if name == "Write":
        return params.get("file_path", "")
    if name == "Edit":
        path = params.get("file_path", "")
        old = params.get("old_string", "")
        preview = old.split("\n")[0][:60]
        if len(old) > len(preview):
            preview += "…"
        return f"{path}: {preview}"
    if name == "Glob":
        pattern = params.get("pattern", "")
        path = params.get("path", ".")
        return f"{pattern} in {path}"
    if name == "Grep":
        pattern = params.get("pattern", "")
        path = params.get("path", ".")
        return f"/{pattern}/ in {path}"
    if name == "Agent":
        desc = params.get("description", "")
        agent_type = params.get("subagent_type", "")
        return f"[{agent_type}] {desc}" if agent_type else desc
    if name == "Skill":
        skill = params.get("skill", "")
        skill_args = params.get("args", "")
        return f"/{skill} {skill_args}".strip()
    if name == "TaskGet":
        return params.get("task_id", "")
    # Generic fallback: key=value pairs on one line
    return ", ".join(f"{k}={v}" for k, v in params.items())


def end_block():
    global in_text, in_thinking, tool_name, tool_json, _col
    if in_text:
        flush_emit()
        _col = 0
        sys.stdout.write(RESET + "\n")
        in_text = False
    if in_thinking:
        flush_emit()
        _col = 0
        sys.stdout.write(RESET + "\n")
        in_thinking = False
    if tool_name:
        # Print tool name + input on a single line, indented
        try:
            parsed = json.loads(tool_json)
        except (json.JSONDecodeError, ValueError):
            parsed = None

        summary = _format_tool(tool_name, parsed) if parsed else None
        type_break("tool")

        if summary:
            icon = "\U0001f916" if tool_name == "Agent" else "\U0001f527"
            print(f"  {TOOL_COLOR}{icon} {tool_name} {summary}{RESET}", flush=True)
        else:
            print(f"  {TOOL_COLOR}\U0001f527 {tool_name}{RESET}", flush=True)
            if parsed:
                formatted = json.dumps(parsed, indent=2)
                for line in formatted.split("\n"):
                    print(f"    {TOOL_COLOR}{line}{RESET}", flush=True)
        tool_name = None
        tool_json = ""


while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        continue

    msg_type = msg.get("type")

    # System events (retries, etc.)
    if msg_type == "system":
        subtype = msg.get("subtype", "")
        if subtype == "api_retry":
            attempt = msg.get("attempt", "?")
            max_retries = msg.get("max_retries", "?")
            delay = msg.get("retry_delay_ms", "?")
            error = msg.get("error", "unknown")
            type_break("system")
            print(
                f"{YELLOW}\U0001f504 Retry {attempt}/{max_retries}{RESET} "
                f"{error} \u2014 retrying in {delay}ms",
                flush=True,
            )
        continue

    # Tool results — check for completion marker in Bash output.
    # finish.py prints "FULL RUN COMPLETE" which arrives as a tool_result
    # in a user message.  Kill the Claude process immediately to prevent
    # wasted turns responding to late-arriving background agents.
    if msg_type == "user":
        for block in msg.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                if isinstance(content, str) and "FULL RUN COMPLETE" in content:
                    end_block()
                    if args.claude_pid:
                        print("--- FULL RUN COMPLETE detected, terminating Claude ---",
                              flush=True)
                        os.kill(args.claude_pid, signal.SIGTERM)
                    sys.exit(42)
        continue

    # Don't exit on 'result' — with --include-partial-messages, it can
    # arrive before Claude finishes background agents. Wait for EOF
    # (FIFO close = Claude actually exited) as the reliable signal.
    if msg_type == "result":
        end_block()
        continue

    # Fullsend emits completed assistant messages without partial deltas.
    # When partial events are enabled, their completed message is a duplicate.
    if msg_type == "assistant":
        if not partial_message:
            for block in msg.get("message", {}).get("content", []):
                kind = block.get("type")
                if kind == "text":
                    emit(block.get("text", ""))
                    flush_emit()
                    print(flush=True)
                elif kind == "tool_use":
                    tool_name = block.get("name", "")
                    tool_json = json.dumps(block.get("input", {}))
                    end_block()
        partial_message = False
        continue

    if msg_type != "stream_event":
        continue

    event = msg.get("event", {})
    event_type = event.get("type")
    partial_message = True

    # Content block start
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type")
        if block_type == "text":
            type_break("text")
            print(f"{CLAUDE_COLOR}\U0001f4ac Claude ", end="", flush=True)
            in_text = True
        elif block_type == "thinking":
            type_break("thinking")
            print(f"{THINK_COLOR}\U0001f9e0 Thinking ", end="", flush=True)
            in_thinking = True
        elif block_type == "tool_use":
            tool_name = block.get("name", "unknown")
            tool_json = ""
        elif block_type == "server_tool_use":
            tool_name = block.get("name", "unknown")
            tool_json = ""

    # Content block deltas
    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            emit(delta.get("text", ""))
        elif delta_type == "thinking_delta":
            emit(delta.get("thinking", ""))
        elif delta_type == "input_json_delta":
            tool_json += delta.get("partial_json", "")

    # Content block stop
    elif event_type == "content_block_stop":
        end_block()

    # Message start — carries cumulative input token counts
    elif event_type == "message_start":
        usage = event.get("message", {}).get("usage", {})
        _total_input_tokens = usage.get("input_tokens", 0)
        _total_cache_read = usage.get("cache_read_input_tokens", 0)
        _total_cache_write = usage.get("cache_creation_input_tokens", 0)

    # Message delta — carries cumulative output token count
    elif event_type == "message_delta":
        usage = event.get("usage", {})
        out = usage.get("output_tokens", 0)
        if out > 0:
            _total_output_tokens = out
            total = _total_input_tokens + _total_output_tokens + _total_cache_read + _total_cache_write
            # Emit every 50k tokens to avoid noise
            if total - _last_emitted_total >= 5_000 or _last_emitted_total == 0:
                now = time.monotonic()
                # Try OTEL collector rate first, fall back to local rate
                rate = 0.0
                try:
                    with open("/tmp/claude-otel-rate.json") as rf:
                        rd = json.load(rf)
                    rate = rd.get("rate", 0)
                except Exception:
                    pass
                # Local fallback: rate from previous emission
                if rate <= 0 and _last_emitted_time > 0:
                    dt = now - _last_emitted_time
                    dv = total - _last_emitted_total
                    if dt > 0:
                        rate = dv / dt
                rate_str = f" rate={rate:.0f}/s" if rate > 0 else ""
                _last_emitted_total = total
                _last_emitted_time = now
                print(f"{TOOL_COLOR}  📊 TOKENS in={_total_input_tokens} out={_total_output_tokens} "
                      f"cache_r={_total_cache_read} cache_w={_total_cache_write} "
                      f"total={total}{rate_str}{RESET}", flush=True)

    # Errors
    elif event_type == "error":
        error = event.get("error", {})
        error_type = error.get("type", "unknown")
        error_msg = error.get("message", "")
        type_break("error")
        print(
            f"{RED}\u274c Error: {error_type}: {error_msg}{RESET}",
            flush=True,
        )

print()
