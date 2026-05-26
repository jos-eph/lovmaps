# Plan: Convert `generate_tiles_pb.py` to a Dated GitHub Releases CI/CD Flow

> **Audience:** This document is written for a human reviewer (`@jos-eph`) to make decisions, and then to be handed to an agentic coding agent (primarily **Claude Code with Opus 4.7**; some isolated tasks may be delegated to **aider + qwen3-coder-next** which has a **64,000-token context window**).
>
> **Status:** Draft. Sections marked **🟡 DECISION** require human input before the plan is handed off.

---

## 0. Tl;dr — what we are building

Today, `generate_tiles_pb.py` runs locally. It downloads / consumes a Geofabrik regional OSM extract, slices it to a Philadelphia-area bounding box, normalizes layers into OpenMapTiles-compatible properties, and emits a single `.pmtiles` file.

We want to move this to GitHub Actions so that:

1. A daily scheduled run (and an on-demand run) executes the script in a free-tier runner.
2. Each successful run uploads dated, versioned artifacts to a **GitHub Release**:
   * `philly_commute_region_<DATE>.pmtiles`
   * `philly_commute_region_<DATE>.pmtiles.sha256` (or `.sha` — see **🟡 DECISION D2**)
   * `philly_commute_region_<DATE>.osm.pbf` (the bounding-box extract — see **🟡 DECISION D1** for which PBF(s))
   * `philly_commute_region_<DATE>.osm.pbf.sha256`
3. Each successful run also **overwrites** a stable "current" pair, so that any consumer with a hard-coded URL always gets the latest:
   * `philly_commute_region_current.pmtiles`
   * `philly_commute_region_current.pmtiles.sha256`
   * (and matching `.osm.pbf` pair, if D1 includes PBFs)
4. The workflow runs **once per day on a schedule** AND **whenever a manual trigger sets `push_release=true`**.
5. Sensible repository security is enabled (branch protection, restricted token scopes, action pinning, etc.).

`<DATE>` is a fixed-width timestamp captured **once** at the start of the run and reused for every asset of that run. See **🟡 DECISION D3** for the exact format.

---

## 1. Current state (verified 2026-05-26)

