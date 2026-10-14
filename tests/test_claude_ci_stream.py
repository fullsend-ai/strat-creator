import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / '.fullsend/scripts/ci/stream-claude.py'


def render(events):
    return subprocess.run(['python3', str(SCRIPT), '--no-color'],
                          input=''.join(json.dumps(e) + '\n' for e in events),
                          text=True, capture_output=True, check=True).stdout


def test_completed_assistant_text_and_tool():
    text = 'A long response ' + 'x' * 10000
    output = render([{'type': 'assistant', 'message': {'content': [
        {'type': 'text', 'text': text},
        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'echo progress'}}]}}])
    assert text in output
    assert 'echo progress' in output


def test_partial_message_not_printed_twice():
    output = render([
        {'type': 'stream_event', 'event': {'type': 'content_block_start', 'content_block': {'type': 'text'}}},
        {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'once'}}},
        {'type': 'stream_event', 'event': {'type': 'content_block_stop'}},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'once'}]}},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'next'}]}}])
    assert output.count('once') == 1
    assert 'next' in output


def test_completion_marker_preserved():
    result = subprocess.run(['python3', str(SCRIPT), '--no-color'], text=True,
        input=json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'content': 'FULL RUN COMPLETE'}]}}) + '\n',
        capture_output=True)
    assert result.returncode == 42
