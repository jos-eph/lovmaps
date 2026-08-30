#!/usr/bin/env python3
"""
Generate an OpenMapTiles-schema-compatible pmtiles archive in stages.

Pipeline:
  1. bbox extract       (osmium extract)
  2. per-layer filter   (osmium tags-filter)
  3. per-layer export   (osmium export -> geojsonseq)
  4. normalize          (rename OSM tags to OMT props: class/subclass/...)
  5. combine            (tippecanoe -L<layer>:<file>)

Output pmtiles will have these source-layers, matching style.json:
  transportation, transportation_name, water, landcover, landuse,
  boundary, place, poi

CLI:
  python generate_tiles_pb.py
      [--source-pbf PATH [--source-pbf PATH ...]] [--source-url URL]
      [--bbox MINLON,MINLAT,MAXLON,MAXLAT]
      [--base-name NAME] [--date STR]
      [--output-dir DIR]
      [--keep-intermediates | --no-keep-intermediates]

Source PBF handling:
  - --source-pbf is repeatable. If given, the script uses the path(s)
    directly and performs no network I/O. This is the path used by the CI
    workflow, which owns download / retry / md5 verification.
      - One --source-pbf: bbox-extracted straight to the region PBF
        (back-compatible with the original single-source behavior).
      - Multiple --source-pbf: each is bbox-extracted to its own clip, then
        the clips are combined with `osmium merge` into the region PBF.
        `osmium merge` dedupes objects sharing identical (type, id,
        version), so shared border ways/nodes between adjoining state
        extracts collapse to one copy. CI passes PA + NJ + DE this way
        (see release-tiles.yml) so Delaware is covered; see SOURCE_PBF_URL
        below for why a single Geofabrik regional extract is no longer the
        canonical CI source.
  - If --source-pbf is omitted, the script downloads --source-url
    into --output-dir as a single-source convenience for local runs. The
    downloaded source PBF is treated as an intermediate and removed on
    clean exit unless --keep-intermediates.

Final outputs (always emitted):
  <output-dir>/<base-name>_<date>.osm.pbf   (bbox-extracted region)
  <output-dir>/<base-name>_<date>.pmtiles   (multi-layer OMT pmtiles)

<date> defaults to an ISO 8601 UTC timestamp YYYY-MM-DDTHH-MM-SSZ
(naturally sortable, filename-safe; per plan decision D3).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

#### DEBUG
print(f"Arguments sent to generate_tiles_pb, {sys.argv = }")
###
# Local-convenience default only (used when --source-pbf/--source-url are
# both omitted). NOT the canonical CI source: us-northeast does not contain
# Delaware (a separate Geofabrik us-south extract), which left a swath of DE
# with no street data. CI (release-tiles.yml) instead downloads the
# Pennsylvania + New Jersey + Delaware state extracts and passes all three
# via repeated --source-pbf, which this script bbox-clips and `osmium merge`s
# (see "Source PBF handling" above). See CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md
# §1.1 for the original single-source decision and SPECS/01_FIXES for the
# PA+NJ+DE follow-up.
SOURCE_PBF_URL = "https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf"
SOURCE_MD5_URL = "https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf.md5"

# The region every release is built for. Must stay identical to BBOX in
# .github/workflows/release-tiles.yml. The north edge bounds the SEPTA service
# area, whose northern tip is Riegelsville at 40.60, so a consumer can treat
# this bbox as covering that whole area. South and east cover Salem and New
# Castle counties and the truncated NJ counties (10 spec Appendix A). Maryland
# is deliberately excluded -- there is no MD source PBF.
DEFAULT_BBOX = "-76.00,39.30,-74.30,40.65"
DEFAULT_BASE_NAME = "philly_commute_region"

# Attribution string embedded directly in the .pmtiles metadata so the OSM +
# Geofabrik copyright travels with the file and is rendered by map clients
# (MapLibre/Leaflet read this from the tileset metadata). This MUST stay in
# sync with the map-data notice in ATTRIBUTION.txt and README.md
# ("Copyright and License"). See CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md §4.3.
MAP_ATTRIBUTION = (
    '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors © <a href="https://www.geofabrik.de/">Geofabrik</a> '
    '(<a href="https://opendatacommons.org/licenses/odbl/1-0/">ODbL 1.0</a>)'
)


def run(cmd):
    print(f"\n[RUNNING] {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] exit {e.returncode}")
        sys.exit(1)


# ---------- Stage 1: bbox extract ----------

def extract_region(input_pbf, bbox, output_pbf):
    # --strategy smart keeps relation member ways that fall outside the bbox,
    # but only for type=multipolygon by default; admin boundaries are
    # type=boundary, so without -S types=any a cross-bbox state/county ring
    # loses members and osmium export silently emits nothing for it.
    print(f"\n=== Stage 1: extract region -> {output_pbf} ===")
    run(["osmium", "extract", "--bbox", bbox,
         "--strategy", "smart", "-S", "types=any",
         "--output", output_pbf, input_pbf])


def merge_regions(clips, output_pbf):
    """Combine bbox-clipped source PBFs into one region PBF.

    `osmium merge` dedupes objects with identical (type, id, version), so
    shared border ways/nodes between adjoining state extracts (e.g. the
    PA/NJ river boundary) collapse to a single copy rather than duplicating.
    """
    print(f"\n=== Stage 1: merge {len(clips)} clipped sources -> {output_pbf} ===")
    run(["osmium", "merge", *clips, "-o", output_pbf])


# ---------- Stage 2: per-layer tag filter ----------

def filter_layer(input_pbf, filters, output_pbf):
    run(["osmium", "tags-filter", input_pbf, *filters, "-o", output_pbf])


# ---------- Stage 3: per-layer GeoJSONSeq export ----------

def export_geojsonseq(input_pbf, output_geojsonseq):
    # --show-errors: by default osmium export silently ignores geometries it
    # cannot build, so a boundary relation whose ring doesn't close vanishes
    # without a trace. This prints one stderr line per failed object (run()
    # inherits stderr, so it lands in the CI log). Deliberately NOT
    # --stop-on-error: the label manifest check is the failure gate.
    run(["osmium", "export", input_pbf, "--show-errors",
         "-f", "geojsonseq", "-o", output_geojsonseq])


# ---------- Stage 4: normalize OSM tags -> OpenMapTiles props ----------

HIGHWAY_TO_CLASS = {
    "motorway": "motorway", "motorway_link": "motorway",
    "trunk": "trunk", "trunk_link": "trunk",
    "primary": "primary", "primary_link": "primary",
    "secondary": "secondary", "secondary_link": "secondary",
    "tertiary": "tertiary", "tertiary_link": "tertiary",
    "unclassified": "minor", "residential": "minor",
    "service": "service",
}

RAILWAY_TO_CLASS = {
    "rail": "rail", "narrow_gauge": "rail", "monorail": "rail",
    "subway": "transit", "light_rail": "transit", "tram": "transit",
}


def normalize_transportation(props):
    hw = props.get("highway")
    if hw in HIGHWAY_TO_CLASS:
        return {"class": HIGHWAY_TO_CLASS[hw]}
    rw = props.get("railway")
    if rw in RAILWAY_TO_CLASS:
        return {"class": RAILWAY_TO_CLASS[rw]}
    return None


def normalize_transportation_name(props):
    out = normalize_transportation(props)
    if out is None or not props.get("name"):
        return None
    out["name"] = props["name"]
    return out


def normalize_water(props):
    if props.get("natural") == "water" or props.get("waterway") == "riverbank":
        out = {}
        if props.get("intermittent") == "yes":
            out["intermittent"] = 1
        return out
    return None


def normalize_landcover(props):
    if props.get("leisure") == "park":
        return {"class": "park"}
    if props.get("natural") == "wood" or props.get("landuse") == "forest":
        return {"class": "wood"}
    if props.get("landuse") in ("grass", "meadow"):
        return {"class": "grass"}
    return None


def normalize_landuse(props):
    if props.get("landuse") == "cemetery":
        return {"class": "cemetery"}
    amenity = props.get("amenity")
    if amenity in ("hospital", "school", "university", "library"):
        return {"class": amenity}
    return None


def normalize_boundary(props):
    al = props.get("admin_level")
    try:
        al = int(al)
    except (ValueError, TypeError):
        return None
    if al not in (4, 6, 8):
        return None
    return {"admin_level": al}


# Boundaries are forced kept from this zoom up so --drop-densest-as-needed
# cannot strip the admin lines while it thins denser layers. Attached as a
# top-level Feature member (sibling of properties/geometry) — the only place
# tippecanoe reads per-feature directives.
BOUNDARY_TIPPECANOE = {"minzoom": 10, "maxzoom": 13}


def iter_boundary_linestrings(geometry):
    """Yield LineString coordinate arrays for any admin boundary geometry.

    Admin AREAS arrive as Polygon/MultiPolygon (a boundary relation — e.g.
    Philadelphia's consolidated city-county — is assembled by `osmium export`
    into an area, and is the *only* carrier of admin_level for that boundary).
    A polygon ring is already a closed coordinate array, so the ring **is** its
    own boundary line: converting here, before tiling, yields the true admin
    perimeter (never a tile-clipped rectangle). Genuine LineStrings pass
    through; anything else (Point/null) yields nothing.
    """
    if not geometry:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if coords is None:
        return
    if gtype == "LineString":
        yield coords
    elif gtype == "MultiLineString":
        yield from coords
    elif gtype == "Polygon":
        yield from coords
    elif gtype == "MultiPolygon":
        for polygon in coords:
            yield from polygon


def normalize_boundary_geojsonseq(input_path, output_path):
    """Boundary layer: emit admin boundary LineStrings only (never area polygons).

    Keeps admin_level as a Number in {4,6,8} via normalize_boundary, explodes
    each kept feature's geometry into one LineString per ring/part, and stamps
    each output feature with BOUNDARY_TIPPECANOE so boundaries survive
    tile-budget dropping. Dropping the area polygons is what lets the downstream
    LineString-only style render Philadelphia's outline instead of a box grid.
    """
    n_in = n_out = 0
    with open(input_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            n_in += 1
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            new_props = normalize_boundary(feat.get("properties") or {})
            if new_props is None:
                continue
            for coords in iter_boundary_linestrings(feat.get("geometry")):
                out_feat = {
                    "type": "Feature",
                    "tippecanoe": BOUNDARY_TIPPECANOE,
                    "properties": new_props,
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
                fout.write("\x1e" + json.dumps(out_feat, separators=(",", ":")) + "\n")
                n_out += 1
    print(f"  normalized {n_out} boundary lines from {n_in} features -> {output_path}")


class BoundaryAssertionError(Exception):
    """Raised when the normalized boundary GeoJSONSeq is not lines-only."""


def assert_boundary_lines_only(path):
    """Fail fast before tiling if the boundary layer is not LineString-only.

    Guards against the box-grid regression and the silent-no-match failure mode:
    a Polygon/MultiPolygon feature, or an admin_level that is not a JSON Number,
    aborts the run with a descriptive message. Streaming line read — negligible
    cost, so it is safe to run on every CI build.
    """
    n = 0
    with open(path, "r", encoding="utf-8") as fin:
        for lineno, line in enumerate(fin, start=1):
            line = line.strip().strip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            gtype = (feat.get("geometry") or {}).get("type")
            if gtype not in ("LineString", "MultiLineString"):
                raise BoundaryAssertionError(
                    f"{path}:{lineno}: boundary feature has geometry type "
                    f"{gtype!r}; expected LineString. Area polygons must be "
                    f"converted to lines before tiling (see "
                    f"normalize_boundary_geojsonseq)."
                )
            al = (feat.get("properties") or {}).get("admin_level")
            if not isinstance(al, int) or isinstance(al, bool):
                raise BoundaryAssertionError(
                    f"{path}:{lineno}: admin_level is {al!r} ({type(al).__name__}); "
                    f"expected a JSON Number. A string admin_level silently "
                    f"matches nothing in the -j filter and the downstream style."
                )
    print(f"  boundary assertion passed: {n} features, all LineString")


# County name labels. Counties are admin_level 6 areas; we emit one label Point
# per county into the `place` layer (class == "county") so the downstream style
# can render a geographic label for it. minzoom 6 keeps them from being dropped.
COUNTY_LABEL_TIPPECANOE = {"minzoom": 6}


def _ring_signed_area(ring):
    """Shoelace signed area of a (closed) ring in coordinate units."""
    a = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        a += x0 * y1 - x1 * y0
    return a * 0.5


def _ring_centroid(ring):
    """Area-weighted centroid of a single (closed) ring -> [x, y].

    Falls back to the vertex average for a degenerate (zero-area) ring.
    """
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return [sum(xs) / len(xs), sum(ys) / len(ys)]
    a *= 0.5
    return [cx / (6 * a), cy / (6 * a)]


def _geometry_area(geometry):
    """Absolute exterior-ring area of a Polygon / MultiPolygon (0 otherwise)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon" and coords:
        return abs(_ring_signed_area(coords[0]))
    if gtype == "MultiPolygon":
        return sum(abs(_ring_signed_area(poly[0])) for poly in coords if poly)
    return 0.0


