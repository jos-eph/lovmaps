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

    def test_open_linestring_geometry_still_yields_a_label(self):
        # Simulates a county relation that didn't reassemble into a closed
        # Polygon after the bbox extract (spec §2.2(2)) -- must not be
        # silently dropped.
        path = _write_geojsonseq([
            _feature({"type": "LineString", "coordinates": [[1, 1], [2, 2], [3, 3]]},
                     admin_level="6", name="Clipped County"),
        ])
        try:
            labels = list(g.iter_county_labels(path))
        finally:
            os.remove(path)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["properties"]["name"], "Clipped County")

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
        self.assertGreaterEqual(len(g.EXPECTED_COUNTY_LABELS), 11)

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


if __name__ == "__main__":
    unittest.main()
