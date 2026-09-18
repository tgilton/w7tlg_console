# w7tlg-console — Audit & Refactor Prompts for Claude Code

How to use this: run Phase 1 first, in its own session, with no code-writing permission beyond creating `AUDIT.md`. Review the output yourself before starting Phase 2. Fill in the "Known issues & feature backlog" section below with your own list before running Phase 2 — the plan is much better if it's reacting to your actual backlog rather than guessing at it.

---

## Phase 1 prompt — generate AUDIT.md (read-only pass)

	You are auditing the w7tlg-console codebase — a Python/FastAPI/WebSocket
	application that controls real amateur radio hardware in real time: CAT
	control of a Yaesu FT-991A via rigctld, an RSPDuo two receiver SDR panadapter, WSJT-X
	digital-mode integration, and an ACOM 1200S/06AT amplifier, plus an
	LLM-based band advisor.
	
	Do NOT modify any code in this pass. Your only output is a file called
	AUDIT.md at the repo root. Read the full repository first — every module,
	not just entry points — before writing anything.
	
	Structure AUDIT.md with these sections:
	
	1. Module & dependency map
	   - What imports what. Draw the boundary (or lack of one) between
	     hardware I/O (rigctld socket calls, SDR SDK calls, amp
	     serial/network calls) and business logic / the FastAPI/WebSocket
	     layer.
	
	2. Concurrency audit
	   - Every `async def` that performs blocking I/O without
	     `run_in_executor` / `asyncio.to_thread` — name the file, line,
	     and call.
	   - Every piece of shared mutable state (rig frequency, mode, amp
	     status, SDR settings) that is written or read from more than one
	     coroutine or WebSocket handler without a lock, queue, or
	     single-writer pattern.
	   - Any call path where a slow hardware operation (rigctld query, SDR
	     tuning, amp status poll) could block the event loop and stall
	     other connected WebSocket clients.
	
	3. Hardware failure-mode audit
	   - For each external device (rig, SDR, amp), trace what happens on:
	     disconnect, timeout, malformed response, and slow response.
	     State explicitly whether the app can end up in a state where the
	     UI shows something (e.g. "not transmitting") that doesn't match
	     physical reality, and how that could happen.
	   - Flag any code path where a control command is sent without first
	     confirming current hardware state.
	
	4. State management
	   - Is there one authoritative source of truth for rig/amp/SDR state
	     that's broadcast to clients, or do clients reconstruct/diverge
	     independently? Trace an example of a state change from hardware
	     event to every connected client's UI.
	
	5. API & WebSocket contract review
	   - Inconsistencies in message schemas, undocumented endpoints,
	     endpoints with no error handling, missing input validation on
	     anything that reaches hardware control.
	
	6. Test coverage map
	   - What's actually covered by automated tests vs. only exercised
	     manually. Be specific about which hardware-control paths have
	     zero test coverage.
	
	7. LLM band-advisor coupling
	   - Confirm (or refute, with evidence) that the band advisor is
	     read-only with respect to hardware state — i.e. it can suggest
	     but has no path, direct or indirect, into actuation. If you find
	     any such path, flag it as high severity regardless of how
	     unlikely it seems to trigger.
	
	8. General code-quality notes
	   - Dead code, duplicated logic, inconsistent error handling
	     patterns, missing type hints in safety-relevant paths,
	     configuration/secrets handling.
	
	For every finding in every section, assign a severity: CRITICAL (could
	cause unsafe hardware actuation or a stuck/incorrect TX state), HIGH
	(functional bug, no safety implication), MEDIUM, LOW/cosmetic. Cite exact
	file paths and line numbers throughout. End with a one-paragraph summary
	ranking the top 5 findings by severity.

---

## Known issues & feature backlog — fill in before Phase 2

*(Replace this section with your actual list — even rough notes are fine, Claude Code will fold them into the plan alongside the audit findings.)*

**Little things / bugs:**
- In opening the app for the first time, RX1 and RX2 are not aligned. RX2 has no mode set and this often results in no audio for RX2; I am required to click on USB in the mode to get RX2 to send audio to  WSJT-X.
- The User Interface is still a bit rough. There are places in the UI where buttons/pills are too close. There are control groups that are separate from the other functions that they work with.
- The Main display where the frequency is shown needs to have a live output power display; the one in the AMP (ACOM 1200S) box is useful but too small.
- The S meter would be better if it was a skeuomorphic analog S-meter for both RXs.
- The AF GAIN and RF GAIN sliders need to be more prominent as I use them very often.
- The MODE selecting buttons need to be evaluated. They are too close to the AF GAIN and RF GAIN sliders, and they do not match between the RXs. RX2 has only USB and LSB, but it needs all the options shown in RX1
	- This means we need to evaluate using RX1 as the main DATA-U receiver. It really isn’t as there is an option to select which RX is used for WSJT-X
- We need a way to adjust the frequency within the console that is not entering the frequency in a box. Evaluate how some of the SDR software packages do this.

