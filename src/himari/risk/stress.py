"""Stress scenarios (§8.5): factor shocks, the bond price change, single-factor and joint losses."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from himari.core.config import BondLoading, StressConfig


def bond_price_change(duration: float, convexity: float, dy: float) -> float:
    """dP/P = -duration * dy + 0.5 * convexity * dy^2, with dy in decimal (50 bp = 0.005)."""
    return -duration * dy + 0.5 * convexity * dy * dy


def fund_return(loadings: Mapping[str, float | BondLoading], moves: Mapping[str, float]) -> float:
    """CAD return of one fund: loading * move per linear factor, the bond formula for yields."""
    r = 0.0
    for factor, loading in loadings.items():
        move = moves.get(factor, 0.0)
        if isinstance(loading, BondLoading):
            r += bond_price_change(loading.duration, loading.convexity, move)
        else:
            r += loading * move
    return r


def portfolio_loss(
    weights: Mapping[str, float], cfg: StressConfig, moves: Mapping[str, float]
) -> float:
    """Loss as a fraction of NAV (positive is a loss). Funds without loadings do not move."""
    return -sum(w * fund_return(cfg.loadings.get(sym, {}), moves) for sym, w in weights.items())


@dataclass(frozen=True)
class Scenario:
    name: str
    moves: dict[str, float]  # factor -> move; yields in decimal


def single_factor_scenarios(cfg: StressConfig) -> list[Scenario]:
    """Each factor, both signs."""
    out: list[Scenario] = []
    for name, factor in cfg.factors.items():
        size = factor.shock if factor.shock is not None else (factor.shock_bp or 0.0) / 10_000
        out.append(Scenario(f"{name}+", {name: size}))
        out.append(Scenario(f"{name}-", {name: -size}))
    return out


def joint_scenario(cfg: StressConfig, with_cad_up: bool = False) -> Scenario:
    """The joint crisis (no FX move); with_cad_up adds CAD +5% for the diagnostic."""
    moves: dict[str, float] = {}
    for key, value in cfg.joint_scenario.items():
        if key.endswith("_bp"):
            moves[key.removesuffix("_bp")] = value / 10_000
        else:
            moves[key] = value
    if with_cad_up:
        moves["CAD_UP"] = cfg.factors["CAD_UP"].shock or 0.0
        return Scenario("all_adverse_including_cad_up", moves)
    return Scenario("joint", moves)


def gains_in_loss_scenarios(cfg: StressConfig, symbols: Sequence[str]) -> dict[str, list[str]]:
    """Scenario -> funds that gain while another fund loses in it.

    Empty means no fund gains in a loss scenario, so cutting any weight can never raise a
    scenario loss and the stress gate needs no second pass after later cuts (v1.7)."""
    out: dict[str, list[str]] = {}
    for sc in [*single_factor_scenarios(cfg), joint_scenario(cfg)]:
        rets = {s: fund_return(cfg.loadings.get(s, {}), sc.moves) for s in symbols}
        if any(r < 0 for r in rets.values()):
            gainers = sorted(s for s, r in rets.items() if r > 0)
            if gainers:
                out[sc.name] = gainers
    return out


@dataclass(frozen=True)
class StressResult:
    losses: dict[str, float]  # single-factor scenario -> loss
    worst_single: float
    worst_single_name: str
    joint: float
    all_adverse_including_cad_up: float  # diagnostic only, never a gate
    scale: float  # min(1, single limit / worst single, joint limit / joint)


def stress_test(weights: Mapping[str, float], cfg: StressConfig) -> StressResult:
    losses = {s.name: portfolio_loss(weights, cfg, s.moves) for s in single_factor_scenarios(cfg)}
    worst_name = max(losses, key=lambda name: losses[name])
    worst = max(0.0, losses[worst_name])
    joint = max(0.0, portfolio_loss(weights, cfg, joint_scenario(cfg).moves))
    diagnostic = portfolio_loss(weights, cfg, joint_scenario(cfg, with_cad_up=True).moves)
    scale = min(
        1.0,
        cfg.single_factor_limit / worst if worst > 0 else math.inf,
        cfg.joint_limit / joint if joint > 0 else math.inf,
    )
    return StressResult(losses, worst, worst_name, joint, diagnostic, scale)
