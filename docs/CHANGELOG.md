# Changelog

All notable changes to HIMARI Core are documented here.

## [Unreleased]

### Amendment v1.7 — approved readings, final ceiling, known-in-advance events, cash sleeve, robustness pack (2026-11-14, approved by the user)
- A (§9.2, §9.3, §9.4, §12.2): the seven v1.6 readings are approved wording and lose their "to
  confirm" markers. No special case for gate cuts: the band compares the final gated target with
  the holding (the fixture run's 2025-03-07 VCN trim stays).
- B (§8.1, §8.2, §14): `final_ceiling`, the volatility ceiling run again as the seventh and last
  gate (`gates/pipeline.py`). Property test (`tests/property/test_gates_final_ceiling.py`): after
  the last gate `σ_p ≤ 0.08` and every stress loss within its limit, for random holdings, states
  and capped funds. Config guard (`risk/stress.py` `gains_in_loss_scenarios`,
  `tests/unit/test_final_ceiling.py`): fails if a fund gains in a scenario where another loses;
  today's loadings have none.
- C (§8.4): the event gate uses only releases on the published calendar before the decision
  close (`Event.known_by`). The archive (`config/events/`, 941 entries) now holds 926 scheduled
  releases (FOMC 87, BOC 88, ECB 88, SNB 44, BOJ 93, US_CPI 131, US_NFP 131, CA_CPI 132,
  CA_LFS 132) and 15 `unscheduled: true` announcements kept for the replays and ignored by the
  gate (FOMC 7, BoC 2, BoJ 2, ECB 1, SNB 3), including all the dates the user listed. BoJ
  2020-03-16 moved from scheduled to unscheduled: its 18-19 March meeting was brought forward on
  15 March, after the Fed's Sunday cut. The Fed's 15 March 2020 cut falls inside the 13 March
  decision window and is ignored (tested). 2025: 11 US CPI and 11 US jobs releases, matching the
  user's count; 4 are `rescheduled: true` with their BLS notices (CPI 24 October, `announced_utc`
  from the notice last modified 10 October 2025; jobs 20 November and 16 December and CPI 18
  December, whose notice gives no announcement date, so the gate ignores them). None of the four
  falls in a 2025 decision window. Unscheduled entries mostly publish no time and are flagged
  `time_assumed`. `tests/integration/test_event_archive.py`; collectors in `scripts/events/`
  (`adjustments.py`, `write_yaml.py`).
- D (§7.4, §2 rule 5, §9.1): the cash sleeve. New `cash_sleeve` block in `frozen_v1.yaml`
  (`symbol: null` until the user picks the fund). `execution/sleeve.py` (`expected_cad_after`,
  `plan_sleeve`); the engine plans the sleeve order after the risk plan and uses the sleeve only
  from the fund's inception. KILL or VENUE_FAULT: no sleeve orders. A data fault on the sleeve:
  no sweeps, while funding sales still go through. Each decision logs the +50 bp diagnostic. The broker fills a session's sales before its buys
  and the sleeve buy last. Reports keep the sleeve out of exposure, time in market and strategy
  turnover and give it its own lines; each sleeve run gets a "cash at the broker" sensitivity
  (`<run>-cash-at-the-broker`, noted in the trial ledger). The CLI summary names the sensitivity
  runs and prints a sleeve line. Fixture: the synthetic sleeve fund `TBILL.SYN`, from 2 July 2024
  at 50.00, accrues the previous session's CORRA less 0.10% and pays it out each month-end. Real
  candidates checked on the issuers' sites: `docs/cash_sleeve_candidates.md` (CBIL, TBIL, CMR,
  ZMMK; CGOV not yet trading).
- E (§12.3, §12.7, §16 M5): the robustness pack, as guide text; built in M5.
- F (§10.7, §9.3, §9.4, §14, §16 M7 and M9): crypto comparator text (nothing built); two
  questions to settle before M7; `scripts/` added to ruff (Makefile and CI).
- G: the IBKR pricing plan and account size were not given, so `account.initial_nav_cad` stays
  at the 100,000 placeholder; to be asked again before M6.