def parse_bbox(bbox_str):
    """Parse 'minlon,minlat,maxlon,maxlat' into a (minlon, minlat, maxlon, maxlat)
    float tuple, the form clip_ring_to_bbox / label_point_for_geometry expect."""
    minlon, minlat, maxlon, maxlat = (float(x) for x in bbox_str.split(","))
    return minlon, minlat, maxlon, maxlat


# Padding for the tippecanoe --clip-bounding-box in generate_pmtiles (07
# resolution spec Chunk L8): keeps the archive's outer frame from looking
# bare just inside the exact service-area bbox.
CLIP_BBOX_PAD_DEG = 0.15


def pad_bbox(bbox_str, pad_deg=CLIP_BBOX_PAD_DEG):
    """Pad a 'minlon,minlat,maxlon,maxlat' bbox string outward by [pad_deg]
    degrees on each side, formatted for tippecanoe's --clip-bounding-box.

    `-S types=any` (Chunk L1) completes admin relations far past the bbox,
    which ballooned the tileset bounds to all of PA+NJ+DE and the z13 tile
    count from 948 to 3,594 -- ~2,650 near-empty border-tracing tiles that
    cost +2.7 MB and buy nothing visible in-region (07 resolution spec §2).
    Clipping the *archive* back to (a slightly padded) bbox recovers that
    without touching label placement, which is computed pre-tippecanoe and
    already clips independently via clip_ring_to_bbox. round() avoids float
    repr artifacts like 40.39999999999999 from plain float addition.
    """
    minlon, minlat, maxlon, maxlat = parse_bbox(bbox_str)
    return (f"{round(minlon - pad_deg, 6)},{round(minlat - pad_deg, 6)},"
            f"{round(maxlon + pad_deg, 6)},{round(maxlat + pad_deg, 6)}")


