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


class ClipRingToBbox(unittest.TestCase):
    def test_clips_square_to_overlapping_bbox(self):
        # SQUARE is [0,0]-[10,10]; bbox keeps only the right half.
        clipped = g.clip_ring_to_bbox(SQUARE, (5, -5, 20, 20))
        xs = [p[0] for p in clipped]
        self.assertTrue(all(x >= 5 - 1e-9 for x in xs), clipped)
        self.assertTrue(any(x > 5 for x in xs), clipped)
        self.assertEqual(clipped[0], clipped[-1])  # stays closed

    def test_ring_entirely_outside_bbox_yields_empty(self):
        clipped = g.clip_ring_to_bbox(SQUARE, (100, 100, 200, 200))
        self.assertEqual(clipped, [])

    def test_ring_entirely_inside_bbox_is_unchanged_as_a_set(self):
        clipped = g.clip_ring_to_bbox(SQUARE, (-100, -100, 100, 100))
        self.assertEqual(set(map(tuple, clipped)), set(map(tuple, SQUARE)))


class PointOnSurface(unittest.TestCase):
    def test_convex_ring_uses_centroid(self):
        pt = g.point_on_surface(SQUARE)
        self.assertTrue(0 < pt[0] < 10 and 0 < pt[1] < 10, pt)

    def test_concave_ring_yields_a_truly_interior_point(self):
        # An L-shaped (concave) ring whose area-weighted centroid falls in the
        # notch, outside the polygon -- mirrors Montgomery Co. wrapping NW of
        # Philadelphia. point_on_surface must not just return that centroid.
        l_shape = [
            [0, 0], [10, 0], [10, 2], [2, 2], [2, 10], [0, 10], [0, 0],
        ]
        centroid = g._ring_centroid(l_shape)
        self.assertFalse(g._point_in_ring(centroid, l_shape),
                          "fixture invalid: centroid should fall outside the L")
        pt = g.point_on_surface(l_shape)
        self.assertTrue(g._point_in_ring(pt, l_shape),
                         f"point_on_surface returned an exterior point: {pt}")


class CountyLabels(unittest.TestCase):
    def test_centroid_inside_square(self):
        pt = g.label_point_for_geometry({"type": "Polygon", "coordinates": [SQUARE]})
        self.assertTrue(0 < pt[0] < 10 and 0 < pt[1] < 10, pt)

    def test_clips_to_bbox_when_county_mostly_outside_it(self):
        # A county whose full extent is [0,0]-[10,10] but the bbox only shows
        # its right edge: the label must land in the visible slice, not at the
        # full-shape centroid (which would be off-screen).
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Edge County"),
        ])
        try:
            labels = list(g.iter_county_labels(path, bbox=(8, -5, 20, 20)))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        x, y = labels[0]["geometry"]["coordinates"]
        self.assertGreaterEqual(x, 8)

    def test_linestring_geometry_no_longer_yields_a_label(self):
        # 07 resolution spec Chunk L7: the LineString/MultiLineString rescue
        # fallback is removed. It admitted rivers/roads whose OSM ways carry
        # admin_level=6 because they happen to form part of a county line
        # (spec §1 Cause 3 -- "Rocky Brook" etc. shipped as county labels).
        # The one legitimate historical beneficiary (an unassembled Delaware
        # County) is fixed upstream by `-S types=any` + the Feb 2026 OSM ring
        # repair; EXPECTED_COUNTY_LABELS now catches any future regression
        # loudly instead of silently rescuing it with a non-county label.
        path = _write_geojsonseq([
            _feature({"type": "LineString", "coordinates": [[1, 1], [2, 2], [3, 3]]},
                     admin_level="6", name="Clipped County"),
        ])
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        self.assertEqual(labels, [])

    def test_junk_river_and_road_linestrings_are_excluded(self):
        # Real junk observed in the shipped archive (07 spec §1 Cause 3): ways
        # that form part of a county boundary but carry admin_level=6 in OSM
        # without being a county at all. Must never become a class=county label.
        path = _write_geojsonseq([
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 0]]},
                     admin_level="6", name="Rocky Brook"),
            _feature({"type": "MultiLineString",
                      "coordinates": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]},
                     admin_level="6", name="Great Egg Harbor River"),
        ])
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        self.assertEqual(labels, [])

    def test_polygon_and_multipolygon_still_accepted(self):
        # Regression guard: only the LineString/MultiLineString branch is
        # removed -- real counties (Polygon/MultiPolygon) are unaffected.
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Real Polygon County"),
            _feature({"type": "MultiPolygon", "coordinates": [[SQUARE], [SQUARE2]]},
                     admin_level="6", name="Real MultiPolygon County"),
        ])
        try:
            names = {lab["properties"]["name"] for lab in g.iter_county_labels(path)}
        finally:
            os.remove(path)
        self.assertEqual(names, {"Real Polygon County", "Real MultiPolygon County"})

    def test_all_manifest_counties_as_polygons_still_pass_the_manifest(self):
        # Confirms removing the LineString fallback does not regress the
        # 11-county manifest when every county exports as a proper (closed)
        # Polygon -- the normal case post types=any + the OSM ring repair.
        features = [
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name=name)
            for name in g.EXPECTED_COUNTY_LABELS
        ]
        path = _write_geojsonseq(features)
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        place_path = path + ".place"
        with open(place_path, "w", encoding="utf-8") as f:
            for lab in labels:
                f.write("\x1e" + json.dumps(lab) + "\n")
        try:
            _, missing_counties = g.check_label_manifest(place_path)
        finally:
            os.remove(place_path)
        self.assertEqual(missing_counties, [])

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


# A state-sized polygon: [0,0]-[100,100], whose full-area centroid (50, 50) is
# far outside the small bbox window the tests below clip it to -- mirroring
# Pennsylvania's centroid landing near Harrisburg, ~150km from the SEPTA bbox.
STATE_LIKE = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]
STATE_BBOX = (40, 40, 60, 60)


