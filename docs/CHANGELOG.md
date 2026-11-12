# Changelog

All notable changes to HIMARI Core are documented here.

## [Unreleased]

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
