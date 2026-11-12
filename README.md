# HIMARI Core

Rules-based, multi-asset trend following system. Long-or-cash on 8 ETFs,
valued in CAD, with weekly decisions and hard risk gates.

See `docs/BUILD_GUIDE.md` for the full specification.

## Quick start

```bash
make setup        # install in editable mode with dev dependencies
make test         # run the test suite
make lint         # ruff + mypy
himari --help     # CLI commands
himari config hash  # print the frozen config hash
```

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Scaffolding | In progress |
| M1 | Data layer | Pending |
| M2 | Strategy and risk | Pending |
| M3 | Gates and state machine | Pending |
| M4 | Simulator | Pending |
| M5 | Research engine | Pending |
| M6 | Baseline study | Pending |
| M7 | Paper trading and shadow run | Pending |
| M8 | Register tests | Pending |
| M9 | Crypto comparator | Pending |
| M10 | Stage 2 futures | Pending |
