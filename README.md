# L*o*V Maps: Custom-Filtered Maps For Low Vision Users

## About
This project aims to provide compact, network-accessible vector map tiles for the low-vision community.

It is a companion project to another project of mine in active development, currently focused on the Greater Philadelphia area.

## Downloads

Built `.pmtiles` (and the matching bbox-extracted `.osm.pbf`) are published as
[GitHub Releases](https://github.com/jos-eph/lovmaps/releases). A new dated
release is produced once per day; a separate, perpetually-updated `current`
release always points to the latest build.

### Stable URLs (always the latest build)

```
https://github.com/jos-eph/lovmaps/releases/download/current/philly_commute_region_current.pmtiles
https://github.com/jos-eph/lovmaps/releases/download/current/philly_commute_region_current.pmtiles.sha256
```

These URLs are overwritten on every successful build, so a hard-coded link
will always serve the freshest data without code changes on your end.

### Dated URLs (immutable, pinnable)

```
https://github.com/jos-eph/lovmaps/releases/download/tiles-<DATE>/philly_commute_region_<DATE>.pmtiles
https://github.com/jos-eph/lovmaps/releases/download/tiles-<DATE>/philly_commute_region_<DATE>.pmtiles.sha256
```

A dated release is never overwritten — pin to a specific `<DATE>` when you
need reproducibility. The most recent 30 dated releases are kept; older ones
are pruned automatically.

### File naming

```
philly_commute_region_<DATE>.pmtiles          # the vector tile bundle
philly_commute_region_<DATE>.pmtiles.sha256   # sha256 sidecar
philly_commute_region_<DATE>.osm.pbf          # the bbox-extracted source OSM PBF
philly_commute_region_<DATE>.osm.pbf.sha256   # sha256 sidecar
```

`<DATE>` is a UTC timestamp captured once at the start of each build and reused
across every asset of that build, so all four files in a release belong to the
same run.

### Verifying a download

Each asset ships with a sibling `.sha256` file in `sha256sum` format. After
downloading both files into the same directory:

```
sha256sum -c philly_commute_region_current.pmtiles.sha256
```

A successful verification prints `philly_commute_region_current.pmtiles: OK`.

### Attribution

The released `.pbf` and `.pmtiles` assets are covered by the map-data notice
below. Every release also includes an `ATTRIBUTION.txt` file that reproduces
this notice along with the full text of the ODbL 1.0; see also
[`MAP-DATA-LICENSE.md`](MAP-DATA-LICENSE.md) in this repository.

The `.pmtiles` assets additionally carry this attribution **embedded in their
tileset metadata** (via tippecanoe's `--attribution`), so the OSM + Geofabrik
copyright renders automatically in map clients such as MapLibre and Leaflet —
no separate configuration required. The OSM PBF format has no standard embedded
copyright field, so the `.osm.pbf` assets rely on the accompanying
`ATTRIBUTION.txt` and the notice below.

## Copyright and License
Map data (`.pbf` and `.pmtiles` files) © OpenStreetMap contributors © Geofabrik, available under the ODbL (Open Database License) 1.0. 

All other files © Joseph D. Mirarchi, available under a 3-clause BSD license.

Processing made possible by open-source tooling.