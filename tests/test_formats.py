from __future__ import annotations

import unittest
from pathlib import Path

from rcol_exporter.api import default_output_path, normalize_json_format, normalize_json_formats
from rcol_exporter.formats import build_readable, build_readable_debug


class ReadableFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = {
            "groupInfos": [{"groupName": "Attack", "_raw": {"offset": "0x70"}}],
            "requestSets": [{"requestSetIndex": 0, "_raw": {"offset": "0x100"}}],
            "ignoreTags": [],
            "rcol_layout": {"confidence": 1.0},
            "rsz": {"_diagnostics": {"schema_compatibility": "compatible"}},
        }
        self.schema = Path("schema.json")

    def test_readable_omits_debug_information(self) -> None:
        document = build_readable(self.parsed, self.schema, None)

        self.assertEqual(set(document), {"groupInfos", "requestSets", "ignoreTags"})
        self.assertNotIn("_raw", document["groupInfos"][0])
        self.assertNotIn("_raw", document["requestSets"][0])

    def test_readable_debug_adds_diagnostics(self) -> None:
        document = build_readable_debug(self.parsed, self.schema, None)

        self.assertEqual(document["_diagnostics"]["rcol_layout"]["confidence"], 1.0)
        self.assertEqual(
            document["_diagnostics"]["rsz"]["schema_compatibility"],
            "compatible",
        )

    def test_format_name_accepts_hyphenated_debug_mode(self) -> None:
        self.assertEqual(normalize_json_format("readable-debug"), "readable_debug")
        self.assertEqual(normalize_json_formats("both"), ["readable", "repack"])

    def test_split_debug_output_uses_the_public_format_name(self) -> None:
        target = default_output_path(
            Path("sample.rcol.37"),
            Path("."),
            Path("output"),
            "readable_debug",
            True,
        )

        self.assertEqual(target.name, "sample.rcol.37.readable-debug.json")


if __name__ == "__main__":
    unittest.main()