**Missing features:**
- Better integration with WSJT-X and possibly JS8CALL to allow more extensive parameters to be stored in the RumLogNG logs that are automatically loaded.
- Need to be able to trigger a log entry from within the w7tlg console. Need to discuss details.
- Create a useful feature that allows querying the previous monitoring results to compare previous operations to current operations and discover problems. 
- Can we get a voice envelop display to tune th SSB operations.

---

## Phase 2 prompt — generate REFACTOR\_PLAN.md (still no code changes)

	You have already produced AUDIT.md for this repository. Read it, along
	with the "Known issues & feature backlog" list below, which is the
	owner's own list of bugs, missing features, and hardware-expansion goals.
	
	KNOWN ISSUES & FEATURE BACKLOG:
	This is a very loose list and should be discussed and evaluated. The goal is to scope the project correctly adn we can get into the detials of each item when the time comes to code.
	## Known issues & feature backlog
	
	**Little things / bugs:**
	- In opening the app for the first time, RX1 and RX2 are not aligned. RX2 has no mode set and this often results in no audio for RX2; I am required to click on USB in the mode to get RX2 to send audio to  WSJT-X.
	- The User Interface is still a bit rough. There are places in the UI where buttons/pills are too close. There are control groups that are separate from the other functions that they work with.
	- The Main display where the frequency is shown needs to have a live output power display; the one in the AMP (ACOM 1200S) box is useful but too small.
	- The S meter would be better if it was a skeuomorphic analog S-meter for both RXs.
	- The AF GAIN and RF GAIN sliders need to be more prominent as I use them very often.
	- The MODE selecting buttons need to be evaluated. They are too close to the AF GAIN and RF GAIN sliders, and they do not match between the RXs. RX2 has only USB and LSB, but it needs all the options shown in RX1
		- This means we need to evaluate using RX1 as the main DATA-U receiver. It really isn’t as there is an option to select which RX is used for WSJT-X
	- We need a way to adjust the frequency within the console that is not entering the frequency in a box. Evaluate how some of the SDR software packages do this.
	- The app is organized into columns: Left column, Middle Column, and Right Column. There is also a header and footer. I want to organize these as tightly coupled boxes of functions.
		- The left column should be general operational functions. 
			- SESSION, BAND, ANTENNA, ANTENNA A/B TEST, 
		- The Middle Column houses the receivers
			- There are two receivers and each should have it’s own column within the app’s middle column.
				- Frequency, S-Meter for each
				- Spectrum and waterfall
				- Spectrum and waterfall controls and options
				- 
			- There are common controls for the receivers that should not be duplicated and should define operations for both receivers 
				- Mode should be the same for both receivers. However, there might be a use case in the future where the two could be in different modes. Let’s not preclude that, but for now
		- The Right Column is the Transmitter and should group together all the parameters, meters, and operational options for the TX.
	
	**Missing features:**
	- Better integration with WSJT-X and possibly JS8CALL to allow more extensive parameters to be stored in the RumLogNG logs that are automatically loaded.
	- Need to be able to trigger a log entry from within the w7tlg console. Need to discuss details.
	- Create a useful feature that allows querying the previous monitoring results to compare previous operations to current operations and discover problems. 
	- Can we get a voice envelop display to tune the SSB operations?
	
	Do NOT modify any code in this pass. Produce REFACTOR_PLAN.md at the
	repo root.
	
	Organize the plan as a sequence of independent work packages, each with:
	- A short name and one-paragraph goal.
	- Which AUDIT.md findings and/or backlog items it addresses (cite them).
	- Explicit dependencies on other work packages (what must land first).
	- Files/modules it will touch.
	- What must NOT change in this package (explicit non-goals) — this
	  matters because this app controls live RF hardware, and scope creep
	  in a single pass is how safety-relevant regressions get introduced.
	- How it will be validated: which parts can be verified against a
	  mock/fake hardware layer, and which parts genuinely require a manual
	  smoke test against real hardware before merging.
	
	Sequence the packages so that anything safety- or correctness-critical
	(hardware abstraction layer, mockable interfaces, centralized state
	management, fixing event-loop-blocking calls) comes before feature work
	or the hardware-expansion items in the backlog. New hardware control
	surfaces should be built against the new abstraction layer, not bolted
	onto the existing direct-I/O pattern, even if that means the
	corresponding work package can't start until the abstraction layer
	package lands.
	
	For each work package, also note whether it's a good candidate to be
	done as its own git branch and its own Claude Code session (it usually
	is, for anything touching hardware control paths).

---

## Phase 3 prompt template — per work package (reusable)

	You are executing work package "[NAME]" from REFACTOR_PLAN.md in this
	repository. Read AUDIT.md and REFACTOR_PLAN.md first for context.
	
	Scope: implement only this work package. Do not touch files or logic
	outside its stated scope, even if you notice other issues — note
	anything else you find in a "## Observations" section at the end of
	your summary instead of fixing it.
	
	After implementation:
	1. Run the existing test suite and report results.
	2. If this package touches hardware-control paths and mock interfaces
	   exist, write or extend tests against the mocks to cover the change.
	3. Summarize exactly what changed, file by file, and flag anything that
	   still needs a manual smoke test against real hardware before this is
	   safe to merge.
