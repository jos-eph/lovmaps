#!/usr/bin/env python3
"""Stdlib-only unit tests for the boundary-line + county-label helpers in
generate_tiles_pb.py (spec Chunk E).

No osmium / tippecanoe / network required — these exercise the pure-Python
geometry logic so they run locally and in CI for negligible cost.

Run:  python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_tiles_pb as g  # noqa: E402


# Geometry fixtures (closed rings, GeoJSON winding-agnostic for these tests).
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
HOLE = [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]
SQUARE2 = [[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]]


def _write_geojsonseq(features):
    """Write features to a temp GeoJSONSeq file (0x1e-prefixed) and return path."""
    fd, path = tempfile.mkstemp(suffix=".geojsonseq")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for feat in features:
            f.write("\x1e" + json.dumps(feat) + "\n")
    return path


def _read_geojsonseq(path):
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().strip("\x1e")
            if line:
                out.append(json.loads(line))
    return out


def _feature(geometry, **props):
    return {"type": "Feature", "properties": props, "geometry": geometry}


class IterBoundaryLineStrings(unittest.TestCase):
    def test_polygon_yields_one_ring(self):
        rings = list(g.iter_boundary_linestrings({"type": "Polygon", "coordinates": [SQUARE]}))
        self.assertEqual(rings, [SQUARE])

    def test_polygon_with_hole_yields_two_rings(self):
        rings = list(g.iter_boundary_linestrings(
            {"type": "Polygon", "coordinates": [SQUARE, HOLE]}))
        self.assertEqual(rings, [SQUARE, HOLE])

    def test_multipolygon_yields_rings_for_each_part(self):
        rings = list(g.iter_boundary_linestrings(
            {"type": "MultiPolygon", "coordinates": [[SQUARE], [SQUARE2]]}))
        self.assertEqual(rings, [SQUARE, SQUARE2])

    def test_linestring_passthrough(self):
        line = [[0, 0], [1, 1]]
        self.assertEqual(
            list(g.iter_boundary_linestrings({"type": "LineString", "coordinates": line})),
            [line])

    def test_multilinestring_explodes(self):
        a, b = [[0, 0], [1, 1]], [[2, 2], [3, 3]]
        self.assertEqual(
            list(g.iter_boundary_linestrings({"type": "MultiLineString", "coordinates": [a, b]})),
            [a, b])

    def test_point_and_null_yield_nothing(self):
        self.assertEqual(list(g.iter_boundary_linestrings({"type": "Point", "coordinates": [0, 0]})), [])
        self.assertEqual(list(g.iter_boundary_linestrings(None)), [])
        self.assertEqual(list(g.iter_boundary_linestrings({})), [])


class NormalizeBoundary(unittest.TestCase):
    def test_admin_level_kept_as_int(self):
        self.assertEqual(g.normalize_boundary({"admin_level": "6"}), {"admin_level": 6})
        self.assertIsInstance(g.normalize_boundary({"admin_level": 6})["admin_level"], int)

    def test_out_of_set_dropped(self):
        self.assertIsNone(g.normalize_boundary({"admin_level": 10}))
        self.assertIsNone(g.normalize_boundary({"admin_level": 2}))

    def test_non_numeric_dropped(self):
        self.assertIsNone(g.normalize_boundary({"admin_level": "city"}))
        self.assertIsNone(g.normalize_boundary({}))


class NormalizeBoundaryGeojsonseq(unittest.TestCase):
    def test_polygon_becomes_linestrings_with_antidrop(self):
        in_path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="6", name="Phila"),
            _feature({"type": "MultiPolygon", "coordinates": [[SQUARE], [SQUARE2]]}, admin_level="4"),
            _feature({"type": "Point", "coordinates": [0, 0]}, admin_level="6"),  # no lines
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="10"),  # dropped
        ])
        out_path = in_path + ".out"
        try:
            g.normalize_boundary_geojsonseq(in_path, out_path)
            feats = _read_geojsonseq(out_path)
        finally:
            os.remove(in_path)
            if os.path.exists(out_path):
                os.remove(out_path)

        # 1 ring from the Polygon (level 6) + 2 rings from the MultiPolygon (level 4).
        self.assertEqual(len(feats), 3)
        for feat in feats:
            self.assertEqual(feat["geometry"]["type"], "LineString")
            self.assertEqual(feat["tippecanoe"], {"minzoom": 10, "maxzoom": 13})
            self.assertIsInstance(feat["properties"]["admin_level"], int)
            self.assertNotIn("name", feat["properties"])  # boundary props are admin_level only
        self.assertEqual({f["properties"]["admin_level"] for f in feats}, {4, 6})


class AssertBoundaryLinesOnly(unittest.TestCase):
    def _run(self, features):
        path = _write_geojsonseq(features)
        try:
            g.assert_boundary_lines_only(path)
        finally:
            os.remove(path)

    def test_passes_on_all_lines(self):
        self._run([
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, admin_level=6),
            _feature({"type": "MultiLineString", "coordinates": [[[0, 0], [1, 1]]]}, admin_level=4),
        ])

    def test_raises_on_polygon(self):
        with self.assertRaises(g.BoundaryAssertionError):
            self._run([_feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level=6)])

    def test_raises_on_string_admin_level(self):
        with self.assertRaises(g.BoundaryAssertionError):
            self._run([_feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, admin_level="6")])

    def test_raises_on_bool_admin_level(self):
        # bool is an int subclass; must be rejected.
        with self.assertRaises(g.BoundaryAssertionError):
            self._run([_feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, admin_level=True)])


class CountyLabels(unittest.TestCase):
    def test_centroid_inside_square(self):
        pt = g.county_label_point({"type": "Polygon", "coordinates": [SQUARE]})
        self.assertTrue(0 < pt[0] < 10 and 0 < pt[1] < 10, pt)

    def test_one_label_per_named_county(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="6", name="Test County"),
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}, admin_level="8", name="Some Township"),
        ])
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        lab = labels[0]
        self.assertEqual(lab["geometry"]["type"], "Point")
        self.assertEqual(lab["properties"], {"class": "county", "name": "Test County"})
        self.assertEqual(lab["tippecanoe"], {"minzoom": 6})
        x, y = lab["geometry"]["coordinates"]
        self.assertTrue(0 < x < 10 and 0 < y < 10)

    def test_consolidated_city_county_yields_one_label(self):
        # Same name at admin_level 6 and 8 (Philadelphia case) -> exactly one,
        # taken from level 6.
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="6", name="Philadelphia"),
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="8", name="Philadelphia"),
        ])
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["properties"]["name"], "Philadelphia")

    def test_unnamed_county_skipped(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]}, admin_level="6"),
        ])
        try:
            self.assertEqual(list(g.iter_county_labels(path)), [])
        finally:
            os.remove(path)


class MergeDedupesSharedBoundaryWay(unittest.TestCase):
    """Chunk 3C: a way shared between two clipped sources (e.g. the PA/NJ
    river boundary, present in both the PA clip and the NJ clip) must end up
    as exactly one boundary line, not two.

    `osmium merge` (Chunk 3A's merge_regions(), see its docstring) dedupes
    objects with identical (type, id, version) at the PBF level, before
    export/normalize ever run -- so it can't be exercised here without a
    real osmium binary (that's Chunk 3D, the human smoke test). This test
    instead simulates merge's documented identity-based dedup in pure
    Python on synthetic "raw export" features, then runs the real
    production normalizer (normalize_boundary_geojsonseq) on the result, to
    confirm the rest of the pipeline doesn't reintroduce a duplicate.
    """

    @staticmethod
    def _dedupe_by_osm_identity(features):
        """Stand-in for `osmium merge`: keep one copy per (type, id, version)."""
        seen = set()
        out = []
        for feat in features:
            props = feat["properties"]
            key = (props["@type"], props["@id"], props["@version"])
            if key in seen:
                continue
            seen.add(key)
            out.append(feat)
        return out

    @staticmethod
    def _shared_way():
        # The PA/NJ river boundary way: identical id/version/geometry as
        # exported independently by both the PA clip and the NJ clip.
        return _feature(
            {"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 2]]},
            admin_level="6", **{"@type": "way", "@id": 100, "@version": 3},
        )

    @staticmethod
    def _pa_only_way():
        return _feature(
            {"type": "LineString", "coordinates": [[5, 5], [6, 6]]},
            admin_level="6", **{"@type": "way", "@id": 101, "@version": 1},
        )

    @staticmethod
    def _nj_only_way():
        return _feature(
            {"type": "LineString", "coordinates": [[7, 7], [8, 8]]},
            admin_level="6", **{"@type": "way", "@id": 102, "@version": 1},
        )

    def _normalize(self, features):
        raw_path = _write_geojsonseq(features)
        norm_path = raw_path + ".norm"
        try:
            g.normalize_boundary_geojsonseq(raw_path, norm_path)
            return _read_geojsonseq(norm_path)
        finally:
            os.remove(raw_path)
            if os.path.exists(norm_path):
                os.remove(norm_path)

    def test_dedup_then_normalize_yields_one_line_for_shared_way(self):
        clip_a = [self._shared_way(), self._pa_only_way()]
        clip_b = [self._shared_way(), self._nj_only_way()]

        merged = self._dedupe_by_osm_identity(clip_a + clip_b)
        self.assertEqual(len(merged), 3, "shared way should collapse to one copy")

        out = self._normalize(merged)
        self.assertEqual(len(out), 3)
        shared_coords = [[0, 0], [1, 1], [2, 2]]
        matches = [f for f in out if f["geometry"]["coordinates"] == shared_coords]
        self.assertEqual(len(matches), 1, "shared way must produce exactly one boundary line")

    def test_without_dedup_shared_way_duplicates(self):
        # Negative control: naively concatenating both clips' raw exports
        # without the merge/dedup step does duplicate the shared way's
        # line -- demonstrating why merge_regions()/osmium merge is needed,
        # and that the dedup step above is doing real work.
        clip_a = [self._shared_way(), self._pa_only_way()]
        clip_b = [self._shared_way(), self._nj_only_way()]
        naive = clip_a + clip_b

        out = self._normalize(naive)
        self.assertEqual(len(out), 4)
        shared_coords = [[0, 0], [1, 1], [2, 2]]
        matches = [f for f in out if f["geometry"]["coordinates"] == shared_coords]
        self.assertEqual(len(matches), 2, "without dedup the shared way is duplicated")


if __name__ == "__main__":
    unittest.main()
