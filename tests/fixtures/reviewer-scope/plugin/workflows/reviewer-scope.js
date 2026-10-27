export const meta = {
  name: 'reviewer-scope',
  description: 'Check single-ticket review scope while three tickets share a workspace',
}
const schema = {
  type: 'object',
  required: ['key', 'recommendation', 'needs_attention'],
  properties: {
    key: { type: 'string', pattern: '^(RHAISTRAT|STRAT)-[0-9]+$' },
    recommendation: { type: 'string', enum: ['approve', 'revise', 'reject'] },
    needs_attention: { type: 'boolean' },
  },
}
const targets = ['RHAISTRAT-9101', 'RHAISTRAT-9102', 'RHAISTRAT-9103']
const results = await parallel(targets.map(key => () => agent(
  `This is a focused review-scope test. Your assigned strategy is ${key}. ` +
  'Use the Skill tool once for each of strategy-feasibility-review, ' +
  'strategy-testability-review, strategy-scope-review and strategy-architecture-review, ' +
  `with args "${key}". Invoke the four roles in parallel and wait for their reports. ` +
  'Other strategy and RFE files are present. Do not read them or pass their content ' +
  'to reviewers. Do not invoke strategy-review, the scorer or publishing scripts. ' +
  'Do not recover reports from transcripts or rerun a reviewer. Do not compose reports ' +
  'for other targets. Each report must assess only your assigned ticket. If a report ' +
  'is missing or returns REVIEW_SCOPE_ERROR, report needs_attention=true. Return ' +
  `the combined status for ${key} through your structured result.`,
  { label: `reviewer scope ${key}`, schema, effort: 'medium' },
)))
if (results.some((result, index) => !result || result.key !== targets[index])) {
  throw new Error('Missing or wrongly attributed review receiver status')
}
return results
