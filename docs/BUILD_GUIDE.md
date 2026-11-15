# HIMARI Core: Build Guide for Claude Code

Version 1.0, 11 November 2026. Design frozen as `frozen_v1`.
Amendments v1.1 to v1.3 (11 November 2026: data-layer clarifications in §5.2 to §5.5 and §6), v1.4 (12 November 2026: §7.3 order and common scale), v1.5 (12 November 2026: resume, loss floor, event archive, kill switch, M4 demo), v1.6 (13 November 2026: band and post-trade check, order reasons, cash interest, warm-up, reinvested distributions) v1.7 (14 November 2026: approved readings, final ceiling, known-in-advance events, cash sleeve, robustness pack, crypto comparator text) v1.8 (15 November 2026: CBIL as the cash sleeve, approved statistics wording, real-data trial counts, Appendix F MinTRL) and v1.9 (16 November 2026: DSR report-only, approved M5 readings, EODHD), marked *(v1.1)* to *(v1.9)*. Log in Appendix G. v1.2 adds `data_quality.fx_bridge_max_calendar_days`, v1.5 `account.loss_floor_cad`, v1.6 `rebalance.gate_tolerance_rel` and `account.initial_nav_cad`, and v1.7 the `cash_sleeve` block; v1.8 sets `cash_sleeve.symbol` and `duration_years`, both null placeholders before. v1.6 and v1.7 amend frozen rules (§9.2; §8.1, §8.4 and the new §7.4).

> **For Claude Code:** read this whole file before writing any code. Build the milestones in §16 in order. The ground rules in §2 override everything else in this file. If anything here conflicts with a market fact you verify (a contract spec, a fee, a calendar, a series code), stop and ask the user. Do not guess, and never change a frozen parameter to make a result look better.

---

## 0. How to use this guide

1. Create a new repository `himari-core` and put this file in it as `docs/BUILD_GUIDE.md`.
2. Create `CLAUDE.md` at the repo root from **Appendix A**. It is the short rule set loaded in every session.
3. Create `config/frozen_v1.yaml`, `config/costs_v1.yaml` and `research/register.yaml` from **Appendices B, C and D**.
4. Work one milestone at a time (§16). A milestone is done only when its acceptance tests pass (`make test`) and it has an entry in `docs/CHANGELOG.md`.
5. Parameters marked **frozen** must not be edited to improve results. A change means a new config version (`frozen_v2`) and a new register entry, and only the user decides that.
6. Values marked **placeholder** are not research results. Replace them with verified figures (issuer factsheet, broker schedule, measured spreads) before the baseline study, and log each replacement in `docs/CHANGELOG.md`.

---

## 1. What we are building

HIMARI Opus 2 started as a crypto "alpha factory": automated strategy generation, reinforcement-learning and transformer decision layers, and real-time crypto data. Evidence reviews completed in November 2026 replaced that design with a slow, rules-based, multi-asset trend system. Its only edge claim is the broad historical evidence for diversified trend following. Everything else is risk control.

| Part | What it does | When |
|---|---|---|
| **Stage 1** | Long-or-cash trend following on 8 ETFs, valued in CAD. Non-registered cash account at IBKR Canada. Weekly decisions. Risk rules can only cut exposure. | Build now |
| **Simulator** | One engine with three modes: historical backtest, paper trading on live market data, and live (disabled). The same strategy code runs in every mode. | Build now |
| **Crypto comparator** | The same engine on BTC and ETH, in simulation only. It is never mixed into Stage 1 results. | After the Stage 1 paper run starts |
| **Stage 2** | CME micro futures: long/short trend plus carry. | Only after Stage 1 passes its promotion gates (§12.7) |

The core pipeline, run once a week:

```
daily data → quality gate → trend signal → risk estimate → target weights
          → risk gates (cut only) → rebalance band → orders (next session)
          → fills → ledger → reconciliation
```

A daily safety check runs outside the weekly loop (§8.3).

Background documents in the user's HIMARI OPUS project explain why each choice was made: `HIMARI_Multi-Asset_Evidence_Synthesis_Nov2026.md`, `HIMARI_Opus2_Strategy_Flow.html` and `HIMARI_Repo_Review_Nov2026.md`. This guide is self-contained; you don't need them to build.

---

## 2. Ground rules (each one is a testable invariant)

1. **Gates only cut.** For every gate and every instrument `i`: `|w_after_i| <= |w_before_i|`, and `sign(w_after_i)` is either `sign(w_before_i)` or 0. No gate opens, adds to or flips a position. Property-test this with Hypothesis.
2. **No look-ahead.** A decision made after the close of session `t` uses only data released by then. Orders execute at session `t+1` at the earliest. A backtest never fills at the close that produced the signal. Test: changing any data after `t` leaves every decision up to `t` unchanged.
3. **Bad data never trades up.** If a required input for instrument `i` is missing, stale or flagged, its target can only stay the same or fall. Never impute prices, distributions, FX rates or signals.
4. **Frozen parameters.** Every parameter lives in `config/frozen_v1.yaml`, and its hash is recorded in every run. Never edit a parameter after seeing a result.
5. **Stage 1 is long-or-cash with no leverage.** At all times `w_i >= 0` and `sum(w) <= 1`. IBKR paper accounts are margin accounts by default, so enforce this in our own code. *(v1.7)* "Cash" means broker cash plus the cash sleeve (§7.4); the sleeve is never part of `w`.
6. **Unused risk stays in cash.** When an instrument is off, unavailable or capped, its budget is never redistributed to other instruments.
7. **Everything is recorded.** Every backtest, register test and paper decision is appended to a ledger together with the code hash, config hash and data version. Nothing is deleted, failed runs included.
8. **Costs.** Every research result is run at 1× and 2× costs. Pass or fail is decided at 2×.
9. **No complexity in the core.** No neural networks, LLM calls, regime models, optimizers or adaptive parameters. New ideas enter only through `research/register.yaml`, which holds at most 10 tests, run one at a time.
10. **Safety states persist.** Drawdown suspension, faults and the kill switch are stored on disk with no expiry and survive restarts. Only `himari resume --reason "..."` clears a suspension.
11. **Live trading is off.** `LIVE_TRADING_ENABLED=false` is the default. The live broker class refuses to start unless that variable is true and a signed promotion record exists (§12.7). Never set it in code or tests.
12. **Secrets stay in `.env`,** which is gitignored. Never print, log or commit keys, passwords or account numbers.
13. **Time.** Store all timestamps in UTC. Use exchange calendars for sessions. Display times in America/Toronto.
14. **Deterministic.** Seeds are fixed. The same inputs give byte-identical outputs; use golden files to check.

---

## 3. Tech stack