class AppendCountyLabelsLogsNames(unittest.TestCase):
    # 07 resolution spec Chunk L7: emitted label *names* are printed to the
    # build log -- previously only a count was logged, so nobody could tell
    # from CI output whether the "26 county labels" were real counties or the
    # junk from Cause 3 (closes that gap).
    def test_prints_emitted_county_names(self):
        raw_path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Bucks County"),
            _feature({"type": "Polygon", "coordinates": [SQUARE2]},
                     admin_level="6", name="Chester County"),
        ])
        place_path = raw_path + ".place"
        with open(place_path, "w", encoding="utf-8"):
            pass
        import contextlib
        import io
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                g.append_county_labels(raw_path, place_path)
        finally:
            os.remove(raw_path)
            if os.path.exists(place_path):
                os.remove(place_path)
        self.assertIn("Bucks County", out.getvalue())
        self.assertIn("Chester County", out.getvalue())


class StateLabels(unittest.TestCase):
    def test_whitelisted_state_label_lands_inside_the_bbox(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Pennsylvania"),
        ])
        try:
            labels = list(g.iter_state_labels(path, STATE_BBOX))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        lab = labels[0]
        self.assertEqual(lab["properties"], {"class": "state", "name": "Pennsylvania"})
        self.assertEqual(lab["tippecanoe"], {"minzoom": 6})
        x, y = lab["geometry"]["coordinates"]
        self.assertTrue(40 <= x <= 60 and 40 <= y <= 60,
                         f"label {x, y} not inside bbox {STATE_BBOX}")

    def test_non_whitelisted_state_is_skipped(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Maryland"),
        ])
        try:
            labels = list(g.iter_state_labels(path, STATE_BBOX))
        finally:
            os.remove(path)
        self.assertEqual(labels, [])

    def test_county_level_feature_is_not_mistaken_for_a_state(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="6", name="Pennsylvania"),  # wrong level
        ])
        try:
            labels = list(g.iter_state_labels(path, STATE_BBOX))
        finally:
            os.remove(path)
        self.assertEqual(labels, [])

    def test_all_three_whitelisted_states_can_be_emitted(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]}, admin_level="4", name="Pennsylvania"),
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]}, admin_level="4", name="New Jersey"),
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]}, admin_level="4", name="Delaware"),
        ])
        try:
            names = {lab["properties"]["name"] for lab in g.iter_state_labels(path, STATE_BBOX)}
        finally:
            os.remove(path)
        self.assertEqual(names, {"Pennsylvania", "New Jersey", "Delaware"})

    def test_append_state_labels_writes_class_state_features(self):
        raw_path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="New Jersey"),
        ])
        place_path = raw_path + ".place"
        with open(place_path, "w", encoding="utf-8") as f:
            pass
        try:
            g.append_state_labels(raw_path, place_path, STATE_BBOX)
            features = _read_geojsonseq(place_path)
        finally:
            os.remove(raw_path)
            if os.path.exists(place_path):
                os.remove(place_path)
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["properties"]["class"], "state")


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


class PlaceZoomFilterIncludesState(unittest.TestCase):
    def test_state_class_branch_present(self):
        # Regression guard: tippecanoe's -j feature-filter only keeps `place`
        # features matching one of these branches, so a "state" place feature
        # emitted by append_state_labels would be silently dropped at tiling
        # if this filter weren't updated alongside it (spec 02_LABELS Issue A).
        place_filter = g.ZOOM_FILTERS["place"]
        self.assertIn(["==", "class", "state"], place_filter)


class BoundaryZoomFilterGatesAdminLevel8(unittest.TestCase):
    # 03_Still_cant_see_counties Chunk 1B: admin_level==8 is every PA
    # municipality (hundreds), not just "cities" -- it must not be encoded
    # into tiles below z13, or the regional (z10-12) view is a purple web and
    # the dense z10 tile risks evicting the sparse county/state label points.
    def test_admin_8_gated_to_zoom_13(self):
        any_branches = g.ZOOM_FILTERS["boundary"][2]
        self.assertIn(["all", [">=", "$zoom", 13], ["==", "admin_level", 8]],
                      any_branches)
        self.assertNotIn(["all", [">=", "$zoom", 8], ["==", "admin_level", 8]],
                         any_branches)

    def test_admin_4_and_6_gates_unchanged(self):
        # Regression guard: only the admin_8 (municipal) gate moves; state
        # stays ungated and county stays gated at its existing zoom>=6.
        any_branches = g.ZOOM_FILTERS["boundary"][2]
        self.assertIn(["==", "admin_level", 4], any_branches)
        self.assertIn(["all", [">=", "$zoom", 6], ["==", "admin_level", 6]],
                      any_branches)


class TransportationZoomFilterGatesStreetsToZoom13(unittest.TestCase):
    # 08_fix_labels spec Chunk B1: street geometry (secondary/tertiary/minor/
    # service) is not needed above zoom 13 and costs ~7.5 MB compressed to
    # keep -- drop it below z13. Human override kept the motorway/trunk/
    # primary skeleton ungated (regional orientation at z10-12) rather than
    # gating it to z13 like the rest of the road classes.
    def test_no_road_class_branch_below_zoom_13(self):
        branches = g.ZOOM_FILTERS["transportation"]
        for branch in branches:
            if (isinstance(branch, list) and len(branch) == 3
                    and branch[0] == "all"
                    and isinstance(branch[2], list)
                    and branch[2][0] == "in" and branch[2][1] == "class"):
                classes = set(branch[2][2:])
                gated_streets = classes & {
                    "secondary", "tertiary", "minor", "service"}
                if gated_streets:
                    self.assertEqual(branch[1], [">=", "$zoom", 13],
                                      f"{gated_streets} must gate at z13")

    def test_motorway_trunk_primary_skeleton_ungated(self):
        # Human override (spec Chunk B1 annotation): retain the regional
        # skeleton at z10-12 rather than dropping it with the other classes.
        branches = g.ZOOM_FILTERS["transportation"]
        self.assertIn(["in", "class", "motorway", "trunk", "primary"],
                      branches)

    def test_rail_transit_still_admitted_at_zoom_10(self):
        branches = g.ZOOM_FILTERS["transportation"]
        self.assertIn(
            ["all", [">=", "$zoom", 10], ["in", "class", "rail", "transit"]],
            branches)

    # The z13 gate is the contract; the exact expression is not. C5 added a
    # class list beside the gate, so assert the behavior rather than the
    # literal, or the next legitimate edit breaks this again.
    def test_transportation_name_gates_at_zoom_13(self):
        expression = g.ZOOM_FILTERS["transportation_name"]
        self.assertTrue(evaluate_filter(expression, {"class": "primary"}, 13))
        self.assertFalse(evaluate_filter(expression, {"class": "primary"}, 12))


