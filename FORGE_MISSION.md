# Forge Mission

Forge is a local-first autonomous branch factory for this repository. It creates
one isolated git worktree per task, runs a bounded coding engine, applies
deterministic gates and council review, writes an audit packet, and commits only
approved branches. Forge never auto-merges or auto-pushes.

Default model: `deepseek/deepseek-v4-flash` through OpenRouter.
Builder throughput model: `deepseek/deepseek-v4-flash:nitro`.

Safety rules:

1. Never edit the target checkout directly during an autonomous run.
2. Never touch secrets, original evidence, court bundles, or `.git` internals.
3. Keep each task small, reversible, and testable.
4. Treat model output as candidate work until gates and review pass.
5. Record every iteration in `.forge/audit/`.
6. Leave merge and push decisions to the operator.
