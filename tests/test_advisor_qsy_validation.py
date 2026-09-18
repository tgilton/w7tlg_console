"""T1 — Tier A + Tier B validation in ClaudeAdvisor.stream_advice_with_tools'
qsy handler (advisor/claude_advisor.py). Drives the real method end to end
against T0's fake rig, with anthropic's streaming client replaced by a
stub that hands back a synthetic qsy_to_band tool call — no real API call,
no live hardware."""
from types import SimpleNamespace

import pytest

from advisor.claude_advisor import ClaudeAdvisor


class _FakeToolUseBlock:
    type = "tool_use"
    name = "qsy_to_band"

    def __init__(self, input_):
        self.input = input_


class _FakeFinalMessage:
    def __init__(self, tool_input):
        self.content = [_FakeToolUseBlock(tool_input)]


class _FakeStreamCM:
    """Stands in for `with self._client.messages.stream(**kwargs) as stream`.
    No text deltas — this test only exercises the tool-call branch."""
    def __init__(self, tool_input):
        self._tool_input = tool_input

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())  # no content_block_delta events

    def get_final_message(self):
        return _FakeFinalMessage(self._tool_input)


def _advisor_with_qsy_call(fake_rig, tool_input: dict) -> ClaudeAdvisor:
    advisor = ClaudeAdvisor(rig=fake_rig)
    advisor._client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: _FakeStreamCM(tool_input)))
    return advisor


async def _run(advisor) -> list[tuple[str, object]]:
    events = []
    async for kind, data in advisor.stream_advice_with_tools(
            rig_state={}, prop_state={}, auto_qsy=True):
        events.append((kind, data))
    return events


async def test_qsy_to_legitimate_amateur_band_reaches_the_rig(fake_rig):
    advisor = _advisor_with_qsy_call(
        fake_rig, {"frequency_hz": 14_074_000, "mode": "PKTUSB", "reason": "20m FT8 open"})

    events = await _run(advisor)

    assert ("qsy", {"frequency_hz": 14_074_000, "mode": "PKTUSB", "reason": "20m FT8 open"}) in events
    assert ("set_frequency", (14_074_000,), {}) in fake_rig.calls
    assert ("set_mode", ("PKTUSB", 0), {}) in fake_rig.calls


async def test_qsy_to_wwv_is_rejected_by_band_plan_guard(fake_rig):
    """The exact case Finding 9 in AUDIT.md was written against: the same
    WWV frequency the manual UI path must accept (Tier A only) has to be
    rejected on the advisor's qsy_to_band path (Tier A + Tier B)."""
    advisor = _advisor_with_qsy_call(
        fake_rig, {"frequency_hz": 10_000_000, "mode": "USB", "reason": "hallucinated"})

    events = await _run(advisor)

    kinds = [k for k, _ in events]
    assert "qsy" not in kinds
    assert "error" in kinds
    assert fake_rig.calls == []  # never reached RigctldClient


async def test_qsy_out_of_hardware_range_is_rejected(fake_rig):
    advisor = _advisor_with_qsy_call(
        fake_rig, {"frequency_hz": 900_000_000, "mode": "USB", "reason": "hallucinated"})

    events = await _run(advisor)

    assert "qsy" not in [k for k, _ in events]
    assert fake_rig.calls == []


async def test_qsy_with_invalid_mode_string_is_rejected(fake_rig):
    advisor = _advisor_with_qsy_call(
        fake_rig, {"frequency_hz": 14_074_000, "mode": "DATA-U", "reason": "hallucinated"})

    events = await _run(advisor)

    assert "qsy" not in [k for k, _ in events]
    assert fake_rig.calls == []


@pytest.mark.parametrize("mode", ["USB", "LSB", "CW"])
async def test_qsy_valid_amateur_frequency_various_modes(fake_rig, mode):
    advisor = _advisor_with_qsy_call(
        fake_rig, {"frequency_hz": 14_100_000, "mode": mode, "reason": "test"})

    events = await _run(advisor)

    assert "qsy" in [k for k, _ in events]
    assert ("set_mode", (mode, 0), {}) in fake_rig.calls