class GeneratePmtilesTileByteBudget(unittest.TestCase):
    # 03_Still_cant_see_counties Chunk 2A: belt-and-suspenders headroom for
    # the label points sharing the densest (z10) tile with the road network.
    def test_maximum_tile_bytes_is_500000(self):
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd

        orig_run = g.run
        g.run = fake_run
        try:
            g.generate_pmtiles([("layer", "/dev/null")], "/tmp/out.pmtiles")
        finally:
            g.run = orig_run

        self.assertIn("--maximum-tile-bytes=500000", captured["cmd"])
        self.assertNotIn("--maximum-tile-bytes=200000", captured["cmd"])


class DefaultBboxPin(unittest.TestCase):
    # 10 spec Appendix A / Chunk B1: DEFAULT_BBOX here and release-tiles.yml's
    # BBOX env var must stay byte-identical (the workflow's value is the one
    # that actually governs releases) -- this pins the Python side so a
    # future edit to one without the other is caught in review, not in prod.
    def test_widened_to_include_salem_and_new_castle_co(self):
        self.assertEqual(g.DEFAULT_BBOX, "-76.00,39.30,-74.30,40.40")


class PadBbox(unittest.TestCase):
    # 07 resolution spec Chunk L8: the padded-bbox string builder feeding
    # tippecanoe's --clip-bounding-box.
    def test_pads_each_side_by_default_amount(self):
        padded = g.pad_bbox("-76.00,39.60,-74.60,40.40")
        self.assertEqual(padded, "-76.15,39.45,-74.45,40.55")

    def test_custom_pad_amount(self):
        padded = g.pad_bbox("-76.00,39.60,-74.60,40.40", pad_deg=1.0)
        self.assertEqual(padded, "-77.0,38.6,-73.6,41.4")

    def test_no_float_repr_artifacts(self):
        # Plain float addition on these inputs produces -74.44999999999999;
        # the builder must round that away.
        padded = g.pad_bbox("-76.00,39.60,-74.60,40.40")
        for part in padded.split(","):
            self.assertNotIn("999999", part)
            self.assertNotIn("000000", part)


class GeneratePmtilesClipBoundingBox(unittest.TestCase):
    # 07 resolution spec Chunk L8: clip the archive back to (a padded) bbox
    # so the types=any ring overhang (z13 tile count 948 -> 3,594, +2.7 MB)
    # doesn't ship -- label placement is unaffected (computed pre-tippecanoe).
    def _captured_cmd(self, **kwargs):
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd

        orig_run = g.run
        g.run = fake_run
        try:
            g.generate_pmtiles([("layer", "/dev/null")], "/tmp/out.pmtiles", **kwargs)
        finally:
            g.run = orig_run
        return captured["cmd"]

    def test_bbox_given_adds_padded_clip_flag(self):
        cmd = self._captured_cmd(bbox="-76.00,39.60,-74.60,40.40")
        self.assertIn("--clip-bounding-box=-76.15,39.45,-74.45,40.55", cmd)

    def test_bbox_omitted_adds_no_clip_flag(self):
        cmd = self._captured_cmd()
        self.assertFalse(any(c.startswith("--clip-bounding-box") for c in cmd))


class ExtractRegionCompletesBoundaryRelations(unittest.TestCase):
    # 06 impl spec Chunk L1: `osmium extract`'s default strategy drops
    # relation member ways outside the bbox, and `--strategy smart` alone
    # only completes type=multipolygon relations. US admin boundaries are
    # type=boundary, so either way a cross-bbox state/county ring arrives
    # incomplete and `osmium export` emits nothing for it (the 7->9
    # county / 0 state label plateau). `-S types=any` completes every
    # relation type with at least one member in the bbox.
    BBOX = "-76.00,39.60,-74.60,40.40"

    def _captured_cmd(self):
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd

        orig_run = g.run
        g.run = fake_run
        try:
            g.extract_region("in.pbf", self.BBOX, "out.pbf")
        finally:
            g.run = orig_run
        return captured["cmd"]

    def test_extract_uses_smart_strategy_with_types_any(self):
        cmd = self._captured_cmd()
        # Each option must directly precede its value or osmium misparses.
        i = cmd.index("--strategy")
        self.assertEqual(cmd[i + 1], "smart")
        j = cmd.index("-S")
        self.assertEqual(cmd[j + 1], "types=any")

    def test_extract_still_passes_bbox_input_and_output(self):
        cmd = self._captured_cmd()
        self.assertEqual(cmd[:2], ["osmium", "extract"])
        k = cmd.index("--bbox")
        self.assertEqual(cmd[k + 1], self.BBOX)
        self.assertIn("in.pbf", cmd)
        m = cmd.index("--output")
        self.assertEqual(cmd[m + 1], "out.pbf")


