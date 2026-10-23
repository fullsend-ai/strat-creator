export const meta = {
  name: 'reviewer-delivery',
  description: 'Check the real reviewer Skills under the S1 review status schema',
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
const result = await agent(
  'This is a focused reviewer-delivery test on authored local inputs. Use the Skill tool ' +
  'once for each of strategy-feasibility-review, strategy-testability-review, ' +
  'strategy-scope-review and strategy-architecture-review, with args RHAISTRAT-9001. ' +
  'Wait for every report. Do not invoke strategy-review, the scorer or any publishing ' +
  'script. Do not retrieve missing reports from native transcripts and do not rerun a ' +
  'reviewer. The input files are in artifacts/strat-tasks, artifacts/rfe-tasks and ' +
  '.context/architecture-context. After all four Skill calls return, report the combined ' +
  'status for RHAISTRAT-9001 through your structured result. Set needs_attention=true ' +
  'if any assessment is missing or has unresolved concerns.',
  { label: 'reviewer delivery RHAISTRAT-9001', schema, effort: 'medium' },
)
if (!result) throw new Error('Review receiver returned no structured status')
return result
