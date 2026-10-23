Smoke-test stub of the scoring rubric. Do exactly this and nothing else.

Write the file `{RUN_DIR}/{KEY}.result.md` with the Write tool. If the last digit of
the number in {KEY} is even, use the scores F=2, T=2, S=1, A=2 (total 7, APPROVE);
otherwise use F=1, T=1, S=1, A=1 (total 4, REVISE). The file content is:

```
TITLE: Smoke strategy for {KEY}

| Criterion | Score | Notes |
|-----------|-------|-------|
| Feasibility     | F/2 | smoke |
| Testability     | T/2 | smoke |
| Scope           | S/2 | smoke |
| Architecture    | A/2 | smoke |
| **Total**       | **TOTAL/8** | |

### Verdict: VERDICT
### Needs Attention: NEEDS

Smoke assessment.
```

with F, T, S, A, TOTAL, VERDICT (APPROVE or REVISE) and NEEDS (false for APPROVE, true
for REVISE) filled in. Then reply `scored {KEY}`.
