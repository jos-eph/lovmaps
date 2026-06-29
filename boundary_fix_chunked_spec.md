# Chunked Spec — Fix missing Philadelphia admin boundary + add county name labels

**Status:** ready to execute
**Owner of merges/pushes-to-main:** `@jos-eph` (human)
**Source analysis:** `upstream_tile_pipeline_handoff.md` (read it; this spec operationalizes it)
**Pipeline under change:** `generate_tiles_pb.py` and `.github/workflows/release-tiles.yml`

This document is **self-contained**. Each chunk is independently reviewable and
labeled **[AI]** (a coding agent may do it, including committing and pushing) or
**[HUMAN]** (`@jos-eph` only). The hard rule that drove the split:

> **An AI may write/commit/push code and YAML. An AI may NOT observe a deploy
> (a CI build run, its logs, its artifacts, or a downstream render) and iterate
> from what it sees. Observing deploys is a HUMAN step.** When a build needs
> watching, the AI stops and hands off; the human runs/observes and reports
> findings back in text; only then does the AI resume.

---

## 0. Root cause (one paragraph, self-contained)

The `boundary` source-layer ships admin **area polygons** as well as boundary
**LineStrings**. Downstream now filters boundary layers to `LineString` only (to
kill a per-tile "purple box grid" caused by stroking clipped polygons). For most
municipalities a real LineString exists, so they still render. **Philadelphia is
a consolidated city-county whose perimeter exists in the tiled data only as a
Polygon** (its boundary is an OSM *relation*; `admin_level` lives on the relation,
and `osmium export` assembles it into an area, while the member ways carry no
`admin_level` and are dropped by `normalize_boundary`). Result: filtering polygons
removes the box grid **and** Philadelphia's outline. **Fix: make the pipeline emit
admin boundary LINES (including Philadelphia's), so the downstream LineString
filter renders them.**

---

## 1. Goals & acceptance criteria

**G1.** `boundary` layer contains admin boundary **LineStrings only** — no
`Polygon`/`MultiPolygon` features (and none carrying `admin_level`).
**G2.** **Philadelphia's** consolidated city-county outline is present as a
LineString at `admin_level` 6 and/or 8 in the output tiles (the decisive test —
counts lie; verify the perimeter specifically).
**G3.** `admin_level` stays a JSON **Number** (a string `"6"` matches nothing in
both the `-j` filter and the downstream style).
**G4.** Boundaries survive tile-budget dropping (stay visible at z10–z13).
**G5.** **County names render as a geographic label** (new feature requested by
`@jos-eph`).
**G6.** No GitHub-cost regressions; no large files committed; branch/push rules
honored (§2).

Acceptance tests live in **Chunk H** (human-run, post-build).

---

## 2. Roles, branch & push discipline (read before any commit)

- **Branch:** all AI work goes on a branch whose name ends in **`_agent_permitted`**
  (CLAUDE.md branch-safety rule). If absent, the AI creates one, e.g.
  `004/fix-boundary_agent_permitted`, branched from the current work branch.
- **Push:** for this task the human owner has explicitly authorized the AI to
  **commit and push** the feature branch. The AI **still must not**: push to
  `main`, merge, delete branches, or amend commits already on origin.
- **Merge to `main`:** **[HUMAN]** only, via reviewed PR.
- **Deploy observation:** **[HUMAN]** only (see banner above).
- **Cost:** never add a trigger that can fire repeatedly without rate-limiting;
  prefer `workflow_dispatch` smoke tests over scheduled trial runs; never commit
  `.osm.pbf` / `.pmtiles` / `.geojsonseq` or any file > 5 MB.
- **No new runtime dependencies** without flagging. The boundary + county-label
  work below is achievable in **pure Python stdlib** (already imported: `json`,
  `math` can be added) — keep it that way so CI needs nothing new.

---

## 3. Design decisions (why the chosen mechanism)

The handoff lists options P1 (re-source lines from OSM via GDAL/osmium), P1-alt
(derive lines from polygons), P2 (`$type` guard in `-j`), P3 (jq line prefilter),
P4 (anti-drop), P5 (dedupe), P6 (municipal detail). This repo uses **osmium**,
not GDAL, and post-processes each layer in Python (`normalize_geojsonseq`). The
spec therefore implements:

- **Primary mechanism = P1-alt done in the existing Python normalizer:** convert
  every admin **Polygon/MultiPolygon ring into LineString features** (pure-Python
  ring extraction), and keep genuine boundary LineStrings. This **guarantees G2**
  (Philadelphia gets a line) regardless of osmium's exact relation handling, adds
  **no dependencies**, and runs before tiling, so we extract true admin perimeters
  — never the tile-clipped rectangles that caused the box grid.
  - *Rejected as primary:* P1 (osmium `export` geometry-config / `multilinestrings`
    re-sourcing) — more fragile to get exactly right and harder to unit-test; it is
    recorded as a future hardening option in Chunk A notes, not a blocker.
  - *Rejected as primary:* P3 jq prefilter — only **drops** polygons; on today's
    data that would **delete** Philadelphia. Kept only as the post-conversion guard
    in Chunk C.
- **P2 `$type` guard** in the `-j` boundary filter — belt-and-suspenders (Chunk B).
- **P4 anti-drop** via per-feature `tippecanoe.minzoom` (Chunk B).
- **Pre-tiling assertion** that the boundary GeoJSONSeq has no polygons (Chunk C).
- **P5 dedupe = out of scope** (low priority; double-stroking a 2 px line over the
  identical path is visually harmless and lines are byte-cheap). Noted, not done.
- **County labels** = new Chunk D.

---

# CHUNKS

> Execute in order. AI chunks A–F can be done in one agent session and pushed
> together, or as small per-chunk commits (preferred for review). After Chunk F
> the AI **stops** and hands to the human for Chunks G–J. Chunk K is conditional
> re-work driven only by the human's written findings.

---

## Chunk A — [AI] Convert admin polygons → boundary LineStrings

**File:** `generate_tiles_pb.py`

**What:** Make the boundary layer emit LineStrings for every admin area, so
Philadelphia's perimeter becomes a line.

**Implementation:**
1. Add a pure-Python helper that, given a GeoJSON geometry, yields one or more
   **LineString** geometries:
   - `LineString` / `MultiLineString` → pass through (one feature per part).
   - `Polygon` → one LineString per ring (exterior + any holes); each ring is
     already a closed coordinate array, so the ring **is** its boundary line.
   - `MultiPolygon` → the above for every polygon.
   - Anything else (Point, null) → yield nothing.
2. Rework boundary handling so the boundary layer's normalized output:
   - keeps `admin_level` as an **int** in `{4,6,8}` (reuse existing
     `normalize_boundary` logic — keep `admin_level` a **Number**, G3);
   - **explodes each kept feature into one feature per LineString** produced by the
     helper, each carrying `{"admin_level": <int>}`;
   - emits **no Polygon/MultiPolygon** features at all.
   - Because `normalize_geojsonseq` is currently 1-feature-in→0-or-1-out, either
     (a) generalize it so a normalizer may return a **list** of `(props, geometry)`
     and write each, or (b) add a dedicated `normalize_boundary_geojsonseq` path.
     Prefer (a) — minimal, and reusable — but keep the change confined to the
     boundary path's behavior; do not alter other layers' output.
3. **Anti-drop (P4):** attach `"tippecanoe": {"minzoom": 10, "maxzoom": 13}` as a
   **top-level member of each emitted boundary Feature** (sibling of `properties`/
   `geometry`, where tippecanoe reads it — *not* inside `properties`). This forces
   boundaries to be kept from z10 up despite `--drop-densest-as-needed`.

**Notes / guardrails:**
- Do **not** touch other layers' normalization (transportation, water, etc.).
- This is intentionally a **map-content** change (boundary geometry), which the
  generic scope rule in `AGENT_OPERATING_RULES.md §7.3` would normally forbid; it
  is **explicitly authorized by `@jos-eph`** for this task. Note that in the commit
  message.
- Future hardening (do **not** do now, record only): re-source boundaries as lines
  directly from OSM (handoff P1) to drop reliance on polygon→line conversion.

**Done when:** code compiles (`python -m py_compile generate_tiles_pb.py`) and the
boundary path provably yields only LineString features (covered by Chunk E tests).

---

## Chunk B — [AI] `$type` guard + keep anti-drop in the tippecanoe `-j`

**File:** `generate_tiles_pb.py` (the `ZOOM_FILTERS["boundary"]` block)

**What:** Defense-in-depth so a future polygon regression can't re-create the box
grid even before it reaches downstream.

**Implementation:** wrap the existing admin_level filter with a geometry guard:
```json
"boundary": ["all",
  ["==", "$type", "LineString"],
  ["any",
    ["==", "admin_level", 4],
    ["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
    ["all", [">=", "$zoom", 8], ["==", "admin_level", 8]]]]
```
**Caveat to record in a code comment:** whether `-j` honors `$type` on tippecanoe
2.79.0 is **verified by the human smoke test (Chunk H), not by the AI.** Even if
`$type` were ignored, correctness still holds because Chunk A already emits
lines-only and Chunk C asserts it — this guard is belt-and-suspenders.

**Done when:** the filter JSON is updated and still valid JSON
(`python -c "import json,…"` or the existing `json.dumps` path runs clean).

---

## Chunk C — [AI] Pre-tiling assertion: no polygons in the boundary layer

**Files:** `generate_tiles_pb.py` (and optionally a tiny check the workflow calls)

**What:** Fail fast (before/at tiling) if the normalized boundary GeoJSONSeq
contains any `Polygon`/`MultiPolygon`, or any feature whose `admin_level` is not a
Number. This is the handoff's "parallel CI check" and the safe form of P3 (assert,
don't blindly drop).

**Implementation:**
- Add a function that scans the **normalized** boundary `.geojsonseq` and exits
  non-zero with a clear message if it finds a polygon feature or a non-numeric
  `admin_level`. Call it right after the boundary layer is normalized and before
  `generate_pmtiles`.
- Keep it pure-Python/stdlib. No jq dependency (jq isn't guaranteed on the runner;
  it isn't installed locally either).
- It must be cheap (streaming line read) so it adds negligible CI time/cost.

**Done when:** running the generator on a fixture with a stray polygon aborts with a
non-zero exit and a descriptive `::error::`-style message; the all-lines fixture
passes. (Exercised by Chunk E.)

---

## Chunk D — [AI] County names as a geographic label (G5)

**File:** `generate_tiles_pb.py`

**What:** Emit a **label point per county** carrying the county name, into a layer
the downstream style can render.

**Design:**
- Source the name + geometry from the **`admin_level == 6`** features in the
  **raw** boundary GeoJSONSeq (raw still has `name`; `normalize_boundary` strips
  it). For each level-6 Polygon/MultiPolygon, compute a representative interior
  **label point** and emit a feature for the **`place`** layer with
  `{"class": "county", "name": <county name>}` and geometry `Point`.
- **Label point:** use the standard **area-weighted polygon centroid** of the
  largest ring (pure-Python; add `import math` only if needed — centroid needs no
  math import). Centroid is adequate for county-sized, roughly-convex polygons.
  *Refinement (optional, note only):* a point-on-surface guarantees the point lies
  inside concave shapes; skip unless a county label visibly lands outside its
  county in the human render check.
- **Wiring:** append these county-label features to the **`place`** layer's
  normalized `.geojsonseq` (the place layer is already shipped & rendered
  downstream), so no new source-layer is introduced. Give them a distinct
  `class: "county"` so the downstream style can target them without disturbing
  city/town/suburb/neighbourhood labels.
- **Zoom gating:** add a clause to `ZOOM_FILTERS["place"]` for `class == "county"`
  at an appropriate zoom (recommend visible from `$zoom >= 6` like `town`, or
  whatever reads well — the exact zoom is tunable and confirmed in the human render
  check). Also tag county-label features with `"tippecanoe": {"minzoom": 6}` so they
  aren't dropped.
- Dedupe: a county appears once (one level-6 area per county) — but a consolidated
  city-county (Philadelphia) is both level 6 and 8; ensure **Philadelphia yields
  exactly one county label** (select from level 6 only, as specified).

**Downstream coordination (NOT this repo's code — see §"Downstream note"):** the
downstream MapLibre style must add a symbol/label layer for `place` features with
`class == "county"`. Flag this in the PR description and the handoff so `@jos-eph`
can coordinate the style change; until then the data is present but unstyled.

**Done when:** the generator produces `place` features with `class:"county"` +
`name` + `Point` geometry, one per county (incl. exactly one for Philadelphia),
covered by Chunk E tests.

---

## Chunk E — [AI] Unit tests for the pure-Python helpers (no external tools)

**File:** new `tests/test_geometry_helpers.py` (or `test_generate_tiles_pb.py`)

**What:** Lock in the geometry logic with **stdlib-only** tests that need no
osmium/tippecanoe/network — so the AI can run them locally and CI can too cheaply.

**Cover at least:**
1. Polygon → LineString: a square `Polygon` yields one closed LineString ring;
   a Polygon with a hole yields two rings; a `MultiPolygon` yields rings for each
   part; a `LineString` passes through; a `Point`/null yields nothing.
2. `admin_level` stays an **int** and out-of-set levels are dropped (G3).
3. Every emitted boundary feature has top-level `tippecanoe.minzoom == 10` and
   geometry type `LineString` (G1/G4).
4. The Chunk C assertion: passes on an all-lines fixture; **exits non-zero** on a
   fixture containing a polygon or a string `admin_level`.
5. County label: a known square level-6 polygon named "Test County" yields one
   `place` Point feature `class:"county"`, `name:"Test County"`, with the point
   **inside** the square; a level-6 + level-8 consolidated shape yields exactly one
   county label.

**Run locally:** `python -m pytest -q` (or `python -m unittest`). Use `unittest`
if avoiding a pytest dependency is preferred — **prefer `unittest`** (stdlib) so CI
needs nothing new.

**Done when:** all tests pass locally (`python -m unittest`).

---

## Chunk F — [AI] Commit, push branch, open a DRAFT PR (no merge), then STOP

**Steps:**
1. Ensure you are on a branch ending `_agent_permitted` (create from the work
   branch if needed — §2).
2. `python -m py_compile generate_tiles_pb.py` and `python -m unittest` both green.
3. Commit in small logical units (A, B+C, D, E) with descriptive messages; note in
   the A/D commit bodies that map-content scope was **explicitly authorized by
   `@jos-eph`**. End each commit message with the required co-author trailer.
4. **Push the feature branch** (authorized for this task). Do **not** push `main`.
5. Open a **draft** PR into `main` summarizing the change and including:
   - the acceptance criteria (G1–G6) as a checklist for the human to tick after the
     build;
   - the **Downstream note** (county-label styling needs a style change);
   - an explicit line: *"Build/deploy verification pending — to be performed by
     `@jos-eph` per Chunks G–J. The AI has not run or observed any build."*
6. **STOP.** Post a short handoff message pointing the human to Chunks G–J. Do not
   trigger, watch, or poll any workflow run.

**Done when:** branch pushed, draft PR open, AI has handed off and stopped.

---

## Chunk G — [HUMAN] Run a build (smoke test) and observe it

> AI must not perform or watch this.

1. From the GitHub UI: **Actions → Release tiles → Run workflow** on the
   `_agent_permitted` branch with **`push_release = false`** (build-only smoke
   test — no release assets, keeps `RELEASE_PIPELINE_ENABLED` irrelevant, zero
   publishing). This stays within free-tier minutes (job timeout is 60 min).
2. Watch the run. Confirm it reaches **"Sanity-check outputs"** and the new
   **boundary no-polygon assertion (Chunk C)** passes (no `::error::`).
3. Download the build's `.pmtiles` for local decoding (artifact or, if you prefer,
   reproduce locally with `test_script.sh` against a fresh PBF).

**Output of this chunk:** a built `philly_commute_region_<DATE>.pmtiles` to inspect.

---

## Chunk H — [HUMAN] Acceptance tests on the output tiles (counts lie — verify geometry)

> Decode real output tiles. Tools: `tippecanoe-decode`, `mapbox_vector_tile`, or a
> stdlib MVT parser.

1. **Philadelphia outline present as a LINE (G2 — the decisive test).** Decode an
   output tile over the **western** Phila ↔ Delaware-County edge (Cobbs Creek;
   ~lat 39.95, lon −75.255; z13). Confirm at least one `admin_level` 6 or 8
   **LineString** whose coordinates trace the city perimeter — **not** a full-tile
   rectangle `(4176,−80)…`. (Recall the earlier z13/x2386 "surviving line" was the
   *eastern* river edge, not this border — don't be fooled again.)
2. **No polygons remain (G1).** Confirm the `boundary` layer has **zero**
   `Polygon`/`MultiPolygon` features (and none carrying `admin_level`), so the box
   grid cannot recur.
3. **`admin_level` is a Number (G3).** Confirm it decodes as numeric, not `"6"`.
4. **County labels present (G5).** Confirm `place` features with `class:"county"` +
   `name` exist, one per county, point inside the county (eyeball Philadelphia +
   one neighbor).

Record pass/fail per test in the PR checklist.

---

## Chunk I — [HUMAN, optional] End-to-end downstream render check

> Optional belt-and-suspenders; uses the downstream Flutter app (not in this repo).

- Point the downstream app at the new archive (stable filename
  `philly_commute_region_current.pmtiles` + `.sha256`; cache invalidates on hash
  change). Render the Nightshift theme over the Phila/Delco edge — expect a mauve
  (`#CC79A7`) boundary line where there were **0** boundary pixels before.
- County labels will only show once the **downstream style adds a `class==county`
  label layer** (see Downstream note). If not yet styled, the data is present but
  invisible — that's expected, not a pipeline failure.

---

## Chunk J — [HUMAN] Decide: merge, or send findings back

- **All G1–G6 pass:** approve and **merge the PR to `main`** (human-only). Then
  (separately) let the daily pipeline publish, or run `workflow_dispatch` with
  `push_release=true` **after** confirming `RELEASE_PIPELINE_ENABLED == 'true'`.
- **Any test fails:** write the specific findings (which test, what was decoded,
  expected vs actual, tile coords) **in the PR**, and assign back to the AI for
  **Chunk K**. Do not have the AI guess from a screenshot of the run — give it the
  decoded facts in text.

---

## Chunk K — [AI, conditional] Iterate from the human's written findings only

- Triggered **only** by `@jos-eph`'s written findings from Chunk J. The AI reads
  the **text** of the findings — it does **not** open, watch, or re-run the build.
- Make the minimal code/YAML fix, extend Chunk E tests to cover the regression,
  re-run `py_compile` + `unittest`, commit, push the same `_agent_permitted` branch,
  and update the PR. Then **STOP** and hand back to Chunk G/H for re-verification.
- Repeat G→K as needed. The AI never closes the loop on its own observations.

---

## Downstream note (carry into `upstream_tile_pipeline_handoff.md` / PR)

- **Keep** the downstream `geometry-type == LineString` filter on every
  `boundary-admin-*` layer permanently (belt-and-suspenders against a future
  polygon regression). This spec's Chunk B/C add the same guarantee upstream.
- **County labels need a downstream style addition:** a symbol/label layer for
  `place` features where `class == "county"`. Until that lands, the new county-label
  data ships but doesn't render. Coordinate via `@jos-eph`.
- **Publishing invariants unchanged:** a tiling run updates both the archive and its
  `.sha256`; the stable filename stays `philly_commute_region_current.pmtiles`.

---

## Quick chunk index

| Chunk | Who | Summary |
|------:|-----|---------|
| A | AI | Polygon→LineString conversion in boundary normalizer (+ per-feature anti-drop) |
| B | AI | `$type==LineString` guard in tippecanoe `-j` |
| C | AI | Pre-tiling assertion: no polygons / numeric admin_level |
| D | AI | County name label points into `place` layer (`class:"county"`) |
| E | AI | Stdlib unit tests for all new helpers |
| F | AI | Commit + push `_agent_permitted` branch, open draft PR, STOP |
| G | HUMAN | Run build (smoke test) and observe it |
| H | HUMAN | Decode tiles, run acceptance tests G1–G5 |
| I | HUMAN | Optional downstream render check |
| J | HUMAN | Merge, or write findings back |
| K | AI | Fix from human's written findings only; never observes the build |
</content>
</invoke>
