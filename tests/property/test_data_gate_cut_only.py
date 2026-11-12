"""Rule 1 for the data gate: it only cuts, and a fault never blocks a cut."""

from hypothesis import given
from hypothesis import strategies as st

from himari.gates.invariants import CutOnlyError, check_cut_only
from himari.quality.gate import InstrumentQuality, QualityReport, apply_data_gate

SYMBOLS = ["VCN", "VUN", "VGV", "PDBC"]
weight = st.floats(min_value=0.0, max_value=0.25, allow_nan=False)


def report(faulted: set[str]) -> QualityReport:
    return QualityReport(
        instruments={
            s: InstrumentQuality(symbol=s, status="FAULT" if s in faulted else "OK", results=[])
            for s in SYMBOLS
        },
        portfolio=[],
    )


@given(
    targets=st.fixed_dictionaries({s: weight for s in SYMBOLS}),
    holdings=st.fixed_dictionaries({s: weight for s in SYMBOLS}),
    faulted=st.sets(st.sampled_from(SYMBOLS)),
)
def test_data_gate_only_cuts(
    targets: dict[str, float], holdings: dict[str, float], faulted: set[str]
) -> None:
    out, log = apply_data_gate(targets, holdings, report(faulted))
    for s in SYMBOLS:
        assert 0.0 <= out[s] <= targets[s]
        if s not in faulted:
            assert out[s] == targets[s]
        elif targets[s] <= holdings[s]:
            assert out[s] == targets[s]  # a cut (or no change) always passes
        else:
            assert out[s] == holdings[s]  # no additions
    assert log.weights_out == out


def test_unchecked_instrument_cannot_add() -> None:
    out, _ = apply_data_gate({"NEW": 0.1}, {}, report(set()))
    assert out["NEW"] == 0.0


def test_invariant_rejects_growth_and_flips() -> None:
    for before, after in (({"A": 0.1}, {"A": 0.2}), ({"A": 0.1}, {"A": -0.05}), ({}, {"A": 0.1})):
        try:
            check_cut_only("test", before, after)
        except CutOnlyError:
            continue
        raise AssertionError(f"{before} -> {after} should violate rule 1")
