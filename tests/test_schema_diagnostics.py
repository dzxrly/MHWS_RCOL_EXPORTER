from __future__ import annotations

import unittest

from rcol_exporter.rsz import schema_compatibility_status


class SchemaCompatibilityTests(unittest.TestCase):
    def test_exact_metadata_is_compatible(self) -> None:
        self.assertEqual(schema_compatibility_status(20, 0, 0, 0), "compatible")

    def test_mostly_known_metadata_is_partial(self) -> None:
        self.assertEqual(schema_compatibility_status(9, 1, 0, 1), "partial")

    def test_unknown_metadata_is_incompatible(self) -> None:
        self.assertEqual(schema_compatibility_status(0, 10, 0, 10), "incompatible")


if __name__ == "__main__":
    unittest.main()