* **Branch policy (CLAUDE.md):** Only `@jos-eph` may push. All agent work must occur on a branch suffixed `_claude`. `main` is the only branch agents may target via PR.
* **Script (`generate_tiles_pb.py`):** Generates `SCRIPTMADE_<YYYYMMDD_HHMMSS>_omt.pmtiles` from a source file currently referenced as `us-northeast-latest.osm.pbf` (expected at the script's working directory) and a hard-coded bbox `-76.00,39.60,-74.60,40.40`.
* **Source PBF is NEVER committed to the repo.** It is multi-hundred-megabyte upstream OSM data and would violate both repo size hygiene and CLAUDE.md's "no large datasets in git" rule. Local runs require the operator to download the file manually before invoking the script; CI runs will fetch it dynamically (see §1.1).
* **Dependencies invoked as subprocesses:** `osmium`, `tippecanoe`. Both must be installed on the runner.
* **Outputs:** intermediate per-layer `.osm.pbf` + `.geojsonseq` files; final `.pmtiles`.
* **No CI/CD yet:** the repo currently has no `.github/workflows/` directory.
* **No releases yet.**

### 1.1 Canonical upstream source

All runs — local and CI — pull from a single canonical URL:

```
SOURCE_PBF_URL = https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf
SOURCE_MD5_URL = https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf.md5
```

This URL is the single source of truth referenced throughout this plan. Any change to the upstream extract is an explicit, reviewed decision (see §10 — out of scope) and would require updating the constant in exactly two places (the script default and the workflow), not scattered string literals.

`.gitignore` must include `*.osm.pbf` (and `*.pmtiles`, `*.geojsonseq`) to make accidental commits impossible.

---

## 2. Target architecture

```
                          ┌────────────────────────────────────────────┐
                          │  GitHub Actions runner (ubuntu-latest)     │
   schedule (daily)       │                                            │
   workflow_dispatch ───► │  1. Install osmium-tool, tippecanoe        │
   (push_release=true)    │  2. Download SOURCE_PBF_URL (Geofabrik     │
                          │     us-northeast) to /tmp; verify .md5     │
                          │  3. Compute run timestamp <DATE>           │
                          │  4. Run generate_tiles_pb.py --base-name   │
                          │     philly_commute_region --date <DATE>    │
                          │  5. sha256 each final artifact             │
                          │  6. Create / update GitHub Release tagged  │
                          │     tiles-<DATE> with versioned assets     │
                          │  7. Copy assets onto the "current" pointer │
                          │     release (overwrite mode)               │
                          └────────────────────────────────────────────┘
```

Key design choices:

* **Two releases, not one.**
  * A dated release `tiles-<DATE>` holds the immutable, versioned assets.
  * A single, perpetual release `current` (with stable tag `current`) holds the always-overwritten `philly_commute_region_current.*` assets.
  * This gives consumers two stable patterns: pin to a date, or always pull `current`.
  * **🟡 DECISION D4:** approve this two-release pattern, or pick an alternative.
* **Source PBF is fetched at runtime, never stored in git.** The workflow downloads `SOURCE_PBF_URL` (see §1.1) into the runner's ephemeral workspace (or `/tmp`) on every run. The repo contains no `.osm.pbf` files and `.gitignore` enforces this.
* **No large data in git.** OSM extracts and outputs live only in `/tmp` (or runner workspace) and on Releases.
* **Idempotent runs.** If a run is re-triggered for the same `<DATE>`, the workflow should refuse to overwrite a dated release's assets (immutability), but is always free to overwrite `current`.
* **Free-tier respect.** Single-job, single-OS, no matrix. Concurrency group prevents overlap. Cache `apt` install if it materially saves time (see D7).

---

## 3. 🟡 Decisions required from `@jos-eph`

Please fill in each decision by replacing the placeholder with your choice (or noting "agent decides"). The downstream coding agent will treat anything still marked 🟡 as a blocker.

| ID | Decision | Recommendation | Your choice |
|----|----------|----------------|-------------|
| **D1** | Which PBFs to publish per run? <br/>Options: (a) only the final `.pmtiles`; (b) `.pmtiles` + the bbox-extracted region `.osm.pbf`; (c) all per-layer PBFs too. | **(b)** — bbox PBF is small (a few MB), reproducible, and useful to downstream tooling. Per-layer PBFs are intermediates and add quota pressure. | _________ |
| **D2** | SHA filename suffix? Original brief says `.sha`, but `.sha256` is the de-facto standard and more self-describing. | **`.sha256`**. Contents = single line `<hex>  <filename>` (matches `sha256sum` output, so consumers can verify with `sha256sum -c`). | _________ |
| **D3** | Exact `<DATE>` format. Brief specifies *seconds-minutes-hours-day-month*. Year is not mentioned but is almost certainly needed for sortability and to disambiguate annual recurrence. | **`SS-MM-HH-DD-MM-YYYY`** zero-padded, UTC, e.g. `42-17-08-26-05-2026`. (If you'd prefer naturally-sortable ISO-ish, say so — but the brief is explicit, so we keep the requested order and only append year.) | _________ |
| **D4** | Two-release pattern (dated immutable + `current` pointer)? | **Approve.** Simpler than asset-renaming inside a single release and cleanly separates "pinned" vs "rolling" consumers. | _________ |
| **D5** | Daily schedule time (UTC). Geofabrik publishes new daily extracts ~01:00–03:00 UTC. | **`30 4 * * *`** (04:30 UTC) — safely after Geofabrik refresh, outside US peak. | _________ |
| **D6** | Retention of dated releases. Unbounded growth is fine until storage matters, but pruning is healthy. | **Keep the most recent 30 dated releases**, prune older ones with a small cleanup step at the end of the workflow. `current` is never pruned. | _________ |
| **D7** | Cache `apt` packages (osmium-tool, tippecanoe) via `actions/cache`? Saves ~30–60s per run. | **Yes**, but only if tippecanoe is available as an apt package on `ubuntu-latest`; otherwise build-from-source and cache the resulting binary. Agent should determine and document. | _________ |
| **D8** | Branch-protection ruleset on `main` (see §6). | **Approve recommended ruleset** as written. | _________ |
| **D9** | Should the workflow file live at `.github/workflows/release-tiles.yml` (one file) or be split (build + publish)? | **Single file.** Simplicity, fewer moving parts, easier to reason about cost. | _________ |
| **D10** | Notification on failure? GitHub already emails on failed scheduled workflows for the repo owner, but you may want a dedicated channel. | **Rely on default email** for now; revisit if noisy. | _________ |
| ~~D11~~ | ~~License/attribution file embedded in each release?~~ | **Decided: yes.** `ATTRIBUTION.txt` will be created at repo root and uploaded with every release. Contents must mirror the README's map-data notice (© OpenStreetMap contributors, © Protomaps) and include the ODbL 1.0 license terms (either inline, or by including the full text of `MAP-DATA-LICENSE.md`). See §4.3. | n/a |

---

## 4. File-by-file changes the agent will make

> Paths are relative to repo root. All work occurs on a branch named `<existing-branch>_claude` (e.g. `001/initial_setup_claude`). The agent **MUST NOT** push; it prepares commits and stops for `@jos-eph` to push.

### 4.1 `generate_tiles_pb.py` — refactor

* Add CLI args via `argparse`:
  * `--source-pbf PATH` (optional; **no default**). If provided, the script uses this local file directly and skips any download. Used by the CI workflow, which downloads the PBF in a dedicated step, and by developers who already have a local copy.
  * `--source-url URL` (default: `SOURCE_PBF_URL` from §1.1, i.e. the Geofabrik us-northeast extract). If `--source-pbf` is not provided, the script downloads from this URL into `--output-dir` (or a temp dir) before processing. This makes local runs work out of the box without manual download.
  * `--bbox "minlon,minlat,maxlon,maxlat"` (default keeps the SEPTA bbox).
  * `--base-name STR` (default `philly_commute_region`).
  * `--date STR` (date stamp; if omitted, generated as in D3).
  * `--output-dir PATH` (default `.`).
  * `--keep-intermediates / --no-keep-intermediates` (default `--no-keep-intermediates` so the runner doesn't run out of disk).
* Define `SOURCE_PBF_URL` and `SOURCE_MD5_URL` as module-level constants (single source of truth — see §1.1). Do not duplicate the URL string.
* Replace `SCRIPTMADE_<timestamp>` naming with `<base-name>_<date>`.
* Emit final filenames `${output_dir}/${base_name}_${date}.pmtiles` and (per D1) `${output_dir}/${base_name}_${date}.osm.pbf`.
* The downloaded source PBF (when fetched by the script) is treated as an intermediate: deleted on clean exit unless `--keep-intermediates`. It is **never** the same file as the bbox-extracted region PBF that gets uploaded to the release.
* On clean exit, delete intermediates if `--no-keep-intermediates`.
* On nonzero exit anywhere, fail the workflow (`set -e` semantics; `run()` already does `sys.exit(1)`, but exit codes must be preserved).
* Preserve the existing OMT normalisation logic exactly — this is **not** a refactor of map content, only of I/O.
* Add a module-level docstring noting the new CLI surface and the dynamic-fetch behaviour.

**Note on responsibility for downloading:** the CI workflow (§4.2) is the canonical downloader in production — it handles retries, backoff, and `.md5` verification using shell tooling. The script's built-in download is a convenience for local runs only and may use a simpler best-effort implementation (single attempt, optional md5 check). If `--source-pbf` is passed, the script does no network IO at all.

### 4.2 `.github/workflows/release-tiles.yml` — new

Triggers:

```yaml
on:
  schedule:
    - cron: '30 4 * * *'   # D5
  workflow_dispatch:
    inputs:
      push_release:
        description: 'Build and publish a release now'
        type: boolean
        default: true
```

The brief mentions "`push_release` set to true in the GitHub yaml". The agent should implement this as a `workflow_dispatch` boolean input (above) — that is the idiomatic GitHub-native way to express "I want a release now". The workflow will short-circuit (no release upload) if `github.event_name == 'workflow_dispatch' && inputs.push_release == false`.

Job-level guarantees:

* `permissions:` block scoped to the minimum needed:
  ```yaml
  permissions:
    contents: write   # required to create releases & upload assets
  ```
  Nothing else.
* `concurrency:` group `release-tiles` with `cancel-in-progress: false` so a manual run never clobbers a scheduled one mid-flight.
* `timeout-minutes: 60` — well under any single-job quota.
* All third-party actions pinned to a **full commit SHA** (not a tag) with the human-readable version in a trailing comment. Example: `actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1`.

Step outline:

1. `actions/checkout` (full SHA-pinned).
2. `actions/setup-python@<SHA>` with Python 3.12.
3. Install system deps (`osmium-tool`, `tippecanoe`). Cache per D7.
4. Compute `DATE` once, export to `$GITHUB_ENV` and `$GITHUB_OUTPUT`.
5. Download the source PBF dynamically from `SOURCE_PBF_URL` (see §1.1) into `${RUNNER_TEMP}/us-northeast-latest.osm.pbf` with polite retry/backoff (3 attempts, exponential). Fetch `SOURCE_MD5_URL` and verify the downloaded file's md5 against it. Fail loudly on mismatch. The PBF must land in ephemeral runner storage — never the repo workspace root, never checked in.
6. Run `python generate_tiles_pb.py --base-name philly_commute_region --date "$DATE" --output-dir ./out --source-pbf "${RUNNER_TEMP}/us-northeast-latest.osm.pbf"` (passing `--source-pbf` explicitly suppresses the script's built-in download path; the workflow owns the network IO).
7. Generate `sha256` files: `sha256sum philly_commute_region_${DATE}.pmtiles > philly_commute_region_${DATE}.pmtiles.sha256` (and likewise for the PBF).
8. Create dated release `tiles-${DATE}` with tag `tiles-${DATE}`, title `Tiles ${DATE}`, body containing source URL, source md5, bbox, git SHA of the workflow run. Upload versioned assets. Use `gh release create`.
9. Copy/rename the same files to `philly_commute_region_current.*` and upload them to (or update) a release with the **fixed** tag `current`. Use `gh release upload --clobber` to overwrite. Create the `current` release on first run if it does not exist.
10. Prune dated releases beyond the most recent 30 (D6).
11. Upload a small `philly_commute_region_${DATE}.run.txt` metadata blob (git SHA, timestamps, source md5, bbox) as a release asset for traceability.

### 4.3 `ATTRIBUTION.txt` — new

Short, plain-text file at repo root. Required contents:

* The exact copyright line(s) from `README.md`'s "Copyright and License" section, namely:
  > Map data (`.pbf` and `.pmtiles` files) © OpenStreetMap contributors © Protomaps, available under the ODbL (Open Database License) 1.0.
* A pointer to the upstream OSM copyright page: `https://www.openstreetmap.org/copyright`.
* The ODbL 1.0 license terms. Preferred form: a short header + the full text of the ODbL as already present in `MAP-DATA-LICENSE.md` (inline). Acceptable alternative: a brief summary plus a same-release link/reference to `MAP-DATA-LICENSE.md`, which the workflow also uploads to each release.
* A note that this file applies to the `.pbf` and `.pmtiles` assets in the release.

The workflow will upload `ATTRIBUTION.txt` verbatim to **every** release (both dated and `current`). If the agent chooses the "inline ODbL" form, no extra upload of `MAP-DATA-LICENSE.md` is needed; if the "reference" form is chosen, the agent must also upload `MAP-DATA-LICENSE.md` to each release.

The agent must keep `ATTRIBUTION.txt` and the README's "Copyright and License" section in sync: if either drifts, downstream consumers see inconsistent attribution. A short comment at the top of `ATTRIBUTION.txt` should call this out (e.g. *"This file mirrors the map-data notice in README.md. Keep them in sync."*).

### 4.4 `README.md` — append a "Downloads" section

Document the stable URLs:

* `https://github.com/jos-eph/lovmaps/releases/download/current/philly_commute_region_current.pmtiles`
* `https://github.com/jos-eph/lovmaps/releases/download/current/philly_commute_region_current.pmtiles.sha256`

And the pattern for dated releases.

### 4.5 `CLAUDE_REFERENCE/AGENT_OPERATING_RULES.md` — new

Persistent operating rules for any future agent run; see §7 (token-budget and stop conditions).

---

## 5. Idempotency & failure modes the agent must handle

| Scenario | Required behaviour |
|----------|-------------------|
| Re-running for an already-existing `tiles-<DATE>` tag | **Fail fast** with a clear log line. Never silently overwrite a dated release. |
| `current` release does not exist yet | Create it on first successful run. Subsequent runs `--clobber` its assets. |
| Geofabrik download fails or md5 mismatches | Retry up to 3 times with exponential backoff (e.g. 30s/2m/8m), then fail the job. Do **not** publish a partial release. |
| Disk full mid-pipeline | Job fails; no release is created. Intermediates are in `/tmp` (ephemeral). |
| Tippecanoe / osmium produces a 0-byte output | Add an explicit size sanity check before upload; fail if final `.pmtiles` < some agent-chosen floor (e.g. 100 KB). |
| `current` upload succeeds but dated upload failed (or vice versa) | Agent should sequence uploads dated-first, then `current`. If `current` upload fails, the dated release is still valid; log clearly. |

---

## 6. Security hardening — recommended baseline (D8)

These are the standard "small-repo, free-tier, single-maintainer" defenses. Most are GitHub UI/API settings, not code, so the agent will prepare a `SECURITY_SETUP.md` checklist for `@jos-eph` to apply manually.

**Branch protection on `main`:**

* Require a pull request before merging.
* Require at least 1 approving review.
* Require status checks to pass (once we have any).
* Restrict who can push to `main` to `@jos-eph` only.
* Disallow force-pushes and branch deletion on `main`.
* (Recommended) Require signed commits.

**Workflow / Actions settings:**

* Repository setting **Actions → General → Workflow permissions**: set to "Read repository contents and packages permissions" (least privilege). Per-workflow `permissions:` block grants `contents: write` only where needed.
* **Fork pull request workflows from outside collaborators:** "Require approval for all outside collaborators."
* Allow only verified-creator and explicitly-listed actions (Actions → General → Allow select actions). Add `actions/*`, `softprops/action-gh-release` (or chosen release action), and any other dependency.
* Pin every action to a **commit SHA** (not a tag). The agent does this.

**Repository-level hardening:**

* Enable **Dependabot security updates**.
* Enable **secret scanning** and **push protection** (free for public repos).
* Enable **code scanning (CodeQL)** with the default Python config (single small job, runs only on PR / weekly cron, free for public repos).
* Disable **GitHub Actions on forks** (Actions → General → Fork pull request workflows from outside collaborators).
* Add a minimal `SECURITY.md` with disclosure contact.

**Release / supply-chain hygiene:**

* All release artifacts are sha256'd. The workflow logs the sha256 of every uploaded file (so it appears in the public workflow log, providing a second verifiable witness alongside the `.sha256` asset).
* Workflow logs the upstream Geofabrik `.md5` for the source PBF, so the provenance chain is reconstructable.

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

---

## 8. Prompts to hand to the coding agent(s)

Each prompt is **self-contained** and assumes the agent has read this plan in full and has access to the repo. Prompts are sized to fit comfortably within the qwen3-coder-next 64,000-token window, so they can be delegated to aider if Claude Code is busy.

### 8.1 Prompt — Refactor `generate_tiles_pb.py` (Claude Code recommended; aider acceptable)

````
You are working on the repo at /home/joe/lovmaps. Read CLAUDE.md and CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md before doing anything else. You may modify files only on a branch suffixed `_claude` — create `001/initial_setup_claude` if it does not exist and check it out. Do not push.

Task: Refactor `generate_tiles_pb.py` per §4.1 of the plan. Specifically:

1. Add an argparse-based CLI with these flags (defaults in parens):
   --source-pbf PATH (no default — if omitted, script downloads from --source-url)
   --source-url URL  (https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf — see §1.1)
   --bbox STR        (-76.00,39.60,-74.60,40.40)
   --base-name STR   (philly_commute_region)
   --date STR        (auto-generated as SS-MM-HH-DD-MM-YYYY in UTC if omitted; see plan D3)
   --output-dir PATH (.)
   --keep-intermediates / --no-keep-intermediates (default: no-keep)

   Define module-level constants `SOURCE_PBF_URL` and `SOURCE_MD5_URL` (the Geofabrik URLs from §1.1) and use them as defaults / for the optional md5 check. Do not hard-code the URL string anywhere else in the file.

   When `--source-pbf` is omitted, download `--source-url` to `<output-dir>/us-northeast-latest.osm.pbf` (or a tempfile), best-effort md5 check, then proceed. When `--source-pbf` is given, do no network IO. Treat the downloaded PBF as an intermediate (delete on clean exit unless --keep-intermediates).

   The source PBF must never be committed. Add `*.osm.pbf`, `*.pmtiles`, `*.geojsonseq` to `.gitignore` if not already present.

2. Replace the existing SCRIPTMADE_<timestamp> naming with <base-name>_<date> for the final pmtiles and the bbox-extracted region PBF. Per-layer intermediates may keep their existing names since they are deleted at the end unless --keep-intermediates.

3. Produce the bbox-extracted region PBF at <output_dir>/<base-name>_<date>.osm.pbf (i.e. rename or copy the region_pbf to this final name at the end of stage 1).

4. Produce the final pmtiles at <output_dir>/<base-name>_<date>.pmtiles.

5. On normal exit, delete all per-layer .osm.pbf, .raw.geojsonseq, and .geojsonseq files unless --keep-intermediates.

6. Preserve ALL existing OpenMapTiles normalization logic byte-for-byte. Do not change ZOOM_FILTERS, HIGHWAY_TO_CLASS, RAILWAY_TO_CLASS, any normalize_* function, or any tippecanoe flags. This is an I/O refactor only.

7. Keep the existing `run()` subprocess helper and its exit semantics.

8. Add a smoke test you can run without OSM data: `python generate_tiles_pb.py --help` must list every new flag. Verify and paste the help output into your commit message.

9. Commit on the _claude branch with a clear message. Do not push.

Self-check before finishing:
- `python -m py_compile generate_tiles_pb.py` exits 0.
- `python generate_tiles_pb.py --help` lists all new flags.
- No new top-level imports beyond stdlib.
- Diff is minimal — no reflowing of untouched code.

Apply the §7 token-budget rule: at ≥88% session usage, stop, write a handoff file, commit it, and exit.
````

### 8.2 Prompt — Author the GitHub Actions workflow (Claude Code recommended)

````
You are working on the repo at /home/joe/lovmaps on branch 001/initial_setup_claude. Read CLAUDE.md and CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md (especially §4.2, §5, §6) before doing anything else. Do not push.

Task: Create `.github/workflows/release-tiles.yml` implementing the workflow described in §4.2 of the plan. Concretely:

1. Triggers: schedule cron '30 4 * * *' AND workflow_dispatch with a boolean input `push_release` (default true).
2. Single job, ubuntu-latest, timeout 60 min, concurrency group `release-tiles` with cancel-in-progress=false.
3. permissions: contents: write — nothing else.
4. Pin every third-party action to a full commit SHA with a trailing version comment.
5. Install osmium-tool and tippecanoe. Determine whether tippecanoe is available via apt on ubuntu-latest; if not, build from the official felt/tippecanoe source release and cache the binary keyed on the source release tag.
6. Compute DATE once as SS-MM-HH-DD-MM-YYYY (UTC) and export via $GITHUB_ENV and step outputs. (If §3 D3 has been changed by the human reviewer, follow that.)
7. Download the source PBF from https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf (the canonical SOURCE_PBF_URL — see §1.1) to `${RUNNER_TEMP}/us-northeast-latest.osm.pbf` with retry/backoff (3 tries, exponential). Verify against the Geofabrik-published .md5 sibling URL. Fail on mismatch. The PBF must land in ephemeral runner storage (`$RUNNER_TEMP` or `/tmp`) — never the repo workspace root, never committed.
8. Run: `python generate_tiles_pb.py --base-name philly_commute_region --date "$DATE" --output-dir ./out --source-pbf "${RUNNER_TEMP}/us-northeast-latest.osm.pbf"`. Passing `--source-pbf` explicitly suppresses the script's built-in download path; the workflow owns the network IO and md5 verification.
9. Sanity-check final .pmtiles is > 100 KB.
10. Generate sha256 sidecars matching `sha256sum` format (so `sha256sum -c file.sha256` works).
11. If a release with tag `tiles-${DATE}` already exists, fail with a clear message — never overwrite dated assets.
12. `gh release create tiles-${DATE}` with versioned assets and a body containing: source URL, source md5, bbox, git SHA, workflow run URL.
13. Ensure a release with tag `current` exists (create if missing). Upload `philly_commute_region_current.pmtiles`, `.pmtiles.sha256`, plus the matching `.osm.pbf` pair if D1 includes PBFs, using `gh release upload --clobber`.
14. Prune dated releases beyond the most recent 30 with `gh release delete` (do NOT delete the `current` release).
15. Upload a `philly_commute_region_${DATE}.run.txt` traceability blob to the dated release.
16. Honor `inputs.push_release == false` by skipping all upload steps but still building (useful for testing).
17. Use only the GITHUB_TOKEN provided to gh; do not add any new secrets.

Self-check before finishing:
- `actionlint` (or `gh actions-linter`) passes on the new file (run via `npx actionlint` or similar, or document that you visually verified syntax).
- All action references are pinned to a 40-character SHA.
- No `set-output` deprecated syntax — use `$GITHUB_OUTPUT`.
- No `secrets.` references other than `GITHUB_TOKEN`.

Commit on the _claude branch with a clear message. Do not push.

Apply the §7 token-budget rule.
````

### 8.3 Prompt — Security setup checklist + repo metadata (Claude Code recommended)

````
You are working on the repo at /home/joe/lovmaps on branch 001/initial_setup_claude. Read CLAUDE.md and CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md (especially §6) before doing anything else. Do not push.

Task: Produce three deliverables, all committed locally:

1. `SECURITY_SETUP.md` at repo root — a copy-pasteable checklist for @jos-eph to enable, in the GitHub UI, every setting recommended in §6 of the plan. For each item, give the exact UI path (Settings → ... → ...) and the value to set. Group by: Branch protection, Actions settings, Repository hardening, Release hygiene. Include the rationale for each setting in one sentence.

2. `SECURITY.md` at repo root — a minimal responsible-disclosure document. Disclosure contact: the email in CLAUDE.md's userEmail field. No bug bounty offered. State supported versions ("the latest release only"). Keep under 40 lines.

3. `ATTRIBUTION.txt` at repo root, per §4.3 of the plan. Required:
   - First line(s) must reproduce the README's "Copyright and License" map-data notice verbatim: `Map data (.pbf and .pmtiles files) © OpenStreetMap contributors © Protomaps, available under the ODbL (Open Database License) 1.0.`
   - Link `https://www.openstreetmap.org/copyright`.
   - The full ODbL 1.0 license text. Default approach: inline it directly from `MAP-DATA-LICENSE.md` so a consumer reading only `ATTRIBUTION.txt` has the complete license. If you instead choose the "reference" form (pointer to `MAP-DATA-LICENSE.md`), update the workflow in 4.2 to also upload `MAP-DATA-LICENSE.md` to every release.
   - A statement that the attribution and license apply to the `.pbf` and `.pmtiles` assets in the release.
   - A "keep in sync with README" reminder comment at the top.
   - Cross-reference `LICENSE.md` for non-data files.

Self-check before finishing:
- All three files render correctly as markdown / plain text.
- SECURITY_SETUP.md has no broken internal links.
- No secrets or PII beyond the disclosure email already in CLAUDE.md.

Commit on the _claude branch with a clear message. Do not push.

Apply the §7 token-budget rule.
````

### 8.4 Prompt — README "Downloads" section + AGENT_OPERATING_RULES (Claude Code or aider)

````
You are working on the repo at /home/joe/lovmaps on branch 001/initial_setup_claude. Read CLAUDE.md and CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md (especially §4.4, §4.5, §7) before doing anything else. Do not push.

Task:

1. Append a `## Downloads` section to README.md documenting:
   - the stable `current` URLs for .pmtiles and .pmtiles.sha256;
   - the URL pattern for dated releases (tiles-<DATE>);
   - how to verify with `sha256sum -c`;
   - the file naming convention and what <DATE> means;
   - a one-line attribution pointer to ATTRIBUTION.txt and MAP-DATA-LICENSE.md.

2. Create CLAUDE_REFERENCE/AGENT_OPERATING_RULES.md containing the rules in §7 of the plan verbatim, so any future agent inherits them without needing to re-read the full plan.

Self-check before finishing:
- README still renders cleanly.
- No URLs beyond github.com/jos-eph/lovmaps/... or openstreetmap.org.

Commit on the _claude branch with a clear message. Do not push.

Apply the §7 token-budget rule.
````

### 8.5 Prompt — Dry-run validation (Claude Code only; do **not** delegate)

````
You are working on the repo at /home/joe/lovmaps on branch 001/initial_setup_claude. Read CLAUDE.md and CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md before doing anything else. Do not push.

Task: Without consuming GitHub Actions minutes, validate the workflow:

1. Run `python -m py_compile generate_tiles_pb.py`.
2. Run `python generate_tiles_pb.py --help` and confirm all new flags are present.
3. Lint the workflow file with actionlint (install via `go install github.com/rhysd/actionlint/cmd/actionlint@latest` if not present; otherwise document that lint was skipped).
4. Statically verify every `uses:` line in the workflow references a 40-character SHA.
5. Verify the workflow's `permissions:` block grants only `contents: write`.
6. Verify there are no references to `secrets.` other than `GITHUB_TOKEN`.
7. Write a short report `CLAUDE_REFERENCE/VALIDATION_REPORT.md` summarising findings.

Do NOT trigger the workflow. Do NOT push.

Commit on the _claude branch. Apply the §7 token-budget rule.
````

---

## 9. Order of operations for `@jos-eph`

1. **Review this plan.** Resolve every 🟡 DECISION row in §3.
2. **Hand off Prompt 8.1** (refactor script) to Claude Code. Review the diff. If acceptable, you push the branch and open a PR to `main`.
3. **Hand off Prompt 8.2** (workflow). Review. Push, open PR.
4. **Hand off Prompts 8.3 and 8.4** (security docs, README). Push, open PR(s).
5. **Hand off Prompt 8.5** (dry-run validation). Apply any fixes flagged.
6. **Apply `SECURITY_SETUP.md` checklist in the GitHub UI yourself** — these are settings, not code.
7. **Trigger the workflow manually once** via `workflow_dispatch` with `push_release=true` to verify the end-to-end pipeline. Inspect the first dated release and the `current` release.
8. **Enable the schedule** (it's enabled by default once the workflow lands on `main`; nothing to do beyond merging).

---

## 10. Out of scope for this plan

* Style / cartography changes to the tiles.
* Adding new regions beyond Philadelphia.
* Migrating off Geofabrik to a different OSM source.
* Hosting tiles anywhere other than GitHub Releases.
* Cross-region or matrix builds.

Any of the above is a separate plan.

---

## 11. Open questions for `@jos-eph` beyond §3

* Do you want the daily workflow active immediately on merge, or paused (`if: false`) until you flip a switch? Default assumption: **active immediately**.
* Should the workflow have an opt-out kill switch (a repo variable, e.g. `RELEASE_PIPELINE_ENABLED`) the human can toggle without editing the file? Recommended: **yes**, single `vars.RELEASE_PIPELINE_ENABLED == 'true'` gate on the publish step.
* Any consumers already pinning to specific URLs we should be careful not to break? If yes, document them so the agent doesn't accidentally rename.
