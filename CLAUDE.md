# HIMARI Core — rules for every session

Read docs/BUILD_GUIDE.md before changing anything. Build milestones in order (§16).
Ask the user when a market fact, fee, contract spec or data source is uncertain. Never guess.

Non-negotiables (each has tests; never weaken a test to make it pass):
1. Risk gates only cut: |w_after| <= |w_before|, sign kept or zero.
2. No look-ahead: decide after the close of t; execute at t+1 at the earliest.
3. Missing/stale/flagged data: no additions; never impute prices, actions, FX or signals.
4. Parameters live in config/frozen_v1.yaml. Never edit them to improve a result.
5. Stage 1 = long-or-cash, sum(w) <= 1. Unused budget stays in cash.
6. Append every backtest/test/paper run to the ledgers. Never delete trials.
7. Run costs at 1x and 2x; pass/fail at 2x.
8. No neural nets, LLM calls, regime models, optimizers, stop-losses or strategy search in the core.
   New ideas only via research/register.yaml (max 10, one at a time).
9. Safety states (SUSPENDED, KILL, faults) persist on disk; manual clear with a reason only.
10. LIVE_TRADING_ENABLED=false. Never enable live trading in code or tests.
11. Secrets only in .env. Never print, log or commit them.
12. UTC storage; exchange calendars for sessions; America/Toronto for display.
13. Deterministic: fixed seeds; golden files must match exactly.

Commands: make setup | make lint | make test | himari --help
Definition of done: tests green, mypy/ruff clean, CHANGELOG entry, no frozen value changed.
