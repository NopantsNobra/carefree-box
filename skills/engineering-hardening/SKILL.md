---
name: engineering-hardening
description: This skill should be used when investigating recurring or high-impact coding failures, fragile workarounds, repeated Agent mistakes, or when converting a confirmed failure into architectural, static, test, runtime, or CI safeguards.
agent_created: true
---

# Engineering hardening

Convert confirmed failures into the smallest maintainable engineering safeguards. Do not use this workflow for routine coding or isolated, low-risk fixes without evidence of meaningful recurrence or impact.

## Workflow

1. **Define the failure.** Record the actual behavior, expected behavior, impact, recurrence evidence, and reproduction path. Mark unreproduced claims as unverified.
2. **Find the root cause.** Inspect the real execution path and distinguish the immediate coding mistake from the system condition that allowed it. Do not assume that another written reminder is the solution.
3. **Select the earliest proportionate layer.** Prefer API or architectural prevention, then type, static, or build-time rejection, then automated test, runtime, or CI detection. Retain written guidance for judgment that cannot be formalized reliably. Include high-impact one-time failures in this evaluation.
4. **Freeze independent acceptance.** Derive expected behavior from requirements, contracts, existing behavior, or the reproduced failure. Preserve a regression case for the original failure; do not derive expectations solely from the proposed implementation.
5. **Implement the minimal safeguard.** Change the smallest practical surface and preserve existing conventions. Do not require every layer or introduce a framework when one focused constraint is sufficient.
6. **Verify without weakening.** Exercise the original failure path where practical, run relevant existing and new checks, and confirm that acceptance criteria were not relaxed. Treat green tests as evidence only for the behaviors they actually exercise.
7. **Report the result.** State the failure mode, root cause, implemented safeguard, verification evidence, unverified items, and residual risk.

## Anti-patterns

- Add another natural-language rule when a practical machine-enforceable constraint is available.
- Modify tests, thresholds, fixtures, or gate configuration merely to obtain a passing result.
- Claim requirement correctness solely because the implementation and its self-authored tests agree.
- Perform the full hardening workflow for every small edit.
