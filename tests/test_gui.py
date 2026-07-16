from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rcol_exporter.binary import display_path
from rcol_exporter.cli import build_parser
from rcol_exporter.gui import ExportOptions, format_export_summary, resolve_export_options


class GuiHelperTests(unittest.TestCase):
    def test_resolves_explicit_paths_without_a_version_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "sample.rcol.37"
            schema = root / "schema.json"
            il2cpp = root / "il2cpp.json"
            source.write_bytes(b"RCOL")
            schema.write_text("{}", encoding="utf-8")
            il2cpp.write_text("{}", encoding="utf-8")

            resolved = resolve_export_options(
                ExportOptions(
                    input_path=str(source),
                    output_dir=str(root / "output"),
                    schema_path=str(schema),
                    il2cpp_dump_path=str(il2cpp),
                    json_format="both",
                    limit=3,
                )
            )

            self.assertEqual(resolved.input_path, source)
            self.assertEqual(resolved.schema_path, schema)
            self.assertEqual(resolved.il2cpp_dump_path, il2cpp)
            self.assertEqual(resolved.json_format, "both")
            self.assertEqual(resolved.limit, 3)

    def test_rejects_a_negative_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "Limit"):
            resolve_export_options(ExportOptions(input_path="missing", limit=-1))

    def test_formats_a_bounded_result_log(self) -> None:
        text = format_export_summary(
            {
                "total": 3,
                "exported": 2,
                "failed": 1,
                "schema": "schema.json",
                "il2cpp_dump": "il2cpp.json",
                "written": ["one.json", "two.json"],
                "errors": [{"source": "bad.rcol.37", "error": "broken"}],
            },
            max_paths=1,
        )

        self.assertIn("成功: 2", text)
        self.assertIn("one.json", text)
        self.assertNotIn("two.json", text)
        self.assertIn("另有 1 个文件", text)
        self.assertIn("bad.rcol.37: broken", text)

    def test_cli_exposes_gui_and_ui_alias(self) -> None:
        parser = build_parser()
        self.assertTrue(hasattr(parser.parse_args(["gui"]), "func"))
        self.assertTrue(hasattr(parser.parse_args(["ui"]), "func"))

    def test_cli_accepts_readable_debug(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["export", "-i", "sample.rcol.37", "--format", "readable-debug"]
        )

        self.assertEqual(args.format, "readable-debug")

    def test_external_output_path_remains_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.json"

            self.assertEqual(display_path(path), str(path))


if __name__ == "__main__":
    unittest.main()