class ExportShowsAssemblyErrors(unittest.TestCase):
    # 06 impl spec Chunk L2: by default `osmium export` SILENTLY ignores
    # geometries it cannot build -- a county whose ring doesn't close just
    # vanishes from the output (Delaware County, relation 417846). With
    # --show-errors each failure becomes a stderr line naming the object,
    # which reaches the CI log because run() inherits stderr. No
    # --stop-on-error: one bad POI polygon must not kill the build; the
    # label manifest (Chunk L3), not raw error text, is the failure gate.
    def _captured_cmd(self):
        captured = {}

        def fake_run(cmd):
            captured["cmd"] = cmd

        orig_run = g.run
        g.run = fake_run
        try:
            g.export_geojsonseq("in.pbf", "out.geojsonseq")
        finally:
            g.run = orig_run
        return captured["cmd"]

    def test_export_shows_errors(self):
        cmd = self._captured_cmd()
        self.assertIn("--show-errors", cmd)
        self.assertNotIn("--stop-on-error", cmd)

    def test_export_format_and_output_args_unchanged(self):
        cmd = self._captured_cmd()
        self.assertEqual(cmd[:2], ["osmium", "export"])
        self.assertIn("in.pbf", cmd)
        i = cmd.index("-f")
        self.assertEqual(cmd[i + 1], "geojsonseq")
        j = cmd.index("-o")
        self.assertEqual(cmd[j + 1], "out.geojsonseq")


def _label(cls, name):
    """A place-layer label feature as append_*_labels writes them."""
    props = {"class": cls}
    if name is not None:
        props["name"] = name
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Point", "coordinates": [-75.2, 39.9]},
    }


class LabelManifestCheck(unittest.TestCase):
    # 06 impl spec Chunk L3: prompts 02->06 all happened because label loss
    # was silent -- a green build with a hole in the map. After the label
    # appends, the emitted names are compared against a static expected list
    # and the build FAILS naming what's missing. Superset semantics: extra
    # labels (neighbor-state bleed like Cecil MD / Richmond NY) are fine,
    # missing expected ones are not. Fixtures derive from the constants so
    # correcting a display-name string later doesn't rewrite these tests.

    def _complete_labels(self):
        return ([_label("state", n) for n in g.EXPECTED_STATE_LABELS]
                + [_label("county", n) for n in g.EXPECTED_COUNTY_LABELS])

    def test_expected_constants_exist_and_are_sane(self):
        # The manifest must expect exactly what emission can produce: states
        # come from the whitelist, and the core county list must include the
        # county whose silent loss started this saga.
        self.assertEqual(set(g.EXPECTED_STATE_LABELS), g.STATE_LABEL_WHITELIST)
        self.assertIn("Delaware County", g.EXPECTED_COUNTY_LABELS)
        # 11 pre-10-spec + Atlantic/Cumberland Co. NJ from Appendix A's bbox
        # widening (10 spec Chunk B1).
        self.assertGreaterEqual(len(g.EXPECTED_COUNTY_LABELS), 13)

    def test_all_present_plus_bleed_extras_passes(self):
        path = _write_geojsonseq(
            self._complete_labels()
            + [_label("county", "Cecil County"),      # MD sliver
               _label("county", "Richmond County"),   # NY bleed
               _label("city", "Philadelphia")])       # other class ignored
        missing_states, missing_counties = g.check_label_manifest(path)
        self.assertEqual(missing_states, [])
        self.assertEqual(missing_counties, [])

    def test_missing_county_is_named(self):
        labels = [f for f in self._complete_labels()
                  if f["properties"].get("name") != "Delaware County"]
        path = _write_geojsonseq(labels)
        missing_states, missing_counties = g.check_label_manifest(path)
        self.assertEqual(missing_states, [])
        self.assertEqual(missing_counties, ["Delaware County"])

    def test_missing_state_is_named(self):
        labels = [f for f in self._complete_labels()
                  if f["properties"].get("name") != "New Jersey"]
        path = _write_geojsonseq(labels)
        missing_states, missing_counties = g.check_label_manifest(path)
        self.assertEqual(missing_states, ["New Jersey"])
        self.assertEqual(missing_counties, [])

    def test_junk_lines_and_nameless_features_are_skipped(self):
        path = _write_geojsonseq(self._complete_labels()
                                 + [_label("county", None)])
        with open(path, "a", encoding="utf-8") as f:
            f.write("\x1enot json at all\n\n")
        missing_states, missing_counties = g.check_label_manifest(path)
        self.assertEqual((missing_states, missing_counties), ([], []))


