# Agent Operating Rules

> Adapted from §7 of `CI_CD_RELEASE_PLAN.md` so that any future
> coding agent inherits these rules without having to re-read the full
> plan. If the plan changes, update this file to match.

---

## 7. Agent operating rules (branches, scope, cost)

This section is **directive** to any coding agent acting on this plan. The agent will also copy these rules into `CLAUDE_REFERENCE/AGENT_OPERATING_RULES.md` on first run so future invocations inherit them.

### 7.1 Branch & push discipline

* Work only on a branch suffixed `agent_permitted`. Create one (e.g. `001/initial_setup_agent_permitted`) if absent.
* Never push. Never merge. Never delete branches. Never amend a commit that exists on origin.
* Commit often with descriptive messages. Small commits are preferred so `@jos-eph` can review per-step.

### 7.2 Scope discipline

* Do not refactor map content / OMT normalization logic in `generate_tiles_pb.py`. Only its I/O surface.
* Do not introduce new runtime dependencies without flagging them and pausing.
* Do not commit `.osm.pbf`, `.pmtiles`, `.geojsonseq`, or any file > 5 MB.

### 7.3 Cost discipline

* Test workflows in `--dry-run` (`act` locally, or `workflow_dispatch` with a no-op flag) before triggering a live build. Avoid trial scheduled runs.
* Never enable a workflow trigger that can fire repeatedly without rate-limiting (e.g. `on: push` to all branches, or webhook-driven loops).
