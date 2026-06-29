#!/usr/bin/env bash

set -euo pipefail
./generate_tiles_pb.py \
  --base-name philly_commute_region \
  --date 2026-05-30T05-50-38Z \
  --output-dir "$(pwd)" \
  --bbox -76.00,39.60,-74.60,40.40 \
  --source-pbf us-northeast-latest.osm.pbf
