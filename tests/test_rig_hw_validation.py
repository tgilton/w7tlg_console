"""T1 — Tier A hardware-range/mode validation, pure-computation checks in
rig/rigctld_client.py. No hardware, no fakes needed (these are just
functions), but grouped here since T1's own scope note calls this "fully
unit-testable ... both tiers are pure computation, no hardware needed"."""
from rig.rigctld_client import (
    HW_RX_COVERAGE_BANDS,
    is_valid_hw_frequency,
    is_valid_hw_mode,
)


def test_hf_frequency_is_valid():
    assert is_valid_hw_frequency(14_074_000) is True  # 20m FT8


def test_wwv_frequency_is_valid_hardware_range():
    """WWV at 10 MHz is off any amateur band but well within the rig's
    tunable hardware range (inside the first coverage band) — Tier A must
    accept it (only Tier B, the advisor-only band-plan guard, is allowed
    to reject it)."""
    assert is_valid_hw_frequency(10_000_000) is True


def test_frequency_below_hardware_floor_is_rejected():
    lo, _ = HW_RX_COVERAGE_BANDS[0]
    assert is_valid_hw_frequency(lo - 1) is False


def test_frequency_above_hardware_ceiling_is_rejected():
    _, hi = HW_RX_COVERAGE_BANDS[-1]
    assert is_valid_hw_frequency(hi + 1) is False


def test_frequency_at_exact_bounds_is_valid():
    lo, _ = HW_RX_COVERAGE_BANDS[0]
    _, hi = HW_RX_COVERAGE_BANDS[-1]
    assert is_valid_hw_frequency(lo) is True
    assert is_valid_hw_frequency(hi) is True


def test_frequency_in_gap_between_hf_and_airband_coverage_is_rejected():
    """56-118 MHz sits between the rig's HF/6m block and its 118-164 MHz
    block — the rig cannot receive here at all, so a single min/max span
    would wrongly accept it."""
    assert is_valid_hw_frequency(90_000_000) is False


def test_frequency_in_gap_between_airband_and_uhf_coverage_is_rejected():
    """164-420 MHz sits between the rig's 118-164 MHz block and its
    420-470 MHz block — same dead-zone case as the 56-118 MHz gap."""
    assert is_valid_hw_frequency(300_000_000) is False


def test_frequency_in_each_coverage_band_is_valid():
    assert is_valid_hw_frequency(50_000_000) is True    # 6m, in HF/6m block
    assert is_valid_hw_frequency(144_000_000) is True   # 2m, in 118-164 block
    assert is_valid_hw_frequency(440_000_000) is True   # 70cm, in 420-470 block


def test_negative_or_garbage_frequency_is_rejected():
    assert is_valid_hw_frequency(-14_074_000) is False
    assert is_valid_hw_frequency(0) is False


def test_valid_modes_accepted():
    for mode in ("USB", "LSB", "CW", "CWR", "AM", "FM", "PKTUSB", "PKTLSB"):
        assert is_valid_hw_mode(mode) is True


def test_unknown_mode_string_is_rejected():
    assert is_valid_hw_mode("NOTAMODE") is False


def test_advisor_placeholder_mode_labels_are_rejected():
    """DATA-U/DATA-L appear in the advisor's own tool schema enum but were
    never real rigctld mode strings — Tier A must catch them."""
    assert is_valid_hw_mode("DATA-U") is False
    assert is_valid_hw_mode("DATA-L") is False
