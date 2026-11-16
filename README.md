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

### Setup note: mypy on Windows

On the project PC, Windows application control (Smart App Control or a similar policy) blocks
the compiled library in mypy's standard wheel ("An Application Control policy has blocked this
file"), which stopped `make lint` at its mypy step. `make setup` therefore installs mypy's
pure-Python build at the pinned version (`MYPY_VERSION` in the `Makefile`, the same version as
in `requirements-lock.txt`). To do it by hand in an existing environment:

```bash
uv pip install --reinstall-package mypy --no-binary mypy "mypy==1.20.2"
```

`mypy --version` then ends in `(compiled: no)`. It checks the same way, only more slowly. No
Windows security setting is changed, and none needs to be. CI (Linux) keeps the standard wheel.

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
