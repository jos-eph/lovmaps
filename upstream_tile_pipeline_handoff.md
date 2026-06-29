# Upstream Tile-Pipeline Handoff — Make Philadelphia-region admin boundaries render as LINES

**Audience:** an Opus run operating *inside the tile-generation repository* (the
tippecanoe/PMTiles pipeline), which has **none** of the downstream Flutter
context. This file is deliberately self-contained.

**Supersedes / corrects:** Appendix B of
`CLAUDE_REFERENCE/purple_tile_boxes_rootcause_and_fix.md` in the downstream app
repo. That appendix's central premise is **wrong for Philadelphia** (see
"The correction" below). Treat *this* file as the source of truth for the
pipeline work.

---

## TL;DR

1. The downstream vector map showed a purple **box around every tile** (a grid).
   Root cause: the archive's `boundary` source-layer ships **polygon admin-AREA**
   features alongside the real boundary LineStrings; downstream `type:line` layers
   stroked the clipped polygons into per-tile boxes.
2. Downstream shipped a **style-only** fix (restrict boundary layers to
   `geometry-type == LineString`) + thicker lines. **The boxes are gone.**
3. **But render verification proved a *render* gap, and data-absence is the likely
   cause:** at the Philadelphia ↔ Delaware-County border the LineString filter
   leaves **no boundary pixels**, so Philadelphia's own outline almost certainly
   exists in the archive **only as a polygon area, with no boundary LineString for
   it.** The filter then removes the boxes *and* leaves that border **invisible** —
   and you cannot thicken or recolor a line that was never tiled. (Strictly, the
   render only proved pixels are missing; the decisive *data*-level confirmation is
   Acceptance test #1 below — decode the **western** Cobbs Creek tile and check for a
   Phila-perimeter LineString. Do that before committing pipeline effort.)
4. **This repo's job:** regenerate the archive so the `boundary` layer ships admin
   **LINES** (state/county/municipal), **including Philadelphia's outline**. Then
   the downstream style already in place will render it correctly.

---

## The correction (why Appendix B was wrong)

Appendix B assumed: *"the boundary layer ships both admin lines and area polygons;
drop the polygons and you keep the real boundary lines."* That is true for New
Jersey municipalities but **false for Philadelphia**.

**Decisive empirical finding (do not re-derive — it cost real effort downstream):**
the downstream Linux app was driven to specific map centers and screenshotted, and
boundary pixels were detected by matching the theme's boundary colour `#CC79A7`:

| Map center | boundary (`#CC79A7`) pixels rendered |
|---|---|
| Cobbs Creek — the Philadelphia ↔ Delaware-County border (~39.95, −75.255) | **0** |
| Delaware River / New Jersey side (~39.965, −75.125) | **~1277** |

The asymmetry is in the **data**, not the style: NJ admin boundaries are tiled as
LineStrings (they render); Philadelphia's outline is tiled as a **Polygon only**
(nothing renders once polygons are filtered). The faint line a human "sees" at the
Cobbs Creek edge is just the **white road** (Cobbs Creek Pkwy) that follows the
boundary — not a boundary feature.

**Tension you must resolve in the source data — counts lie.** An earlier
hand-decode of the *archive* reported `admin_level 6` had **348 LineStrings** and
claimed "a county/city LineString survives at the city edge" (simulated tile
`z13/2386/3103`). The render contradicts that for the Phila/Delco segment. The tile
geography resolves the apparent conflict: at z13, x=2386 ≈ lon −75.15 — the **eastern
Delaware-River edge** (PA/NJ side, where LineStrings genuinely exist), **not** the
western Cobbs Creek border (≈ −75.255 ≈ x≈2383). So that "surviving line at the city
edge" was almost certainly a **river-side** line mislabeled "county/city," fully
consistent with the *western* Phila/Delco edge having no line. The 348 are almost
certainly **other** county borders (Delaware/Chester/Montgomery county lines, NJ
counties), **not** the Philadelphia perimeter. **Do not trust aggregate geometry
tallies. Verify the Philadelphia outline specifically.**

---

## Archive facts (measured by decoding the on-disk PMTiles — preserve, do not re-decode)

- **Archive:** `philly_commute_region_current.pmtiles`, PMTiles v3, MVT tiles,
  gzip-compressed, **zoom 10–13**, ~31 MB. bbox `−76.069,39.537 → −74.544,40.443`.
  Source: OpenStreetMap via Geofabrik (ODbL).
- **Generator:** `tippecanoe v2.79.0`, writing PMTiles directly.
- **Inputs:** one GeoJSONSeq per layer, e.g. `-Lboundary:out/..._boundary.geojsonseq`
  (also `transportation`, `transportation_name`, `water`, `landcover`, `landuse`,
  `place`, `poi`). An OSM→GeoJSON step upstream produces each layer's features;
  tippecanoe just tiles them.
