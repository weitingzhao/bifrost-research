# Post-program review — `research-loop-automation`

Run 2026-09-07 against the plan's own acceptance criteria, seven dimensions in parallel,
every finding adversarially verified by a second agent before it was written down.
Raw record, including the ten refuted claims and each finding's evidence and proposed fix:
[`research-loop-automation-postreview.json`](research-loop-automation-postreview.json).

**59 confirmed** (14 high / 26 medium / 19 low), 10 refuted. Seven findings were the same
defect seen from two or three dimensions, so the list below is the 50 distinct ones.

The three walls held under review: no code path reaches an order, nothing writes the Trade
database, neither tree contains a secret, and the trust gate fails closed.

## Fixed and released — research `0.77.0`, frontend `3c2d41f` (16)

| Severity | Where | What the Owner would have seen |
|---|---|---|
| high | `bifrost-research/k8s/engines/cronjob-intraday.yaml:89` | research-gex-intraday and research-settlement CronJobs are declared unsuspended at the same cron minutes as the Dagster schedules that replaced them |
| high | `bifrost-research/src/bifrost_research/api/copilot.py:391` | The approval gate has no lock: /approve, /execute and approve-all take no owner, and auth_required() is False in every shipped manifest |
| high | `bifrost-research/src/bifrost_research/api/harness.py:339` | Owner Approve-all applies the default leash floor 0.45, ignoring the objective's min_source_hit_rate |
| high | `bifrost-research/src/bifrost_research/copilot/approvals.py:19` | Approval HMAC silently falls back to a constant published in this PUBLIC repo; nothing in the tree sets or verifies the real one |
| high | `bifrost-research/src/bifrost_research/mcp/tools/write_loop.py:377` | Every loop write tool with a non-empty default fails chat approval as "tampering" — run_objective, propose_candidate, draft_decision, propose_order_intent |
| high | `bifrost-trade-frontend/src/lib/lensVerdict.ts:144` | Verdict strips print the trigger count as the sample size of hit rates computed over a smaller denominator — and a test pins the wrong string |
| high | `bifrost-trade-frontend/src/pages/research/analyze/scenario/SessionsSection.tsx:200` | Sessions hub verdict label is computed from the one selected settlement (0/1), contradicting the 30-day narrative on the same strip |
| high | `bifrost-trade-frontend/src/pages/research/loop/HarnessConsolePage.tsx:179` | Approve-all toast counts approved drafts, so a partial leash accept reports "Auto-approved 0" after creating hypotheses |
| high | `bifrost-trade-frontend/src/utils/ivRadar/universe.ts:81` | IV Radar buckets at >60/<30 and looks up the ≥80 trigger's hit rate with that bucket |
| medium | `bifrost-research/PROGRESS.md:3` | PROGRESS.md's status paragraph contradicts its own D3 cell — states a $0.50 cap and a pending unattended run that the same file already reports as superseded |
| medium | `bifrost-research/src/bifrost_research/copilot/agents/symbol_verdicts.py:48` | latest_digest prefers the newest *pending* digest, so approving today's digest makes every hub strip show an older day's bands |
| medium | `bifrost-research/src/bifrost_research/copilot/guardrails.py:50` | One quoted phrase anywhere in the conversation disables the D10 chat guardrail for the whole session |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/entry.py:95` | Cron summary line can never say "auto-approved" — it reads a key nothing writes |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/runtime.py:742` | Run outputs and the trace claim auto_approve_eligible=true when the judge stage errored |
| medium | `bifrost-research/src/bifrost_research/lenses/verdict.py:38` | verdict_for gives the lean bands the full hot/cold assertion, re-creating 'short-premium bias' at IV Rank 62 |
| low | `bifrost-trade-frontend/src/lib/analyzeDepth.ts:270` | analyzeDepth.bandFromScore disagrees with the registry at exactly 40 and 60 and has no caller |

## Open (34)

None of these is a safety boundary. Two of them — the skew exhibit's neighbour search and
the forecast calibration panel — change what a verdict *means* rather than how it is drawn,
so they are proposals for the Owner rather than repairs to make unasked.