def clip_ring_to_bbox(ring, bbox):
    """Sutherland-Hodgman clip of a closed ring to an axis-aligned bbox.

    [ring] is a closed [[x, y], ...] list (first == last). [bbox] is
    (minlon, minlat, maxlon, maxlat). Returns the closed ring of the portion
    of the ring's interior inside the bbox, or [] if nothing survives.
    Used so an area mostly outside the bbox (e.g. a state) still gets a label
    point inside its on-screen portion rather than at its full-area centroid.
    """
    minx, miny, maxx, maxy = bbox
    points = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)

    def clip_edge(pts, inside, intersect):
        if not pts:
            return []
        out = []
        n = len(pts)
        for i in range(n):
            curr, prev = pts[i], pts[i - 1]
            curr_in, prev_in = inside(curr), inside(prev)
            if curr_in:
                if not prev_in:
                    out.append(intersect(prev, curr))
                out.append(curr)
            elif prev_in:
                out.append(intersect(prev, curr))
        return out

    def intersect_x(value):
        def fn(p0, p1):
            x0, y0 = p0
            x1, y1 = p1
            t = 0.0 if x1 == x0 else (value - x0) / (x1 - x0)
            return [value, y0 + t * (y1 - y0)]
        return fn

    def intersect_y(value):
        def fn(p0, p1):
            x0, y0 = p0
            x1, y1 = p1
            t = 0.0 if y1 == y0 else (value - y0) / (y1 - y0)
            return [x0 + t * (x1 - x0), value]
        return fn

    points = clip_edge(points, lambda p: p[0] >= minx, intersect_x(minx))
    points = clip_edge(points, lambda p: p[0] <= maxx, intersect_x(maxx))
    points = clip_edge(points, lambda p: p[1] >= miny, intersect_y(miny))
    points = clip_edge(points, lambda p: p[1] <= maxy, intersect_y(maxy))

    if not points:
        return []
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def _point_in_ring(point, ring):
    """Ray-casting point-in-polygon test against a closed ring."""
    x, y = point
    n = len(ring) - 1
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def point_on_surface(ring):
    """A point guaranteed to lie inside a (possibly concave) closed ring, or None.

    Tries the cheap case first (the area-weighted centroid, which is already
    interior for a convex/roughly-convex ring); falls back to a horizontal
    scanline through the ring's vertical midpoint, taking the widest interior
    span, which is robust for concave shapes (e.g. a county that wraps around
    a neighbor) where the raw centroid can fall outside the polygon.
    """
    if not ring or len(ring) < 4:
        return None
    centroid = _ring_centroid(ring)
    if _point_in_ring(centroid, ring):
        return centroid

    ys = [p[1] for p in ring]
    scan_y = (min(ys) + max(ys)) / 2.0
    crossings = []
    n = len(ring) - 1
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        if y0 == y1:
            continue
        if min(y0, y1) <= scan_y < max(y0, y1):
            t = (scan_y - y0) / (y1 - y0)
            crossings.append(x0 + t * (x1 - x0))
    crossings.sort()
    spans = [
        (crossings[i + 1] - crossings[i], crossings[i], crossings[i + 1])
        for i in range(0, len(crossings) - 1, 2)
    ]
    if spans:
        _, x0, x1 = max(spans)
        return [(x0 + x1) / 2.0, scan_y]
    return centroid  # last-resort fallback; should not happen for a simple ring


