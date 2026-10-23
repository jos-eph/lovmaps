#!/usr/bin/env python3
"""Write the `current` release's pointer to its immutable dated twin.

`current`'s assets are overwritten in place on every build, so a client that
streams `current` with HTTP range reads can straddle a swap and read new bytes
at old offsets. This small JSON file, published on `current` alongside the
archive, names the dated `tiles-<DATE>` release holding the same bytes, so a
streaming client can read that immutable copy instead:

    {"schema": 1,
     "tag": "tiles-<DATE>",
     "pmtiles": "<base>_<DATE>.pmtiles",
     "sha256": "<64 hex>",
     "issued_at": "<UTC ISO-8601>"}

Clients build the download URL from their own constants plus `tag` and
`pmtiles`; they must never take a host or URL from this file. `sha256` and
`issued_at` are for tracing, and for verifying a download of the dated asset.

Stdlib only. Run:
    python write_current_pointer.py --date 2026-10-22T08-44-01Z \\
        --base-name philly_commute_region --sha256 <hex> \\
        --issued-at 2026-10-22T08:44:01Z --output out/philly_commute_region_current.pointer.json
"""

import argparse
import json
import re
import sys
from datetime import datetime

SCHEMA = 1

# The run date stamp (release-tiles.yml "Compute run date stamp"): naturally
# sortable ISO, filename-safe (no colons).
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z")
BASE_NAME_PATTERN = re.compile(r"[a-z0-9_]+")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ISSUED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def build_pointer(date, base_name, sha256, issued_at):
    """The pointer document; raises ValueError on any malformed field."""
    if not DATE_PATTERN.fullmatch(date):
        raise ValueError(f"--date must look like YYYY-MM-DDTHH-MM-SSZ, got {date!r}")
    if not BASE_NAME_PATTERN.fullmatch(base_name):
        raise ValueError(f"--base-name must be lowercase letters, digits and _, got {base_name!r}")
    sha256 = sha256.strip().lower()
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ValueError(f"--sha256 must be 64 hex characters, got {sha256!r}")
    datetime.strptime(issued_at, ISSUED_AT_FORMAT)  # raises ValueError if malformed
    return {
        "schema": SCHEMA,
        "tag": f"tiles-{date}",
        "pmtiles": f"{base_name}_{date}.pmtiles",
        "sha256": sha256,
        "issued_at": issued_at,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--date", required=True, help="run date stamp, YYYY-MM-DDTHH-MM-SSZ")
    parser.add_argument("--base-name", required=True, help="asset base name, e.g. philly_commute_region")
    parser.add_argument("--sha256", required=True, help="sha256 of the dated .pmtiles")
    parser.add_argument("--issued-at", required=True, help="build time, YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--output", required=True, help="where to write the pointer JSON")
    args = parser.parse_args(argv)
    try:
        pointer = build_pointer(args.date, args.base_name, args.sha256, args.issued_at)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    text = json.dumps(pointer, indent=2) + "\n"
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
