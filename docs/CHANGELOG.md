# Changelog

All notable changes to HIMARI Core are documented here.

## [Unreleased]

### Amendment v1.5 — resume, loss floor, events, kill switch, M4 demo (2026-11-12, approved by the user)
- §8.6: `himari resume` resets the drawdown reference to the current NAV; the old HWM and
  drawdown stay in the record. It no longer waits for a recovery (that could deadlock a
  suspended, all-cash account). It is refused when NAV is below `account.loss_floor_cad`.
- New config key `account.loss_floor_cad: null` (null allowed in backtest and paper; required
  before live). Config hash changes from `801af849…` to `ca1b6e98…`.
- §8.4: event entries carry `time_assumed` when only the date is published; collected politely
  from the official sites with `source_url` and `retrieved_utc`.
- §8.7: KILL sends no orders, cuts included (`orders_blocked`); from M7 it also cancels open
  broker orders. The safety store verifies its record chain at startup and refuses to run when
  it is broken (`himari status` still reports it).
- §16: M4 is done only when `himari backtest --demo` prints a weekly log and writes a one-page
  HTML report from the fixture data.

### M3 — Gates and state machine (2026-11-12)
- `himari.gates.pipeline.run_gates`: data -> volatility ceiling -> event -> stress -> drawdown ->
  faults and kill switch, with rule 1 checked after every gate (a violation raises).
- `gates.ceiling`: volatility ceiling (scale to 8%) and the daily safety check (above 10%, cut
  the holdings back to 8%).
- `gates.events`: event calendar loader for `config/events/events_YYYY.yaml` (unknown types and
  times without a timezone are errors), the decision-close-to-execution-close window, a coverage
  report naming missing event types per year, and the no-additions event gate. The 2015-2025
  archive is not in the repo yet.
- `risk.stress` and `gates.stress`: factor shocks, the bond price change, single-factor scenarios
  in both directions, the joint scenario, the CAD +5% all-adverse diagnostic (logged only), and
  `scale = min(1, 0.03 / worst single, 0.05 / joint)`.
- `risk.state_machine`: kill switch (command or `state/KILL` file), venue fault and drawdown state
  (NORMAL / HALVED / SUSPENDED) in `state/himari.sqlite`. Records are append-only (UPDATE and
  DELETE are refused by triggers) and chained by SHA-256 with the OS user, UTC time and reason;
  nothing expires. Drawdown thresholds are compared with a 1e-12 tolerance so an exact -10% or
  -15% is never missed through floating point. `resume` is refused while the drawdown is still at
  or below -15%.
- KILL output: the weights get no additions and the gate log sets `orders_blocked`, which the
  order stage (M4) will honour by sending nothing, cuts included.
- CLI: `himari kill`, `unkill`, `resume` (all need a reason) and `himari status`.
- Config: `events` and `stress` sections are now typed models (the YAML and its hash are
  unchanged).
- Tests: cut-only property test over the whole pipeline, Appendix F stress goldens through the
  real functions, persistence tests (restart, no expiry, reasons, KILL file, append-only, chain).

### Amendment v1.4 — sizing order and common scale (2026-11-12, approved by the user)
- §7.3: `k = 0.08 / sqrt(b' rho_s b)` over the whole frozen universe; an instrument without 252
  sessions enters with zero correlation, weight 0 and its budget in cash. Caps and the cash
  constraint act on the all-on weights `a = k u`; signals come last (`w = a s`), so switching a
  signal off never raises another instrument's pre-gate weight (rule 6).
- Appendix F sizing toy case: after caps [0.25, 0.25, 0.15], sum 0.65; after signals
  [0.25, 0.25, 0]; final volatility 0.038324 (unchanged).
- Fixture decision golden for 2025-12-31 re-pinned because of this amendment: the cash constraint
  now binds on the all-on weights (sum 1.11), so VIU 0.0691 -> 0.0623, VEE 0.0605 -> 0.0545,
  VGV 0.2500 -> 0.2251, PDBC 0.1844 -> 0.1660 (invested 56.4% -> 50.8%). k is unchanged.
- CI: strict mypy now also covers `sizing`.
- FX splice log: 2017-01-18 (-0.59%) annotated as a Bank of Canada decision day (CAD fell more
  than a cent intraday; noon snapshot versus daily average). No action; legacy rows are used only
  through 2016-12-30.

### M2 — Strategy and risk (2026-11-12)
- `himari.strategy.trend`: excess-return trend signal over 252 observations; exactly zero is
  off; fewer than 253 valid observations means not eligible. Observations are the fund's
  sessions with a wealth value, so a bridged FX gap day is skipped (as for the EWMA).
- `himari.risk.ewma`: zero-mean EWMA variance seeded with the mean of r^2 over the first 252
  returns; annualized with 252; floors applied per asset class in the decision.
- `himari.risk.covariance`: common TSX-date return matrix (US sessions compounded into the next
  TSX interval), EWMA correlation (half-life 60), shrinkage 0.5 toward I, Sigma = D rho_s D.
- `himari.sizing.weights`: §7.3 steps 1-6 with a trace of every step and the limits that bound.
- `himari.strategy.stage1`: one decision from as-of indices; inputs are cut at the decision
  date. A fund is eligible with a signal, a volatility and 252 returns on the TSX grid.
- Tests: Appendix F goldens through the real functions (decay factors, trend signal, sizing toy
  case); property tests for limits and the common scale; a fixture decision with a golden,
  determinism and no-look-ahead checks. The old inline-numpy toy case was removed.