class LabelManifestEnforcement(unittest.TestCase):
    # The main()-level gate around check_label_manifest: exit 1 with every
    # missing name printed, unless the emergency override env is set.

    def setUp(self):
        self._saved_env = os.environ.pop("LOVMAPS_ALLOW_MISSING_LABELS", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["LOVMAPS_ALLOW_MISSING_LABELS"] = self._saved_env
        else:
            os.environ.pop("LOVMAPS_ALLOW_MISSING_LABELS", None)

    def _incomplete_path(self):
        labels = ([_label("state", n) for n in g.EXPECTED_STATE_LABELS
                   if n != "Delaware"]
                  + [_label("county", n) for n in g.EXPECTED_COUNTY_LABELS
                     if n != "Delaware County"])
        return _write_geojsonseq(labels)

    def _complete_path(self):
        return _write_geojsonseq(
            [_label("state", n) for n in g.EXPECTED_STATE_LABELS]
            + [_label("county", n) for n in g.EXPECTED_COUNTY_LABELS])

    def test_missing_labels_exit_nonzero_and_name_the_gaps(self):
        import contextlib
        import io
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                g.enforce_label_manifest(self._incomplete_path())
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("Delaware County", out.getvalue())
        self.assertIn("Delaware", out.getvalue())

    def test_env_override_warns_but_does_not_exit(self):
        import contextlib
        import io
        os.environ["LOVMAPS_ALLOW_MISSING_LABELS"] = "1"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            g.enforce_label_manifest(self._incomplete_path())  # must not raise
        self.assertIn("MANIFEST OVERRIDE", out.getvalue())

    def test_complete_manifest_passes_quietly(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            g.enforce_label_manifest(self._complete_path())  # must not raise
        self.assertIn("manifest OK", out.getvalue())


class CountyLabelDedupeKey(unittest.TestCase):
    # 06 impl spec Chunk L4 / hazards H3-H5: dedupe by `name` alone collapses
    # same-name counties in different states (Mercer PA + Mercer NJ, in one
    # merged region) into a single label, and lets any future named duplicate
    # polygon win by size. Dedupe key is the first available of
    # wikidata -> nist:fips_code -> name; a data-poor county with neither
    # optional field must still get a label (never require optional fields).

    def _labels_for(self, features):
        path = _write_geojsonseq(features)
        try:
            return list(g.iter_county_labels(path))
        finally:
            os.remove(path)

    def test_same_name_different_wikidata_yields_two_labels(self):
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Mercer County", wikidata="Q495687"),
            _feature({"type": "Polygon", "coordinates": [SQUARE2]},
                     admin_level="6", name="Mercer County", wikidata="Q138464"),
        ])
        self.assertEqual(len(labels), 2)
        self.assertEqual({lab["properties"]["name"] for lab in labels},
                         {"Mercer County"})

    def test_same_wikidata_fragments_merge_keeping_larger(self):
        # One county split into two exported fragments (same wikidata):
        # exactly one label, placed within the larger fragment.
        small = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [small]},
                     admin_level="6", name="Sussex County", wikidata="Q156213"),
            _feature({"type": "Polygon", "coordinates": [SQUARE2]},
                     admin_level="6", name="Sussex County", wikidata="Q156213"),
        ])
        self.assertEqual(len(labels), 1)
        x, y = labels[0]["geometry"]["coordinates"]
        self.assertTrue(20 < x < 30 and 20 < y < 30, (x, y))

    def test_fips_key_used_when_wikidata_absent(self):
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Cumberland County",
                     **{"nist:fips_code": "42041"}),
            _feature({"type": "Polygon", "coordinates": [SQUARE2]},
                     admin_level="6", name="Cumberland County",
                     **{"nist:fips_code": "34011"}),
        ])
        self.assertEqual(len(labels), 2)

    def test_name_only_county_still_emitted(self):
        # Lycoming-style data poverty (no wikidata, no fips) -- the dedupe
        # upgrade must never drop it.
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Lycoming County"),
        ])
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["properties"]["name"], "Lycoming County")

    def test_nameless_feature_still_skipped_even_with_wikidata(self):
        # The unnamed duplicate Sussex polygon (hazard H3): has wikidata but
        # no label text -> no label, and no crash.
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", wikidata="Q156213"),
        ])
        self.assertEqual(labels, [])


class CountyLabelDebugProps(unittest.TestCase):
    # 06 impl spec Chunk L5 (optional): carry wikidata/fips onto the county
    # label so the NEXT silent gap can be diagnosed from the tile archive
    # itself. Keys are omitted (not null) when the source lacks them --
    # test_one_label_per_named_county already pins the bare-props case.

    def _labels_for(self, features):
        path = _write_geojsonseq(features)
        try:
            return list(g.iter_county_labels(path))
        finally:
            os.remove(path)

    def test_county_label_carries_wikidata_and_fips_when_present(self):
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Delaware County",
                     wikidata="Q27844", **{"nist:fips_code": "42045"}),
        ])
        self.assertEqual(len(labels), 1)
        props = labels[0]["properties"]
        self.assertEqual(props["wikidata"], "Q27844")
        self.assertEqual(props["fips"], "42045")

    def test_absent_keys_are_omitted_not_null(self):
        labels = self._labels_for([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Lycoming County",
                     wikidata="Q156334"),
        ])
        props = labels[0]["properties"]
        self.assertEqual(props["wikidata"], "Q156334")
        self.assertNotIn("fips", props)

    def test_state_labels_unchanged(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Delaware",
                     wikidata="Q1393", **{"ref:fips": "10"}),
        ])
        try:
            labels = list(g.iter_state_labels(path, STATE_BBOX))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["properties"],
                         {"class": "state", "name": "Delaware"})


class DouglasPeucker(unittest.TestCase):
    # 07 resolution spec Chunk L9: the simplification step for
    # region_labels.json ring geometry.
    def test_collinear_points_are_dropped(self):
        # A square ring with extra collinear points along each edge must
        # simplify down to just the four corners.
        ring = [
            [0, 0], [5, 0], [10, 0],
            [10, 5], [10, 10],
            [5, 10], [0, 10],
            [0, 5], [0, 0],
        ]
        simplified = g.douglas_peucker(ring, epsilon=0.01)
        self.assertEqual(simplified, [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])

    def test_point_beyond_epsilon_is_kept(self):
        # A notch that deviates well past epsilon from the straight line
        # must survive simplification.
        ring = [[0, 0], [5, 3], [10, 0]]
        simplified = g.douglas_peucker(ring, epsilon=0.5)
        self.assertIn([5, 3], simplified)

    def test_point_within_epsilon_is_dropped(self):
        ring = [[0, 0], [5, 0.01], [10, 0]]
        simplified = g.douglas_peucker(ring, epsilon=0.5)
        self.assertEqual(simplified, [[0, 0], [10, 0]])

    def test_short_input_returned_unchanged(self):
        self.assertEqual(g.douglas_peucker([], 1.0), [])
        self.assertEqual(g.douglas_peucker([[0, 0]], 1.0), [[0, 0]])
        self.assertEqual(g.douglas_peucker([[0, 0], [1, 1]], 1.0), [[0, 0], [1, 1]])

    def test_does_not_mutate_input(self):
        ring = [[0, 0], [5, 0.01], [10, 0]]
        original = [list(p) for p in ring]
        g.douglas_peucker(ring, epsilon=0.5)
        self.assertEqual(ring, original)