def _multiline_coords(geometry):
    """All vertex chains of a LineString/MultiLineString geometry, or []."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "LineString":
        return [coords] if coords else []
    if gtype == "MultiLineString":
        return [line for line in coords if line]
    return []


def _points_in_bbox(points, bbox):
    minx, miny, maxx, maxy = bbox
    return [p for p in points if minx <= p[0] <= maxx and miny <= p[1] <= maxy]


def _geometry_size(geometry):
    """Heuristic size for picking the largest among same-named duplicate
    features: polygon area when available, else vertex count (for an
    admin area that exported as an incomplete LineString/MultiLineString)."""
    if not geometry:
        return 0.0
    gtype = geometry.get("type")
    if gtype in ("Polygon", "MultiPolygon"):
        return _geometry_area(geometry)
    if gtype in ("LineString", "MultiLineString"):
        return float(sum(len(line) for line in _multiline_coords(geometry)))
    return 0.0


def label_point_for_geometry(geometry, bbox=None):
    """Representative interior label point for an admin-area geometry, or None.

    For Polygon/MultiPolygon, clips to [bbox] first (if given) so an area
    mostly outside the bbox -- a state's full-area centroid can be ~150km
    off-screen -- gets a label inside its visible portion, then takes a
    point-on-surface of the largest surviving ring.

    `osmium extract` keeps only nearby boundary ways, so a relation that
    extends past the bbox may not reassemble into a closed Polygon and can
    export as an open LineString/MultiLineString instead. Rather than drop
    the label entirely (the prior behavior), this falls back to the
    vertex-average of the longest bbox-visible chain -- not a true interior
    point, but a reasonable position along the visible boundary arc, and
    strictly better than no label. Which geometry type admin_level 4/6 areas
    actually export as needs a human smoke check (see SPECS/02_LABELS).
    """
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    if gtype == "Polygon":
        rings = [coords[0]]
    elif gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
    else:
        rings = None

    if rings is not None:
        if bbox is not None:
            rings = [
                c for r in rings
                for c in [clip_ring_to_bbox(r, bbox)]
                if len(c) >= 4
            ]
        if not rings:
            return None
        best_ring = max(rings, key=lambda r: abs(_ring_signed_area(r)))
        return point_on_surface(best_ring)

    lines = _multiline_coords(geometry)
    if not lines:
        return None
    if bbox is not None:
        in_bbox = [pts for line in lines if (pts := _points_in_bbox(line, bbox))]
        if in_bbox:
            lines = in_bbox
    longest = max(lines, key=len)
    xs = [p[0] for p in longest]
    ys = [p[1] for p in longest]
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


def iter_county_labels(raw_boundary_path, bbox=None):
    """Yield one `place` label Point per named county (admin_level 6).

    Reads the RAW boundary export (which still carries `name`; the normalized
    boundary layer drops it). A consolidated city-county such as Philadelphia is
    both admin_level 6 and 8 — selecting level 6 only yields exactly one county
    label for it.

    Deduplicated by the first available of wikidata -> nist:fips_code -> name,
    keeping the largest geometry. Name alone is ambiguous in a merged
    multi-state region (Mercer County exists in both PA and NJ) and both
    optional keys are genuinely absent from some real counties (Lycoming PA
    has no fips), so the ladder never *requires* either — a data-poor county
    still labels, keyed by name.

    Accepts Polygon/MultiPolygon geometry only. Earlier versions also accepted
    a LineString/MultiLineString fallback (for a county relation that didn't
    reassemble into a closed area after the bbox extract), but that fallback
    also admitted rivers/roads whose OSM ways carry admin_level=6 because they
    happen to form part of a county line (e.g. "Rocky Brook", "Great Egg
    Harbor River", "Princeton Avenue" — see 07 resolution spec §1 Cause 3,
    which found 8 such junk entries shipped as `class=county` labels). The one
    legitimate historical beneficiary (an unassembled Delaware County) is
    fixed upstream by `-S types=any` plus the Feb 2026 OSM ring repair; the
    label manifest (EXPECTED_COUNTY_LABELS) now catches any future regression
    loudly instead of silently rescuing it with a non-county label. If [bbox]
    is given, the label is placed inside the county's bbox-visible portion.
    """
    best_by_key = {}  # wikidata|fips|name -> (size, name, geometry)
    with open(raw_boundary_path, "r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            try:
                al = int(props.get("admin_level"))
            except (ValueError, TypeError):
                continue
            if al != 6:
                continue
            name = props.get("name")
            if not name:
                continue
            geom = feat.get("geometry") or {}
            if geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            size = _geometry_size(geom)
            key = props.get("wikidata") or props.get("nist:fips_code") or name
            prev = best_by_key.get(key)
            if prev is None or size > prev[0]:
                # wikidata/fips ride along onto the label (when present) so a
                # future gap can be diagnosed from the tile archive itself.
                out_props = {"class": "county", "name": name}
                if props.get("wikidata"):
                    out_props["wikidata"] = props["wikidata"]
                if props.get("nist:fips_code"):
                    out_props["fips"] = props["nist:fips_code"]
                best_by_key[key] = (size, out_props, geom)
    for _, out_props, geom in best_by_key.values():
        point = label_point_for_geometry(geom, bbox=bbox)
        if point is None:
            continue
        yield {
            "type": "Feature",
            "tippecanoe": COUNTY_LABEL_TIPPECANOE,
            "properties": out_props,
            "geometry": {"type": "Point", "coordinates": point},
        }


def append_county_labels(raw_boundary_path, place_norm_path, bbox=None):
    """Append county label features (from the boundary export) to the place layer."""
    n = 0
    names = []
    with open(place_norm_path, "a", encoding="utf-8") as fout:
        for feat in iter_county_labels(raw_boundary_path, bbox=bbox):
            fout.write("\x1e" + json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
            names.append(feat["properties"]["name"])
    print(f"  appended {n} county labels -> {place_norm_path}")
    # Names, not just a count -- so a run can be eyeballed for junk without a
    # binary decode (07 resolution spec §1 Cause 3 found 8 river/road names
    # hiding inside a bare "26 county labels" count).
    print(f"  county label names: {', '.join(sorted(names)) if names else '(none)'}")


# State name labels. States are admin_level 4 areas; we emit one label Point
# per whitelisted state into the `place` layer (class == "state"). A state
# polygon's full-area centroid is routinely far outside the bbox (Pennsylvania's
# centroid is near Harrisburg, ~150km from the SEPTA bbox), so placement always
# clips to the bbox first. Whitelisted to {PA, NJ, DE} per 02_LABELS spec §8 so
# a sliver of an adjoining state (MD, NY) at the bbox edge doesn't also get a
# label.
STATE_LABEL_TIPPECANOE = {"minzoom": 6}
STATE_LABEL_WHITELIST = {"Pennsylvania", "New Jersey", "Delaware"}


def iter_state_labels(raw_boundary_path, bbox):
    """Yield one `place` label Point per whitelisted state (admin_level 4).

    Mirrors iter_county_labels for states: reads the raw boundary export
    (still carries `name`), restricts to STATE_LABEL_WHITELIST, and places the
    label inside the bbox-clipped portion of the state via
    label_point_for_geometry (bbox is required here, not optional, since an
    unclipped state centroid is never useful for this bbox).
    """
    best_by_name = {}  # name -> (size, geometry)
    with open(raw_boundary_path, "r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            try:
                al = int(props.get("admin_level"))
            except (ValueError, TypeError):
                continue
            if al != 4:
                continue
            name = props.get("name")
            if name not in STATE_LABEL_WHITELIST:
                continue
            geom = feat.get("geometry") or {}
            if geom.get("type") not in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
                continue
            size = _geometry_size(geom)
            prev = best_by_name.get(name)
            if prev is None or size > prev[0]:
                best_by_name[name] = (size, geom)
    for name, (_, geom) in best_by_name.items():
        point = label_point_for_geometry(geom, bbox=bbox)
        if point is None:
            continue
        yield {
            "type": "Feature",
            "tippecanoe": STATE_LABEL_TIPPECANOE,
            "properties": {"class": "state", "name": name},
            "geometry": {"type": "Point", "coordinates": point},
        }


def append_state_labels(raw_boundary_path, place_norm_path, bbox):
    """Append state label features (from the boundary export) to the place layer."""
    n = 0
    with open(place_norm_path, "a", encoding="utf-8") as fout:
        for feat in iter_state_labels(raw_boundary_path, bbox):
            fout.write("\x1e" + json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
    print(f"  appended {n} state labels -> {place_norm_path}")


# Label manifest: the states and core service-area counties whose labels MUST
# be present after the append steps, or the build fails naming the gaps. OSM
# admin boundaries are volunteer-edited and break without notice (a county
# whose relation ring doesn't close is silently dropped by osmium export), so
# presence is verified every build instead of assumed. Superset semantics:
# bbox-edge extras (Berks PA, Cecil MD, Kent DE, ...) are expected and fine.
# Entries must match the emitted `name` exactly. Verified against the real
# export (fixmaps PROMPTS/06 grep data + first CI run 2026-07-02): the
# consolidated city-county IS named "Philadelphia County" on its admin_6
# relation, despite the city relation being plain "Philadelphia".
#
# Atlantic County / Cumberland County (NJ) added per 10 spec Appendix A's
# bbox widening -- both now substantially inside DEFAULT_BBOX and expected
# to follow the same "<Name> County" admin_6 naming the other NJ entries
# below already confirm for this region, but not yet verified against a
# real export (no local pipeline run); Chunk B2's CI run is the first real
# check. If either name is wrong, the manifest fails loudly and names the
# gap -- see check_label_manifest / LOVMAPS_ALLOW_MISSING_LABELS below.
# Maryland is deliberately NOT added here: DEFAULT_BBOX's southwest corner
# overlaps Cecil County MD, but there is no Maryland source PBF (human
# directive, 10 spec), so that corner is expected to render without a
# label -- it is not manifest-required.
EXPECTED_STATE_LABELS = frozenset(STATE_LABEL_WHITELIST)
EXPECTED_COUNTY_LABELS = frozenset({
    # PA (in/overlapping DEFAULT_BBOX)
    "Bucks County", "Montgomery County", "Chester County",
    "Delaware County", "Philadelphia County",
    # NJ
    "Burlington County", "Camden County", "Gloucester County",
    "Mercer County", "Salem County", "Atlantic County", "Cumberland County",
    # DE
    "New Castle County",
})


def check_label_manifest(place_norm_path):
    """Compare emitted state/county label names against the expected sets.

    Returns (missing_states, missing_counties) as sorted lists. Pure check --
    reporting/exiting is the call site's job (enforce_label_manifest).
    Malformed lines and nameless features are skipped, not fatal.
    """
    emitted = {"state": set(), "county": set()}
    with open(place_norm_path, "r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            cls = props.get("class")
            name = props.get("name")
            if cls in emitted and name:
                emitted[cls].add(name)
    return (sorted(EXPECTED_STATE_LABELS - emitted["state"]),
            sorted(EXPECTED_COUNTY_LABELS - emitted["county"]))


def enforce_label_manifest(place_norm_path):
    """Fail the build (exit 1) if any expected state/county label is missing.

    Every missing name is printed first so the log says exactly which
    boundary to investigate. LOVMAPS_ALLOW_MISSING_LABELS=1 downgrades the
    failure to a loud warning -- emergency lever only.
    """
    missing_states, missing_counties = check_label_manifest(place_norm_path)
    if not missing_states and not missing_counties:
        print("  label manifest OK: all expected state/county labels present")
        return
    for name in missing_states:
        print(f"  [MANIFEST] missing state label: {name}")
    for name in missing_counties:
        print(f"  [MANIFEST] missing county label: {name}")
    if os.environ.get("LOVMAPS_ALLOW_MISSING_LABELS") == "1":
        print("  [WARN] MANIFEST OVERRIDE (LOVMAPS_ALLOW_MISSING_LABELS=1): "
              "continuing despite missing labels")
        return
    print("  [ERROR] label manifest check failed; missing labels are listed "
          "above (see fixmaps SPECS/06 runbook)")
    sys.exit(1)


# ---------- region_labels.json: client-side dynamic area-label sidecar ----------
#
# 07 resolution spec §1 Cause 2 (decisive): the renderer discards any tile-
# embedded label whose text box crosses a tile edge, so a single Point label
# per admin area can never label a border viewport at street zoom -- and
# "PENNSYLVANIA" is geometrically impossible at every zoom regardless of
# placement. §3 Option A's fix moves label placement out of the tile pipeline
# entirely: ship simplified, bbox-clipped outline rings so a map client can
# compute the centroid of whatever portion of each area is actually visible,
# continuously as the viewport pans (the Google Maps behavior this was all
# for). This section builds that sidecar; iter_county_labels/iter_state_labels
# above still emit the tile-embedded Point labels unchanged (CI manifest
# value, other consumers, debuggability -- the app just stops drawing them,
# per spec Chunk B6).

REGION_LABELS_SCHEMA_VERSION = 1
# ~150 m at these latitudes -- adequate for area-label placement (not
# road-accuracy geometry); keeps the sidecar within its ~100-150 KB budget.
REGION_LABELS_SIMPLIFY_EPSILON_DEG = 0.0015


def _perpendicular_distance(point, line_start, line_end):
    """Perpendicular distance from [point] to the (infinite) line through
    [line_start]/[line_end]; falls back to point-to-point distance if the
    two endpoints coincide."""
    x, y = point
    x1, y1 = line_start
    x2, y2 = line_end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return ((x - proj_x) ** 2 + (y - proj_y) ** 2) ** 0.5


def douglas_peucker(points, epsilon):
    """Ramer-Douglas-Peucker polyline simplification.

    [points] is a list of [x, y] pairs (open chain or closed ring -- a closed
    ring's shared start/end point is always an endpoint of some recursive
    call, so it is always kept and the result stays closed). [epsilon] is the
    maximum perpendicular distance (coordinate units) a dropped point may
    deviate from the straight line between its surviving neighbors. Does not
    mutate [points]. Fewer than 3 points is already maximally simple.
    """
    if len(points) < 3:
        return list(points)
    start, end = points[0], points[-1]
    max_dist = -1.0
    max_idx = 0
    for i in range(1, len(points) - 1):
        dist = _perpendicular_distance(points[i], start, end)
        if dist > max_dist:
            max_dist = dist
            max_idx = i
    if max_dist <= epsilon:
        return [start, end]
    left = douglas_peucker(points[:max_idx + 1], epsilon)
    right = douglas_peucker(points[max_idx:], epsilon)
    return left[:-1] + right


def _admin_areas_by_key(raw_boundary_path, admin_level, name_ok=None):
    """Read the raw boundary export; keep the largest-geometry feature per
    dedupe key (wikidata -> nist:fips_code -> name -- the Chunk L4 ladder),
    restricted to [admin_level] and, if given, to names for which
    name_ok(name) is true. Polygon/MultiPolygon geometry only (mirrors the
    Chunk L7 junk filter in iter_county_labels).

    Returns {key: (name, properties, geometry)}. Separate from
    iter_county_labels/iter_state_labels (which predate this helper and emit
    a different shape -- a label Point, not outline rings) to avoid
    disturbing their already CI-verified behavior.
    """
    best = {}
    sizes = {}
    with open(raw_boundary_path, "r", encoding="utf-8") as fin:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            props = feat.get("properties") or {}
            try:
                al = int(props.get("admin_level"))
            except (ValueError, TypeError):
                continue
            if al != admin_level:
                continue
            name = props.get("name")
            if not name:
                continue
            if name_ok is not None and not name_ok(name):
                continue
            geom = feat.get("geometry") or {}
            if geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            size = _geometry_size(geom)
            key = props.get("wikidata") or props.get("nist:fips_code") or name
            if key not in sizes or size > sizes[key]:
                sizes[key] = size
                best[key] = (name, props, geom)
    return best


def region_label_rings(geometry, bbox, epsilon=REGION_LABELS_SIMPLIFY_EPSILON_DEG):
    """Exterior ring(s) of a Polygon/MultiPolygon, bbox-clipped
    (clip_ring_to_bbox) then Douglas-Peucker simplified. A ring clipped down
    to fewer than 4 points (nothing visible in-region) is dropped. Interior
    holes are not carried -- irrelevant for centroid-based label placement."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon" and coords:
        raw_rings = [coords[0]]
    elif gtype == "MultiPolygon":
        raw_rings = [poly[0] for poly in coords if poly]
    else:
        raw_rings = []
    out = []
    for ring in raw_rings:
        clipped = clip_ring_to_bbox(ring, bbox)
        if len(clipped) < 4:
            continue
        out.append(douglas_peucker(clipped, epsilon))
    return out