### Amendment v1.3 — publication-record calendar and FX history (2026-11-11, approved by the user)
- Bank of Canada business days now come from the Bank's publication record (Valet observation
  dates of `FXUSDCAD`, `IEXE0101` and `AVG.INTWO`); `config/calendars/boc_holidays.yaml` only
  decides dates after the record (2026-2027: federal holidays plus Ontario Family Day and Civic
  Holiday closed; Quebec-only days and Easter Monday open). `himari data boc-calendar` lists
  disagreements for manual review. Replaces the v1.1 holiday-list definition.
- FX history: legacy U.S. dollar noon rate `IEXE0101` through 2016-12-30, `FXUSDCAD` from
  2017-01-03 (`himari.data.fx`); rows keep their series name; legacy rows are released at
  18:30 ET; the 2017 overlap is logged to `state/logs/fx_splice_overlap.csv`.
- A bridged FX return is released at the later of the rate's publication and the bar's release.
- CI: strict mypy now also covers `data`, `accounting`, `quality` and `gates`.

### M1 — Data layer (2026-11-11)
- Sources (`himari.data.sources`): `DataSource` and `SeriesSource` interfaces; `CsvSource`;
  `BocValetSource` for `FXUSDCAD` and `AVG.INTWO` (fetch and parse split, so raw responses are
  kept and parsing is tested offline); `VendorSource` stub until a vendor is chosen.
- Calendars (`himari.data.calendars`): XTSE/XNAS sessions, last completed session, weekly
  decision sessions, release times, Bank of Canada business days from
  `config/calendars/boc_holidays.yaml` (no year marked complete yet; see open questions).
- Storage (`himari.data.storage`): append-only Parquet tables for bars, actions and series;
  corrections become new versions; as-of views filter on `released_at_utc`; raw pulls kept
  unchanged under `data/raw/`. Ingestion (`himari.data.ingest`) stamps release times (§5.4 v1.1).
- Indices (`himari.accounting.indices`): CAD total-return index with splits, gross
  distributions, FX fill (`FX_FILLED`) and FX bridging (`FX_BRIDGED`, `FX_OUTAGE`, v1.2);
  cash index with optional point-in-time cutoffs.
- Quality gate (`himari.quality`): Q1–Q9 as pure checks, per-instrument OK/FAULT, the data gate
  applied cut-only (`himari.gates.invariants.check_cut_only`), confirmations on disk.
- CLI: `himari data update --source csv|boc`, `himari data check`, `himari data confirm`.
- Synthetic fixtures for the 8 ETFs, a cross-check source, FX, CORRA and a test holiday calendar
  (`tests/fixtures/make_market_fixtures.py`). Tests: index goldens, every quality check,
  deterministic ingest (byte-identical Parquet and a content-hash golden), cut-only property
  test, and a live Bank of Canada smoke test that runs only with `--run-network`.

### Amendment v1.2 — FX bridging (2026-11-11, approved by the user)
- §5.5: for the index history only, US sessions whose latest FX rate is more than 3 days old
  get no CAD return (`FX_BRIDGED`); the first session with a rate books the compounded return,
  released no earlier than that rate. Gaps over 7 calendar days are outages (`FX_OUTAGE`,
  `DATA_FAULT` until `himari data confirm FXUSDCAD <date>`). Decision-time Q7 is unchanged.
- New parameter `data_quality.fx_bridge_max_calendar_days: 7` in `config/frozen_v1.yaml`.
  Config hash changes from `fc49074f…` to `801af849…`; no runs were recorded under the old hash.

### Amendment v1.1 — data-layer clarifications (2026-11-11, approved by the user)
Recorded in `docs/BUILD_GUIDE.md` (marked *(v1.1)*, log in Appendix G). No value in
`config/frozen_v1.yaml` changed.
- §5.3: `bars.parquet` gains `adj_close` (vendor-adjusted close, used only by Q5);
  action type is `cash` or `split` only; a correction is released at its `observed_at_utc`.
- §5.4: release times. Bars and actions from 18:30 ET on the session/ex-date;
  `FXUSDCAD` for day t from 16:30 ET on t; CORRA for day t from 11:00 ET on the next
  Bank of Canada business day. Bank of Canada business days come from the Bank's published
  holiday list (`config/calendars/boc_holidays.yaml`), not XTSE; Q8 counts them.
- §5.5: `FX_FILLED` flag when the latest earlier FX rate (at most 3 calendar days old) is used.
- §6 Q5: runs on every session with the existing 0.10% tolerance, so missing actions are caught.
- §6 Q6: a missing cross-check on the latest (decision) bar is a fault; on an older date it is
  a warning counted in the weekly report; it never blocks a cut.
- §6 Q9: `ZERO_VOLUME` warning flag.

### M0 — Scaffolding (2026-11-11)
- Created repository structure per §4 of the build guide
- Added `CLAUDE.md` with non-negotiable rules
- Added `config/frozen_v1.yaml` (Appendix B), `config/costs_v1.yaml` (Appendix C),
  `research/register.yaml` (Appendix D)
- Core types: Instrument, Bar, CorporateAction, Order, Fill, Position, SafetyState, etc.
- Config loader with SHA-256 hashing for frozen parameter audit trail
- Clock abstraction: SimClock (historical) and LiveClock (wall time)
- CLI skeleton with all command stubs via typer
- Makefile: setup, lint, test, backtest, shadow, dashboard
- pyproject.toml with dependency groups: core, broker, research, dashboard, dev
- Initial test suite: config hashing, types, golden values
