export const meta = {
  name: 'strat-pipeline',
  description: 'Create, refine, push and review strategies for the RFEs the host pre-script locked',
  whenToUse: 'The strat-single, strat-batch and strat-resume fullsend agents run it; reads tmp/strat-input.json',
  phases: [
    { title: 'Read', detail: 'run input and progress' },
    { title: 'Create', detail: '/strategy-create over the locked RFEs' },
    { title: 'Refine', detail: 'one /strategy-refine per strategy' },
    { title: 'Push', detail: 'scripts/push_refined_strategies.py' },
    { title: 'Score', detail: 'one strat-scorer per strategy' },
    { title: 'Review', detail: 'one /strategy-review --scores-from per strategy' },
    { title: 'Result', detail: 'agent-result.json from the recorded progress' },
  ],
}

// The order lives here; the work lives in the skills and helpers. A workflow
// has no shell, so every command runs in a step agent, and each step records
// its outcome in tmp/strat-progress.json through scripts/strat_pipeline_state.py.
// A second fullsend iteration reruns this script from the top and skips what
// that file already records. Workflow agents have no Agent tool, so the
// strat-scorer that /strategy-review would launch runs here as its own agent
// and the review consumes its result with --scores-from.
//
// Steps: create, refine, push, review. The host pre-script gives each RFE a
// resume point (resume_points, from Jira labels and the previous run's
// progress). create always runs: the sandbox starts with no artifacts, and for
// an RFE that already has a STRAT, strategy-create imports it (Path A) instead
// of cloning. An RFE whose resume point is review skips refine and push.

// Two copies of this file ship, byte for byte the same (a test checks it):
// .fullsend/plugins/strat-pipeline/workflows/ for fullsend, where the
// pre-script has vendored the skills into the target repository under their
// own names, and workflows/ in the strat-creator plugin for interactive use,
// where the skills and the scorer are namespaced: pass args
// {"namespace": "strat-creator:"} there.
const NS = (args && args.namespace) || ''
if (NS && !/^[a-z0-9-]+:$/.test(NS)) throw new Error(`namespace must look like "strat-creator:", got ${NS}`)

const HEADLESS =
  'You are one step of an automated pipeline running headless in a sandbox. Nobody can ' +
  'answer questions: never use AskUserQuestion, and decide from the skill\'s own rules. ' +
  'Never lock or unlock Jira issues and never run scripts/lock_issues.py; the host owns ' +
  'the locks. Run commands from the repository root exactly as written.\n\n'

const STRAT = '^(RHAISTRAT|STRAT)-[0-9]+$'
const RFE = '^RHAIRFE-[0-9]+$'
const PAIR = {
  type: 'object',
  required: ['rfe', 'strat'],
  properties: { rfe: { type: 'string', pattern: RFE }, strat: { type: 'string', pattern: STRAT } },
}
const READ = {
  type: 'object',
  required: ['cwd', 'run_id', 'mode', 'dry_run', 'acquired_keys', 'review_only', 'create_done',
    'pending_create', 'strategies', 'refined', 'pushed', 'reviewed'],
  properties: {
    cwd: { type: 'string', minLength: 1 },
    run_id: { type: 'string', minLength: 1 },
    mode: { type: 'string', enum: ['single', 'batch', 'resume'] },
    dry_run: { type: 'boolean' },
    acquired_keys: { type: 'array', items: { type: 'string', pattern: RFE } },
    review_only: { type: 'array', items: { type: 'string', pattern: RFE } },
    create_done: { type: 'boolean' },
    pending_create: { type: 'array', items: { type: 'string', pattern: RFE } },
    strategies: { type: 'array', items: PAIR },
    refined: { type: 'array', items: { type: 'string', pattern: STRAT } },
    pushed: { type: 'array', items: { type: 'string', pattern: STRAT } },
    reviewed: { type: 'array', items: { type: 'string', pattern: STRAT } },
  },
}
const CREATE = {
  type: 'object',
  required: ['strategies'],
  properties: { strategies: { type: 'array', items: PAIR } },
}
const RECORDED = {
  type: 'object',
  required: ['recorded'],
  properties: {
    recorded: { type: 'string' },
    strategies: { type: 'array', items: { type: 'string', pattern: STRAT } },
  },
}
const refineReceipt = (key) => ({
  type: 'object',
  additionalProperties: false,
  required: ['recorded'],
  properties: { recorded: { type: 'string', enum: [key, 'none'] } },
})