- Config hash changes from `b7563f9b…` to `de69ce67…`. The run-log golden is re-pinned
  (`2a8b67c6…` to `4f1efa7a…`): the 1x fixture run now carries the sleeve (its orders, fills and
  distributions) and a `final_ceiling` entry in every gate log.
- Fixed before commit: the synthetic sleeve first accrued at the highest CORRA seen so far
  instead of the latest, about 3.9% a year instead of 2.8% in 2025; a fixture test now ties its
  payouts to the fixture CORRA.
- Demo (synthetic data; metrics from 2025-01-03, 258 warm-up sessions; gates bound at 52
  decisions: event 4, stress 4, others 0, in every run):
  - sleeve, 1x: NAV 108,933.50, +6.08% (cash +2.89%), max drawdown -3.65%, strategy costs
    486.36, 51 trades; sleeve average weight 39.2%, 12 trades, costs 175.85, income 1,190.22.
  - sleeve, 2x: NAV 108,153.74, +5.43%, -3.82%, costs 971.23, 51 trades; sleeve costs 351.89,
    income 1,182.97.
  - cash at the broker: 1x NAV 107,956.63 (+5.72%), 2x 107,469.11 (+5.25%), unchanged from v1.6.
- Not yet approved (marked "to confirm" in the guide): `cash_sleeve.duration_years` and the
  diagnostic as sleeve weight x duration x 0.005 (§7.4); every archived event, unscheduled
  included, still marks its execution session as stressed in the cost model (§11). Demo only,
  synthetic: the sleeve's 1 bp half spread and 0.15 duration (`sim/run.py`).

### Amendment v1.6 — band and post-trade limits, order reasons, cash interest, warm-up, reinvested and Q10 (2026-11-13, approved by the user)
- §9.2 (amends a frozen rule): the band's exceptions are full exits, daily safety cuts and the
  cut on entering HALVED; other gate cuts follow the band. After the band, if the post-trade
  `σ_p` or a stress loss exceeds its limit by more than `rebalance.gate_tolerance_rel` (0.10),
  this decision's buys shrink pro rata until it fits; if it still does not fit with no buys,
  every fund trades to its gated target (`execution/post_trade.py`). Property test: never more
  than 10% over a limit, and buys shrink before any holding is cut. On the fixtures, the
  2025-01-24 decision (VUN switching on, stress gate x0.949) now trades VUN only.
- §9.3: every order records its reason (`band`, `full_exit`, `post_trade_limit`, `halved`,
  `safety_cut`) and the gates that cut its fund with their scales; the trade list and logs show
  them.
- §9.4: interest on settled cash (`costs_v1.yaml` `cash_interest`, placeholders to verify on
  IBKR's interest-rate page before M6; `settlement_days`: T+3, T+2 from 2017-09-05, T+1 from
  2024-05-27 in Canada and 2024-05-28 in the US, from the SEC and CSA notices), accrued daily,
  posted monthly, accruals in NAV. `account.initial_nav_cad: 100000` (placeholder) is the default
  starting cash and sets the tiers. Alert when a distribution is unpaid 30 days after its ex-date.
- §12.2: metrics start at the first weekly decision with a valid signal; charts shade the
  warm-up; average gross exposure reported. §12.3: benchmarks use the same cash model.
- §5.5, §6: action type `reinvested` (no cash, `D_t = 0`, never explains a Q4 jump); check Q10
  (`ACTION_UNCONFIRMED`) for unconfirmed December splits on CAD-listed funds; confirmations name
  their check (`himari data confirm ... --check Q4|Q7|Q10`).
- Fixtures: the synthetic distributions gained pay dates (5 sessions after the ex-date), VCN
  gained reinvested year-end distributions, and `confirmations.jsonl` holds their Q10
  confirmations. Prices, FX and CORRA are byte-identical; the stage-1 golden weights are
  unchanged; the ingest content hash is re-pinned (`225ed574…` to `36b1122f…`).
