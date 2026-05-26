# Agent Operating Rules

> Copied verbatim from §7 of `CI_CD_RELEASE_PLAN.md` so that any future
> coding agent inherits these rules without having to re-read the full
> plan. If the plan changes, update this file to match.

---

## 7. Agent operating rules (token budget, stopping, handoff)

This section is **directive** to any coding agent acting on this plan. The agent will also copy these rules into `CLAUDE_REFERENCE/AGENT_OPERATING_RULES.md` on first run so future invocations inherit them.

### 7.1 Token budget — the 88% rule

The user pays for context. Agents must self-monitor and stop cleanly before exhausting the session.

* **At ≥ 88% of the session token limit, stop starting new work.**
* Before stopping, the agent **MUST** write a file `CLAUDE_REFERENCE/HANDOFF_<UTC-timestamp>.md` containing:
  1. A one-paragraph summary of what was attempted.
  2. A bulleted list of files created or modified, with absolute paths and a one-line description of each change.
  3. A bulleted list of work **not yet done**, in priority order, with enough detail that a fresh agent can resume without re-reading the full plan.
  4. Any decisions the agent had to make on the user's behalf (with justification) — flagged for human review.
  5. The exact git branch name, last commit SHA on that branch, and whether there are uncommitted changes.
  6. Open questions for `@jos-eph`.
* Then commit the handoff file (do not push), and exit.

### 7.2 Branch & push discipline

* Work only on a branch suffixed `_claude`. Create one (e.g. `001/initial_setup_claude`) if absent.
* Never push. Never merge. Never delete branches. Never amend a commit that exists on origin.
* Commit often with descriptive messages. Small commits are preferred so `@jos-eph` can review per-step.

### 7.3 Scope discipline

* Do not refactor map content / OMT normalisation logic in `generate_tiles_pb.py`. Only its I/O surface.
* Do not introduce new runtime dependencies without listing them in the handoff and pausing.
* Do not commit `.osm.pbf`, `.pmtiles`, `.geojsonseq`, or any file > 5 MB.

### 7.4 Cost discipline

* Test workflows in `--dry-run` (`act` locally, or `workflow_dispatch` with a no-op flag) before triggering a live build. Avoid trial scheduled runs.
* Never enable a workflow trigger that can fire repeatedly without rate-limiting (e.g. `on: push` to all branches, or webhook-driven loops).
