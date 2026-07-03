"""
Claude AI band advisor — ported from ft991a-panel's advisor.py.

Packages current rig state + propagation data into context for Claude,
which acts as an HF propagation/DX strategy advisor. Called on demand
(when the operator asks, or a monitor.py alert fires) rather than on a
fixed timer, to keep API costs down and responses relevant.

Adaptations from ft991a-panel:
  - format_context() reads this console's actual RigState field names
    (freq_hz, strength_db, rf_power_pct, preamp_name, nb_on, nr_on) —
    ft991a-panel used raw Hamlib-adjacent names (freq, strength, rfpower,
    preamp as a raw 0/10/20 code) that don't exist here.
  - SYSTEM_PROMPT no longer hardcodes "Indio, California (DM13)" — it's
    built from config/station_profile.py so advice reflects whichever
    QTH (La Quinta or Boise) is actually currently selected.
  - Auto-QSY calls rig.set_frequency()/set_mode() on this console's
    RigctldClient (verified those methods exist at rig/rigctld_client.py)
    instead of ft991a-panel's own rig.py wrapper.
"""

from __future__ import annotations

from typing import Optional

import anthropic

from config.station_profile import station_profile
from rig.rigctld_client import RigctldClient

MODEL = "claude-sonnet-4-5"

QSY_TOOL = {
    "name": "qsy_to_band",
    "description": "Command the radio to change to a specific frequency and mode. Use this "
                    "when recommending a band change and the operator has auto-QSY enabled.",
    "input_schema": {
        "type": "object",
        "properties": {
            "frequency_hz": {
                "type": "integer",
                "description": "Frequency in Hz, e.g. 14074000 for 20m FT8",
            },
            "mode": {
                "type": "string",
                "description": "Mode string understood by rigctld",
                "enum": ["USB", "LSB", "CW", "CWR", "AM", "FM", "PKTUSB", "PKTLSB", "DATA-U", "DATA-L"],
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of why this band change is recommended",
            },
        },
        "required": ["frequency_hz", "mode", "reason"],
    },
}


def _system_prompt() -> str:
    profile = station_profile.current
    return f"""You are an expert amateur radio operator and HF propagation specialist
assisting {profile.call} (Terry), an Amateur Extra class operator currently operating from
{profile.city}, {profile.state} (grid square {profile.grid}).

Terry is primarily interested in FT8 DX — working as many distant and rare stations as
possible. He is not contesting.

You have access to real-time data including:
- Current rig state (frequency, mode, signal strength, power settings)
- Live FT8 band activity from PSKReporter (spot counts and DXCC entities heard)
- Current solar conditions (SFI and Kp index)

When giving advice:
- Be specific and actionable — recommend exact bands and frequencies
- Explain WHY a band is good or bad given current conditions
- Note interesting DX opportunities visible in the spot data
- Consider the time of day and the operator's location for propagation paths
- Keep responses concise — 3 to 5 short paragraphs maximum
- If conditions are poor, say so honestly and explain what to expect

Solar flux interpretation:
- SFI below 80: poor, higher bands dead, stick to 40m and 80m
- SFI 80-120: fair, 20m reliable, 15m possible
- SFI 120-150: good, 15m and 17m open, 10m possible
- SFI above 150: excellent, all bands including 10m active

Kp interpretation:
- Kp 0-2: quiet, excellent conditions especially on low bands
- Kp 3-4: unsettled, some high-latitude path degradation
- Kp 5+: storm, significant disruption, avoid polar paths
"""


