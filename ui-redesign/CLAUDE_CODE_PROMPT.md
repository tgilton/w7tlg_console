# Prompt for Claude Code

Paste this into Claude Code from the repo root:

---

Read `ui-redesign/SPEC.md` and open `ui-redesign/mockup.html` in a browser
(or view it directly) as the visual target for a redesign of the operating
console.

Before changing anything: read `dashboard/console.html` in full, including
its top comment block, and read `dashboard/server.py`'s websocket and REST
routes. The current console is an iframe shell over `index.html`,
`panadapter.html`, and `monitor.html`, deliberately isolated to protect
TX-audio-mute timing, feedback-loop prevention, and digital-mode view
locking. Tell me explicitly how you plan to preserve or replace that
isolation before writing any code — I want to review that plan first.

Then implement the layout in `SPEC.md`, in `dashboard/`, following its
block breakdown, data-plane separation, and phase-2 scoping for the
diversity combiner panel (mock data only — no new SDR/combiner backend
this pass).

Do not modify `/monitor`, `/panadapter`, `/propagation`, `/spotseek`, or
`/advisor` routes or their pages.

Confirm the acceptance criteria in `SPEC.md` before considering this done,
especially TX audio mute timing and feedback-loop prevention — these need
actual verification, not just "should still work."

Once I've approved the implementation: create a branch (e.g.
`ui-redesign`), commit the changes with a clear message, and push it to
GitHub using my existing git credentials. Open a PR rather than pushing
directly to main, so I can review the diff before merging.

Also preserve the PKTUSB known-good mode defaults in `setMode()`
(dashboard/index.html) — the bundled AGC/NB/DNF/IPO/DNR/EQ settings that
fire only on an explicit PKTUSB mode-select click, never reactively. This
was confirmed live and tuned by hand; don't let a refactor drop or
generalize it.