class RegionLabelRings(unittest.TestCase):
    def test_polygon_clipped_and_simplified(self):
        # Extra collinear points on the visible (right) half must simplify
        # away; the invisible left half must not appear at all.
        ring = [
            [0, 0], [5, 0], [10, 0], [10, 10], [5, 10], [0, 10], [0, 0],
        ]
        rings = g.region_label_rings(
            {"type": "Polygon", "coordinates": [ring]}, bbox=(5, -5, 20, 20),
            epsilon=0.01)
        self.assertEqual(len(rings), 1)
        xs = [p[0] for p in rings[0]]
        self.assertTrue(all(x >= 5 - 1e-9 for x in xs), rings[0])
        self.assertEqual(rings[0], [[5, 0], [10, 0], [10, 10], [5, 10], [5, 0]])

    def test_multipolygon_yields_one_ring_per_part(self):
        rings = g.region_label_rings(
            {"type": "MultiPolygon", "coordinates": [[SQUARE], [SQUARE2]]},
            bbox=(-100, -100, 100, 100), epsilon=0.01)
        self.assertEqual(len(rings), 2)

    def test_entirely_outside_bbox_yields_no_rings(self):
        rings = g.region_label_rings(
            {"type": "Polygon", "coordinates": [SQUARE]},
            bbox=(100, 100, 200, 200), epsilon=0.01)
        self.assertEqual(rings, [])


class IterRegionLabelFeatures(unittest.TestCase):
    def test_state_and_county_both_emitted(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Pennsylvania"),
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Maryland"),  # not whitelisted
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Bucks County"),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(-100, -100, 100, 100)))
        finally:
            os.remove(path)
        by_class = {(f["class"], f["name"]) for f in feats}
        self.assertIn(("state", "Pennsylvania"), by_class)
        self.assertIn(("county", "Bucks County"), by_class)
        self.assertNotIn(("state", "Maryland"), by_class)
        for f in feats:
            self.assertTrue(f["rings"])

    def test_dedupe_ladder_matches_county_labels(self):
        # Same wikidata, two fragments -> exactly one feature (Chunk L4 ladder).
        small = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [small]},
                     admin_level="6", name="Sussex County", wikidata="Q156213"),
            _feature({"type": "Polygon", "coordinates": [SQUARE2]},
                     admin_level="6", name="Sussex County", wikidata="Q156213"),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(-100, -100, 100, 100)))
        finally:
            os.remove(path)
        self.assertEqual(len(feats), 1)

    def test_junk_linestring_never_yields_a_region_label(self):
        path = _write_geojsonseq([
            _feature({"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 0]]},
                     admin_level="6", name="Rocky Brook"),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(-100, -100, 100, 100)))
        finally:
            os.remove(path)
        self.assertEqual(feats, [])

    def test_feature_with_no_surviving_rings_is_skipped(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Offscreen County"),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(100, 100, 200, 200)))
        finally:
            os.remove(path)
        self.assertEqual(feats, [])

    def test_wikidata_and_fips_carried_when_present(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Delaware County",
                     wikidata="Q27844", **{"nist:fips_code": "42045"}),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(-100, -100, 100, 100)))
        finally:
            os.remove(path)
        self.assertEqual(len(feats), 1)
        self.assertEqual(feats[0]["wikidata"], "Q27844")
        self.assertEqual(feats[0]["fips"], "42045")

    def test_absent_keys_omitted(self):
        path = _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Lycoming County"),
        ])
        try:
            feats = list(g.iter_region_label_features(path, bbox=(-100, -100, 100, 100)))
        finally:
            os.remove(path)
        self.assertNotIn("wikidata", feats[0])
        self.assertNotIn("fips", feats[0])


class RegionLabelsDocument(unittest.TestCase):
    # 07 resolution spec Chunk L9: schema round-trip + manifest wiring.
    def _raw_path(self):
        return _write_geojsonseq([
            _feature({"type": "Polygon", "coordinates": [STATE_LIKE]},
                     admin_level="4", name="Pennsylvania"),
            _feature({"type": "Polygon", "coordinates": [SQUARE]},
                     admin_level="6", name="Bucks County"),
        ])

    def test_build_region_labels_schema(self):
        raw_path = self._raw_path()
        try:
            doc = g.build_region_labels(raw_path, "-100,-100,100,100")
        finally:
            os.remove(raw_path)
        self.assertEqual(doc["version"], g.REGION_LABELS_SCHEMA_VERSION)
        self.assertEqual(doc["bbox"], "-100,-100,100,100")
        names = {f["name"] for f in doc["features"]}
        self.assertEqual(names, {"Pennsylvania", "Bucks County"})

    def test_write_region_labels_round_trips_through_json(self):
        raw_path = self._raw_path()
        out_path = raw_path + ".region_labels.json"
        try:
            doc = g.write_region_labels(raw_path, "-100,-100,100,100", out_path)
            with open(out_path, "r", encoding="utf-8") as f:
                reloaded = json.load(f)
        finally:
            os.remove(raw_path)
            if os.path.exists(out_path):
                os.remove(out_path)
        self.assertEqual(reloaded, doc)
        for feat in reloaded["features"]:
            self.assertIn("class", feat)
            self.assertIn("name", feat)
            self.assertIn("rings", feat)
            for ring in feat["rings"]:
                self.assertGreaterEqual(len(ring), 4)
                self.assertEqual(ring[0], ring[-1])


