# Development Methods & Lessons

A short record of the process patterns and lessons learned from building the QtGUI
(and the widget→QML migration that produced this structure). The intent is that a
future session reads this before touching the code, rather than re-learning the
lessons the hard way. `Plan.md` says *what*; `ARCHITECTURE.md` says *how*; this file
says *how we work*.

---

## 1. Validate against the real device — don't trust the mock

During the QML migration the app "worked" offscreen, but seven defects surfaced only
when a human tested on the actual ODrive: wrong setpoint on mode switch, a stuck
error-dialog placeholder, non-editable setpoints, controls enabled while
disconnected, the 2-column settings layout missing, dialogs that couldn't be moved,
and a light-vs-dark contrast regression.

None were caught by the mock-based tests — partly because those tests were written
*after* the bugs, but mainly because an incomplete mock hides exactly the paths that
bite on hardware.

**Rules:**
- Build the mock harness **before** the feature, not as a retrospective.
- The mock must mirror the *real* device object shape completely enough to drive
  every code path (a mock missing `vel_estimate` silently passes the poll loop that
  is precisely where hardware breaks).
- Never treat "tests pass offscreen" as equivalent to "device works". The two are
  different feedback loops; both are required.
- Close the loop with a manual on-device acceptance pass after any UI/device change.

## 2. Ask before building when the requirement is ambiguous

An ambiguous instruction ("control settings below", "dialogs") was assumed rather
than confirmed, and corrected later. The fix was small that time; it won't always be.

**Rules:**
- Pin down behavior *up front* when it's genuinely ambiguous: setpoint commit
  semantics (edit-locally vs write-through), dialog UX (movable/titled vs popup),
  gating, layout.
- Ask **before** writing the code, not after. A question at the wrong time is a
  rebase.

## 3. Split responsibilities before the god-object forms

Pushing all device logic into one `GuiBackend` (782 lines) worked for the migration,
but it is a single object owning connection, polling, mode/input-mode logic, the
config API, setpoints, and four dialogs' data. Fine at this size; a liability at the
next feature.

**Rules:**
- "Fewest files / one backend object" (ponytail) is the right reflex for *scope*, but
  stop treating it as the goal once an object outgrows one responsibility.
- When a feature arrives (Phase 3 plotting, calibration wizard), pull responsibilities
  *out* of `GuiBackend` rather than growing it.

## 4. What worked — repeat these

- **Plan before building.** A written phase (Plan.md) gave the migration structure to
  refer back to.
- **Squash to one coherent commit.** Clean, reviewable history; easy to `--amend` as
  corrections arrive. Rebase carefully — a manual `git commit` mid-rebase splits a
  squash into a wrong pair of commits; verify the final tree against the base and
  collapse with `git reset --soft` if it goes wrong.
- **Ponytail-review at the end.** Found real dead code / smells cheaply (a dead QML
  property, unused ids, a getattr guard).
- **Stop and confirm** when a step is ambiguous rather than plowing through.

## 5. Mechanical discipline

Malformed/incomplete tool calls cost the session time and patience. Emit complete,
well-formed commands; verify output is real before acting on it.

---

## A compression for the next session

1. **Test harness first, mirroring the real device** — used from day one, not bolted on
   after the bugs.
2. **Ask before building when ambiguous** — pin behavior (setpoint commit, dialog UX,
   layout, gating) before writing code.
3. **Stop growing the god-object** — split responsibilities as they appear, not when
   the file becomes unreadable.
4. **Always close with a hardware acceptance pass** — offscreen tests are not the real
   feedback loop.