async function refinementCheckpoint(run, keys) {
  const key = { type: 'string', enum: keys }
  const snapshot = await agent(
    HEADLESS + 'Run `python3 scripts/strat_pipeline_state.py refine-status` once. ' +
      'Return its JSON field for field. This verifies current-run saved progress and ' +
      'the actual refined artifacts. Do not mark, edit or rerun refinement here. ' +
      'The plugin checks this structured result against the saved state.',
    { label: 'verify refinement progress', phase: 'Refine', effort: 'low', schema: {
      type: 'object', additionalProperties: false,
      required: ['kind', 'run_id', 'refined', 'rejected'],
      properties: {
        kind: { type: 'string', const: 'strat-refine-readiness' },
        run_id: { type: 'string', const: run.run_id },
        refined: { type: 'array', items: key, uniqueItems: true },
        rejected: { type: 'array', items: {
          type: 'object', additionalProperties: false, required: ['key', 'reason'],
          properties: { key, reason: { type: 'string', enum: ['not_recorded', 'invalid_artifact'] } },
        } },
      },
    } },
  )
  if (!snapshot || snapshot.kind !== 'strat-refine-readiness' || snapshot.run_id !== run.run_id) return null
  const covered = [...snapshot.refined, ...snapshot.rejected.map((item) => item.key)]
  if (covered.length !== keys.length || new Set(covered).size !== keys.length ||
      covered.some((value) => !keys.includes(value))) return null
  return snapshot
}
const VERDICT = {
  type: 'object',
  required: ['key', 'recommendation', 'needs_attention'],
  additionalProperties: false,
  oneOf: [
    { properties: { recommendation: { const: 'approve' }, needs_attention: { const: false } } },
    { properties: { recommendation: { enum: ['revise', 'reject'] }, needs_attention: { const: true } } },
  ],
  properties: {
    key: { type: 'string', pattern: STRAT },
    recommendation: { type: 'string', enum: ['approve', 'revise', 'reject'] },
    needs_attention: { type: 'boolean' },
  },
}
const RESULT = {
  type: 'object',
  additionalProperties: false,
  required: ['kind', 'run_id', 'action', 'check_exit', 'receipt_id', 'receipt_sha256'],
  oneOf: [
    { properties: { action: { const: 'completed' }, check_exit: { const: 0 } } },
    { properties: { action: { const: 'failed' }, check_exit: { const: 1 } } },
  ],
  properties: {
    kind: { type: 'string', const: 'strat-completion' },
    run_id: { type: 'string', minLength: 1 },
    action: { type: 'string', enum: ['completed', 'failed'] },
    check_exit: { type: 'integer', enum: [0, 1] },
    receipt_id: { type: 'string', pattern: '^[a-f0-9]{32}$' },
    receipt_sha256: { type: 'string', pattern: '^[a-f0-9]{64}$' },
  },
}

const failed = []
const fail = (step, key) => {
  failed.push(key ? `${step}:${key}` : step)
  log(`${step}${key ? ' ' + key : ''}: no result`)
  return null
}

phase('Read')
const run = await agent(
  HEADLESS + 'Run `python3 scripts/strat_pipeline_state.py read` once and return the JSON it ' +
    'prints, field for field. If it exits nonzero, stop and report its error.',
  { label: 'read input', phase: 'Read', schema: READ, effort: 'low' },
)
if (!run) throw new Error('read: strat_pipeline_state.py read returned no result')
const dry = run.dry_run ? ' --dry-run' : ''
log(`run ${run.run_id}: ${run.acquired_keys.length} locked RFE(s)${run.dry_run ? ', dry run' : ''}`)