def iter_region_label_features(raw_boundary_path, bbox):
    """Yield one {class, name, wikidata?, fips?, rings} dict per whitelisted
    state or named county whose bbox-clipped outline survives (07 resolution
    spec Chunk L9). [bbox] is a (minlon, minlat, maxlon, maxlat) tuple."""
    for cls, admin_level, name_ok in (
        ("state", 4, lambda n: n in STATE_LABEL_WHITELIST),
        ("county", 6, None),
    ):
        for name, props, geom in _admin_areas_by_key(raw_boundary_path, admin_level, name_ok).values():
            rings = region_label_rings(geom, bbox)
            if not rings:
                continue
            out = {"class": cls, "name": name}
            if props.get("wikidata"):
                out["wikidata"] = props["wikidata"]
            if props.get("nist:fips_code"):
                out["fips"] = props["nist:fips_code"]
            out["rings"] = rings
            yield out


def build_region_labels(raw_boundary_path, bbox_str):
    """Build the region_labels.json document: {version, bbox, features}.
    [bbox_str] is the pipeline's 'minlon,minlat,maxlon,maxlat' string,
    embedded verbatim so a consumer can tell what area the outlines were
    clipped to."""
    bbox = parse_bbox(bbox_str)
    return {
        "version": REGION_LABELS_SCHEMA_VERSION,
        "bbox": bbox_str,
        "features": list(iter_region_label_features(raw_boundary_path, bbox)),
    }