def format_context(rig: dict, prop_state: dict, user_question: Optional[str] = None) -> str:
    freq_hz = rig.get("freq_hz") or 0
    freq_mhz = f"{freq_hz / 1e6:.4f} MHz" if freq_hz else "unknown"

    strength_db = rig.get("strength_db")
    if strength_db is not None:
        smeter_label = rig.get("smeter_label") or ""
        strength_str = f"{smeter_label} ({strength_db:+.0f}dB)" if smeter_label else f"{strength_db:+.0f}dB"
    else:
        strength_str = "unknown"

    solar = prop_state.get("solar", {})
    sfi, kp = solar.get("sfi"), solar.get("kp")
    solar_str = f"SFI={sfi}, Kp={kp:.1f}" if sfi and kp else "unavailable"

    bands = prop_state.get("bands", {})
    band_lines = []
    for band in ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]:
        b = bands.get(band, {})
        count = b.get("count", 0)
        if count > 0:
            avg_snr = b.get("avg_snr")
            dxcc = b.get("dxcc", [])
            parts = [p for p in [
                f"avg SNR {avg_snr:+.0f}dB" if avg_snr is not None else "",
                f"DXCC heard: {', '.join(dxcc)}" if dxcc else "",
            ] if p]
            band_lines.append(f"  {band}: {count} spots — {' | '.join(parts)}")
        else:
            band_lines.append(f"  {band}: no spots (dead)")

    context = f"""CURRENT RIG STATE:
Frequency: {freq_mhz}
Mode: {rig.get('mode', 'unknown')}
Signal strength: {strength_str}
RF Power: {rig.get('rf_power_pct', 0)}%
Preamp: {rig.get('preamp_name', 'IPO')}
NB: {'on' if rig.get('nb_on') else 'off'} | NR: {'on' if rig.get('nr_on') else 'off'}

SOLAR CONDITIONS ({solar.get('updated', 'unknown')}):
{solar_str}

FT8 BAND ACTIVITY FROM {prop_state.get('my_grid', '?')} (last 15 minutes):
{chr(10).join(band_lines)}
"""
    if user_question:
        context += f"\nUSER QUESTION: {user_question}"
    else:
        context += "\nPlease assess current conditions and recommend the best band and strategy for DX right now."
    return context


class ClaudeAdvisor:
    def __init__(self, rig: RigctldClient):
        self._client = anthropic.Anthropic()
        self._rig = rig
        self.conversation_history: list = []

    def clear_history(self):
        self.conversation_history = []

    async def stream_advice_with_tools(self, rig_state: dict, prop_state: dict,
                                        user_question: Optional[str] = None,
                                        auto_qsy: bool = False):
        """Async generator yielding ('text', str) | ('qsy', dict) | ('done', None).
        Executes the QSY tool call directly against RigctldClient when
        auto_qsy is True and Claude invokes it — this is the one place in
        this port where an LLM can command the radio, so it stays strictly
        opt-in per request (auto_qsy defaults False at the API layer)."""
        context = format_context(rig_state, prop_state, user_question)
        if auto_qsy:
            context += (
                "\n\nAuto-QSY is ENABLED. You MUST use the qsy_to_band tool to command the "
                "radio. Do not just describe what frequency to go to — actually call the tool. "
                "Call it with the best frequency and mode for current conditions."
            )
        else:
            context += "\n\nAuto-QSY is disabled. Do not use the qsy_to_band tool."

        messages = self.conversation_history + [{"role": "user", "content": context}]
        full_response: list[str] = []

        kwargs = dict(model=MODEL, max_tokens=1024, system=_system_prompt(), messages=messages)
        if auto_qsy:
            kwargs["tools"] = [QSY_TOOL]
            kwargs["tool_choice"] = {"type": "auto"}

        # anthropic's streaming client is synchronous-iterator based even
        # on the sync Anthropic() client; running the whole stream in a
        # worker thread and forwarding chunks through a queue keeps this
        # method a clean async generator without blocking the event loop.
        import asyncio
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _run_stream():
            try:
                with self._client.messages.stream(**kwargs) as stream:
                    for event in stream:
                        if getattr(event, "type", None) == "content_block_delta":
                            delta_text = getattr(event.delta, "text", None)
                            if delta_text:
                                loop.call_soon_threadsafe(queue.put_nowait, ("text", delta_text))
                    final = stream.get_final_message()
                    for block in final.content:
                        if getattr(block, "type", None) == "tool_use" and block.name == "qsy_to_band":
                            loop.call_soon_threadsafe(queue.put_nowait, ("qsy", block.input))
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        asyncio.get_event_loop().run_in_executor(None, _run_stream)

        while True:
            kind, data = await queue.get()
            if kind == "text":
                full_response.append(data)
                yield ("text", data)
            elif kind == "qsy":
                freq = data.get("frequency_hz")
                mode = data.get("mode", "PKTUSB")
                if freq:
                    await self._rig.set_frequency(int(freq))
                    await self._rig.set_mode(mode)
                yield ("qsy", data)
            elif kind == "error":
                yield ("error", data)
            elif kind == "done":
                break

        full_text = "".join(full_response)
        self.conversation_history.append({"role": "user", "content": context})
        if full_text:
            self.conversation_history.append({"role": "assistant", "content": full_text})
        if len(self.conversation_history) > 6:
            self.conversation_history[:] = self.conversation_history[-6:]

        yield ("done", None)
