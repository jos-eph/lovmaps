#!/usr/bin/env python3
"""Stdlib-only unit tests for write_current_pointer.py.

Run:  python -m unittest discover -s tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import write_current_pointer as w  # noqa: E402

DATE = "2026-10-22T08-44-01Z"
SHA = "ab" * 32
ISSUED = "2026-10-22T08:44:01Z"


class BuildPointer(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(
            w.build_pointer(DATE, "philly_commute_region", SHA, ISSUED),
            {
                "schema": 1,
                "tag": "tiles-2026-10-22T08-44-01Z",
                "pmtiles": "philly_commute_region_2026-10-22T08-44-01Z.pmtiles",
                "sha256": SHA,
                "issued_at": ISSUED,
            },
        )

    def test_tag_and_filename_share_the_date(self):
        pointer = w.build_pointer(DATE, "philly_commute_region", SHA, ISSUED)
        self.assertEqual(pointer["tag"].removeprefix("tiles-"), DATE)
        self.assertTrue(pointer["pmtiles"].endswith(f"_{DATE}.pmtiles"))

    def test_sha_is_normalized(self):
        pointer = w.build_pointer(DATE, "philly_commute_region", " " + SHA.upper() + "\n", ISSUED)
        self.assertEqual(pointer["sha256"], SHA)

    def test_rejects_malformed_fields(self):
        bad = [
            ("2026-10-22T08:44:01Z", "philly_commute_region", SHA, ISSUED),  # colons
            ("2026-10-22", "philly_commute_region", SHA, ISSUED),
            (DATE, "../evil", SHA, ISSUED),
            (DATE, "Philly", SHA, ISSUED),
            (DATE, "philly_commute_region", "ab" * 31, ISSUED),
            (DATE, "philly_commute_region", "zz" * 32, ISSUED),
            (DATE, "philly_commute_region", SHA, "2026-10-22 08:44:01"),
        ]
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    w.build_pointer(*args)


class Main(unittest.TestCase):
    def test_writes_and_prints_the_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pointer.json")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = w.main(["--date", DATE, "--base-name", "philly_commute_region",
                               "--sha256", SHA, "--issued-at", ISSUED, "--output", out])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as handle:
                written = handle.read()
            self.assertEqual(written, stdout.getvalue())
            self.assertEqual(json.loads(written)["tag"], f"tiles-{DATE}")

    def test_bad_input_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pointer.json")
            with redirect_stderr(io.StringIO()):
                code = w.main(["--date", "nope", "--base-name", "philly_commute_region",
                               "--sha256", SHA, "--issued-at", ISSUED, "--output", out])
            self.assertEqual(code, 1)
            self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