- **Flags:** `-Z10 -z13 --drop-densest-as-needed --coalesce
  --simplify-only-low-zooms --detect-shared-borders --maximum-tile-bytes=200000`.
- **Generation-time `-j` boundary filter (admin_level ONLY — never geometry type):**
  ```json
  "boundary": ["any",
    ["==", "admin_level", 4],
    ["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
    ["all", [">=", "$zoom", 8], ["==", "admin_level", 8]]]
  ```
- **`boundary` source-layer fields:** `admin_level` (**Number**) only. No
  `maritime` / `disputed`, so the polygons are genuine admin areas, not maritime
  fills.
- **Geometry tally in the archive `boundary` layer:**

  | `admin_level` | LineString | Polygon | full-tile-rectangle polygons |
  |--------------:|-----------:|--------:|-----------------------------:|
  | 4 (state)     | 9          | 6       | 0  |
  | 6 (county)    | 348        | 242     | 55 |
  | 8 (city/muni) | 1049       | 1794    | 38 |

- **Full-tile rectangle ring example** (the box-grid culprit): a polygon clipped to
  a tile interior becomes `(4176,−80)(4176,4176)(−80,4176)(−80,−80)` — tile extent
  `4096` plus an `80`-unit buffer, i.e. the whole tile.
- **`strategies` block:** heavy dropping at mid/high zoom (`dropped_as_needed`
  ≈ 95k / 387k / 210k across z11/z12/z13, plus thousands of `tiny_polygons`). A
  large share is admin-area **polygon fill** competing with roads for the 200 KB
  budget — bytes spent producing the artifact you don't want. Removing the area
  polygons also **shrinks the archive**.

---

## The actual defect in this pipeline

1. The `-j` boundary filter selects on `admin_level` only; it **never restricts
   geometry type**. Anything carrying `admin_level` passes.
