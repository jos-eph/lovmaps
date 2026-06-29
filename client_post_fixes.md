# Client (downstream) post-fixes — verification & changes needed

Actions the **downstream tile consumer** (the Flutter app / MapLibre style) must
take after the pipeline change in PR
[#3](https://github.com/jos-eph/lovmaps/pull/3) (boundary LineStrings + county
labels) ships. None of these are changes to *this* (tile-pipeline) repo — they
live in the consuming app and must be done there.

Source of truth for the pipeline side: `boundary_fix_chunked_spec.md` and
`upstream_tile_pipeline_handoff.md`.

---

## 1. Add a county label layer (REQUIRED for the new feature to render)

The pipeline now emits county name labels into the **`place`** source-layer as:

```
properties: { "class": "county", "name": "<County Name>" }   geometry: Point
```

These ship in the tiles but **will not appear** until the downstream style adds a
symbol/label layer targeting them.

- **Change:** add a MapLibre `symbol` layer with `source-layer: "place"` and
  filter `["==", ["get", "class"], "county"]`, rendering `text-field` from
  `name`. Style distinctly from city/town/suburb/neighbourhood labels (counties
  are larger-area, lower-priority labels).
- **Verify:** at the app's zoom range (tiles are z10–z13) county names render,
  one per county, the label sitting inside its county. Eyeball **Philadelphia**
  plus one neighboring county (e.g. Delaware, Montgomery).
- **Note:** the label point is an area-weighted centroid. If a county label
  visibly lands outside its (concave) county, file it back upstream — the
  pipeline fix is to switch that feature to a point-on-surface.

## 2. Keep the boundary `geometry-type == LineString` filter (REQUIRED — do not remove)

The downstream style already filters every `boundary-admin-*` layer to
`geometry-type == LineString`. **Keep it permanently** as belt-and-suspenders.
The pipeline now also guarantees lines-only (conversion + `$type` guard +
pre-tiling assertion), but retaining the downstream filter means a future archive
regression that slips a polygon back in still cannot stroke the per-tile box grid.

- **Verify:** the purple per-tile box grid does **not** appear anywhere.

## 3. Confirm the Philadelphia boundary now renders (REQUIRED)

This is the headline fix. Before, the Philadelphia ↔ Delaware-County (Cobbs Creek)
border had **0** boundary pixels.

- **Verify:** center the map on the Cobbs Creek edge (~lat 39.95, lon −75.255)
  and confirm a boundary line is now drawn there. With the Nightshift theme this
  is the mauve `#CC79A7` boundary colour on the dark canvas where there were none.
- Do **not** mistake the white road (Cobbs Creek Pkwy, which follows the border)
  for the boundary line — confirm the boundary-coloured pixels specifically.

## 4. Invalidate / re-fetch the tile archive (REQUIRED)

The stable filename is unchanged: `philly_commute_region_current.pmtiles` (+
`.sha256`). The new build changes the file's hash.

- **Verify:** the app re-fetches the archive and the cached copy invalidates on
  hash change (compare the served `.sha256` to the cached one). Confirm the app is
  actually reading the **new** archive before judging items 1–3.

## 5. Bump the rendered-tile cache key if the style changed (REQUIRED if §1 done)

The downstream rendered-tile cache is keyed by the style `metadata.version`.
Adding the county label layer (§1) is a style change.

- **Change:** bump `metadata.version` in the downstream style so previously
  rendered tiles (without county labels) are not served from cache.
- **Verify:** county labels appear on fresh app state, not only after a manual
  cache clear.

---

## Quick checklist

- [ ] County label layer added to downstream style (`place`, `class=="county"`)
- [ ] Boundary `geometry-type == LineString` filter retained
- [ ] Philadelphia / Cobbs Creek boundary line renders (no 0-pixel gap)
- [ ] No per-tile purple box grid
- [ ] App re-fetched the new archive (hash changed; cache invalidated)
- [ ] Style `metadata.version` bumped (rendered-tile cache busted)
</content>