let strategies = run.strategies
// A partial create from an earlier attempt leaves the unresolved RFEs pending;
// only those go to strategy-create again.
if (!run.create_done && run.pending_create.length) {
  phase('Create')
  const created = await agent(
    HEADLESS +
      `Use the Skill tool to run the ${NS}strategy-create skill with args "${run.pending_create.join(' ')}${dry}". ` +
      'The keys are the selection; do not ask which RFEs or which source to use (use Jira). ' +
      'When the skill has finished, run `python3 scripts/strat_pipeline_state.py record-create` ' +
      'and return the strategies list it prints.',
    { label: 'strategy-create', phase: 'Create', schema: CREATE },
  )
  if (!created) fail('create')
  else strategies = created.strategies
}

const reviewOnly = new Set(run.review_only)
const refined = new Set(run.refined)
const receipts = new Map()
const toRefine = strategies.filter((p) => !refined.has(p.strat) && !reviewOnly.has(p.rfe))
if (reviewOnly.size) log(`resume point review: refine and push skipped for ${[...reviewOnly].join(' ')}`)
if (toRefine.length) {
  phase('Refine')
  const results = await parallel(
    toRefine.map((p) => () =>
      agent(
        HEADLESS +
          `Use the Skill tool to run the ${NS}strategy-refine skill with args "${p.strat}${dry}". ` +
          `When it has finished and artifacts/strat-tasks/${p.strat}.md holds the refined strategy, ` +
          `run \`python3 scripts/strat_pipeline_state.py mark refine ${p.strat}\` and return what it prints. ` +
          'If the skill stopped without refining (for example a label gate), do not run the command; ' +
          'return {"recorded": "none"}.',
        { label: `refine ${p.strat}`, phase: 'Refine', schema: refineReceipt(p.strat) },
      ),
    ),
  )
  results.forEach((r, i) => {
    receipts.set(toRefine[i].strat, r)
  })
}

// A missing/malformed handoff is not proof that work failed, and a plausible
// handoff is not proof it finished. Reconcile disk evidence before push/review.
const refineKeys = strategies.filter((p) => !reviewOnly.has(p.rfe)).map((p) => p.strat)
if (refineKeys.length) {
  const snapshot = await refinementCheckpoint(run, refineKeys)
  refined.clear()
  if (snapshot) snapshot.refined.forEach((key) => refined.add(key))
  refineKeys.forEach((key) => {
    if (!refined.has(key)) fail('refine', key)
    else if (receipts.has(key) && (!receipts.get(key) || receipts.get(key).recorded !== key)) {
      log(`refine ${key}: receipt rejected/missing; completion verified from saved progress`)
    }
  })
}

const ready = strategies.filter((p) => refined.has(p.strat) || reviewOnly.has(p.rfe))
// A refined strategy is reviewed only once a recorded push covers it:
// strategy-review posts to Jira, and a review of text Jira does not show
// would be posted again by the resume run. Push coverage is per strategy, so
// a strategy refined after an earlier attempt's push gets a push of its own.
const pushed = new Set(run.pushed)
if (strategies.some((p) => refined.has(p.strat) && !pushed.has(p.strat))) {
  phase('Push')
  const pushPrompt = run.dry_run
    ? 'This is a dry run, so nothing is pushed to Jira. Run ' +
      '`python3 scripts/strat_pipeline_state.py mark push --skipped` once and return what it prints, ' +
      'including its strategies list.'
    : 'Run `python3 scripts/push_refined_strategies.py` once. If it exits 0, run ' +
      '`python3 scripts/strat_pipeline_state.py mark push` and return what it prints, including its ' +
      'strategies list; otherwise ' +
      'return {"recorded": "none"} and do not retry.'
  const push = await agent(HEADLESS + pushPrompt, { label: 'push', phase: 'Push', schema: RECORDED, effort: 'low' })
  // Coverage is what `mark push` recorded: only strategies the push script
  // actually pushed (status Refined, matching jira_key, valid frontmatter).
  if (push && push.recorded === 'push') (push.strategies || []).forEach((k) => pushed.add(k))
  else fail('push')
}

