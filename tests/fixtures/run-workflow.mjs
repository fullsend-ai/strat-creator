// Runs a Workflow script with mocked agent() results, for tests.
//
// Usage: node run-workflow.mjs <script.js> <scenario.json>
// The scenario is {"args": ..., "responses": {"<label>": <result or null>}}.
// Every agent() call must have a response for its label; the run prints
// {"calls": [labels in call order], "result": ..., "logs": [...], "error": ...}.
import { readFileSync } from 'node:fs'

const [scriptPath, scenarioPath] = process.argv.slice(2)
const scenario = JSON.parse(readFileSync(scenarioPath, 'utf8'))
const source = readFileSync(scriptPath, 'utf8').replace(/^export const meta/m, 'const meta')

const calls = []
const logs = []
async function agent(prompt, opts = {}) {
  const label = opts.label || prompt.slice(0, 40)
  calls.push(label)
  if (!(label in scenario.responses)) throw new Error(`no mocked response for agent "${label}"`)
  return structuredClone(scenario.responses[label])
}
async function parallel(thunks) {
  return Promise.all(thunks.map((t) => t().catch(() => null)))
}
async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item, index) => {
    let value = item
    for (const stage of stages) {
      try {
        value = await stage(value, item, index)
      } catch {
        return null
      }
    }
    return value
  }))
}
const phase = () => {}
const log = (message) => logs.push(message)

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('agent', 'parallel', 'pipeline', 'phase', 'log', 'args', source)
let result = null
let error = null
try {
  result = await run(agent, parallel, pipeline, phase, log, scenario.args)
} catch (e) {
  error = String(e && e.message ? e.message : e)
}
process.stdout.write(JSON.stringify({ calls, result, logs, error }) + '\n')