2. The OSM→GeoJSON step emits admin **AREA polygons** (prime suspect: the GDAL/OGR
   OSM driver's `multipolygons` layer) — both boundary lines *and* closed area
   polygons carry `admin_level`.
3. For most municipalities you also get a line; for **Philadelphia** (a
   consolidated city-county) the render evidence says you get **only the polygon**.
   So filtering polygons out (downstream, or here) deletes Philadelphia's border.

---

## Goal, restated for the pipeline

1. `boundary` layer = admin boundary **LineStrings only** (`admin_level` 4 = state,
   6 = county, 8 = municipality). **Never** area polygons.
2. **Acceptance test:** Philadelphia's consolidated city-county outline MUST be
   present as a LineString (`admin_level` 6 and/or 8) in the output tiles.
3. `admin_level` stays a **Number**.
4. Boundaries survive tile-budget dropping so they stay visible.

---

## Recommended changes (priority order — hedged; verify against the real repo)

### P1 — preferred & durable: source admin boundaries as LINES from OSM, not areas
OSM admin boundaries are **relations** (`boundary=administrative`) whose members are
**ways** (linework). That is the canonical line source.
- **GDAL/OGR OSM driver** on the `.osm.pbf`: build the boundary layer from the
  **`multilinestrings`** layer — it assembles `boundary=administrative` *relations*
  into linestrings **and carries the relation's `admin_level`**, which is exactly
  what the `-j` filter needs. **Do not** use `multipolygons` (the area twin), and be
  wary of the flat **`lines`** layer: `admin_level` is a tag on the *relation*, not
  on the member ways, so many boundary ways there are untagged — filtering `lines`
  by `boundary=administrative` silently drops them and loses `admin_level`.
- **osmium:** `osmium tags-filter` to `boundary=administrative` (+ `admin_level`),
  then export relation member ways merged into linestrings; do **not** export the
  closed area geometry.
- **Critical:** confirm **Philadelphia's** boundary relation member ways survive
  the filter — it is one large relation; its perimeter ways are the line you need.

### P1-alt — if you must keep area polygons as the source: derive lines from them
Guarantees Philadelphia gets a line even if your OSM extract lacks the linework:
- Convert polygon rings → LineStrings: PostGIS `ST_Boundary` / `ST_ExteriorRing`;
  or `ogr2ogr -nlt MULTILINESTRING` with a `ST_Boundary` SQL; or `mapshaper -lines`;
  or turf `polygonToLine`.
- Then de-duplicate coincident borders (a shared border appears once per area).

### P2 — geometry guard at the tippecanoe `-j` (defense in depth)
```json
"boundary": ["all",
  ["==", "$type", "LineString"],
  ["any",
    ["==", "admin_level", 4],
    ["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
    ["all", [">=", "$zoom", 8], ["==", "admin_level", 8]]]]
```
**Verify `$type` is honoured by `-j` on tippecanoe 2.79.0** (tile a tiny
mixed-geometry sample and inspect output). If not, rely on P1/P3.

### P3 — guaranteed, tool-agnostic: jq-prefilter the GeoJSONSeq to lines
```bash
jq -c 'select(.geometry.type=="LineString" or .geometry.type=="MultiLineString")' \
  out/..._boundary.geojsonseq > out/..._boundary.lines.geojsonseq
# (add `jq --seq` if the file uses the RFC 8142 0x1e record separator)
```
**WARNING — order matters.** P3 only *drops* polygons; it does **not** create
lines. Running it on the **current** (Philadelphia-area-is-polygon-only) data would
**delete the Philadelphia border**. P3 is a *guard to run after* P1/P1-alt has
produced lines for every wanted boundary — **not** a standalone fix.

### P4 — protect boundaries from being dropped
Per-feature, in the GeoJSON properties:
```json
"tippecanoe": { "minzoom": 10, "maxzoom": 13 }
```
`tippecanoe.minzoom` forces the feature kept from that zoom up. `--coalesce` only
merges identical-attribute features (won't blur distinct admin levels);
`--simplify-only-low-zooms` already keeps z13 (the max) full-detail.

### P5 — de-duplicate coincident borders (low priority)
A consolidated city-county (Philadelphia) makes its perimeter both `admin_level 6`
and `8`, and shared borders appear once per area → some segments drawn 2–4×. Dedupe
in the extraction or keep the lowest `admin_level` for overlapping segments.

### P6 — decide municipal detail (preference)
`admin_level 8` is *every* PA/NJ township/borough (~1049 line features) — great for
"boundaries everywhere," busy if you only want Philadelphia's outline. Restrict or
drop `8` if you want less clutter.

---

## Acceptance / verification — MUST do (counts lie)

1. **Decode an output tile** covering the Philadelphia ↔ Delaware-County edge
   (Cobbs Creek; ~lat 39.95, lon −75.255; z13). Confirm at least one
   `admin_level` 6 or 8 feature of geometry type **LineString** whose coordinates
   trace the city perimeter — **not** a full-tile rectangle. Tools:
   `tippecanoe-decode`, `mapbox_vector_tile`, or a stdlib MVT parser.
2. Confirm **no Polygon features remain** in the `boundary` layer (or none carrying
   `admin_level`), so the downstream box grid cannot recur.
3. Confirm `admin_level` is still a JSON **Number** (a string `"6"` silently matches
   nothing in both the `-j` and the downstream style).
4. **End-to-end (optional):** the downstream app re-fetches
   `philly_commute_region_current.pmtiles` + its `.sha256` (stable filename) from
   GitHub Releases; the cached copy invalidates on hash change. A render-check of
   the Nightshift theme over the Phila/Delco edge should now show a mauve
   (`#CC79A7`) boundary line where there was none.

---

## Downstream interactions (keep in mind; do not undo)

- The downstream Flutter style **also** filters every `boundary-admin-*` layer to
  `geometry-type == LineString`. **Keep that permanently** as belt-and-suspenders —
  it guarantees the box grid can't return if a future archive regression slips a
  polygon back into `boundary`. Consider a parallel CI check in *this* repo that
  asserts the `boundary` GeoJSONSeq contains no `Polygon`/`MultiPolygon` before
  tiling.
- **Publishing:** a new tiling run must update **both** the archive and its
  `.sha256` companion; the stable filename stays `philly_commute_region_current.pmtiles`.
- The downstream rendered-tile cache is keyed by the app's style `metadata.version`
  (a downstream concern, not this repo's) — no action needed here.

---

## How the empirical finding was produced (method note — NOT reproducible in this repo)

The render verification ran the downstream **Linux Flutter app** (there is no app
in the tiling repo). Method, recorded so you understand the provenance of the
correction:
- Temporarily set the map **initial center** to the target border in the app.
- Select the theme by editing `shared_preferences.json` (`selectedVectorStyle`)
  — e.g. `style.nightshift.json` for the high-contrast `#CC79A7` boundary on a dark
  canvas.
- Screenshot via `xwininfo` + ImageMagick `import` (a compositor was active, so the
  window content was grabbed through the covering terminal).
- Detect boundary pixels by matching `#CC79A7` / channel math (`R−G>22 & B−G>4 &
  R>B`), excluding the purple app-bars.
- The 0-vs-~1277 mauve-pixel asymmetry is the proof the gap is in the **data**.

You can't run that here. Use **tile decoding** (above) for acceptance instead.
