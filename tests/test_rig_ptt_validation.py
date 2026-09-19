"""Finding I — strict PTT reply validation (SAFETY).

A phantom PTT gates the SDR audio, freezes the panadapter, drives the TX
meters, pushes a TX-start to the amp bridge, and switches off the poll
loop's frequency sanity check (`f` is only polled while PTT is false).
It was observed live on 2026-09-19 holding a fake TX for up to 37 s.

These are pure-computation checks on `parse_ptt_reply`, same style as
test_rig_hw_validation.py — no hardware, no fakes."""
import pytest

from rig.rigctld_client import parse_ptt_reply


# --- well-formed replies (Hamlib rig.h ptt_t) -------------------------------

def test_zero_is_receive():
    assert parse_ptt_reply("0") is False


def test_one_is_transmit():
    assert parse_ptt_reply("1") is True


@pytest.mark.parametrize("raw", ["2", "3"])
def test_on_mic_and_on_data_are_transmit(raw):
    """Hamlib defines RIG_PTT_ON_MIC=2 and RIG_PTT_ON_DATA=3. Rejecting
    these would be a safety bug in the other direction — a real
    transmission read as receive, leaving the SDR ungated under RF."""
    assert parse_ptt_reply(raw) is True


@pytest.mark.parametrize("raw", ["1\n", " 1 ", "1\r\n"])
def test_surrounding_whitespace_is_tolerated(raw):
    assert parse_ptt_reply(raw) is True


# --- the actual phantom-PTT values, taken from the 2026-09-19 capture ------
#
# Each of these is a real reply to a DIFFERENT command that landed in the
# `t` slot after the reply stream desynced. Every one was previously read
# as "TX ON" by the old float()/bool(int()) path.

@pytest.mark.parametrize("raw,source", [
    ("1.0",       "l SWR / l RFPOWER reply"),
    ("1.2",       "l SWR reply"),
    ("-73",       "l STRENGTH reply, dB below S9"),
    ("-1",        "l STRENGTH reply, just below S9"),
    ("14074000",  "f (frequency) reply, 20m FT8"),
    ("0.0",       "l ALC reply"),
    ("0.38",      "l COMP reply"),
])
def test_other_commands_replies_are_rejected(raw, source):
    assert parse_ptt_reply(raw) is None, f"{source} must not set PTT"


def test_float_formatted_one_is_rejected_not_read_as_transmit():
    """The specific regression: '1.0' is what `l SWR` and `l RFPOWER`
    return, and SWR is the last GET of a TX-state poll cycle — so its
    straggler lands on the next cycle's `t` read. float('1.0') was 1.0
    and bool(int(1.0)) was True, which is the phantom."""
    assert parse_ptt_reply("1.0") is not True
    assert parse_ptt_reply("1.0") is None


# --- other malformed input --------------------------------------------------

@pytest.mark.parametrize("raw", ["PKTUSB", "USB", "RPRT 0", "RPRT -9", ""])
def test_non_numeric_replies_are_rejected(raw):
    assert parse_ptt_reply(raw) is None


@pytest.mark.parametrize("raw", ["4", "9", "10", "-0", "+1", "01"])
def test_out_of_enum_range_values_are_rejected(raw):
    """Only 0-3 are defined. '10' matters in particular: it is two valid
    characters, so a sloppy check could accept it."""
    assert parse_ptt_reply(raw) is None


def test_no_reply_is_rejected():
    assert parse_ptt_reply(None) is None


def test_miss_is_distinguishable_from_receive():
    """None and False must not be conflated: the caller holds the previous
    PTT state on None, but acts on False. `is` comparisons, not truthiness,
    because both are falsy."""
    assert parse_ptt_reply("1.0") is None
    assert parse_ptt_reply("0") is False
    assert parse_ptt_reply("0") is not None