- Python 3.11+. Must run on Windows 11 (the user's machine) and Linux (CI).
- **Core libraries:** `numpy`, `pandas`, `scipy`, `pydantic` v2 (config validation), `pyarrow` (Parquet), `sqlite3` (state and ledgers), `typer` (CLI), `httpx` (HTTP), and `exchange_calendars` (XTSE, XNYS, XNAS) or `pandas_market_calendars`.
- **Broker:** `ib_async`, the maintained successor of `ib_insync`, or IBKR's official TWS API (`ibapi`). IBKR says `ib_insync` is no longer updated.
- **Research extras** (optional install `[research]`, never imported by the core): `statsmodels`, `arch` (only for H4), `lightgbm` or `scikit-learn` (only for H5).
- **Dashboard:** `streamlit` and `plotly`.
- **Quality:** `pytest`, `hypothesis`, `pytest-cov`, `ruff`, `mypy` (strict on `src/himari/core`, `risk`, `strategy` and `execution`).
- Keep dependencies minimal and pin them in a lock file.

---

## 4. Repository layout

```
himari-core/
├── CLAUDE.md
├── README.md
├── Makefile                      # setup, lint, test, backtest, shadow, dashboard
├── pyproject.toml
├── .env.example                  # names of variables only, no values
├── config/
│   ├── frozen_v1.yaml            # Appendix B
│   ├── costs_v1.yaml             # Appendix C
│   ├── instruments/              # instrument master (conId, inception, duration, …)
│   └── events/                   # events_YYYY.yaml (official release times)
├── research/
│   ├── register.yaml             # Appendix D (max 10 tests)
│   └── reports/                  # generated reports (gitignored except summaries)
├── src/himari/
│   ├── core/                     # types, config loader + hashing, clock, ids
│   ├── data/                     # sources, calendars, storage, corporate actions, FX, rates
│   ├── quality/                  # data-quality gate (§6)
│   ├── accounting/               # CAD total-return index, cash index, ledger, NAV
│   ├── strategy/                 # trend signal (Stage 1); carry (Stage 2, later)
│   ├── risk/                     # EWMA, correlation, covariance, stress, state machine
│   ├── sizing/                   # target weights (§7.3)
│   ├── gates/                    # vol ceiling, event, stress, drawdown, faults (§8)
│   ├── execution/                # band, order builder, brokers (sim, paper, ibkr)
│   ├── sim/                      # event loop, fill model, cost model
│   ├── research/                 # metrics, bootstrap, Holm, DSR, MinTRL, PBO, register, trial ledger
│   ├── monitoring/               # reconciliation, alerts, operational-gate checklist
│   ├── dashboard/                # streamlit app
│   └── cli.py
├── tests/
│   ├── unit/  property/  golden/  failure_modes/  crisis/  integration/
│   └── fixtures/                 # small synthetic data sets, committed
├── data/                         # gitignored (raw/, clean/)
└── state/                        # gitignored (himari.sqlite, trials.sqlite, logs/)
```

---

## 5. Instruments and data

### 5.1 Stage 1 universe

| Symbol | Listing | Currency | Class | Exposure |
|---|---|---|---|---|
| VCN | TSX | CAD | equity | Canadian equities |
| VUN | TSX | CAD | equity | US equities (unhedged) |
| VIU | TSX | CAD | equity | Developed markets ex-North America (unhedged) |
| VEE | TSX | CAD | equity | Emerging markets (unhedged) |
| VGV | TSX | CAD | gov_bond | Canadian government bonds |
| CGL.C | TSX | CAD | gold | Gold bullion, not hedged |
| PDBC | NASDAQ | USD | commodity | Broad commodities (futures-based, no K-1) |
| DLR | TSX | CAD | currency | US dollar against the Canadian dollar |

- Resolve each symbol to an IBKR `conId` with `reqContractDetails` and store it in `config/instruments/`. Confirm the symbols by hand; IBKR's form for `CGL.C` may differ.
- Record each fund's inception date, current fund name and (for VGV) duration and convexity from the issuer's factsheet. Keep the source URL and retrieval date.
- An instrument becomes eligible only after it has 252 valid sessions of history. Before that, its budget stays in cash (rule 6).

### 5.2 Data sources

Every source sits behind a `DataSource` interface. The research data set must use **two independent price sources** for each ETF: a primary and a cross-check.

| Source class | Use | Notes |
|---|---|---|
| `CsvSource` | Manual imports | Always available; used for test fixtures |
| `VendorSource` | Primary end-of-day history: prices, distributions, splits | The user chooses the vendor (it must cover TSX listings and corporate actions). API key goes in `.env`. *(v1.9)* The vendor is **EODHD** (EOD Historical plan): `data/eodhd.py`, key in `VENDOR_API_KEY`; `himari data coverage` checks each fund from its listing (`config/fund_inceptions.yaml`). The independent cross-check is the user's pick from `docs/m6_data_sources.md` |
| `IbkrSource` | Recent history and live quotes through TWS or IB Gateway | Needs market-data subscriptions |
| `YFinanceSource` | Optional cross-check for research only | Unofficial. Never the only source |
| `BocValetSource` | FX `FXUSDCAD` (CAD per USD, daily) and cash rate `AVG.INTWO` (CORRA, % per year). *(v1.3)* For history through 2016-12-30, the legacy U.S. dollar noon rate `IEXE0101` (group `LEGACY_NOON_RATES`, also CAD per USD, last updated 2017-04-28) | `https://www.bankofcanada.ca/valet/observations/{series}/json`. Official and free |
| Event calendar | Official release times (§8.4) | Maintained by hand as YAML, with source URLs |

Respect each provider's terms. Never scrape sites that forbid it. If the network or an API key is missing, stop and ask the user.

### 5.3 Storage and versioning

- `data/raw/{source}/…`: every pull, stored unchanged.
- `data/clean/bars.parquet`: symbol, session_date, open, high, low, close, volume, source, version, observed_at_utc, released_at_utc, quality_flags, and *(v1.1)* `adj_close`: the vendor's split- and distribution-adjusted close, used only by Q5. `close` stays unadjusted.
- `data/clean/actions.parquet`: symbol, ex_date, pay_date, type (`cash` or `split`, nothing else), amount or ratio, currency, source, version. *(v1.1)* A cash amount is per post-split share.
- `data/clean/fx.parquet` and `data/clean/rates.parquet`.
- **Corrections never overwrite.** A correction is a new version row. Any replay can rebuild "what was known at time t" by filtering `released_at_utc <= t`. *(v1.1)* `released_at_utc` follows the release times in §5.4; a correction (version 2 or later) is released at its `observed_at_utc`.
- `state/himari.sqlite` holds decisions, orders, fills, the ledger and risk states. `state/trials.sqlite` is the research trial ledger.

### 5.4 Calendars and decision timing

- The master decision calendar is TSX (`XTSE`). The **weekly decision** is taken after the close of the last TSX session of each ISO week: normally Friday, or Thursday if Friday is a holiday.
- Each instrument uses its own exchange calendar (`XTSE` for the TSX funds, `XNAS` for PDBC). At decision time, use each instrument's last completed session.
- **Execution** happens in the next session of each instrument's own exchange.
- Daily jobs run after the time set by `schedule.daily_job_after_et` (default 18:30 ET). By then the Bank of Canada FX rate and vendor end-of-day data are available.
- *(v1.1)* **Release times.** Every stored row gets `released_at_utc` from these rules, so as-of filtering enforces them:

  | Data | Known from |
  |---|---|
  | End-of-day bars for session t | `schedule.daily_job_after_et` (18:30 ET) on t |
  | Corporate action with ex-date t | 18:30 ET on t |
  | `FXUSDCAD` for day t | 16:30 ET on t |
  | *(v1.3)* Legacy noon rate `IEXE0101` for day t (history only) | 18:30 ET on t |
  | CORRA (`AVG.INTWO`) for day t | 11:00 ET on the next Bank of Canada business day (published about 09:00 ET and may be republished until 11:00 ET) |

- *(v1.3, replaces the v1.1 definition)* **Bank of Canada business days** come from the Bank's publication record: between the first and last published dates, a day is a business day exactly when the Bank published `FXUSDCAD`, `IEXE0101` or CORRA for it (the Valet observation dates). After the last published date, the forward holiday file `config/calendars/boc_holidays.yaml` decides: federal holidays plus Ontario's Family Day and Civic Holiday are closed, Quebec-only days and Easter Monday are open, and a weekend holiday moves to the next weekday. Once a date has passed, the record wins. Beyond both, a day counts as a business day for staleness and a CORRA value is treated as released when observed, so neither can err early. Q8 counts these days. `himari data boc-calendar` lists, for manual review, dates with FX but no CORRA, CORRA but no FX, and any disagreement between the file and the record. This is not the XTSE calendar: the TSX trades on Remembrance Day; the Bank does not publish.

### 5.5 CAD total-return index (one per instrument)

Inputs: unadjusted close `P_t`, cash distribution per share `D_t` on its ex-date (0 otherwise), split ratio `S_t` on its ex-date (1 otherwise; 2.0 means two-for-one), and `X_t` = CAD per USD (`FXUSDCAD`).

```
local return     r_loc_t = S_t * (P_t + D_t) / P_{t-1} - 1
CAD return       r_cad_t = r_loc_t                                       (CAD-listed funds)
                 r_cad_t = (1 + r_loc_t) * X_t / X_{t-1} - 1             (USD-listed: PDBC)
wealth index     W_t     = W_{t-1} * (1 + r_cad_t),   W_0 = 1
```

- CAD-listed foreign-equity funds already include currency effects. Never convert them twice.
- If the Bank of Canada has no rate for a US session date, use the latest earlier rate (at most 3 calendar days old) and set a flag (*(v1.1)* `FX_FILLED`).
- *(v1.3)* **FX history.** `X_t` is the legacy noon rate `IEXE0101` through 2016-12-30 and `FXUSDCAD` from 2017-01-03. Every stored row keeps its series name, and the January to April 2017 overlap between the two series is written to `state/logs/fx_splice_overlap.csv` on each Bank of Canada pull.
- *(v1.2)* **FX bridging, for the index history only.** A US session whose latest rate is older than 3 days is inside a gap: it gets no CAD return of its own (`FX_BRIDGED`). The first session with a usable rate books the compounded return, `prod(1 + r_loc) * X_new / X_old - 1`, also flagged `FX_BRIDGED`; its `released_at` is the later of that rate's publication and the bar's release *(v1.3)*, so a replay cannot see it early. A bridged multi-day return enters the EWMA as one observation. If the two rates are more than `data_quality.fx_bridge_max_calendar_days` (7) calendar days apart, the gap is a data outage (`FX_OUTAGE`): the instrument is in `DATA_FAULT` and the index has no value after the gap until a person runs `himari data confirm FXUSDCAD <first rate date after the gap>`. Bridging never unlocks a trade: Q7 at decision time is unchanged.
- The signal index uses **gross** distributions. The account ledger applies withholding tax (§9.4).
- Test the split-and-distribution-on-the-same-day edge case explicitly.
- *(v1.6)* **Reinvested distributions.** An action of type `reinvested` is a year-end distribution the fund reinvests and consolidates (common for Canadian ETFs). It pays no cash and moves no price, so it has no total-return effect: `D_t = 0`. Some sources list it as cash, and Q5 cannot catch that when the vendor's adjusted series makes the same error, so Q10 (§6) requires every December split to be confirmed.

### 5.6 Cash index

```
y_t  = CORRA (AVG.INTWO) in percent per year, as published for business day t
B_t  = B_{t-1} * (1 + y_{t-1}/100 * Δdays_t / 365),   B_0 = 1   (Δdays = calendar days between sessions)
```

Daily excess return: `r_ex_t = r_portfolio_t - (B_t / B_{t-1} - 1)`.

---

## 6. Data-quality gate

Port the validators from `layer-0-data-quality-monitor` (Appendix E). Replace its weighted quality score with **hard pass/fail checks**. That repo traded whenever its weighted score was at least 0.70; here, any failed required check blocks additions.

| ID | Check | Rule | On failure |
|---|---|---|---|
| Q1 | Fresh bar | The latest bar is the instrument's last completed session | `STALE` → fault |
| Q2 | OHLC sane | `0 < low <= min(open, close) <= max(open, close) <= high`, and volume ≥ 0 | `INVALID` → fault |
| Q3 | Order | Dates strictly increasing, no duplicates, no future dates | `INVALID` → fault |
| Q4 | Jump | `abs(close / prev_close - 1) > 0.15` with no corporate action that day (*(v1.6)* a cash distribution or a split; a reinvested distribution never explains a price move) | `ANOMALY` → fault until a person runs `himari data confirm` |
| Q5 | Actions reconcile | *(v1.1)* On **every** session, our total-return ratio `S_t·(P_t + D_t)/P_{t−1}` agrees with the vendor-adjusted ratio `adj_t/adj_{t−1}` within 0.10% (`data_quality.action_reconcile_tol`). On days without an action this catches distributions and splits missing from our records. A session without an adjusted close cannot be reconciled | `ACTION_MISMATCH` → fault |
| Q6 | Cross-source | Primary and cross-check closes differ by at most 0.5% on every date both sources have. *(v1.1)* A missing cross-check on the latest (decision) bar is a fault; a missing cross-check on an older date is a warning, counted in the weekly report. A missing cross-check never blocks a cut | `SOURCE_MISMATCH` → fault |
| Q7 | FX present | `FXUSDCAD` for the date, or the latest rate at most 3 calendar days old (*(v1.1)* flagged `FX_FILLED`). *(v1.2)* In the history, an FX gap longer than 7 calendar days is an outage | `FX_STALE` → fault for PDBC and USD cash valuation. *(v1.2)* `FX_OUTAGE` → fault until confirmed with `himari data confirm` |
| Q8 | Cash rate present | CORRA at most 5 business days old (*(v1.1)* Bank of Canada business days, §5.4) | `RATE_STALE` → use the last value and set a flag |
| Q9 | Zero volume | A session with zero volume | Warning only (*(v1.1)* `ZERO_VOLUME`) |
| Q10 | *(v1.6)* December split | For a CAD-listed fund, every December distribution (cash or reinvested) has its cash/reinvested split confirmed against the issuer's announcement: `himari data confirm SYMBOL EX_DATE --check Q10 --reason "..."`. Every December in the history is confirmed before the baseline (M6) | `ACTION_UNCONFIRMED` → fault until confirmed |

*(v1.6)* Each confirmation names the check it answers (`--check Q4`, `Q7` or `Q10`; FX outages are always Q7) and clears only that check.

**Outcome:** each instrument is either `OK` or `FAULT`. A `FAULT` blocks additions to that instrument; cuts are always allowed. Faults appear on the dashboard and in the weekly report. A data fault never blocks a risk reduction.

---

## 7. Strategy (Stage 1)

### 7.1 Trend signal (frozen)

```
m_i,t = ln(W_i,t / W_i,t-252) - ln(B_t / B_t-252)      # 252 sessions of the instrument's own calendar
s_i,t = 1 if m_i,t > 0 else 0                          # exactly 0 means off
```

- Fewer than 252 valid sessions of history means `s = 0` and the instrument is not eligible.
- If the data gate has faulted the instrument, the computed target is capped at the current holding (rule 3).
- **Register test H1** replaces `s` with `(1{m63 > 0} + 1{m126 > 0} + 1{m252 > 0}) / 3`. That is a test only; it is not part of the baseline.

### 7.2 Risk model (frozen)

- Returns are daily CAD simple returns `r_t` from §5.5.
- **Volatility:** zero-mean EWMA variance `v_t = λ v_{t-1} + (1 - λ) r_t^2`, with `λ = 0.5^(1/20) = 0.965936`. Initialize `v` with the mean of `r^2` over the first 252 sessions. Annualize as `σ_i = sqrt(252 * v_t)`.
- **Volatility floors** (`σ_i = max(σ_i, floor)`): equity 5%, gold 5%, gov_bond 2%, commodity 8%, currency 3%.
- **Correlation:** EWMA covariance with a 60-session half-life (`λ = 0.988514`), computed on a common TSX-date return matrix. US-session returns for PDBC are compounded between consecutive TSX dates. Convert to a correlation matrix `ρ`, then shrink it: `ρ_s = 0.5 ρ + 0.5 I`.
- **Covariance:** `Σ = D ρ_s D`, with `D = diag(σ_floored)`.

### 7.3 Target weights (frozen algorithm)

1. **Budgets.** `b_i` = class budget (20% per class) ÷ number of instruments in the class. The four equity funds get 5% each.
2. **Unit weights.** `u_i = b_i / σ_i`.
3. *(v1.4)* **Common scale.** Because `uᵀ Σ u = bᵀ ρ_s b`, compute `k = 0.08 / sqrt(bᵀ ρ_s b)` over the **whole frozen universe, as if every signal were on**. An instrument without 252 sessions of history is included with zero correlation to the others; its weight stays 0 and its budget stays in cash.
4. *(v1.4)* **All-on weights.** `a_i = k * u_i` (0 for an instrument that is not eligible).
5. *(v1.4)* **Caps on `a`** (scale down only): `a_i <= 0.25` per instrument; the equity group (VCN, VUN, VIU, VEE) sum `<= 0.40`, scaled proportionally if exceeded; `a_DLR <= 0.15`.
6. *(v1.4)* **Cash constraint on `a`.** If `sum(a) > 1`, scale every `a_i` by `1 / sum(a)`.
7. *(v1.4)* **Apply signals.** `w_i = a_i * s_i`. Because caps and the cash constraint act before the signals, switching any signal off never raises another instrument's pre-gate weight (rule 6).
8. **Then the gates** in §8 run, scale-only, in their fixed order.

The result is often below the 8% ceiling because caps bind. That is by design: never scale up to "use" the ceiling. A golden example is in Appendix F.

### 7.4 Cash sleeve *(v1.7)*

Idle CAD cash is held in one CAD-listed T-bill or money-market ETF (`cash_sleeve.symbol`). It is cash management only: no signal, no risk budget, not part of `w`, not scaled by any gate, and left out of `σ_p` and the stress gate. In rule 5, "cash" means broker cash plus the sleeve. NAV includes the sleeve; risk weights are computed over the eight funds only.

- **Config** (`frozen_v1.yaml`, Appendix B): `cash_sleeve: {enabled: true, symbol: null, buffer_nav: 0.02, buffer_min_cad: 200, band_nav: 0.03, band_min_cad: 1000}`. The sleeve runs only when it is enabled and has a symbol; while `symbol` is null, idle cash stays at the broker. *(v1.8)* The fund is **CBIL** (Global X 0-3 Month T-Bill ETF, Government of Canada T-bills), chosen by the user from the candidates in `docs/cash_sleeve_candidates.md`: `symbol: CBIL`, `duration_years: 0.16` (weighted average duration on the issuer's page, as at 31 October 2026).
- **Weekly rule**, after the risk order plan is built (§9.1). `C` is the CAD cash expected after that plan: its buys and sells at their reference prices moved by the cost model, commissions, the CAD spent funding USD buys (with the 1% buffer) and any USD surplus converted back. `buffer = max(0.02 × NAV, 200)` and `band = max(0.03 × NAV, 1000)`:

  | Condition | Order | Reason | Shares |
  |---|---|---|---|
  | `C < buffer / 2` | sell sleeve to bring `C` back to `buffer`; always trades (no band, no CAD 100 minimum), at most the holding | `sleeve_funding` | rounded up, net of costs |
  | `C > buffer + band` | buy sleeve with `C - buffer` | `sleeve_sweep` | rounded down, costs included |
  | otherwise | no sleeve order | | |

- **Execution:** same session and same cost model as the risk orders, with no cheaper treatment: the sleeve's half spread is measured like any fund's (§11), and a run refuses to start without one. Backtest: the session's sales are applied before its buys, and the sleeve buy fills after the risk buys. Paper and live: sales first, buys as cash becomes available; if cash can't cover a buy by the cancel time, the buys are cut pro rata and logged. CAD and USD cash never go negative. *(v1.8)* The sleeve keeps the risk orders' cost model, 10 bp per-side floor included; CBIL's half spread in `costs_v1.yaml` is a placeholder (the floor binds) until the paper run measures it.
- **States:** `KILL` and `VENUE_FAULT` stop all sleeve orders. A data fault on the sleeve stops sleeve buys only. HALVED and SUSPENDED don't affect it.
- **Inception:** the sleeve is used only from the fund's own inception; before that, cash stays at the broker. No proxy in the baseline. *(v1.8)* For CBIL that is 12 April 2023. The M6 baseline report adds one sensitivity labelled "synthetic, pre-inception" (§12.1): a sleeve fund returning CORRA minus 0.11% a year from 2015, traded with the same rules and costs.
- **Diagnostic:** each decision logs the sleeve's loss under the +50 bp yield shock (§8.5). It is not a gate. *(v1.8)* The loss, as a share of NAV, is the sleeve's weight × `duration_years` × 0.5%, with `cash_sleeve.duration_years` from the fund's factsheet.
- **Reports:** gross exposure, time in market and strategy turnover exclude the sleeve; sleeve turnover, costs, income and trades get their own lines; every §12.3 benchmark uses the same cash handling. With the sleeve on, it is the baseline of record, and "cash at the broker" (the same run with the sleeve off) is a labelled sensitivity.

---

## 8. Risk gates and the state machine

### 8.1 Order and interface

Gates run in this fixed order:

1. data gate (§6)
2. volatility ceiling (§8.2)
3. event gate (§8.4)
4. stress gate (§8.5)
5. drawdown state (§8.6)
6. faults and kill switch (§8.7)
7. *(v1.7)* final ceiling: the volatility ceiling again (§8.2)

Each gate is a pure function `apply(weights_in, current_holdings, context) -> (weights_out, GateLog)`. The rule-1 invariant is checked after every gate, in tests and at runtime; a violation raises an error and the run stops.

### 8.2 Portfolio volatility ceiling (frozen)

`σ_p = sqrt(wᵀ Σ w)`. If `σ_p > 0.08`, multiply every weight by `0.08 / σ_p`.

*(v1.7)* **Final ceiling re-check.** The ceiling runs again as the last gate (`final_ceiling`): if `σ_p > 0.08` on the fully gated targets, every weight is multiplied by `0.08 / σ_p`. The gates in between only cut, but cutting one side of a negatively correlated pair can raise `σ_p`. A property test checks that after the last gate `σ_p ≤ 0.08` and every stress loss is within its limit, for random holdings, states and capped funds. The stress gate needs no second pass with today's loadings: no fund gains in a loss scenario, so a cut can't raise a scenario loss. A test fails if a future config breaks that (a fund that gains in a scenario where another fund loses).

### 8.3 Daily safety check (frozen)

Each session after the close, compute `σ_p` for the **current holdings**. If it exceeds `1.25 × 0.08 = 0.10`, create reduction orders that bring it back to 0.08, to execute in the next session. Risk is added back only at the next weekly decision.

### 8.4 Event gate (frozen)

- **Event types and official sources:**
  - FOMC (federalreserve.gov) and Bank of Canada rate announcements (bankofcanada.ca)
  - ECB (ecb.europa.eu), BoJ (boj.or.jp) and SNB (snb.ch) policy decisions
  - US CPI and Employment Situation (bls.gov)
  - Canada CPI and Labour Force Survey (statcan.gc.ca)
- **Storage:** `config/events/events_YYYY.yaml`, with `type`, `release_utc`, `source_url`, `retrieved_utc` and `version`. A historical archive covering 2015–2025 is required for backtests; the report must name any event types that are missing. *(v1.5)* Entries are collected politely from the official sites, with times as published; when only the date is published, the institution's standard time is used and the entry is flagged `time_assumed`.
- **Window:** from the decision close to the end of the execution session.
- **Rule:** affected instruments (`events.map` in config) cannot be increased; `target_i = min(target_i, current_i)`. Cuts are allowed. There are no automatic exits before events and no restoring trades after them; the next weekly decision handles that.
- *(v1.7)* **Known in advance only** (rule 2). The gate uses only releases that were on the published calendar before the decision close.
  - **Unscheduled** announcements (intermeeting decisions, emergency statements, notation votes) stay in the archive with `unscheduled: true` for the crisis replays, and the gate ignores them. Checked: the Fed's 3 and 15 March 2020 cuts (the Sunday 15 March cut falls inside the 13 March decision window); the Bank of Canada's 13 and 27 March 2020 cuts; the BoJ's 16 March 2020 meeting (its 18–19 March meeting, brought forward on 15 March after the Fed's cut); the ECB's 18 March 2020 decision; the SNB's 15 January 2015 removal of the franc floor. The archive holds 15 unscheduled entries (FOMC 7, BoC 2, BoJ 2, ECB 1, SNB 3). Their pages mostly publish no time, so they use the institution's standard time, flagged `time_assumed`.
  - **Rescheduled** releases keep the date they actually happened, flagged `rescheduled: true` with the official notice in `notice_url`. One counts for the gate from `announced_utc`, when its new date was published; without a dated notice the gate ignores it. 2025 was checked against the BLS revised schedule (https://www.bls.gov/bls/2025-lapse-revised-release-dates.htm): the October CPI and October jobs reports were cancelled, the September jobs report moved from 3 October to 20 November, and the November jobs report and CPI from 5 and 10 December to 16 and 18 December; the September CPI moved from 15 to 24 October (separate BLS notice, last modified 10 October 2025, its `announced_utc`). The archive has 11 US CPI and 11 US jobs releases for 2025, 4 of them rescheduled.
  - Coverage (missing event types per year) counts scheduled releases only.
- Register test H6 checks a stronger version (a 50% cap). It is not part of the baseline.

### 8.5 Stress gate (frozen)

- **Factor shocks:** equity ±7% (all equity funds together), gold ±8%, commodities ±10%, CAD up or down 5% against all foreign currencies (`CAD_UP`), and Canadian government yields ±50 bp.
- **Bond price change:** `ΔP/P = -duration * Δy + 0.5 * convexity * Δy²`, using VGV's factsheet values (placeholders in config until replaced).
- **Named single-factor scenarios** (each factor, both signs): loss must be ≤ **3% of NAV**.
- **Joint crisis scenario:** equities −7%, gold −8%, commodities −10% and yields +50 bp together, with no FX move. Loss must be ≤ **5% of NAV**.
- **Action:** `scale = min(1, 0.03 / worst_single_loss, 0.05 / joint_loss)`, applied to every risky weight.
- **Diagnostic only, not a gate:** an "everything adverse, including CAD +5%" scenario is computed and logged. It is left out of the gate because the Canadian dollar usually falls in global sell-offs, which cushions losses on unhedged foreign holdings. Including a simultaneous CAD jump would permanently cut the portfolio by about half. If the user wants it as a gate, that is a new config version.

### 8.6 Drawdown states (frozen, persisted)

- Track NAV in CAD after costs, valued at Bank of Canada FX, and its high-water mark `HWM`. Drawdown `DD = NAV / HWM - 1`.
- **NORMAL:** `DD > -10%`.
- **HALVED:** `-15% < DD <= -10%`. New targets are multiplied by 0.5. The state returns to NORMAL at a weekly decision when `DD > -10%`.
- **SUSPENDED:** `DD <= -15%`. No additions at all; existing positions can still be cut by the other rules. Leaving this state requires `himari resume --reason "..."`, which writes a signed record (user, time, reason). It never clears automatically. *(v1.5)* `resume` resets the drawdown reference to the current NAV (the old HWM and drawdown stay in the record), so a suspended, all-cash account can never deadlock. It is refused when NAV is below `account.loss_floor_cad`; that key may be null in backtest and paper and must be set before live.

### 8.7 Faults and kill switch

- `DATA_FAULT` applies per instrument (§6).
- `VENUE_FAULT` covers a broker disconnect, a reconciliation mismatch, rejected orders or an unknown order state. It blocks additions across the whole portfolio until resolved.
- `KILL` comes from `himari kill`, or from a `KILL` file in `state/`. It cancels open orders and allows no new orders. It does **not** liquidate positions. *(v1.5)* No orders at all, cuts included: the gate blocks additions and flags `orders_blocked` for the order stage; from M7 it also cancels open broker orders.
- All of these states are stored in SQLite with no expiry. The old Redis circuit breaker let halts expire after an hour; that bug must not be carried over. *(v1.5)* A signed record is an append-only entry that stores the previous record's SHA-256, the OS user and the UTC time; the chain is verified at startup and a broken chain stops the run.

### 8.8 State summary

| State | Additions | Cuts | Clears how |
|---|---|---|---|
| NORMAL | yes | yes | — |
| PRE_EVENT (per instrument) | no | yes | the window ends |
| DATA_FAULT (per instrument) | no | yes | data fixed and checks pass |
| HALVED | at half the risk | yes | `DD > -10%` at a weekly decision |
| SUSPENDED | no | yes | manual `resume` with a reason |
| VENUE_FAULT | no | yes, if executable | reconciliation passes |
| KILL | no orders at all | no orders | manual `unkill` with a reason |

---

## 9. Execution and accounting

### 9.1 Weekly cycle (after the decision close)

1. Ingest the data.
2. Run the quality gate.
3. Compute the signals.
4. Update the risk model.
5. Compute the targets.
6. Run the gates.
7. Apply the band.
8. *(v1.7)* Plan the cash-sleeve order from the CAD cash expected after the risk orders (§7.4).
9. Save an order plan with a unique `decision_id`, set to execute in the next session.

### 9.2 Rebalance band (frozen)

Trade instrument `i` only if `abs(w_target_i - w_current_i) > max(0.005, 0.10 * abs(w_current_i))`.

~~Exceptions that always trade: full exits (target 0 with a nonzero holding), cuts forced by the gates, and daily safety cuts.~~ *(v1.6, replaces the line above)* Exceptions that always trade: full exits (target 0 with a nonzero holding), daily safety cuts (§8.3), and the cut on entering HALVED. Cuts by the other gates follow the band like any other target. *(v1.7)* "Entering HALVED" is the first weekly decision taken in HALVED after one that wasn't; the cut that always trades is each fund the drawdown gate cut below its holding. No special case for gate cuts: the band compares the final gated target with the holding, and where the change came from doesn't matter (the fixture run's 2025-03-07 VCN trim stays).

*(v1.6)* **Post-trade limit check.** After the band, the post-trade portfolio is the holdings plus this decision's banded trades. If its `σ_p` or any stress loss (single-factor or joint, §8.5) exceeds its limit by more than `rebalance.gate_tolerance_rel` (0.10, i.e. 10% of the limit):

1. shrink this decision's buys pro rata until it fits (*implementation:* the largest fitting scale, searched in steps of 1/1000, since `σ_p` need not fall monotonically as buys shrink);
2. if it still does not fit with no buys, trade every instrument to its gated target, ignoring the band.

Buys therefore always shrink before any holding is cut beyond the band's plan; a property test checks that, and that the post-trade portfolio never exceeds `σ_p` or a stress limit by more than 10%. *Note:* a holding the band keeps is at most about 11% above its gated target, so the check is a backstop that rarely binds; it binds mostly through small positions inside the absolute band.

### 9.3 Orders

- **Shares:** `shares_i = trunc(w_i * NAV_CAD / price_CAD_i)`, rounded toward zero. Skip orders under CAD 100 and log them.
- *(v1.6)* **Reason and gates on every order.** Each order records why it exists, one of `band` (outside the band), `full_exit`, `post_trade_limit` (traded only by the post-trade fallback), `halved` (the cut on entering HALVED) or `safety_cut` (§8.3), plus every gate that cut that fund at the decision with the scale it applied (weight out / weight in). Logs and the trade list show both. *(v1.7)* A buy the post-trade check shrank keeps reason `band` and lists `post_trade_buys` with its scale. Cash-sleeve orders carry `sleeve_funding` or `sleeve_sweep` (§7.4).
- **PDBC funding:**
  - Before buying, convert CAD to USD to cover the buy plus a 1% buffer. Tag the conversion `fx_funding`. *(v1.7)* USD funding covers the buy's estimated commission (without it a small remainder could never fill).
  - Sales leave the proceeds in USD.
  - Convert surplus USD back to CAD at a weekly decision when it exceeds 2% of NAV.
- **Backtest:** use the fill model in §10.4.
- **Paper and live (placeholder):** limit orders inside the window 15:30–15:50 ET.
  - Buy limit = `min(ask * 1.001, prev_close * 1.005)`. Sell limit = `max(bid * 0.999, prev_close * 0.995)`.
  - Cancel anything unfilled at 15:55; the old position stays.
  - Retry in the next session, up to 2 more sessions, then alert.
  - Every order carries an idempotency key built from `decision_id` and the instrument.
  - *(v1.7)* Before M7, find out from IBKR's documentation whether a Canadian cash account can buy with the proceeds of a same-day sale. Rotations and sleeve funding depend on it.

### 9.4 Ledger

- **Contents:** cash in CAD and USD, positions, trades, commissions, conversions, distributions, withholding, NAV in CAD and the high-water mark. *(v1.6)* Also interest and reinvested distributions.
- **Distributions:** entitlement is booked on the ex-date and cash on the pay date. US-listed distributions have withholding applied at the configured rate (placeholder 15%; confirm with a Canadian tax advisor). *(v1.6)* An entitlement still unpaid 30 days after its ex-date raises an alert. A reinvested distribution (§5.5) is recorded and moves no cash.
- **Two FX rates:** keep the valuation rate (Bank of Canada) separate from the execution rate (broker). Currency conversions are tagged so they never count as strategy trades.
- *(v1.6)* **Cash interest** (`costs_v1.yaml` `cash_interest`, placeholders to verify on IBKR's interest-rate page before M6). Interest accrues daily on **settled** cash per currency and is posted monthly (credited at the first session of the next month); the accrued, unposted amount counts in NAV. Per currency: nothing on the first `zero_first` (CAD 13,000; USD 10,000), then `benchmark - spread_below_benchmark` (CORRA - 0.50% for CAD), times `NAV_USD / full_rate_nav_usd` when NAV is below US$100,000. USD has no benchmark proxy and earns 0 in Stage 1. A trade's cash settles after `settlement_days` business days (T+3, then T+2 for trades from 2017-09-05, then T+1 from 2024-05-27 in Canada and 2024-05-28 in the US, per the SEC and CSA notices). *(v1.7)* Interest accrues at the previous session's CORRA over calendar days / 365, as the cash index (§5.6), and posts on the next month's first session. Settlement days count on the exchange calendar (settlement-system holidays are not modelled); distributions, interest and FX conversions settle when credited. The cost multiplier never scales interest. In the backtest the simulator is the broker: it accrues and posts, and the ledger books each posting as reported. *(v1.7)* Before M7, decide how the interest and distributions the broker credits are matched against the modelled amounts, so reconciliation (§9.5) compares like with like. `account.initial_nav_cad` (placeholder 100,000) is the planned account size and the default starting cash, so it sets the tiers. Excess returns are still measured against the full CORRA cash line (§5.6), and every §12.3 benchmark uses the same cash model.

### 9.5 Reconciliation

After every session with activity, compare broker positions and cash with the ledger. Tolerance: 0 shares and ±CAD 1 of cash. Any mismatch sets `VENUE_FAULT` and raises an alert.

---

## 10. Simulator: backtest, paper and live

### 10.1 One engine, three modes

| Mode | Clock | Market data | Broker |
|---|---|---|---|
| `backtest` | `SimClock` (historical sessions) | `HistoricalBars` | `SimBroker` (§10.4) |
| `paper` | `LiveClock` | `LiveQuotes` (IBKR; public crypto feed) | `QuotePaperBroker` and/or `IBKRPaperBroker` |
| `live` | `LiveClock` | `LiveQuotes` | `IBKRLiveBroker` (refuses to start, rule 11) |

The strategy, sizing, gates, ledger and reports are **the same code in every mode**. Only the clock, the data and the broker are swapped.

### 10.2 Interfaces (Python Protocols)

```python
class Clock(Protocol):
    def now_utc(self) -> datetime: ...
    def sessions(self, exchange: str, start: date, end: date) -> list[Session]: ...

class MarketData(Protocol):
    def bars(self, symbol: str, end_session: date, n: int) -> pd.DataFrame: ...     # as known at now_utc()
    def quote(self, symbol: str) -> Quote | None: ...                                # bid, ask, sizes, ts

class Broker(Protocol):
    def positions(self) -> dict[str, Position]: ...
    def cash(self) -> dict[str, Decimal]: ...
    def submit(self, orders: list[Order]) -> list[OrderAck]: ...
    def fills(self, since_utc: datetime) -> list[Fill]: ...
    def cancel(self, order_id: str) -> None: ...
```

`MarketData` must enforce rule 2 itself: it never returns rows released after `now_utc()`.

### 10.3 Event loop

```
for each tick of the clock (one per session close; one per execution window in paper/live):
    ingest → quality gate → update indices and risk
    if daily: run the safety check (§8.3); queue reduction orders if needed
    if weekly decision: build targets → gates → band → order plan for the next session
    if execution window: submit the planned orders → collect fills → update the ledger → reconcile
    write the decision, gate and fill logs; update the dashboard state
```

### 10.4 Backtest fill model

- **Price:** the next session's close of the instrument's own exchange, adjusted by the cost model (§11): pay up when buying, receive less when selling.
- **Participation cap:** an order may not exceed 0.5% of the trailing 20-session median traded value. The excess carries to the next session, for at most 3 sessions, then is logged as unfilled.
- **No session or a halt:** no fill; the order carries forward.
- **Whole shares only.**
- **Sensitivity variant (reported, never used for pass or fail):** fill at the next session's open.

### 10.5 Paper brokers

- **`QuotePaperBroker`** (internal):
  - Fills against live top-of-book quotes at the execution window: buys at the ask if the limit is at or above it, sells at the bid if the limit is at or below it.
  - Adds the cost model's impact and commission.
  - Fills at most the displayed size; the rest waits for the next window.
  - Works for ETFs (IBKR quotes) and crypto (a public exchange feed).
- **`IBKRPaperBroker`:**
  - A real IBKR paper account, reached through TWS or IB Gateway with the paper login, using `ib_async` or `ibapi`. Paper ports are configurable; check the TWS/Gateway API settings.
  - Every IBKR client can open a paper account.
  - For real-time data, share the live account's market-data subscriptions with the paper account. The same subscription can't be used in live and paper at the same time. Unfunded accounts get delayed data only.
  - Paper accounts are margin accounts by default, so rule 5 is enforced in our code.
- **`AlpacaPaperBroker`** (optional):
  - Anyone can open a paper-only account with an email address, and it includes crypto.
  - US listings only, so it can't trade the TSX funds.
  - Paper-only accounts get IEX market data only.
  - Use it only for the crypto comparator or US-listed experiments.

During the shadow run, feed the same decisions to `QuotePaperBroker` and `IBKRPaperBroker` side by side.

### 10.6 Fidelity: how close to the real market

No simulator is identical to live trading. The broker documentation lists what paper trading misses:

- market impact and information leakage
- queue position and slippage from latency
- regulatory fees
- dividends (Alpaca)
- execution on an exchange (IBKR paper fills are simulated from real prices)

Rules:

1. Research results use the **cost model at 2×**, never paper fills.
2. Distributions come from our own corporate-action data, never from the paper broker.
3. Log the **fill basis** for every order: `(paper_fill - model_fill) / model_fill`, in bp. Report its median and 90th percentile weekly.
4. If the 90-day median basis is worse than the cost model assumes, raise the cost model (new config version) before promotion. **Never lower costs because paper fills looked good.**

### 10.7 Crypto comparator (simulation only)

- **Route A, spot:** BTC/CAD and ETH/CAD daily bars closing at 00:00 UTC, plus live top-of-book from a Canadian-registered exchange's public market-data API, filled by `QuotePaperBroker`. Check the venue's API terms.
- **Route B, regulated wrapper:** TSX-listed spot bitcoin and ether ETFs (for example, Purpose BTCC and ETHH; verify tickers before use) through IBKR paper, on the Stage 1 calendar.
- **Route C (optional):** Alpaca paper crypto.
- **Rules:** long-or-cash; 365-bar lookback (24/7 market); EWMA half-life of 20 bars; volatility floor 30%; budgets BTC 50% and ETH 50%; sleeve ceiling 8%. The weekly decision is taken on the bar ending Saturday 00:00 UTC and executed on the next bar.
- **Separation:** it has its own ledger and report. It is never mixed into Stage 1 results, is not part of the register, and has no effect on Stage 1 promotion.
- *(v1.7, guide text only: build nothing for crypto now; a separate Crypto Lab guide will replace M9.)*
  - Annualize with 365 bars on the spot route (A) and 252 on the ETF route (B).
  - The cash leg is CAD. Stablecoins are never cash.
  - Stress scenario: BTC −25% and ETH −30% in a day, with the 3% limit; plus a diagnostic at −40% and −45%.
  - Outage test: fills 1 and 3 bars late.
  - Benchmarks: CAD cash; buy-and-hold BTC/ETH 50/50; the same scaling and gates with every signal on.

---

## 11. Cost model (`config/costs_v1.yaml`, Appendix C)

- **ETF cost per side** = `max(10 bp, half_spread + 1 bp)` plus the broker commission. Use half-spreads measured from 60 normal sessions of quotes; the config holds placeholders until then.
- **Currency conversion:** the broker's actual cost (placeholder 5 bp). Never assume a zero FX fee.
- **Stress sessions** (event windows and crises in replays): spreads × 5. *(v1.8)* Unscheduled events widen execution costs in crisis replays like any archived event: costs only, never decisions, so no look-ahead.
- **Futures (Stage 2):** US$1.50 per contract per side plus 1 tick of slippage (MTN: 2 ticks). Micro FX: US$1.00 per contract plus 1 tick.
- **Crypto:** taker fee (placeholder 40 bp) plus the half-spread, with a minimum of 5 bp.
- Every research run is done at 1× and 2× costs. Pass or fail is decided at **2×**.

---

## 12. Research and validation engine

### 12.1 Data windows (frozen)

- **Development:** up to 2014-12-31, where history exists.
- **Evaluation:** 2015-01-01 to 2025-12-31, reported by calendar year. Fitted models (H2, H4, H5, H8) refit using past data only (expanding or rolling, as their register entries specify).
- **Replication:** 2026 data up to the freeze date. This is not a hidden holdout.
- **Prospective:** everything after the freeze, which is the only clean out-of-sample record.
- Use **actual ETF histories** from each fund's inception. Index proxies before inception may only appear in sensitivity analyses, labelled "synthetic, pre-inception".

### 12.2 Metrics

Every metric is computed on daily CAD excess returns `r_ex` at 1× and 2× costs.

- Annualized return, volatility and Sharpe: `mean(r_ex) / std(r_ex) * sqrt(252)`. *(v1.8)* The daily excess return is the strategy's daily return less the cash index's; the annualized excess return is `252 * mean(r_ex)`.
- Maximum drawdown, longest time underwater, turnover (one-way, as a fraction of NAV per year), and time in market.
- **97.5% expected shortfall:** the mean of daily losses beyond the 97.5% quantile. *(v1.8)* That is the mean of the worst `ceil(0.025 * T)` daily losses.
- **Certainty-equivalent return:** `CER = 252 * mean(r_ex) - (3/2) * 252 * var(r_ex)`, with risk aversion 3.
- Gate statistics: how often each gate binds, and the average scale it applies. *(v1.9)* Each gate's log records its weights at entry and exit; the average scale is the gross weight leaving the gate over the gross weight entering it, at the decisions where it bound.
- *(v1.6)* Average gross exposure (the mean of `sum(|w_i|)` over the measured sessions).
- *(v1.7)* Gross exposure, time in market and strategy turnover exclude the cash sleeve (§7.4); sleeve turnover, costs and income are reported on their own lines.
- *(v1.6)* **Warm-up excluded.** Metrics start at the later of the window start and the first weekly decision with at least one valid signal (*(v1.7)* a valid signal is any computed signal, on or off). Before that the portfolio cannot hold anything, so its all-cash sessions would dilute return, turnover and time in market. Charts shade the warm-up and reports state where measurement starts.

### 12.3 Benchmarks (always reported)

1. CAD cash (the cash index itself).
2. **Buy-and-hold of the same eight funds** with the same budgets, caps, ceiling and stress gate, but with every signal on. This separates the value of the trend signal from the value of volatility scaling.
3. Unscaled timing: equal weights with the trend signal and no volatility scaling.

*(v1.6)* Benchmarks 2 and 3 run through the same ledger, so their idle cash earns interest under the same cash model (§9.4). *(v1.7)* Every benchmark uses the same cash handling as the baseline, the cash sleeve included (§7.4).

*(M5, to confirm)* Benchmark 2 is the strategy with every signal on and nothing else changed: the same gates, band, daily safety check and post-trade check.

*(v1.9)* Benchmark 3 is labelled **"naive equal-weight timing (uncapped)"**: `1/8` for each eligible fund whose signal is on, no caps, and only the data and fault gates (no volatility ceiling, daily safety check or post-trade check, and no event, stress or drawdown gate). With every signal on it holds 50% in the four equity funds against the strategy's 40% cap, and the stress gate would otherwise scale it by 0.8 (CAD +5% on 75% foreign exposure).

*(v1.7)* **Robustness pack** (M5). Reported with the baseline, labelled, and never used to choose anything; each run is logged as `adhoc` in the trial ledger (§12.6).
- **Leave-one-out:** the 2× net excess return with each calendar year removed, and with each instrument's P&L removed. This is how the research gate's "no dependence on a single crisis or a single instrument" (§12.7) is judged: every row must stay positive.
- **Delay:** every fill one session later.
- *(v1.9)* A fund's daily P&L is its market-value change, the cash its fills moved (commissions included), its distributions booked that session, cash in lieu from its splits, and the revaluation of its unpaid USD distributions, all in CAD at the Bank of Canada rate. Leaving an instrument out subtracts its P&L over the previous NAV from each day's excess return. A test checks that every fund's P&L plus interest, USD cash revaluation and conversion costs equals the NAV change. The delay variant makes each order pass over its fund's first session after the decision.

### 12.4 Statistics

- **Paired stationary bootstrap** (Politis–Romano): mean block length 20 sessions, 10,000 resamples, seed 20261111. Baseline and challenger use the same resampled indices. P-values are one-sided, for improvement. *(v1.8)* `p = (1 + #{d* - d >= d}) / (B + 1)` from the centred paired bootstrap (`d` is the challenger-minus-baseline statistic, `d*` its value on a resample): one-sided, for improvement against zero. A register threshold such as "Sharpe difference >= 0.10" applies to the point estimate.
- **Holm correction** across the register family at α = 0.05. Sort the p-values in ascending order and reject `p_(k)` while `p_(k) <= α / (m - k + 1)`; stop at the first failure.
- **Probabilistic Sharpe ratio** (per-period Sharpe `SR`, skewness `γ3`, ordinary kurtosis `γ4`, `T` observations):
  ```
  PSR(SR*) = Φ( (SR - SR*) * sqrt(T - 1) / sqrt(1 - γ3*SR + (γ4 - 1)/4 * SR^2) )
  ```
  *(v1.9)* The per-period `SR` uses the sample standard deviation (ddof 1); `γ3` and `γ4` use population moments.
- **Deflated Sharpe ratio:** `DSR = PSR(SR0*)`, where
  ```
  SR0* = sqrt(V) * ( (1 - γ) * Φ^-1(1 - 1/N) + γ * Φ^-1(1 - 1/(N*e)) ),   γ = 0.5772156649
  ```
  `N` is the number of trials and `V` the variance of their per-period Sharpe ratios. `N` counts every row in the trial ledger **plus the old alpha factory's trials**. That count is unknown, so report DSR for N = 10, 100, 1,000 and 10,000, and state the caveat. *(v1.8)* DSR and PBO count only ledger rows run on real market data; fixture and demo runs (data version `fixtures:...`) stay in the ledger but are excluded from the count. *(v1.9)* `V = max(the variance of the real-data trials' per-period Sharpe ratios, 1 / (T - 1))`; with fewer than 2 real-data trials it is `1 / (T - 1)`, the variance of an unskilled trial's Sharpe estimate. *(v1.9)* **DSR is reported, not judged:** with 11 years of data, DSR ≥ 0.95 would need an annual Sharpe of about 0.97 at N = 10 and 1.66 at N = 10,000, while a realistic Sharpe here is 0.3 to 0.5. The baseline is one pre-registered strategy taken from published evidence, not the winner of a search, and register tests are controlled by Holm. The research report shows DSR at the ledger's real-data trial count and at N = 10, 100, 1,000 and 10,000, with PSR(0) and MinTRL, on its first page, with the caveat that 11 years cannot prove a Sharpe ratio near 0.5.
- **Minimum track record length (in periods):**
  ```
  MinTRL = 1 + (1 - γ3*SR + (γ4 - 1)/4 * SR^2) * (Φ^-1(1 - α) / (SR - SR*))^2
  ```
- **Probability of backtest overfitting** (CSCV with 16 partitions): diagnostic only. *(v1.9)* The earliest `T mod 16` sessions are dropped so the blocks are equal, and out-of-sample ranks are ordinal (ties rank low).
- Overlapping windows are not independent observations. Always use the bootstrap for inference.

### 12.5 Register procedure

- `research/register.yaml` holds at most 10 tests (Appendix D).
- Run them **one at a time**, in order. Each is a single change against the frozen baseline, with every other setting held fixed.
- A test that fails keeps the simpler rule. Never re-tune it and rerun it under the same ID.
- Don't peek at a p-value while a test is running. Read each test's p-value once, at its frozen endpoint. The Holm correction is computed over the whole family once all 10 tests have reached their endpoints. Any decision before then needs p ≤ 0.005.

### 12.6 Trial ledger (`state/trials.sqlite`)

Columns: `trial_id`, `hypothesis_id` (or `baseline` or `adhoc`), `utc`, `git_commit`, `code_hash`, `config_hash`, `data_version`, `cost_multiplier`, `window`, `metrics_json`, `p_value`, `result`, `notes`.

Every run is appended, failures and ad-hoc explorations included. Rows are never updated or deleted. The DSR trial count comes from this table. *(v1.8)* Only real-data rows count (§12.4). *(M5)* Each row's daily excess returns go in an append-only `trial_returns` table (`trial_id`, `day`, `r_ex`), which PBO reads.

### 12.7 Promotion gates

**Research gate** (baseline, and any challenger claimed as better), all at 2× costs:
- positive net excess return and positive CER
- at least 5 years of common tradable history and at least 3 non-overlapping annual out-of-sample blocks
- no dependence on a single crisis or a single instrument (*(v1.7)* judged by the leave-one-out rows of the robustness pack, §12.3: every row positive; *(v1.9)* a negative row fails even when another row cannot be computed)
- Holm pass for challengers
- ~~DSR ≥ 0.95, with the trial-count caveat, and MinTRL reported~~ *(v1.9)* DSR, PSR(0) and MinTRL are reported on the research report's first page and are not gate criteria (§12.4). Going live stays the user's decision.
- *(v1.9)* Common tradable history is measured sessions / 252; an annual block is a calendar year with at least 240 measured sessions.

**Operational gate** (shadow run):
- at least 90 calendar days, 63 sessions and 13 weekly decisions
- 100% reconciliation
- zero unresolved duplicate orders and zero additions made on stale data
- at least 99% of scheduled jobs completed
- the fill basis reported

**Live ramp:**
- Requires a promotion record (`himari promote --stage 1 --reason "..."`), written only by the user.
- Start at 25% of planned risk. Never raise risk automatically.

**Rollback** (cancel additions immediately):
- a reconciliation failure
- an unknown order state or a bad quote convention
- a stale required input
- the SUSPENDED state

A 90-day run checks operations; it cannot prove a slow strategy's edge. At a Sharpe of 0.5, about 11 years of data are needed for 95% confidence. Say so in every report.

---

## 13. Monitoring and dashboard (Streamlit)

**Pages:**
1. **This week:** data-gate status per instrument, signals, target vs current weights, which gates bound and why, the order plan.
2. **Orders and fills:** model vs paper fills, fill basis, non-fills.
3. **Risk:** portfolio volatility vs the 8% ceiling, stress losses vs their limits, drawdown and HWM, state machine status.
4. **Shadow run:** operational-gate checklist with progress (days, sessions, decisions, reconciliation rate, incidents).
5. **Research:** register status, trial count, last report links.

**Alerts:** written to a log, plus optional email (SMTP settings in `.env`). Alert on any fault, any rollback trigger, a missed scheduled job, or a non-fill after the final retry.

---

## 14. Testing requirements

- **Unit tests** for every formula, with the golden values in Appendix F.
- **Property tests** (Hypothesis):
  - The gates' cut-only invariant holds for random weights, holdings and states.
  - Weights are always ≥ 0, sum to at most 1, and respect the caps.
  - No look-ahead: perturbing data after `t` changes nothing at or before `t`.
  - *(v1.7)* After the last gate, `σ_p ≤ 0.08` and every stress loss is within its limit, for random holdings, states and capped funds (§8.2).
- *(v1.7)* **Config guard:** a test fails if any fund gains in a stress scenario where another fund loses (§8.2).
- *(v1.7)* **Lint:** ruff covers `src/`, `tests/` and `scripts/`; the event collectors build a backtest input.
- **Determinism:** the same fixture run twice produces byte-identical decision logs (golden files).
- **Failure-mode tests** ported from Layer 2's catalog (Appendix E). Each must behave as stated:
  - stale data → no additions
  - wide spread → order skipped
  - correlation spike → exposure reduced through the covariance
  - capacity cap → partial fill
  - slippage → the 2× cost run fails before the 1× run
  - missed heartbeat → venue fault
  - clock drift or timestamp regression → flagged
- **Persistence tests:** SUSPENDED and KILL survive a process restart. `resume` without a reason is refused.
- **Crisis replays** on real data (each prints its gate bindings and must finish without errors):
  - Swiss franc floor removal, 15 Jan 2015 (FX gap; affects VIU)
  - Pound flash crash, 7 Oct 2016
  - Yen flash crash, 3 Jan 2019
  - March 2020
  - The 2022 rate shock
  - Yen carry unwind, 5 Aug 2024
- **Synthetic stresses:**
  - volatility doubles overnight
  - a feed freezes for 3 sessions
  - CAD moves 10%
  - a distribution arrives late
  - a duplicate event triggers a duplicate order (must be refused through idempotency)
  - the broker disconnects mid-execution
  - market and local holidays diverge
- **Integration:** an end-to-end backtest on the committed fixture data, plus a `paper` dry run against a mock broker.

---

## 15. CLI and commands

```
make setup | make lint | make test
himari config hash
himari data update --source vendor|ibkr|boc|csv [--from YYYY-MM-DD]
himari data check                     # runs the quality gate, prints faults
himari data confirm SYMBOL DATE --reason "..."
himari backtest --config config/frozen_v1.yaml --costs 1x|2x --from 2015-01-01 --to 2025-12-31
himari register run H1                # runs one registered test, appends to the trial ledger
himari register status
himari shadow run --broker quote_paper|ibkr_paper [--crypto]
himari report weekly|baseline|register
himari report baseline --demo         # M5: the research report on the fixture set
himari dashboard
himari kill --reason "..." | himari unkill --reason "..."
himari resume --reason "..."
himari promote --stage 1 --reason "..."    # user only; writes the promotion record
```

**Scheduling** (Windows Task Scheduler on the user's PC):
- daily data job after 18:30 ET
- weekly decision on the last TSX session of the week after 18:30 ET
- execution job before the 15:30 ET window on execution days

IB Gateway must be running and logged in to the paper account. If the user automates restarts, keep credentials in the OS credential store, never in the repo.

---

## 16. Milestones and definitions of done

| # | Milestone | Done when |
|---|---|---|
| M0 | Scaffolding: repo, `CLAUDE.md`, configs from the appendices, Makefile, CI (lint + tests), config hashing | `make test` passes; `himari config hash` prints a stable hash |
| M1 | Data layer: sources, calendars, versioned storage, total-return and cash indices, the Bank of Canada adapter, quality gate | Golden tests for the indices; every quality check tested; fixture data ingests deterministically |
| M2 | Strategy and risk: signal, EWMA, correlation shrinkage, covariance, sizing | Golden tests (Appendix F); property tests for weights and caps |
| M3 | Gates and state machine: ceiling, daily check, events, stress, drawdown, faults, kill | Cut-only property test; stress goldens; persistence tests |
| M4 | Simulator: clocks, data, `SimBroker`, cost model, ledger, weekly loop, band, reconciliation | No-look-ahead and determinism tests; 1× and 2× runs on fixtures. *(v1.5)* `himari backtest --demo` runs the fixture data, prints a weekly log (signals, gates that bound and why, orders, fills, costs, NAV) and writes a one-page HTML report (NAV vs cash, weights over time, trade list) |
| M5 | Research engine: metrics, CER, ES, bootstrap, Holm, PSR/DSR/MinTRL, PBO, trial ledger, register loader, reports. *(v1.7)* The robustness pack (§12.3) | Golden tests (Appendix F); reports render |
| M6 | **Baseline study** on real data (user's PC): 2015–2025, 1× and 2×, against the three benchmarks. *(v1.6)* Before real data: every December distribution in the history confirmed (Q10); the event archive holds all nine §8.4 types for 2015–2025; `tests/fixtures/commissions_demo.yaml` replaced by the IBKR schedule; the `cash_interest` placeholders verified on IBKR's interest-rate page. *(v1.7)* The user picks the cash-sleeve fund and its half spread is measured; the IBKR pricing plan and the account size are asked again (`account.initial_nav_cad` stays at the 100,000 placeholder until then). *(v1.8)* The fund is CBIL (its spread is measured in the paper run); the report adds the "synthetic, pre-inception" sleeve sensitivity (§7.4); the user picks the end-of-day vendor from `docs/m6_data_sources.md` and then gives the IBKR pricing plan and the account size; every fund's coverage from its inception is checked on the vendor's key. *(v1.9)* Vendor: EODHD; `himari data coverage` lists every gap. VIU (listed 2015-12-08) and VGV (2017-02-07) start after 2015-01-01, so under §7.1 they have no signal until 252 sessions later and their budgets stay in cash | Report written and trial logged. **No parameter changes after seeing it.** If it fails the research gate, stop and tell the user |
| M7 | **Paper trading and shadow run:** live clock and quotes (IBKR), `QuotePaperBroker`, `IBKRPaperBroker`, dashboard, alerts, scheduler, kill switch. *(v1.7)* Before M7: whether a Canadian cash account can buy with same-day sale proceeds (§9.3), and how broker-credited interest and distributions are matched against the modelled amounts (§9.4) | One week of dry runs; then the 90-day shadow run starts, with the operational checklist tracked automatically |
| M8 | Register tests in order (H1, H2, …) on historical data while the shadow run continues | Each test logged; Holm applied once the family's endpoints are reached |
| M9 | Crypto comparator (simulation only). *(v1.7)* To be replaced by the user's Crypto Lab guide; build nothing for crypto until then | Runs alongside without touching the Stage 1 ledgers; separate report |
| M10 | Stage 2 (§17), only after a Stage 1 promotion record exists | Its own tests, then its own shadow run |

---

## 17. Stage 2: CME micro futures (deferred; interfaces only until M10)

- **Account:** IBKR Canada margin account, non-registered. Futures are not qualified investments for a TFSA or RRSP.
- **Universe:**
  - MES (S&P 500), MTN (Micro Ultra 10-Year Treasury, price-quoted), MGC (gold), MCL (WTI oil)
  - Micro FX: M6E (EUR), M6A (AUD), M6B (GBP), MSF (CHF), MJY (JPY), MCD (CAD/USD, CAD 10,000, physical delivery)
  - CME delisted the old Micro USD/JPY, USD/CAD and USD/CHF contracts (M6J, M6C, M6S); never use them.
  - Never mix price-quoted and yield-quoted Treasury futures: their signs are opposite.
- **Contract master:** multiplier, tick, quote convention, settlement type, first notice, last trade, broker deadline and permissions. Refresh before every roll.
- **Roll rule:** roll at the earliest of 5 business days before first notice, 5 business days before last trade, or the broker's deadline. Roll earlier if the next contract's volume has been higher for 2 sessions. Cost both legs.
- **Trend sleeve:** long/short, using the Stage 1 rule (or the H1 blend if H1 passed) on contract-dollar P&L. Never use returns from back-adjusted price levels.
- **Carry sleeve** (shadow first; register test H3):
  - **FX:** `carry = ln(F_near / F_far) * 365 / (T_far - T_near)` for USD-per-foreign quotes. Go long the 2 highest and short the 2 lowest among at least 4 eligible contracts, with equal USD notional on each side and inverse-volatility weights within a side.
  - **Commodities:** the sign of the curve slope.
  - Bond and equity carry stay off.
  - Use two liquid same-time settlements; otherwise the instrument gets zero carry.
- **Mix:** each sleeve gets 5% standalone risk, combined at equal weight. Fixed Share runs in shadow only (register test H10).
- **Limits** (scale-only):
  - portfolio volatility ≤ 8%
  - gross notional ≤ 2× NAV
  - overnight initial margin ≤ 20% of NAV, and ≤ 40% if margins double
  - each macro class ≤ 40% of absolute standalone risk, each market ≤ 25%, and the FX group ≤ 25%
  - whole contracts, rounded toward zero; revalue after rounding; get a broker margin preview before submitting
- **Sizing check:** if a single contract is too large for the account's risk budget, that market is left out. Never round up.

---

## 18. Security and operations

- **Secrets:**
  - `.env` holds IBKR host, port and client ID, vendor API keys and SMTP settings. It is gitignored. `.env.example` lists the variable names only.
  - Use pre-commit hooks for secret scanning.
- **Backups:** copy `state/` and `data/clean/` every day to a second location.
- **Logs:** structured (JSON lines), rotated, and with no secrets.
- **Idempotent orders:** a repeated `decision_id` is refused.
- **Upgrades:** pin dependency versions; rerun the golden tests after any upgrade.

---

## 19. Do not build

Each of these is excluded from the core:

- strategy generators or any search over strategies
- neural networks, reinforcement learning, transformers or LLM calls
- regime classifiers or regime multipliers (the jump model exists only as register test H8)
- Kalman or Jurik-style trend filters
- hard stop-losses or trailing stops
- blanket exits before events
- stacked risk triggers
- carry earned from spot-FX broker swaps
- crypto funding-rate carry
- real-time infrastructure (Redis pub/sub, Kafka, sub-second loops)
- TFSA or RRSP accounts for this system
- automatic risk increases or automatic retirement of the core on short-window performance

---

## 20. What the user must provide

| Needed for | Item |
|---|---|
| M1 | Choice of end-of-day vendor and its API key, and permission for network access on the user's PC |
| M6 | Account size and acceptable loss budget, which set the CAD numbers in reports. *(v1.6)* The account size goes in `account.initial_nav_cad` (it sets the interest tiers); the IBKR pricing plan and its commission schedule; the cash/reinvested split of every December distribution, from the issuers' announcements. *(v1.7)* The cash-sleeve fund (`cash_sleeve.symbol`), from `docs/cash_sleeve_candidates.md` (*(v1.8)* CBIL). *(v1.8)* The end-of-day vendor, from `docs/m6_data_sources.md` |
| M7 | IBKR account with a paper login; market-data subscriptions shared with the paper account; IB Gateway or TWS installed |
| Promotion | Canadian tax advice on account type and withholding; a signed promotion record |
| DSR | The best available estimate of how many strategies the old Explorer tested (it may be unknown) |

---

## Appendix A: `CLAUDE.md` (create at the repo root)

```markdown
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
```

---

## Appendix B: `config/frozen_v1.yaml`

```yaml
version: frozen_v1
created: 2026-11-11
base_currency: CAD
display_timezone: America/Toronto

calendars:
  master: XTSE
  by_exchange: {TSX: XTSE, NASDAQ: XNAS}

schedule:
  daily_job_after_et: "18:30"
  weekly_decision: last_master_session_of_iso_week
  execution:
    session_offset: 1
    window_et: ["15:30", "15:50"]
    cancel_unfilled_et: "15:55"
    max_retry_sessions: 2
    buy_limit: {ask_mult: 1.001, prev_close_mult: 1.005}    # placeholder
    sell_limit: {bid_mult: 0.999, prev_close_mult: 0.995}   # placeholder

stage1:
  long_only: true
  max_gross: 1.0
  min_history_sessions: 252
  universe:
    - {symbol: VCN,   exchange: TSX,    currency: CAD, class: equity}
    - {symbol: VUN,   exchange: TSX,    currency: CAD, class: equity}
    - {symbol: VIU,   exchange: TSX,    currency: CAD, class: equity}
    - {symbol: VEE,   exchange: TSX,    currency: CAD, class: equity}
    - {symbol: VGV,   exchange: TSX,    currency: CAD, class: gov_bond}
    - {symbol: CGL.C, exchange: TSX,    currency: CAD, class: gold}
    - {symbol: PDBC,  exchange: NASDAQ, currency: USD, class: commodity}
    - {symbol: DLR,   exchange: TSX,    currency: CAD, class: currency}
  class_budget: {equity: 0.20, gov_bond: 0.20, gold: 0.20, commodity: 0.20, currency: 0.20}
  within_class_split: equal
  caps:
    per_instrument: 0.25
    groups:
      - {name: all_equity, members: [VCN, VUN, VIU, VEE], max: 0.40}
      - {name: usd_cash_fund, members: [DLR], max: 0.15}

signal:
  type: excess_return_sign
  lookback_sessions: 252
  zero_is_off: true

risk:
  ewma_vol_half_life: 20
  ewma_corr_half_life: 60
  corr_shrink_to_identity: 0.50
  init_sessions: 252
  annualization: 252
  vol_floor: {equity: 0.05, gold: 0.05, gov_bond: 0.02, commodity: 0.08, currency: 0.03}
  portfolio_vol_ceiling: 0.08
  daily_safety_multiple: 1.25

rebalance:
  band_abs: 0.005
  band_rel: 0.10
  min_order_cad: 100
  usd_surplus_convert_above_nav: 0.02
  fx_funding_buffer: 0.01
  gate_tolerance_rel: 0.10   # amendment v1.6: post-trade sigma_p and stress losses may exceed their limits by 10%

events:
  window: decision_close_to_execution_end
  rule: no_additions
  map:
    FOMC:   [VUN, CGL.C, PDBC, DLR]
    US_CPI: [VUN, CGL.C, PDBC, DLR]
    US_NFP: [VUN, CGL.C, PDBC, DLR]
    BOC:    [VCN, VUN, VIU, VEE, VGV, CGL.C, PDBC, DLR]
    CA_CPI: [VCN, VUN, VIU, VEE, VGV, CGL.C, PDBC, DLR]
    CA_LFS: [VCN, VUN, VIU, VEE, VGV, CGL.C, PDBC, DLR]
    ECB:    [VIU]
    BOJ:    [VIU]
    SNB:    [VIU]

stress:
  factors:
    EQUITY:   {shock: 0.07}
    GOLD:     {shock: 0.08}
    COMMOD:   {shock: 0.10}
    CAD_UP:   {shock: 0.05}        # +5% = CAD appreciates against all foreign currencies
    CA_YIELD: {shock_bp: 50}
  loadings:
    VCN:   {EQUITY: 1}
    VUN:   {EQUITY: 1, CAD_UP: -1}
    VIU:   {EQUITY: 1, CAD_UP: -1}
    VEE:   {EQUITY: 1, CAD_UP: -1}
    VGV:   {CA_YIELD: {duration: 7.5, convexity: 70}}   # placeholder: replace with factsheet values
    CGL.C: {GOLD: 1, CAD_UP: -1}
    PDBC:  {COMMOD: 1, CAD_UP: -1}
    DLR:   {CAD_UP: -1}
  single_factor_limit: 0.03
  joint_scenario: {EQUITY: -0.07, GOLD: -0.08, COMMOD: -0.10, CA_YIELD_bp: 50, CAD_UP: 0.0}
  joint_limit: 0.05
  diagnostic_all_adverse_including_fx: true

account:
  loss_floor_cad: null    # amendment v1.5: resume refused below this NAV; required before live
  initial_nav_cad: 100000 # amendment v1.6: placeholder for the planned account size; sets the interest tiers

cash_sleeve:                # amendment v1.7 (section 7.4): idle CAD cash in one CAD-listed T-bill ETF
  enabled: true
  symbol: CBIL              # amendment v1.8: Global X 0-3 Month T-Bill ETF (TSX), from its inception (2023-04-12)
  buffer_nav: 0.02          # buffer = max(0.02 x NAV, 200)
  buffer_min_cad: 200
  band_nav: 0.03            # band = max(0.03 x NAV, 1000)
  band_min_cad: 1000
  duration_years: 0.16      # amendment v1.8: CBIL weighted average duration, issuer page, as at 2026-10-31

drawdown:
  halve_at: -0.10
  suspend_at: -0.15
  halve_multiplier: 0.5
  resume: manual_with_reason

data_quality:
  max_jump_without_action: 0.15
  action_reconcile_tol: 0.001
  cross_source_tol: 0.005
  fx_max_age_calendar_days: 3
  rate_max_age_business_days: 5
  fx_bridge_max_calendar_days: 7    # amendment v1.2

sources:
  boc_valet_base: https://www.bankofcanada.ca/valet
  fx_series: FXUSDCAD          # CAD per USD
  cash_rate_series: AVG.INTWO  # CORRA, percent per year

crypto_comparator:
  enabled: false
  universe: [BTC, ETH]
  quote_currency: CAD
  bar_close_utc: "00:00"
  lookback_bars: 365
  ewma_vol_half_life: 20
  vol_floor: 0.30
  budget: {BTC: 0.5, ETH: 0.5}
  vol_ceiling: 0.08
  weekly_decision_bar_close_utc: "Saturday 00:00"

research:
  seed: 20261111
  bootstrap: {type: stationary, mean_block: 20, resamples: 10000}
  risk_aversion: 3
  es_level: 0.975
  holm_alpha: 0.05
  early_decision_p: 0.005
  dsr_min: 0.95
  dsr_sensitivity_n: [10, 100, 1000, 10000]
  legacy_trials_estimate: null
  windows:
    development_end: 2014-12-31
    evaluation: [2015-01-01, 2025-12-31]
    replication: [2026-01-01, 2026-11-11]

promotion:
  research:
    min_common_history_years: 5
    min_oos_annual_blocks: 3
    require_positive_excess_return_at_2x: true
    require_positive_cer_at_2x: true
  operational:
    min_calendar_days: 90
    min_sessions: 63
    min_weekly_decisions: 13
    reconciliation_pass_rate: 1.0
    max_stale_input_additions: 0
    min_job_completion: 0.99
  live_ramp_fraction: 0.25
```

---

## Appendix C: `config/costs_v1.yaml`

```yaml
version: costs_v1
multipliers: [1.0, 2.0]
pass_fail_multiplier: 2.0

etf:
  per_side_floor_bps: 10
  impact_bps: 1
  half_spread_bps:            # placeholder: replace with medians measured over 60 normal sessions
    {VCN: 3, VUN: 3, VIU: 5, VEE: 5, VGV: 3, CGL.C: 5, PDBC: 5, DLR: 3,
     CBIL: 1}                 # v1.8 cash sleeve: placeholder, measured in the paper run; the 10 bp floor binds
  commission: broker_schedule # load the current IBKR Canada schedule; never assume zero
  participation_cap: 0.005
  carry_forward_sessions: 3

fx_conversion:
  cost_bps: 5                 # placeholder: replace with the broker's actual conversion cost

withholding:
  us_listed_distributions: 0.15   # placeholder: confirm with a Canadian tax advisor

cash_interest:                # amendment v1.6. Placeholders: verify on IBKR's interest-rate page before M6
  model: ibkr_pro_tiered
  CAD: {zero_first: 13000, spread_below_benchmark: 0.005, benchmark_proxy: CORRA}
  USD: {zero_first: 10000, spread_below_benchmark: 0.005, benchmark_proxy: none}  # USD earns 0 in Stage 1
  full_rate_nav_usd: 100000   # below this, rate x NAV_USD / 100,000
  accrual: daily              # on settled cash, calendar days / 365 (as the cash index, section 5.6)
  posting: monthly            # accrued interest is credited at the first session of the next month

settlement_days:              # amendment v1.6: business days after the trade date, for settled cash
  # T+3 to T+2 for trades from 2017-09-05 (SEC Release 34-80295; CSA, NI 24-101 amendments);
  # T+2 to T+1 for trades from 2024-05-27 in Canada (CSA) and 2024-05-28 in the US (SEC).
  # Counted on the fund's exchange calendar; settlement-system holidays are not modelled.
  TSX:    [{from: 2000-01-01, days: 3}, {from: 2017-09-05, days: 2}, {from: 2024-05-27, days: 1}]
  NASDAQ: [{from: 2000-01-01, days: 3}, {from: 2017-09-05, days: 2}, {from: 2024-05-28, days: 1}]

stress_spread_multiplier: 5

futures:                      # Stage 2
  per_contract_usd: 1.50
  slippage_ticks: 1
  overrides: {MTN: {slippage_ticks: 2}}
  micro_fx_per_contract_usd: 1.00

crypto:
  taker_fee_bps: 40           # placeholder: use the venue's actual schedule
  min_half_spread_bps: 5
```

---

## Appendix D: `research/register.yaml`

Order matters: run them top to bottom. Every test is a single change against the frozen baseline at 2× costs. Thresholds are acceptance rules chosen in advance, not historical findings.

```yaml
family_alpha: 0.05
correction: holm
tests:
  - id: H1
    title: 3/6/12-month trend blend vs 12-month rule
    stage: 1
    change: "s = (1{m63>0} + 1{m126>0} + 1{m252>0}) / 3"
    primary: "net Sharpe difference >= 0.10, one-sided paired bootstrap p (enters Holm)"
    guards: ["CER difference >= 0", "97.5% ES not worse by more than 10%"]
    window: evaluation
  - id: H2
    title: daily-data log-HAR vs EWMA volatility forecast
    stage: 1
    change: "log-HAR on 1/5/22-session squared-return components; direct 5-session forecast; 1,260-session window (min 756); monthly refit; ridge 1e-4 on standardized inputs; training-only smearing"
    primary: "pooled out-of-sample QLIKE at least 5% lower (Diebold-Mariano with block bootstrap)"
    guards: ["risk-matched CER not lower at 2x costs"]
    window: evaluation
  - id: H3
    title: trend-filtered carry vs plain carry
    stage: 2
    change: "hold carry only when the 63/252-session trend signs agree; keep dollar matching"
    primary: "97.5% daily ES at least 15% lower"
    guards: ["CER difference lower bound > 0", "net Sharpe not worse by more than 0.05"]
    window: futures 2015-2025
  - id: H4
    title: Student-t GARCH(1,1) vs log-HAR
    stage: 1
    depends_on: H2
    primary: "QLIKE at least 5% lower than both EWMA and log-HAR; compound p = larger of the two"
    guards: ["no worse ES", "fit fallback on fewer than 1% of forecasts"]
    window: evaluation
  - id: H5
    title: one shallow boosted-tree volatility model (50/50 with log-HAR)
    stage: 1
    depends_on: H4
    change: "depth 2, 100 trees, learning rate 0.03, min leaf 50, fixed seed; H2 features plus 63-session log mean square return"
    primary: "QLIKE at least 5% lower than each simpler model"
    guards: ["annual CER +0.10 pp or more", "turnover up by no more than 10%"]
    window: evaluation
  - id: H6
    title: 50% exposure cap before scheduled releases
    stage: 1
    change: "affected exposure capped at 50% from the session before the event to the session after"
    primary: "event-window 97.5% ES at least 15% lower"
    guards: ["annual CER difference lower bound > 0", "Sharpe not worse by more than 0.10"]
    window: evaluation (2010-2014 archive used only for development)
  - id: H7
    title: one joint risk filter on carry (VIX plus FX volatility)
    stage: 2
    change: "trigger = VIX close above its trailing 252-session 90th percentile (prior data only) AND the equal-weight FX-basket 20-session realized vol above 1.25x its 60-session realized vol; while triggered, carry risk is cut to 75%; release only after 5 consecutive non-trigger sessions, at a weekly decision"
    primary: "97.5% ES at least 15% lower"
    guards: ["CER difference lower bound > 0", "time under the cap at most 25%"]
    window: futures 2015-2025
  - id: H8
    title: two-state statistical jump-model cap on trend
    stage: 1
    change: "features: 20-session EWMA mean return, 20/60-session log volatility; penalty grid {1,10,100} chosen by past-only validation; refit yearly on 5 years; filtering only (no smoothing); cap trend exposure at 50% in the adverse state"
    primary: "CER difference >= 0.50 pp per year"
    guards: ["ES at least 10% better"]
    window: evaluation
  - id: H9
    title: monthly instead of weekly rebalancing
    stage: 1
    change: "orders on the first session after month-end; same bands; daily safety cuts unchanged"
    primary: "turnover at least 20% lower"
    guards: ["CER difference lower bound > 0 at 2x", "ES not worse by more than 10%"]
    window: evaluation
  - id: H10
    title: Fixed Share vs equal weight across sleeves
    stage: 2
    change: "weekly updates, eta = 1, alpha = 0.01, bounded loss, max 75% per sleeve, uniform start"
    primary: "net Sharpe difference >= 0.10"
    guards: ["annual CER +0.25 pp or more", "ES not worse by more than 10%"]
    window: futures 2015-2025 plus shadow
```

---

## Appendix E: Reusing code from the old repos

| Source repo and files | Target | Changes |
|---|---|---|
| `layer-0-data-quality-monitor`: `src/validators/schema_validator.py`, `temporal_validator.py`, `anomaly_detector.py`, `src/sources/source_manager.py`, `tests/test_layer0.py` | `src/himari/quality/`, `src/himari/data/sources/` | Daily sessions instead of milliseconds; hard pass/fail instead of the weighted score; drop `redis_publisher.py`, the latency budget, Graph-R1/Neo4j and the Binance, Glassnode and Santiment defaults |
| `himari-opus-layer-2-tactical`: `failure_mode_catalog.md`, `tests/test_failure_mode_catalog.py` | `docs/failure_modes.md`, `tests/failure_modes/` | Rewrite the tests against the new interfaces; keep FM-1 to FM-8 |
| `explorer-agent_v3`: `forward_testing/shadow_monitor.py`, `forward_testing/analytics/performance.py` | `src/himari/monitoring/`, `src/himari/research/metrics.py` | Remove the 7–28-day transfer gate and Sharpe transfer ratio; use the operational gate in §12.7 |
| `explorer-agent_v3`: `emt/production.py` (hashing and audit trail) | `src/himari/core/audit.py` | Replace pickle with JSON or Parquet plus SHA-256 |
| `explorer-agent_v3`: `feedback/failure_archive.py` | `src/himari/research/trial_ledger.py` | Persist to SQLite; no 10,000-row cap; append-only |
| `explorer-agent_v3`: `dashboard/shadow_dashboard.py` | `src/himari/dashboard/` | New pages from §13 |
| `himari-opus-redis-intergration-shared-module`: `circuit_breaker.py` (idea only) | `src/himari/risk/state_machine.py` | Persist states with no expiry (the old one expired after 1 hour); no close-all emergency; no regime multipliers |
| `himari_retail`: `leverage-enforcement.ts`, `liquidation-monitor.ts` (ideas only) | Stage 2 `risk/margin.py` | 2× gross and 20%/40% margin limits instead of 5× |
| Everything else (generators, Layer 2 models and preprocessing, alchemy on-chain metrics, trading panel) | none | Do not port |

Repos: `github.com/CharithL/<name>`. Several are private; ask the user for access if needed.

---

## Appendix F: Golden test values

- **EWMA decay factors:** `λ(half-life 20) = 0.965936`; `λ(half-life 60) = 0.988514`.
- **Trend signal:**
  - wealth +6% and cash +2.5% over 252 sessions: `m = ln(1.06) - ln(1.025) = 0.033576`, so `s = 1`
  - wealth +2% and cash +2.5%: `m = -0.004890`, so `s = 0`
- **Sizing toy case** (3 instruments; budgets 0.5/0.3/0.2; σ = 0.15/0.05/0.07; ρ_AB = −0.2, ρ_AC = 0.1, ρ_BC = 0; shrinkage 0.5; ceiling 8%; caps: 0.25 per instrument, A alone as a 0.40 group, C ≤ 0.15; signals A = 1, B = 1, C = 0):
  - `u = [3.333333, 6.000000, 2.857143]`, `k = 0.133333`
  - all-on weights `[0.444444, 0.800000, 0.380952]`, with volatility exactly 0.080000
  - *(v1.4)* after caps `[0.25, 0.25, 0.15]`, sum 0.65 (no cash scaling); after signals `[0.25, 0.25, 0]`
  - final portfolio volatility 0.038324 (below the ceiling, as designed)
- **Holm:** with p-values `[0.01, 0.04, 0.03, 0.20]` and α = 0.05, only the first hypothesis is rejected.
- **Bond price change** (duration 7.5, convexity 70): +50 bp gives −0.036625; −50 bp gives +0.038375.
- **Stress example 1:** weights VCN 0.05, VUN 0.05, VGV 0.25, CGL.C 0.10, DLR 0.15. Worst single-factor loss 0.015000, joint loss 0.024156, scale 1.0.
- **Stress example 2:** weights VCN/VUN/VIU/VEE 0.055 each, VGV 0.229, CGL.C 0.229, PDBC 0.183, DLR 0.138.
  - Worst single-factor loss 0.035750 (CAD +5%), joint loss 0.060407, so scale = 0.827717.
  - After scaling: worst single-factor 0.029591 and joint 0.050000.
- **MinTRL** (normal returns, one-sided 95%, `SR* = 0`): ~~about 10.82 years~~ *(v1.8)* 10.83 years at an annual Sharpe of 0.5 with the full formula on daily data (10.82 is the leading term `(z / SR)^2`), and about 2.71 years at 1.0. Tests allow ±0.02 years.
- **PSR property tests:** `PSR(SR*) = 0.5` when `SR = SR*`. DSR falls as `N` rises, other things equal.

---

## Appendix G: Amendments

| Version | Date | Approved by | Changes |
|---|---|---|---|
| v1.1 | 11 November 2026 | User | §5.3: `bars.parquet` adds `adj_close`; action type is `cash` or `split` only; corrections are released at `observed_at_utc`. §5.4: release times for bars, actions, `FXUSDCAD` (16:30 ET same day) and CORRA (11:00 ET next Bank of Canada business day); Bank of Canada business days from the Bank's published holiday list. §5.5: `FX_FILLED` flag. §6: Q5 runs on every session with the existing 0.10% tolerance; Q6 missing-cross-check rule; flags `FX_FILLED` and `ZERO_VOLUME`. No parameter changed. |
| v1.2 | 11 November 2026 | User | §5.5: FX bridging for the index history (`FX_BRIDGED`), capped at 7 calendar days; longer gaps are outages (`FX_OUTAGE`, `DATA_FAULT` until confirmed). §6 Q7: outage check in the history; decision-time Q7 unchanged. New parameter `data_quality.fx_bridge_max_calendar_days: 7` in `frozen_v1.yaml` (Appendix B); the config hash changes. |
| v1.3 | 11 November 2026 | User | §5.2: legacy noon rate `IEXE0101` for FX history. §5.4: release time for `IEXE0101` (18:30 ET); Bank of Canada business days from the Bank's publication record, with a forward holiday file (federal holidays plus Ontario Family Day and Civic Holiday) for dates not yet published; disagreements listed by `himari data boc-calendar`. §5.5: FX splice at 2016-12-30 / 2017-01-03 with the 2017 overlap logged; a bridged return is released at the later of the rate's publication and the bar's release. No parameter changed. |
| v1.4 | 12 November 2026 | User | §7.3: `k = 0.08 / sqrt(bᵀ ρ_s b)` over the whole frozen universe, an instrument without 252 sessions included with zero correlation (weight 0, budget in cash); caps and the cash constraint act on the all-on weights `a = k u`, and signals are applied last (`w = a s`), so switching a signal off never raises another weight (rule 6). Appendix F sizing toy case: after caps `[0.25, 0.25, 0.15]`, sum 0.65; after signals `[0.25, 0.25, 0]`; final volatility 0.038324. No parameter changed. |
| v1.5 | 12 November 2026 | User | §8.6: `resume` resets the drawdown reference to the current NAV (old HWM kept in the record) and is refused below the new `account.loss_floor_cad` (null allowed in backtest and paper, required before live). §8.4: event archive collected from the official sites, times as published, `time_assumed` when only the date is published, missing types named in reports. §8.7: KILL sends no orders, cuts included, and from M7 cancels open broker orders; signed record = hash chain + OS user + UTC time, verified at startup. §16: M4 adds `himari backtest --demo`. Config hash changes. |
| v1.6 | 13 November 2026 | User | Amends a frozen rule. §9.2: band exceptions are full exits, daily safety cuts and the cut on entering HALVED; other gate cuts follow the band; a post-trade check shrinks this decision's buys pro rata, then trades every fund to its gated target, when `σ_p` or a stress loss exceeds its limit by more than `rebalance.gate_tolerance_rel` (0.10). §9.3: every order records its reason and the gates that cut it, with their scales. §9.4: interest on settled cash per `costs_v1.yaml` `cash_interest` (placeholders) and `settlement_days`, posted monthly; `account.initial_nav_cad` (placeholder 100,000) sets the tiers; an alert for a distribution unpaid 30 days after its ex-date. §12.2: the warm-up before the first valid signal is excluded from metrics and shaded on charts; average gross exposure reported. §12.3: benchmarks use the same cash model. §5.5 and §6: action type `reinvested` (`D_t = 0`), check Q10 with `ACTION_UNCONFIRMED`, confirmations name their check. §16 M6 and §20: prerequisites before real data. New keys `rebalance.gate_tolerance_rel` and `account.initial_nav_cad` (frozen config hash `b7563f9b…`), `cash_interest` and `settlement_days` (costs config). Implementation choices marked "to confirm" in §9.2, §9.3, §9.4 and §12.2 are not part of the approval. |
| v1.7 | 14 November 2026 | User | Amends frozen rules. §9.2, §9.3, §9.4, §12.2: the seven v1.6 readings approved as worded (entering HALVED; a shrunk buy keeps `band` and lists `post_trade_buys`; USD funding covers the commission; interest at the previous session's CORRA, calendar days / 365, posted on the next month's first session; settlement days on the exchange calendar, with distributions, interest and FX conversions settled when credited; the cost multiplier never scales interest; a valid signal is any computed signal); no special case for gate cuts. §8.1, §8.2: a final ceiling re-check as the last gate, with a property test and a config guard (no fund gains in a loss scenario). §8.4: the event gate uses only releases on the published calendar before the decision close; unscheduled announcements are archived with `unscheduled: true` and ignored; rescheduled releases keep their actual date, flagged `rescheduled: true`, and count from `announced_utc`. §7.4 (new), §2 rule 5, §9.1: the cash sleeve, with the new `cash_sleeve` block in `frozen_v1.yaml` (frozen config hash `de69ce67…`). §12.3, §12.7: the M5 robustness pack (leave-one-out by year and by instrument at 2×, every row positive; fills one session later; logged `adhoc`). §10.7: crypto comparator text for M9 (nothing built). §9.3, §9.4, §16: questions to settle before M7. §14: `scripts/` linted. Not part of the approval (marked "to confirm"): `cash_sleeve.duration_years` and the sleeve diagnostic formula (§7.4); unscheduled events still stress execution costs (§11). |
| v1.8 | 15 November 2026 | User | §7.4: the cash sleeve is CBIL (`cash_sleeve.symbol: CBIL`, `duration_years: 0.16` from the issuer's page as at 31 October 2026), used from its inception (12 April 2023), with the same cost model as risk orders (10 bp floor; CBIL's half spread is a placeholder until the paper run measures it); the M6 report adds a "synthetic, pre-inception" sleeve sensitivity (CORRA minus 0.11% a year from 2015). Approved wording: the `duration_years` key and the diagnostic (sleeve weight × duration × 0.5%, §7.4); unscheduled events widen execution costs, never decisions (§11); the centred paired bootstrap p-value, one-sided for improvement against zero, with register thresholds on the point estimate (§12.4); the annualized excess return as 252 × the mean daily excess return and 97.5% ES as the mean of the worst `ceil(0.025 × T)` daily losses (§12.2). §12.4, §12.6: DSR and PBO count only real-data ledger rows. Appendix F: MinTRL 10.83 years at a Sharpe of 0.5 (full formula), ±0.02 tolerance. §16, §20: the M6 data-vendor choice (`docs/m6_data_sources.md`). Frozen config hash `0f7fd7a1…`. Not part of the approval (marked "to confirm"): the M5 readings in §12.2 (gate statistics), §12.3 (benchmark definitions, P&L attribution, delay), §12.4 (DSR's `V`) and §12.7 (how the report judges the research gate). |
| v1.9 | 16 November 2026 | User | Amends a frozen rule. §12.4, §12.7: DSR is reported, not judged; "DSR ≥ 0.95" leaves the research gate, and the report's first page shows DSR at the ledger's real-data trial count and at N = 10 to 10,000 with PSR(0) and MinTRL, with the caveat that 11 years cannot prove a Sharpe near 0.5. The gate keeps positive net excess return and CER at 2×, 5 years of history, 3 annual blocks and no single year or instrument carrying the result; going live stays the user's decision. Approved wording: the gate statistics' average scale (§12.2); benchmark 3, "naive equal-weight timing (uncapped)" (§12.3); the P&L attribution and delay mechanics (§12.3); history = measured sessions / 252 and an annual block ≥ 240 sessions (§12.7); a negative leave-one-out row fails even when another cannot be computed (§12.7); the part 1 variance, moment and PBO conventions (§12.4); DSR's `V = max(cross-trial variance, 1 / (T - 1))` (§12.4). §5.2, §16: EODHD as the primary vendor, `himari data coverage`, `config/fund_inceptions.yaml`. Frozen config hash unchanged (`0f7fd7a1…`). Not part of the approval (marked "to confirm"): the benchmark 2 reading (§12.3). |