def write_region_labels(raw_boundary_path, bbox_str, output_path):
    """Write the region_labels.json document and return it (for the manifest
    check / logging at the call site)."""
    doc = build_region_labels(raw_boundary_path, bbox_str)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    size = os.path.getsize(output_path)
    print(f"  wrote {len(doc['features'])} region-label features -> "
          f"{output_path} ({size} bytes)")
    return doc


def check_region_labels_manifest(region_labels_path):
    """Compare region_labels.json's feature names against the same
    EXPECTED_STATE_LABELS / EXPECTED_COUNTY_LABELS manifest used for the
    tile-embedded place layer (Chunk L3) -- the two pathways must never
    silently diverge on which areas are present. Returns (missing_states,
    missing_counties) as sorted lists."""
    with open(region_labels_path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    emitted = {"state": set(), "county": set()}
    for feat in doc.get("features", []):
        cls = feat.get("class")
        name = feat.get("name")
        if cls in emitted and name:
            emitted[cls].add(name)
    return (sorted(EXPECTED_STATE_LABELS - emitted["state"]),
            sorted(EXPECTED_COUNTY_LABELS - emitted["county"]))


def enforce_region_labels_manifest(region_labels_path):
    """Fail the build (exit 1) if region_labels.json is missing any expected
    state/county area. Mirrors enforce_label_manifest, including the
    LOVMAPS_ALLOW_MISSING_LABELS emergency override."""
    missing_states, missing_counties = check_region_labels_manifest(region_labels_path)
    if not missing_states and not missing_counties:
        print("  region_labels manifest OK: all expected state/county areas present")
        return
    for name in missing_states:
        print(f"  [MANIFEST] region_labels.json missing state area: {name}")
    for name in missing_counties:
        print(f"  [MANIFEST] region_labels.json missing county area: {name}")
    if os.environ.get("LOVMAPS_ALLOW_MISSING_LABELS") == "1":
        print("  [WARN] MANIFEST OVERRIDE (LOVMAPS_ALLOW_MISSING_LABELS=1): "
              "continuing despite missing region_labels areas")
        return
    print("  [ERROR] region_labels manifest check failed; missing areas are "
          "listed above (see fixmaps SPECS/07_still_problems)")
    sys.exit(1)


def normalize_place(props):
    p = props.get("place")
    if p not in ("city", "town", "suburb", "neighbourhood"):
        return None
    out = {"class": p}
    if props.get("name"):
        out["name"] = props["name"]
    return out


def normalize_poi(props):
    if props.get("highway") == "bus_stop":
        sub = "bus_stop"
    elif props.get("railway") == "tram_stop":
        sub = "tram_stop"
    elif props.get("railway") == "halt":
        sub = "halt"
    elif props.get("railway") == "station":
        sub = "subway" if props.get("station") == "subway" else "station"
    else:
        return None
    out = {"subclass": sub}
    if props.get("name"):
        out["name"] = props["name"]
    return out


def normalize_geojsonseq(input_path, output_path, normalizer):
    n_in = n_out = 0
    with open(input_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip().strip("\x1e")
            if not line:
                continue
            n_in += 1
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            new_props = normalizer(feat.get("properties") or {})
            if new_props is None:
                continue
            feat["properties"] = new_props
            fout.write("\x1e" + json.dumps(feat, separators=(",", ":")) + "\n")
            n_out += 1
    print(f"  normalized {n_out}/{n_in} features -> {output_path}")


# ---------- Stage 5: combine into multi-layer pmtiles ----------

# Per-layer zoom gates. Mirrors the style's minzoom + class/admin_level filters
# so we don't encode features into tiles where they would never paint.
ZOOM_FILTERS = {
    "transportation": [
        "any",
        # motorway/trunk/primary skeleton stays ungated (z10+, matching the
        # tileset's minzoom) for regional orientation -- 08 spec Chunk B1,
        # human override of the "drop all road classes below z13" default.
        ["in", "class", "motorway", "trunk", "primary"],
        # Rail/transit stay. No BusNeighbor style draws them today, but this is
        # a general-purpose tile source and they are the most plausible thing
        # another consumer wants; they also cost only ~0.33 MB at z13 (0.9% of
        # feature bytes). Keeping them was a deliberate call when the 08 spec
        # gated the street classes -- see the test that pins this branch.
        ["all", [">=", "$zoom", 10], ["in", "class", "rail", "transit"]],
        # `service` removed (C5): no BusNeighbor style selects it, here or in
        # transportation_name, and it was 110,948 features / 8.31 MB = 23.6% of
        # all z13 feature bytes -- decoded on the phone once per DISPLAYED tile
        # and then discarded, because above z13 every display tile re-parses
        # the whole z13 parent. See the BusNeighbor repo's
        # CLAUDE_REFERENCE/tile_render_cost_chunked_spec.md, C5.
        # The osmium extract in LAYERS still collects service ways, so
        # re-admitting them is a one-line change here, not a re-download.
        ["all", [">=", "$zoom", 13],
         ["in", "class", "secondary", "tertiary", "minor"]],
    ],
    # Was an unqualified zoom gate, which admitted the name of every class the
    # transportation extract carries -- including 8,605 service-road names the
    # geometry filter above already excludes. Mirror the class list so the two
    # layers cannot drift apart. (C5)
    "transportation_name": [
        "all",
        [">=", "$zoom", 13],
        ["in", "class",
         "motorway", "trunk", "primary", "secondary", "tertiary", "minor"],
    ],
    "landcover": [
        "any",
        ["in", "class", "park", "wood"],
        ["all", [">=", "$zoom", 10], ["==", "class", "grass"]],
    ],
    "landuse": [">=", "$zoom", 12],
    # Belt-and-suspenders geometry guard: the boundary GeoJSONSeq is already
    # LineString-only (normalize_boundary_geojsonseq) and asserted polygon-free
    # before tiling, so correctness does not depend on $type being honoured by
    # -j. This guard simply ensures that if a future regression slips a polygon
    # back into the layer, it still cannot stroke into the per-tile box grid.
    # NOTE: whether tippecanoe 2.79.0 honours $type in -j is confirmed by the
    # human smoke test (spec Chunk H), not asserted here.
    "boundary": [
        "all",
        ["==", "$type", "LineString"],
        [
            "any",
            ["==", "admin_level", 4],
            ["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
            ["all", [">=", "$zoom", 13], ["==", "admin_level", 8]],
        ],
    ],
    "place": [
        "any",
        ["==", "class", "state"],
        ["==", "class", "city"],
        ["==", "class", "county"],
        ["all", [">=", "$zoom", 6],  ["==", "class", "town"]],
        ["all", [">=", "$zoom", 10], ["==", "class", "suburb"]],
        ["all", [">=", "$zoom", 12], ["==", "class", "neighbourhood"]],
    ],
    "poi": [
        "any",
        ["all", [">=", "$zoom", 11], ["in", "subclass", "station", "halt", "subway", "tram_stop"]],
        ["all", [">=", "$zoom", 13], ["==", "subclass", "bus_stop"]],
    ],
}


def generate_pmtiles(layer_files, output_pmtiles, min_zoom="10", max_zoom="13", bbox=None):
    print(f"\n=== Stage 5: tippecanoe combine -> {output_pmtiles} ===")
    cmd = [
        "tippecanoe",
        "-o", output_pmtiles,
        "--force",
        # Embed OSM + Geofabrik copyright in the tileset metadata so the
        # attribution travels inside the .pmtiles file itself, not just in the
        # sidecar ATTRIBUTION.txt. Map clients render --attribution on the map.
        "--name", "Philadelphia commute region (OpenStreetMap / Geofabrik)",
        "--attribution", MAP_ATTRIBUTION,
        f"-Z{min_zoom}",
        f"-z{max_zoom}",
        "--drop-densest-as-needed",
        "--coalesce",
        "--simplify-only-low-zooms",
        "--detect-shared-borders",
        "--maximum-tile-bytes=500000",
    ]
    if bbox is not None:
        # Chunk L8: discard the types=any ring overhang past the service
        # area (see pad_bbox docstring) -- ~2.7 MB of near-empty border tiles
        # with no in-region visual benefit.
        cmd.append(f"--clip-bounding-box={pad_bbox(bbox)}")
    cmd += ["-j", json.dumps(ZOOM_FILTERS)]
    for layer_name, path in layer_files:
        cmd.append(f"-L{layer_name}:{path}")
    run(cmd)


# ---------- Layer definitions ----------

LAYERS = [
    {
        "name": "transportation",
        "filter": [
            "w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link,"
            "secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,service",
            "w/railway=rail,light_rail,subway,tram,monorail,narrow_gauge",
        ],
        "normalize": normalize_transportation,
    },
    {
        "name": "transportation_name",
        "filter": [
            "w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link,"
            "secondary,secondary_link,tertiary,tertiary_link,unclassified,residential,service",
        ],
        "normalize": normalize_transportation_name,
    },
    {
        "name": "water",
        "filter": ["nwr/natural=water", "wr/waterway=riverbank"],
        "normalize": normalize_water,
    },
    {
        "name": "landcover",
        "filter": [
            "wr/leisure=park",
            "nwr/natural=wood",
            "wr/landuse=forest,grass,meadow",
        ],
        "normalize": normalize_landcover,
    },
    {
        "name": "landuse",
        "filter": [
            "wr/landuse=cemetery",
            "nwr/amenity=hospital,school,university,library",
        ],
        "normalize": normalize_landuse,
    },
    {
        "name": "boundary",
        "filter": ["wr/boundary=administrative", "wr/admin_level=4,6,8"],
        # Geometry-aware path: converts admin area polygons to boundary
        # LineStrings (see normalize_boundary_geojsonseq). normalize stays for
        # unit tests / reference; normalize_seq takes precedence in main().
        "normalize": normalize_boundary,
        "normalize_seq": normalize_boundary_geojsonseq,
        "assert_lines_only": True,
    },
    {
        "name": "place",
        "filter": ["n/place=city,town,suburb,neighbourhood"],
        "normalize": normalize_place,
    },
    {
        "name": "poi",
        "filter": [
            "nwr/railway=station,halt,tram_stop",
            "nwr/highway=bus_stop",
        ],
        "normalize": normalize_poi,
    },
]


# ---------- Source PBF handling ----------

def default_date_stamp():
    # Per CI_CD_RELEASE_PLAN.md D3: naturally sortable ISO 8601, UTC,
    # filename-safe (`:` replaced with `-`).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def md5_of_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_expected_md5(url):
    # Geofabrik .md5 sidecar format: "<hex>  <filename>\n"
    with urllib.request.urlopen(url, timeout=60) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    parts = text.split()
    return parts[0] if parts else ""


def download_source_pbf(source_url, md5_url, dest_path):
    """Single-attempt, best-effort download with optional md5 verification.

    The CI workflow does its own retry/backoff and strict md5 verification
    before invoking the script with --source-pbf, so this path is only used
    for local convenience runs.
    """
    print(f"\n=== Downloading source PBF ===\n  {source_url}\n  -> {dest_path}")
    urllib.request.urlretrieve(source_url, dest_path)

    try:
        expected = fetch_expected_md5(md5_url)
    except Exception as e:
        print(f"[WARN] could not fetch md5 sidecar ({e}); skipping verification")
        return

    if not expected:
        print("[WARN] md5 sidecar was empty; skipping verification")
        return

    actual = md5_of_file(dest_path)
    if actual.lower() != expected.lower():
        print(f"[ERROR] md5 mismatch for {dest_path}")
        print(f"  expected: {expected}")
        print(f"  actual:   {actual}")
        sys.exit(1)
    print(f"[OK] md5 verified: {actual}")


# ---------- Main ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Build an OpenMapTiles-compatible .pmtiles archive for the "
            "Philadelphia commute region from a Geofabrik OSM extract."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--source-pbf",
        action="append",
        default=None,
        help=(
            "Path to a local OSM .pbf to use as input. Repeatable: pass it "
            "more than once to bbox-clip each source and `osmium merge` the "
            "clips into the region PBF (e.g. one per state extract). If "
            "omitted entirely, the script downloads --source-url into "
            "--output-dir. Pass this flag in CI; passing it suppresses all "
            "network I/O."
        ),
    )
    p.add_argument(
        "--source-url",
        default=SOURCE_PBF_URL,
        help="Single-source URL to download when --source-pbf is not given.",
    )
    p.add_argument(
        "--bbox",
        default=DEFAULT_BBOX,
        help='Bounding box "minlon,minlat,maxlon,maxlat".',
    )
    p.add_argument(
        "--base-name",
        default=DEFAULT_BASE_NAME,
        help="Stem for output filenames: <base-name>_<date>.{osm.pbf,pmtiles}.",
    )
    p.add_argument(
        "--date",
        default=None,
        help=(
            "Date stamp embedded in output filenames. Defaults to a UTC "
            "ISO 8601 timestamp (YYYY-MM-DDTHH-MM-SSZ) generated at start of run."
        ),
    )
    p.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files (and per-layer intermediates).",
    )
    keep_group = p.add_mutually_exclusive_group()
    keep_group.add_argument(
        "--keep-intermediates",
        dest="keep_intermediates",
        action="store_true",
        help="Retain per-layer .osm.pbf / .geojsonseq files (and the source PBF if downloaded).",
    )
    keep_group.add_argument(
        "--no-keep-intermediates",
        dest="keep_intermediates",
        action="store_false",
        help="Delete intermediates on clean exit (default).",
    )
    p.set_defaults(keep_intermediates=False)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    date_stamp = args.date or default_date_stamp()
    stem = f"{args.base_name}_{date_stamp}"

    region_pbf = output_dir / f"{stem}.osm.pbf"
    final_pmtiles = output_dir / f"{stem}.pmtiles"
    region_labels_path = output_dir / f"{stem}.region_labels.json"

    intermediates = []

    # Resolve source PBF(s) (provided locally, or downloaded as a convenience).
    if args.source_pbf:
        source_pbfs = [Path(p) for p in args.source_pbf]
        for source_pbf in source_pbfs:
            if not source_pbf.exists():
                print(f"Error: --source-pbf '{source_pbf}' does not exist.")
                sys.exit(1)
        downloaded_source = False
    else:
        single_source = output_dir / "us-northeast-latest.osm.pbf"
        if single_source.exists():
            print(f"[INFO] reusing existing source PBF at {single_source}")
        else:
            download_source_pbf(args.source_url, SOURCE_MD5_URL, str(single_source))
        source_pbfs = [single_source]
        downloaded_source = True
        intermediates.append(single_source)

    print(f"Pipeline output: {final_pmtiles}")

    if len(source_pbfs) == 1:
        extract_region(str(source_pbfs[0]), args.bbox, str(region_pbf))
    else:
        clips = []
        for i, source_pbf in enumerate(source_pbfs):
            clip_pbf = output_dir / f"{stem}_clip{i}.osm.pbf"
            extract_region(str(source_pbf), args.bbox, str(clip_pbf))
            clips.append(str(clip_pbf))
            intermediates.append(clip_pbf)
        merge_regions(clips, str(region_pbf))

    layer_files = []
    raw_paths = {}
    norm_paths = {}
    for layer in LAYERS:
        name = layer["name"]
        print(f"\n=== Layer: {name} ===")
        layer_pbf = output_dir / f"{stem}_{name}.osm.pbf"
        raw_geojson = output_dir / f"{stem}_{name}.raw.geojsonseq"
        norm_geojson = output_dir / f"{stem}_{name}.geojsonseq"
        raw_paths[name] = raw_geojson
        norm_paths[name] = norm_geojson
        filter_layer(str(region_pbf), layer["filter"], str(layer_pbf))
        export_geojsonseq(str(layer_pbf), str(raw_geojson))
        normalize_seq = layer.get("normalize_seq")
        if normalize_seq is not None:
            normalize_seq(str(raw_geojson), str(norm_geojson))
        else:
            normalize_geojsonseq(str(raw_geojson), str(norm_geojson), layer["normalize"])
        if layer.get("assert_lines_only"):
            assert_boundary_lines_only(str(norm_geojson))
        layer_files.append((name, str(norm_geojson)))
        intermediates.extend([layer_pbf, raw_geojson, norm_geojson])

    # Geographic name labels: emit one place label per admin_level 4/6 area into
    # the place layer (class == "state" / "county"). Derived from the raw
    # boundary export (still carries name) while intermediates are present,
    # before tiling. Both are placed within the bbox-clipped portion of their
    # area so a label lands on-screen even when the area extends well past the
    # bbox (see SPECS/02_LABELS).
    if "boundary" in raw_paths and "place" in norm_paths:
        bbox_tuple = parse_bbox(args.bbox)
        print("\n=== County labels -> place layer ===")
        append_county_labels(str(raw_paths["boundary"]), str(norm_paths["place"]), bbox=bbox_tuple)
        print("\n=== State labels -> place layer ===")
        append_state_labels(str(raw_paths["boundary"]), str(norm_paths["place"]), bbox_tuple)
        print("\n=== Label manifest check ===")
        enforce_label_manifest(str(norm_paths["place"]))

        # region_labels.json: client-side dynamic area-label sidecar (07
        # resolution spec Chunk L9). Built from the same raw boundary export
        # while it's still present, before intermediates are cleaned up.
        print("\n=== region_labels.json (dynamic area-label sidecar) ===")
        write_region_labels(str(raw_paths["boundary"]), args.bbox, str(region_labels_path))
        enforce_region_labels_manifest(str(region_labels_path))

    generate_pmtiles(layer_files, str(final_pmtiles), bbox=args.bbox)

    if not args.keep_intermediates:
        print("\n=== Cleaning up intermediates ===")
        for path in intermediates:
            try:
                os.remove(path)
                print(f"  removed {path}")
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  [WARN] could not remove {path}: {e}")
    else:
        if downloaded_source:
            print(f"[INFO] keeping downloaded source PBF(s): {source_pbfs}")
        print("[INFO] keeping per-layer intermediates")

    print(f"\nDone:")
    print(f"  region PBF:     {region_pbf}")
    print(f"  pmtiles:        {final_pmtiles}")
    print(f"  region labels:  {region_labels_path}")


if __name__ == "__main__":
    main()
