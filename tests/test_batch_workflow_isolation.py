"""Execute the Workflow control flow with authored receipts, without a model."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''
import fs from 'node:fs'
const scenario = process.argv[1]
const source = fs.readFileSync(process.argv[2], 'utf8').replace('export const meta =', 'const meta =')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const invoke = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'log', source)
const pairs = [1, 2].map(n => ({ rfe: `RHAIRFE-${n}`, strat: `RHAISTRAT-${n}` }))
const deferred = [{ key: 'RHAIRFE-2', reason: 'freshness: live RFE inputs changed: RHAIRFE-2' }]
const calls = []
const agent = async (prompt, options) => {
  calls.push(options.label)
  switch (options.label) {
    case 'read input': return {
      cwd: '/fixture', run_id: 'batch-test', mode: 'batch', dry_run: true,
      acquired_keys: pairs.map(p => p.rfe), review_only: [], create_done: true, pending_create: [],
      strategies: pairs, refined: [], pushed: [], reviewed: [],
      deferred: scenario === 'initial' ? deferred : [],
    }
    case 'verify refinement progress': return {
      kind: 'strat-refine-readiness', run_id: 'batch-test',
      refined: scenario === 'initial' || scenario === 'refine' ? ['RHAISTRAT-1'] : pairs.map(p => p.strat),
      rejected: scenario === 'initial' || scenario === 'refine'
        ? [{ key: 'RHAISTRAT-2', reason: 'input_changed' }] : [],
    }
    case 'push': return { recorded: 'push', strategies: pairs.map(p => p.strat) }
    case 'prepare reviews': return { reviewed: [], deferred: scenario === 'review' ? [] : deferred }
    case 'write result': return {
      kind: 'strat-completion', run_id: 'batch-test', action: scenario === 'failure' ? 'failed' : 'completed',
      check_exit: scenario === 'failure' ? 1 : 0, receipt_id: 'a'.repeat(32), receipt_sha256: 'b'.repeat(64),
      deferred_rfes: ['RHAIRFE-2'],
    }
  }
  if (options.label.startsWith('refine ')) return { recorded: options.label.slice(7) }
  if (options.label.startsWith('score ')) return 'Authored assessment.'
  if (options.label === 'review RHAISTRAT-2') return { key: 'RHAISTRAT-2', deferred: true }
  if (options.label.startsWith('review ')) return {
    key: options.label.slice(7), recommendation: 'approve', needs_attention: false,
  }
  throw new Error(`Unexpected step: ${options.label}`)
}
const parallel = tasks => Promise.all(tasks.map(task => task()))
const pipeline = (items, first, second) => Promise.all(items.map(async item => second(await first(item), item)))
try {
  const result = await invoke({}, agent, parallel, pipeline, () => {}, () => {})
  console.log(JSON.stringify({ result, calls }))
} catch (error) {
  console.log(JSON.stringify({ error: error.message, calls }))
}
'''


@pytest.mark.parametrize('scenario', ['initial', 'refine', 'prepare', 'review', 'failure'])
@pytest.mark.skipif(shutil.which('node') is None,
                    reason='Node required for offline Workflow control-flow tests; CI installs it')
def test_deferred_ticket_does_not_override_verified_completion(scenario):
    node = shutil.which('node')
    assert node, 'Node is required for offline Workflow control-flow tests'
    response = subprocess.run([node, '--input-type=module', '-e', DRIVER, scenario,
                               str(ROOT / 'workflows/strat-pipeline.js')],
                              check=True, capture_output=True, text=True, timeout=15)
    value = json.loads(response.stdout)
    if scenario == 'failure':
        assert 'business completion not verified' in value['error']
        return
    assert value.get('result', {}).get('action') == 'completed', value
    assert 'review RHAISTRAT-1' in value['calls']
    assert ('review RHAISTRAT-2' in value['calls']) == (scenario == 'review')
    if scenario in ('initial', 'refine', 'prepare'):
        assert 'score RHAISTRAT-2' not in value['calls']