- Config hash changes from `ca1b6e98…` to `b7563f9b…`; the costs config gains two blocks.
- Implementation choices, not yet approved (marked "to confirm" in the guide): entering HALVED
  is the first weekly decision in HALVED after one that was not; a shrunk buy keeps reason
  `band` and lists `post_trade_buys` among its gates; USD funding covers the buy's estimated
  commission; interest accrues at CORRA for the previous session over calendar days / 365, a
  month is posted at the next month's first session, settlement days count on the exchange
  calendar, distributions, interest and conversions settle when credited, the simulator accrues
  and the ledger books its postings, and the cost multiplier does not scale interest; a "valid
  signal" is a fund with a computed trend signal (on or off).

### M4 — Simulator (2026-11-13)
- `sim/market.py`: `PointInTimeData`, the strategy's as-of view (rule 2). Rows are visible from
  their release time; corrections and late rows start epochs, and indices are rebuilt per epoch
  and cut at the decision date (prefix-stable), so a backtest is exact without re-reading the
  store each session. `MarketTruth`, the simulator's market (final data), for fills only.
- `sim/broker.py`: `SimBroker` (§10.4): next-session close (or open, sensitivity variant) moved
  by the cost model, stress spreads in event windows, participation cap with 3 carried sessions,
  halts, whole shares, no shorts or borrowing, idempotency keys, USD funding, splits,
  distributions with withholding, interest.
- `execution/`: cost model (§11; the commission schedule must be given explicitly), the band,
  the order builder (whole shares toward zero, CAD 100 minimum), the post-trade check,
  reconciliation (0 shares, CAD 1).
- `accounting/ledger.py` (§9.4) and `accounting/interest.py`; `research/trials.py`, the
  append-only trial ledger (§12.6), so every run is logged (rule 6).
- `sim/engine.py`: the §10.3 loop over XTSE and XNAS sessions running the §9.1 weekly cycle,
  the daily safety check, the drawdown state, reconciliation with VENUE_FAULT on a mismatch,
  KILL. `sim/report.py`: the run log (JSON lines, the determinism golden), NAV and trade CSVs,
  the weekly log and a one-page HTML report (assets in `sim/assets/`).
- Gate log details print weights to 6 decimals (they were full floats inside strings, which the
  run log's rounding could not reach). USD funding covers the buy's commission (without it a
  one-share PDBC remainder could never fill).
- `himari backtest`: `--demo` runs the synthetic fixtures at 1x and 2x with a SYNTHETIC
  commission schedule (`tests/fixtures/commissions_demo.yaml`); a real run refuses to start
  without `--commissions`. Outputs under `state/runs/<run>/`, trials in `state/trials.sqlite`.
- Demo (synthetic data; metrics from 2025-01-03, 258 warm-up sessions): 1x NAV 107,956.63,
  +5.72% vs cash +2.89%, 51 trades; 2x NAV 107,469.11, +5.25%, 51 trades.
- Tests: costs, band, orders, post-trade (unit and property), interest and settlement, ledger,
  broker (capacity cap partial fill, duplicate order refused, holiday divergence), trial ledger,
  CLI, and on the fixtures: 1x/2x, byte-identical reruns, a pinned run-log golden, no
  look-ahead (future prices +50%, FX +10%), KILL, a reconciliation mismatch, an unpaid
  distribution, point-in-time epochs. `sim` and `research` join strict mypy.

### Event archive 2015–2025 (2026-11-13)
- `config/events/events_2015..2025.yaml` hold all nine §8.4 types, 927 scheduled releases:
  FOMC 87, BOC 88, ECB 88, SNB 44, BOJ 94, US_CPI 131, US_NFP 131, CA_CPI 132, CA_LFS 132. No
  type is missing in any year. 2025 has 11 US CPI and 11 jobs releases (the September reports
  moved to 24 October and 20 November, with no separate October release).
- Times as published; `time_assumed` (498 entries) where only the date is published: ECB 13:45
  CET (14:15 from 2022-07-21), SNB 09:30, BoJ 12:00 JST, StatCan 08:30 ET, FOMC 2015.
  Unscheduled decisions are left out (they could not be known at the decision close).
- Collectors in `scripts/events/` (polite: robots.txt, crawl delay, cache, backoff on resets).

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