| Severity | Where | Finding |
|---|---|---|
| high | `bifrost-research/src/bifrost_research/lenses/exhibit_lenses.py:514` | Skew exhibit's `similar` block feeds a 0–100 percentile into a k-NN over raw ATM slope |
| medium | `bifrost-research/src/bifrost_research/api/agents.py:489` | Model-generated policy_suggestion is merged into policy_json with no LoopPolicy validation, and Approve-all never says a policy changed |
| medium | `bifrost-research/src/bifrost_research/api/orchestration_schedules.py:31` | Console husbandry rollup counts 32 schedules while the code defines 31 — research_vrp_schedule is in the API whitelist and gone from the code |
| medium | `bifrost-research/src/bifrost_research/api/wave4.py:476` | forecast/calibration compares two different events and labels the difference "Claimed" / over-confident |
| medium | `bifrost-research/src/bifrost_research/copilot/agents/daily_digest.py:65` | A read failure inside gather_facts leaves a zeroed daily digest that still posts, because the exhibit readers repair the poisoned transaction on the way out |
| medium | `bifrost-research/src/bifrost_research/copilot/agents/hypothesis_resolution.py:68` | resolution.benchmark only labels the reason string — the excess is always measured against SPY |
| medium | `bifrost-research/src/bifrost_research/copilot/agents/symbol_verdicts.py:160` | The verdict strip can never show a waiting order_intent or decision_draft — neither payload carries a top-level symbol |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/batch.py:116` | Held candidate batches never retire, and after the pool TTL the Owner's Approve promotes nothing without saying so |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/persona_eval.py:319` | Judge spend reaches the ledger only after the whole batch, so a mid-stage kill resets the purse |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/policy_schema.py:96` | ResolutionPolicy.horizon_days accepts horizons the settlement engine never writes |
| medium | `bifrost-research/src/bifrost_research/copilot/harness/run_digest.py:138` | digest_run reports the objective's CURRENT policy as the policy the run used |
| medium | `bifrost-research/src/bifrost_research/engines/scan/build.py:26` | Scan's `pin` flag flags the names farthest from max pain, under the registry's opex_pin alias |
| medium | `bifrost-research/src/bifrost_research/engines/signal_hit/build.py:45` | Skew's cold verdict band and the skew track-record's cold side name opposite regimes |
| medium | `bifrost-research/src/bifrost_research/repositories/candidate_pool.py:149` | Two objectives proposing the same symbol on the same day collapse into one pool row, and the second run overwrites source_ref.objective_id |
| medium | `bifrost-trade-frontend/src/hooks/useLensRegistry.ts:30` | The exhibit endpoint takes no trade_date, so a hub strip mixes the selected date's readings with the latest date's band and percentile |
| medium | `bifrost-trade-frontend/src/lib/harness/harnessTrace.ts:174` | resolution and min_source_hit_rate have editors but no mount point — unreachable in the UI |
| medium | `bifrost-trade-frontend/src/pages/research/analyze/volRegime/IvRankSection.tsx:261` | Two different iv_rank hit rates on one IV Radar screen: the table uses a 365-day window, the verdict strip a 90-day one |
| medium | `bifrost-trade-frontend/src/pages/research/validate/SignalDecayPage.tsx:432` | Signal Decay narrates mean-reversion for every lens, including the magnitude lens that owns 704 of 706 rows |
| low | `bifrost-research/docs/CAPABILITY_MATRIX.md:42` | docs/CAPABILITY_MATRIX.md still lists the nine retired Analyze routes as live pages and names none of the five hubs |
| low | `bifrost-research/scripts/verify_husbandry_schedulers.sh:130` | verify_husbandry_schedulers.sh checks three schedules that no longer exist and none of the six added since, and still exits PASSED |
| low | `bifrost-research/src/bifrost_research/copilot/agents/daily_digest.py:430` | The digest hard-codes a calendar claim under an empty resolutions section |
| low | `bifrost-research/src/bifrost_research/copilot/agents/daily_digest.py:630` | A forced digest reports replaced: true but leaves the previous one pending — two digests for one day |
| low | `bifrost-research/src/bifrost_research/copilot/harness/batch.py:78` | curator_trace.new_draft_ids is read but never written — a dead branch in the approve path |
| low | `bifrost-research/src/bifrost_research/copilot/harness/batch_orchestrate.py:38` | trust_status() tells the operator to set an override flag they have already set |
| low | `bifrost-research/src/bifrost_research/copilot/harness/persona_eval.py:303` | auto_approve_eligible is True on a single judge, contradicting the leash and consensus' own docstring |
| low | `bifrost-research/src/bifrost_research/lenses/exhibit_lenses.py:131` | term_structure_label keeps a second copy of the registry's term_slope bands with no test tying them together |
| low | `bifrost-research/src/bifrost_research/lenses/exhibit_lenses.py:164` | term_slope requires no minimum gap between near and far expiry, so ±2 / −3 vol-point bands calibrated for 30-vs-90 DTE can be applied to a 7-day gap |
| low | `bifrost-research/src/bifrost_research/mcp/server.py:18` | Canonical MCP registry omits four registered tools; the parity test is a subset assertion so it cannot catch it |
| low | `bifrost-research/src/bifrost_research/orchestration/engine_assets.py:178` | engines/backtest belongs to no schedule and writes a table nothing reads |
| low | `bifrost-research/src/bifrost_research/repositories/objective.py:197` | A partial resolution suggestion replaces the rule instead of merging, silently restoring defaults |
| low | `bifrost-research/src/bifrost_research/schema/ddl.py:171` | The B3 ALTER aborts the transaction before the two B2 ALTERs, so a non-owner DDL run reports one missing column and silently skips the other two |
| low | `bifrost-trade-frontend/src/components/research/AnalyzeVerdictStrip.tsx:65` | The verdict strip prints a similar-only evidence line under a bold 'Track record' heading |
| low | `bifrost-trade-frontend/src/lib/harness/dailyDigest.ts:9` | The Inbox's folded batch rows drop the leash's accepted / held, leaving it only in prose the model may cut |
| low | `bifrost-trade-frontend/src/pages/research/analyze/dealerLevels/OpexSection.tsx:70` | OpEx pin-history table grades cycles at 0.5%/1.5% while the strip's hot band is 1% |


## Two more from the live unattended run, outside the review's scope

Neither is in the table above; both surfaced while watching the D3 acceptance run judge real
candidates, and both change the semantics of the judgement chain.

**A tool error inside a judge is indistinguishable from a considered abstain.** On IWM the
validate specialist errored repeatedly with "not found" and the stage recorded an abstain, which
the leash then counted as an honest "no opinion". The judge never looked at the evidence. An
abstain born from a broken tool should be its own outcome, and should hold the candidate rather
than count toward agreement.

**A judge's stated reason is never checked against the evidence it was given.** For NVDA
gpt-4o-mini wrote "hit rate of 0%" while the evidence block it received said `hit_rate: 0.5`
over `judged: 8`. Nothing compares the prose to the numbers, so a verdict can be right by
accident or wrong for a reason the Owner cannot see. The cheap version is a post-check on the
few figures the evidence block actually contains; the honest version is to mark such a verdict
low-confidence and hold it.
