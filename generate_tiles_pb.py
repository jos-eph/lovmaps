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
"""

import json
import os
import subprocess
import sys
from datetime import datetime


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
        "normalize": normalize_boundary,
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


def main():
    source_pbf = "us-northeast-latest.osm.pbf"
    if not os.path.exists(source_pbf):
        print(f"Error: missing '{source_pbf}'. Download it first.")
        sys.exit(1)

    bbox = "-76.00,39.60,-74.60,40.40"  # SEPTA service region

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"SCRIPTMADE_{timestamp}"
    region_pbf = f"{prefix}_region.osm.pbf"
    final_pmtiles = f"{prefix}_omt.pmtiles"

    print(f"Pipeline output: {final_pmtiles}")

    extract_region(source_pbf, bbox, region_pbf)

    layer_files = []
    for layer in LAYERS:
        name = layer["name"]
        print(f"\n=== Layer: {name} ===")
        layer_pbf = f"{prefix}_{name}.osm.pbf"
        raw_geojson = f"{prefix}_{name}.raw.geojsonseq"
        norm_geojson = f"{prefix}_{name}.geojsonseq"
        filter_layer(region_pbf, layer["filter"], layer_pbf)
        export_geojsonseq(layer_pbf, raw_geojson)
        normalize_geojsonseq(raw_geojson, norm_geojson, layer["normalize"])
        layer_files.append((name, norm_geojson))

    generate_pmtiles(layer_files, final_pmtiles)

    print(f"\nDone: {final_pmtiles}")
    print(f"To use it:")
    print(f"  mv {final_pmtiles} target.pmtiles")
    print(f"  docker compose down && docker compose up")


if __name__ == "__main__":
    main()