const reviewed = new Set(run.reviewed)
const toReview = ready.filter((p) => !reviewed.has(p.strat) && (reviewOnly.has(p.rfe) || pushed.has(p.strat)))
const unpushed = ready.filter((p) => !reviewed.has(p.strat) && !toReview.includes(p))
if (unpushed.length) log(`not reviewed, no recorded push: ${unpushed.map((p) => p.strat).join(' ')}`)
if (toReview.length) {
  log(`${toReview.length} strateg${toReview.length === 1 ? 'y' : 'ies'} to score and review`)
  await pipeline(
    toReview,
    async (p) => {
      const runDir = `/tmp/strat-assess/${p.strat}`
      const scored = await agent(
        'You are a strategy quality assessor. Before scoring, run ' +
          `\`python3 scripts/publish_review.py ${p.strat} --check-only\`. ` +
          'Exit 0 means a verified current-run review already exists: return without scoring. ' +
          'Exit 3 means no review; follow the rubric below. Any other error stops this step.\n' +
          `1. Read \`${run.cwd}/scripts/assess-strat/agent_prompt.md\` for the full scoring rubric.\n` +
          '2. Follow its instructions exactly, substituting {KEY} for the strategy key and {RUN_DIR} for ' +
          "the run directory. Read the strategy from the data file below (not the path in the rubric's step 1).\n" +
          `3. If architecture context is available at \`${run.cwd}/.context/architecture-context/\`, use Glob ` +
          'and Grep to validate architecture claims against real component docs.\n' +
          `Strategy key: ${p.strat}\n` +
          `Data file: ${run.cwd}/artifacts/strat-tasks/${p.strat}.md\n` +
          `Run directory: ${runDir}`,
        { label: `score ${p.strat}`, phase: 'Score', agentType: `${NS}strat-scorer` },
      )
      if (scored === null) {
        fail('score', p.strat)
        throw new Error(`score ${p.strat}`)
      }
      return runDir
    },
    async (runDir, p) => {
      const verdict = await agent(
        HEADLESS +
          `Use the Skill tool to run the ${NS}strategy-review skill with args "${p.strat} --scores-from ${runDir}${dry}". ` +
          `The scorer already wrote ${runDir}/${p.strat}.result.md. When the skill has finished, run ` +
          `\`python3 scripts/strat_pipeline_state.py record-review ${p.strat}\` and return what it prints.`,
        { label: `review ${p.strat}`, phase: 'Review', schema: VERDICT },
      )
      if (!verdict || verdict.key !== p.strat) {
        fail('review', p.strat)
        return null
      }
      reviewed.add(p.strat)
      return verdict
    },
  )
}

phase('Result')
const failedArgs = failed.map((f) => ` --failed ${f}`).join('')
const result = await agent(
  HEADLESS +
    `Run \`python3 scripts/strat_pipeline_state.py result${failedArgs}\` once. ` +
    'It validates actual input coverage, progress, artifacts and numeric review provenance, ' +
    'writes agent-result.json and tmp/strat-completion.json, and prints a completion receipt. ' +
    'Return that JSON exactly; never invent a successful check. The plugin verifies it against disk. ' +
    'Do not rerun skills, scoring or publication to repair a missing handoff.',
  { label: 'write result', phase: 'Result', schema: {
    ...RESULT, properties: { ...RESULT.properties, run_id: { type: 'string', const: run.run_id } },
  }, effort: 'low' },
)
if (!result) throw new Error('result: strat_pipeline_state.py result returned no result')
if (result.kind !== 'strat-completion' || result.run_id !== run.run_id ||
    result.action !== 'completed' || result.check_exit !== 0 ||
    !/^[a-f0-9]{32}$/.test(result.receipt_id || '') || !/^[a-f0-9]{64}$/.test(result.receipt_sha256 || '')) {
  throw new Error(`result: business completion not verified (${result.action || 'missing'}, check ${result.check_exit})`)
}
return result
