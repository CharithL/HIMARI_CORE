"""Live network tests run only with --run-network (never by default or in CI)."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-network", action="store_true", default=False, help="run live network tests"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="live network test; pass --run-network to run it")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
