# forge-guard roadmap

Deployed baseline (2026-08-10): branch-protection sweep (hourly) + advisory AI
review lane (15-min ticks) + quality gate in ci-tools (piloted on acme-sdk).
Design record: `specs/2026-08-07-forge-guard-design.md`.

## Review depth (added 2026-08-12 — planned, NOT started)

Current lane 3 supports Claude or Codex as the MR reviewer (Claude by default;
the agent-box trial target is GPT-5.6 Sol through `codex exec`, 1–2 calls/MR).
Quality was acceptable on the prior Claude deployment; compare the provider
trial on real MRs before claiming improvement. Grounding data (sampled 2026-08-12):
median MR = 14KB diff / 234KB full changed-files; 22/100 recent MRs target
prod-ish branches (main/master/prod/develop-105).

0. **Instrument usage first** — log tokens/duration/model per review for both
   provider runners. All go/no-go decisions below use this data. (Near-zero cost.)
1. **Full-file context** — include complete content of changed files alongside
   the diff. Cap: skip files >100KB, total payload ≤300KB, fall back to
   diff-only beyond cap (uncapped worst case seen: 1.6MB on service!211).
   Main win is precision: kills the "flagged X but it's handled above the hunk"
   false-positive class the skeptic pass was built for. ~60–160k tokens and
   ~2–3.5 min per MR (~10× today, ~1.5–2M tokens/day).
2. **MR discussion + linked issue in prompt** — near-free; better severity and
   tests_opinion judgment from knowing intent (hotfix vs feature).
3. **Agentic review in a shallow clone** — checkout at MR head, `claude -p`
   with tools so it can chase callers/read neighbors; the only mode that sees
   cross-file breakage, and immune to the diff-size cap (service!211's 348KB
   diff exceeds FORGEGUARD_DIFF_CAP and gets no review today). ~5–10 min and
   200–500k tokens per MR. Never run wholesale.
4. **Tiered depth = 1+2 for all MRs; 3 only for MRs targeting
   main/master/prod/production/develop-105 or >300 changed lines** (~4–5/day →
   +1–2.5M tokens/day). Quota note: all of this rides the Max subscription's
   shared 5-hour windows — verify headroom from item 0's data before enabling
   the deep tier.

Optional calibration before committing: re-run the last 10 reviewed MRs with
full-file context and diff the notes against the shipped reviews (~1M tokens,
run off-peak).

Isolation for item 3: sandbox the clone-side review process fail-closed
(probe-then-confine, runner failure ≠ clean review, env scrub) — patterns and
platform mechanics recorded in
`specs/2026-08-15-deepseek-harness-fail-closed-reference.md`.

## Other open items

- `inject_gate --apply` fleet rollout (include injection + allowlist adds +
  `only_allow_merge_if_pipeline_succeeds` flip) — announce to team first.
- Gate-Skip Feishu audit alert.
- Review lane blind spot: `state=opened` filter means MRs opened AND merged
  while the workstation is offline are never reviewed — fix is `state=all`
  with the same cursor, or move forge-guard to an always-on office host.
- Remove the 3 access-blocked excludes (argocd-deployments, claude-plugins,
  legacy-ci-executor) when an admin PAT lands; delete orphan project
  `acme-group/quality-gate` (id 108) — needs admin.
