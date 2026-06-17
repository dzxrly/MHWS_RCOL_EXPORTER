from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .binary import display_path
from .api import RCOLConverter, find_il2cpp_dump, find_schema


def add_export_args(parser: argparse.ArgumentParser, positional: bool = False) -> None:
    if positional:
        parser.add_argument("path", help="RCOL file or directory containing *.rcol.<version> files.")
        schema_flags = ("--schema", "--schema-path")
        dump_flags = ("--il2cpp-dump", "--il2cpp-dump-path")
    else:
        schema_flags = ("--schema-path", "--schema", "-s")
        dump_flags = ("--il2cpp-dump-path", "--il2cpp-dump", "-p")
        parser.add_argument(
            "--input-dir",
            "-i",
            "--input",
            dest="input_path",
            required=True,
            help="RCOL file or directory containing *.rcol.<version> files.",
        )
    parser.add_argument(
        *schema_flags,
        dest="schema_path",
        help="Path to rszmhws.json. If omitted, nearby debug paths are searched.",
    )
    parser.add_argument(
        *dump_flags,
        dest="il2cpp_dump_path",
        help="Path to il2cpp_dump.json, used for enum labels and shape metadata.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        dest="output_dir",
        help="Optional output root. Defaults to writing next to the source file.",
    )
    parser.add_argument(
        "--format",
        choices=("readable", "repack", "both"),
        default="readable",
        help="readable is compact and editable; repack embeds lossless bytes.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional processing limit.")


def build_export_parser(prog: str = "rcol-exporter export") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Export RE Engine RCOL files to JSON.")
    add_export_args(parser)
    parser.set_defaults(func=run_export)
    return parser


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export RE Engine RCOL files to JSON.")
    add_export_args(parser, positional=True)
    parser.set_defaults(func=run_legacy_export)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcol-exporter",
        description="Convert RE Engine RCOL files to readable or lossless JSON.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command")

    export_parser = subparsers.add_parser("export", help="Export RCOL files to JSON.")
    add_export_args(export_parser)
    export_parser.set_defaults(func=run_export)

    web_parser = subparsers.add_parser("web", help="Start the local Web quick exporter.")
    web_parser.add_argument("--host", default="127.0.0.1", help="Host interface for the local server.")
    web_parser.add_argument("--port", type=int, default=8766, help="TCP port for the local server.")
    web_parser.set_defaults(func=run_web)
    return parser


def run_export(args: argparse.Namespace) -> int:
    schema_path = find_schema(args.input_path, args.schema_path)
    il2cpp_path = find_il2cpp_dump(args.input_path, args.il2cpp_dump_path)
    converter = RCOLConverter(schema_path=schema_path, il2cpp_dump_path=il2cpp_path)
    print(f"Schema: {display_path(schema_path)}")
    print(f"IL2CPP: {display_path(il2cpp_path) or 'not found; enum labels disabled'}")
    print(f"Format: {args.format}")
    summary = converter.export_path(
        args.input_path,
        output_root=args.output_dir,
        json_format=args.format,
        limit=args.limit,
    )
    for target in summary["written"]:
        print(f"[OK] {target}")
    for error in summary["errors"]:
        print(f"[ERROR] {error['source']}: {error['error']}", file=sys.stderr)
    print("Done:", json.dumps({k: v for k, v in summary.items() if k != "written"}, ensure_ascii=False))
    return 1 if summary["failed"] else 0


def run_legacy_export(args: argparse.Namespace) -> int:
    args.input_path = args.path
    return run_export(args)


def run_web(args: argparse.Namespace) -> int:
    from .web.server import run_server

    run_server(host=args.host, port=args.port)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] not in {"export", "web", "-h", "--help", "--version"}:
        legacy = build_legacy_parser()
        args = legacy.parse_args(raw_args)
        return args.func(args)
    parser = build_parser()
    args = parser.parse_args(raw_args)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)