class RegionLabelsManifest(unittest.TestCase):
    # Mirrors LabelManifestCheck/LabelManifestEnforcement but against the
    # region_labels.json schema (top-level class/name per feature, not
    # nested under "properties").
    def setUp(self):
        self._saved_env = os.environ.pop("LOVMAPS_ALLOW_MISSING_LABELS", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["LOVMAPS_ALLOW_MISSING_LABELS"] = self._saved_env
        else:
            os.environ.pop("LOVMAPS_ALLOW_MISSING_LABELS", None)

    @staticmethod
    def _region_feature(cls, name):
        return {"class": cls, "name": name, "rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}

    def _write_doc(self, features):
        fd, path = tempfile.mkstemp(suffix=".region_labels.json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "bbox": "0,0,1,1", "features": features}, f)
        return path

    def _complete_features(self):
        return ([self._region_feature("state", n) for n in g.EXPECTED_STATE_LABELS]
                + [self._region_feature("county", n) for n in g.EXPECTED_COUNTY_LABELS])

    def test_complete_document_has_no_missing(self):
        path = self._write_doc(self._complete_features())
        try:
            missing_states, missing_counties = g.check_region_labels_manifest(path)
        finally:
            os.remove(path)
        self.assertEqual((missing_states, missing_counties), ([], []))

    def test_missing_county_is_named(self):
        features = [f for f in self._complete_features()
                    if f["name"] != "Delaware County"]
        path = self._write_doc(features)
        try:
            _, missing_counties = g.check_region_labels_manifest(path)
        finally:
            os.remove(path)
        self.assertEqual(missing_counties, ["Delaware County"])

    def test_enforce_exits_nonzero_and_names_the_gap(self):
        import contextlib
        import io
        features = [f for f in self._complete_features()
                    if f["name"] != "Delaware County"]
        path = self._write_doc(features)
        out = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stdout(out):
                    g.enforce_region_labels_manifest(path)
        finally:
            os.remove(path)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("Delaware County", out.getvalue())

    def test_enforce_env_override_warns_but_does_not_exit(self):
        import contextlib
        import io
        os.environ["LOVMAPS_ALLOW_MISSING_LABELS"] = "1"
        features = [f for f in self._complete_features()
                    if f["name"] != "Delaware County"]
        path = self._write_doc(features)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                g.enforce_region_labels_manifest(path)  # must not raise
        finally:
            os.remove(path)
        self.assertIn("MANIFEST OVERRIDE", out.getvalue())

    def test_enforce_complete_passes_quietly(self):
        import contextlib
        import io
        path = self._write_doc(self._complete_features())
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                g.enforce_region_labels_manifest(path)  # must not raise
        finally:
            os.remove(path)
        self.assertIn("manifest OK", out.getvalue())


class RegionLabelsSizeBudget(unittest.TestCase):
    # 07 resolution spec Chunk L9: "Douglas-Peucker simplified to a ~100 KB
    # total budget" -- a sanity check that REGION_LABELS_SIMPLIFY_EPSILON_DEG
    # actually keeps a realistically-complex build under the stated ~150 KB
    # ceiling, using synthetic county/state-sized noisy-circle rings as a
    # stand-in for real OSM detail (real validation happens against the
    # actual archive at the H7 human checkpoint).
    @staticmethod
    def _noisy_ring(cx, cy, radius, n=800, noise=0.0003, seed=0):
        import math
        rnd = __import__("random").Random(seed)
        pts = []
        for i in range(n):
            theta = 2 * math.pi * i / n
            r = radius + rnd.uniform(-noise, noise)
            pts.append([cx + r * math.cos(theta), cy + r * math.sin(theta)])
        pts.append(pts[0])
        return pts

    def test_fourteen_areas_stay_under_150kb(self):
        features = []
        # 11 counties + 3 states, spread so bbox-clipping doesn't trivially
        # drop any of them -- each is its own noisy ring, county-radius-ish.
        names = list(g.EXPECTED_COUNTY_LABELS) + list(g.EXPECTED_STATE_LABELS)
        for i, name in enumerate(names):
            cx, cy = (i % 4) * 3, (i // 4) * 3
            admin_level = "4" if name in g.STATE_LABEL_WHITELIST else "6"
            ring = self._noisy_ring(cx, cy, radius=1.0, seed=i)
            features.append(_feature({"type": "Polygon", "coordinates": [ring]},
                                      admin_level=admin_level, name=name))
        raw_path = _write_geojsonseq(features)
        out_path = raw_path + ".region_labels.json"
        try:
            doc = g.build_region_labels(raw_path, "-2,-2,20,20")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            size = os.path.getsize(out_path)
            raw_points = sum(len(self._noisy_ring(0, 0, 1.0, seed=i))
                              for i in range(len(names)))
            simplified_points = sum(len(r) for f in doc["features"] for r in f["rings"])
        finally:
            os.remove(raw_path)
            if os.path.exists(out_path):
                os.remove(out_path)
        self.assertEqual(len(doc["features"]), len(names))
        self.assertLess(simplified_points, raw_points,
                         "Douglas-Peucker did not reduce vertex count")
        self.assertLessEqual(size, 150_000, f"region_labels.json is {size} bytes")


# ---------------------------------------------------------------------------
# ZOOM_FILTERS: the -j payload handed to tippecanoe (C5 of the BusNeighbor
# tile_render_cost_chunked_spec.md; C6 was considered and declined).
#
# These are pure assertions over the filter dict. They deliberately do NOT run
# tippecanoe: a real tile build is billable CI minutes on a nonprofit's free
# tier, and the thing worth testing here is the filter logic, which is data.
#
# The evaluator below implements only the operators ZOOM_FILTERS actually uses.
# It is a test-local reimplementation of tippecanoe's -j semantics, so it
# proves the filter says what we think it says -- not that tippecanoe agrees.
# That second question is settled by a human smoke test on a real build, the
# same split already used for the `$type` guard on the boundary layer.
# ---------------------------------------------------------------------------

def _filter_value(token, properties, zoom):
    if token == "$zoom":
        return zoom
    if token == "$type":
        return properties.get("$type")
    return properties.get(token)


def evaluate_filter(expression, properties, zoom):
    """Evaluate a tippecanoe -j filter expression against one feature."""
    op = expression[0]
    if op == "any":
        return any(evaluate_filter(e, properties, zoom) for e in expression[1:])
    if op == "all":
        return all(evaluate_filter(e, properties, zoom) for e in expression[1:])
    if op == "in":
        return _filter_value(expression[1], properties, zoom) in expression[2:]
    if op == "==":
        return _filter_value(expression[1], properties, zoom) == expression[2]
    if op == ">=":
        value = _filter_value(expression[1], properties, zoom)
        return value is not None and value >= expression[2]
    raise AssertionError(f"unsupported filter operator {op!r}")


class ZoomFilterEvaluatorTest(unittest.TestCase):
    """Guards the evaluator itself, so a bug in it cannot silently make the
    filter assertions below vacuously pass."""

    def test_operators(self):
        self.assertTrue(evaluate_filter(["==", "class", "rail"], {"class": "rail"}, 10))
        self.assertFalse(evaluate_filter(["==", "class", "rail"], {"class": "minor"}, 10))
        self.assertTrue(evaluate_filter(["in", "class", "a", "b"], {"class": "b"}, 10))
        self.assertFalse(evaluate_filter(["in", "class", "a", "b"], {"class": "c"}, 10))
        self.assertTrue(evaluate_filter([">=", "$zoom", 13], {}, 13))
        self.assertFalse(evaluate_filter([">=", "$zoom", 13], {}, 12))
        self.assertTrue(evaluate_filter(
            ["all", [">=", "$zoom", 13], ["==", "class", "minor"]],
            {"class": "minor"}, 13))
        self.assertFalse(evaluate_filter(
            ["all", [">=", "$zoom", 13], ["==", "class", "minor"]],
            {"class": "minor"}, 12))
        self.assertTrue(evaluate_filter(
            ["any", ["==", "class", "x"], ["==", "class", "y"]], {"class": "y"}, 0))
        with self.assertRaises(AssertionError):
            evaluate_filter(["!=", "class", "x"], {"class": "y"}, 0)


class ZoomFiltersTest(unittest.TestCase):
    """What each layer admits, class by class and zoom by zoom."""

    def admits(self, layer, properties, zoom):
        return evaluate_filter(g.ZOOM_FILTERS[layer], properties, zoom)

    def road(self, klass, zoom, layer="transportation"):
        return self.admits(layer, {"class": klass, "$type": "LineString"}, zoom)

    # -- C5: class=service was 23.6% of z13 feature bytes and is selected by
    # no BusNeighbor style, in either layer.
    def test_service_roads_are_not_tiled(self):
        for zoom in (10, 11, 12, 13):
            self.assertFalse(self.road("service", zoom),
                             f"transportation admitted service at z{zoom}")
            self.assertFalse(self.road("service", zoom, "transportation_name"),
                             f"transportation_name admitted service at z{zoom}")

    # -- C6 was NOT taken for rail/transit: owner decision to keep them.
    # No BusNeighbor style draws rail lines, but this is a general-purpose tile
    # source and they cost ~0.33 MB at z13 (0.9% of feature bytes). The
    # behavioral half of test_rail_transit_still_admitted_at_zoom_10 above,
    # which pins the branch's shape.
    def test_rail_and_transit_are_still_tiled(self):
        for klass in ("rail", "transit"):
            for zoom in (10, 11, 12, 13):
                self.assertTrue(self.road(klass, zoom),
                                f"transportation dropped {klass} at z{zoom}")
            self.assertFalse(self.road(klass, 9),
                             f"{klass} is z10+, so z9 must reject it")

    # -- The other half of the contract: everything a style DOES select must
    # survive. A filter that drops a needed class is not a saving, it is a
    # missing road.
    def test_styled_road_classes_survive(self):
        for klass in ("motorway", "trunk", "primary"):
            self.assertTrue(self.road(klass, 10),
                            f"{klass} skeleton must reach z10 for orientation")
        for klass in ("secondary", "tertiary", "minor"):
            self.assertTrue(self.road(klass, 13), f"{klass} must reach z13")
            self.assertFalse(self.road(klass, 12),
                             f"{klass} is z13+, so z12 must reject it")

    def test_styled_road_names_survive(self):
        for klass in ("motorway", "trunk", "primary",
                      "secondary", "tertiary", "minor"):
            self.assertTrue(self.road(klass, 13, "transportation_name"),
                            f"{klass} names must reach z13")
            self.assertFalse(self.road(klass, 12, "transportation_name"),
                             "transportation_name is z13-only")

    # -- NOT changed by C6. The place layer still carries state and county
    # label points, because generate_tiles_pb.py synthesizes them on purpose
    # (iter_state_labels / iter_county_labels) and dropping the filter alone
    # would leave that machinery running into a tippecanoe that discards its
    # output. See the C6 note in the commit message.
    def test_place_still_carries_synthesized_area_labels(self):
        for klass in ("state", "county"):
            self.assertTrue(self.admits("place", {"class": klass}, 6),
                            f"place must still admit {klass} labels")

    def test_place_still_carries_settlement_labels(self):
        self.assertTrue(self.admits("place", {"class": "city"}, 4))
        self.assertTrue(self.admits("place", {"class": "town"}, 6))
        self.assertTrue(self.admits("place", {"class": "suburb"}, 10))
        self.assertTrue(self.admits("place", {"class": "neighbourhood"}, 12))

    # -- poi is untouched: every subclass it admits is drawn by a style.
    def test_poi_transit_stops_survive(self):
        self.assertTrue(self.admits("poi", {"subclass": "station"}, 11))
        self.assertTrue(self.admits("poi", {"subclass": "bus_stop"}, 13))
        self.assertFalse(self.admits("poi", {"subclass": "bus_stop"}, 12))

    def test_filter_payload_is_json_serializable(self):
        # tippecanoe receives this via json.dumps in generate_pmtiles; a
        # non-serializable value would fail at build time, not here.
        self.assertIsInstance(json.dumps(g.ZOOM_FILTERS), str)


if __name__ == "__main__":
    unittest.main()
