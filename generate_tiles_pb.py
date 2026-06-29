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
      [--source-pbf PATH] [--source-url URL]
      [--bbox MINLON,MINLAT,MAXLON,MAXLAT]
      [--base-name NAME] [--date STR]
      [--output-dir DIR]
      [--keep-intermediates | --no-keep-intermediates]

Source PBF handling:
  - If --source-pbf is given, the script uses it directly and performs no
    network I/O. This is the path used by the CI workflow, which owns
    download / retry / md5 verification.
  - If --source-pbf is omitted, the script downloads --source-url
    (default: the Geofabrik us-northeast extract) into --output-dir as a
    convenience for local runs. The downloaded source PBF is treated as
    an intermediate and removed on clean exit unless --keep-intermediates.

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
# Canonical upstream source — single source of truth. See
# CLAUDE_REFERENCE/CI_CD_RELEASE_PLAN.md §1.1. Any change here is an
# explicit, reviewed decision.
SOURCE_PBF_URL = "https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf"
SOURCE_MD5_URL = "https://download.geofabrik.de/north-america/us-northeast-latest.osm.pbf.md5"

DEFAULT_BBOX = "-76.00,39.60,-74.60,40.40"  # SEPTA service region
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
    print(f"\n=== Stage 1: extract region -> {output_pbf} ===")
    run(["osmium", "extract", "--bbox", bbox, input_pbf, "--output", output_pbf])


# ---------- Stage 2: per-layer tag filter ----------

def filter_layer(input_pbf, filters, output_pbf):
    run(["osmium", "tags-filter", input_pbf, *filters, "-o", output_pbf])


# ---------- Stage 3: per-layer GeoJSONSeq export ----------

def export_geojsonseq(input_pbf, output_geojsonseq):
    run(["osmium", "export", input_pbf, "-f", "geojsonseq", "-o", output_geojsonseq])


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
        ["in", "class", "motorway", "trunk", "primary"],
        ["all", [">=", "$zoom", 8],  ["in", "class", "secondary", "tertiary"]],
        ["all", [">=", "$zoom", 10], ["in", "class", "rail", "transit"]],
        ["all", [">=", "$zoom", 11], ["in", "class", "minor", "service"]],
    ],
    "transportation_name": [">=", "$zoom", 12],
    "landcover": [
        "any",
        ["in", "class", "park", "wood"],
        ["all", [">=", "$zoom", 10], ["==", "class", "grass"]],
    ],
    "landuse": [">=", "$zoom", 12],
    "boundary": [
        "any",
        ["==", "admin_level", 4],
        ["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
        ["all", [">=", "$zoom", 8], ["==", "admin_level", 8]],
    ],
    "place": [
        "any",
        ["==", "class", "city"],
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


def generate_pmtiles(layer_files, output_pmtiles, min_zoom="10", max_zoom="13"):
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
        "--maximum-tile-bytes=200000",
        "-j", json.dumps(ZOOM_FILTERS),
    ]
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
        default=None,
        help=(
            "Path to a local OSM .pbf to use as input. If omitted, the script "
            "downloads --source-url into --output-dir. Pass this flag in CI; "
            "passing it suppresses all network I/O."
        ),
    )
    p.add_argument(
        "--source-url",
        default=SOURCE_PBF_URL,
        help="URL to download when --source-pbf is not given.",
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

    intermediates = []

    # Resolve source PBF (provided locally, or downloaded as a convenience).
    if args.source_pbf:
        source_pbf = Path(args.source_pbf)
        if not source_pbf.exists():
            print(f"Error: --source-pbf '{source_pbf}' does not exist.")
            sys.exit(1)
        downloaded_source = False
    else:
        source_pbf = output_dir / "us-northeast-latest.osm.pbf"
        if source_pbf.exists():
            print(f"[INFO] reusing existing source PBF at {source_pbf}")
        else:
            download_source_pbf(args.source_url, SOURCE_MD5_URL, str(source_pbf))
        downloaded_source = True
        intermediates.append(source_pbf)

    print(f"Pipeline output: {final_pmtiles}")

    extract_region(str(source_pbf), args.bbox, str(region_pbf))

    layer_files = []
    for layer in LAYERS:
        name = layer["name"]
        print(f"\n=== Layer: {name} ===")
        layer_pbf = output_dir / f"{stem}_{name}.osm.pbf"
        raw_geojson = output_dir / f"{stem}_{name}.raw.geojsonseq"
        norm_geojson = output_dir / f"{stem}_{name}.geojsonseq"
        filter_layer(str(region_pbf), layer["filter"], str(layer_pbf))
        export_geojsonseq(str(layer_pbf), str(raw_geojson))
        normalize_seq = layer.get("normalize_seq")
        if normalize_seq is not None:
            normalize_seq(str(raw_geojson), str(norm_geojson))
        else:
            normalize_geojsonseq(str(raw_geojson), str(norm_geojson), layer["normalize"])
        layer_files.append((name, str(norm_geojson)))
        intermediates.extend([layer_pbf, raw_geojson, norm_geojson])

    generate_pmtiles(layer_files, str(final_pmtiles))

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
            print(f"[INFO] keeping downloaded source PBF: {source_pbf}")
        print("[INFO] keeping per-layer intermediates")

    print(f"\nDone:")
    print(f"  region PBF: {region_pbf}")
    print(f"  pmtiles:    {final_pmtiles}")


if __name__ == "__main__":
    main()
