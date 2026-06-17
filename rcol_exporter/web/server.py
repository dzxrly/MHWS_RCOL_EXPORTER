from __future__ import annotations

import argparse
import cgi
import html
import json
import re
import tempfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..binary import display_path
from ..api import RCOLConverter, find_il2cpp_dump, find_schema


DEFAULT_FORM = {
    "input_path": "",
    "schema_path": "debug/rszmhws.json",
    "il2cpp_dump_path": "debug/il2cpp_dump.json",
    "output_dir": "output",
    "format": "readable",
    "limit": "0",
}


@dataclass
class UploadedFile:
    field_name: str
    filename: str
    data: bytes


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def make_handler() -> type[BaseHTTPRequestHandler]:
    class RCOLWebHandler(BaseHTTPRequestHandler):
        server_version = "RCOLExporterWeb/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_text("ok\n", "text/plain; charset=utf-8")
                return
            if parsed.path == "/pick":
                self._send_picker_response(parsed.query)
                return
            self._send_html(render_page(DEFAULT_FORM, None))

        def do_POST(self) -> None:
            form, uploads = parse_post_data(self)
            values = {**DEFAULT_FORM, **form}
            try:
                result = run_export_from_form(values, uploads)
                if self.path == "/api/export":
                    self._send_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "application/json")
                    return
                self._send_html(render_page(values, result))
            except Exception as exc:
                result = {"failed": 1, "error": f"{exc.__class__.__name__}: {exc}"}
                if self.path == "/api/export":
                    self._send_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "application/json", 500)
                    return
                self._send_html(render_page(values, result), 500)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _send_html(self, text: str, status: int = 200) -> None:
            self._send_text(text, "text/html; charset=utf-8", status)

        def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
            payload = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_picker_response(self, query: str) -> None:
            mode = (parse_qs(query).get("mode") or [""])[0]
            try:
                result = {"path": pick_local_path(mode)}
                status = 200
            except Exception as exc:
                result = {"path": "", "error": f"{exc.__class__.__name__}: {exc}"}
                status = 500
            self._send_text(json.dumps(result, ensure_ascii=False) + "\n", "application/json", status)

    return RCOLWebHandler


def parse_post_data(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], list[UploadedFile]]:
    content_type = handler.headers.get("Content-Type", "")
    if content_type.startswith("multipart/form-data"):
        form = cgi.FieldStorage(
            fp=handler.rfile,
            headers=handler.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
            },
            keep_blank_values=True,
        )
        values: dict[str, str] = {}
        uploads: list[UploadedFile] = []
        for item in form.list or []:
            if item.filename:
                data = item.file.read()
                if data:
                    uploads.append(
                        UploadedFile(
                            field_name=item.name or "rcol_files",
                            filename=item.filename,
                            data=data,
                        )
                    )
            else:
                if item.name:
                    values[item.name] = item.value if isinstance(item.value, str) else ""
        return values, uploads

    length = int(handler.headers.get("Content-Length", "0") or "0")
    body = handler.rfile.read(length).decode("utf-8", errors="replace")
    values = {key: items[-1] for key, items in parse_qs(body, keep_blank_values=True).items()}
    return values, []


def run_export_from_form(values: dict[str, str], uploads: list[UploadedFile] | None = None) -> dict[str, Any]:
    uploads = uploads or []
    input_path = values.get("input_path", "").strip()
    schema_text = values.get("schema_path", "").strip() or None
    il2cpp_text = values.get("il2cpp_dump_path", "").strip() or None
    output_text = values.get("output_dir", "").strip() or None
    json_format = values.get("format", "readable").strip() or "readable"
    limit = int(values.get("limit", "0") or "0")

    if uploads:
        with tempfile.TemporaryDirectory(prefix="rcol_exporter_upload_") as temp_dir:
            upload_root = Path(temp_dir)
            rcol_uploads = [item for item in uploads if item.field_name == "rcol_files"]
            schema_uploads = [item for item in uploads if item.field_name == "schema_file"]
            il2cpp_uploads = [item for item in uploads if item.field_name == "il2cpp_file"]

            saved_files = save_uploads(rcol_uploads, upload_root / "rcol") if rcol_uploads else []
            directory_upload = any("/" in item.filename.replace("\\", "/") for item in rcol_uploads)
            if schema_uploads:
                schema_text = str(save_uploads(schema_uploads[:1], upload_root / "metadata" / "schema")[0])
            if il2cpp_uploads:
                il2cpp_text = str(save_uploads(il2cpp_uploads[:1], upload_root / "metadata" / "il2cpp")[0])

            if saved_files:
                export_source = upload_root / "rcol" if directory_upload or len(saved_files) > 1 else saved_files[0]
            elif input_path:
                export_source = Path(input_path)
            else:
                raise ValueError("select an RCOL file or directory")

            schema_path = find_schema(export_source, schema_text)
            il2cpp_path = find_il2cpp_dump(export_source, il2cpp_text)
            converter = RCOLConverter(schema_path=schema_path, il2cpp_dump_path=il2cpp_path)
            result = converter.export_path(
                export_source,
                output_root=output_text,
                json_format=json_format,
                limit=limit,
            )
            if saved_files:
                result["uploaded"] = [path.relative_to(upload_root / "rcol").as_posix() for path in saved_files]
            result["schema"] = display_path(schema_path)
            result["il2cpp_dump"] = display_path(il2cpp_path)
            return result

    if not input_path:
        raise ValueError("select an RCOL file or directory")

    schema_path = find_schema(input_path, schema_text)
    il2cpp_path = find_il2cpp_dump(input_path, il2cpp_text)
    converter = RCOLConverter(schema_path=schema_path, il2cpp_dump_path=il2cpp_path)
    result = converter.export_path(input_path, output_root=output_text, json_format=json_format, limit=limit)
    result["schema"] = display_path(schema_path)
    result["il2cpp_dump"] = display_path(il2cpp_path)
    return result


def save_uploads(uploads: list[UploadedFile], root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    used_names: set[str] = set()
    for upload in uploads:
        relative_path = safe_upload_path(upload.filename)
        if relative_path is None:
            continue
        target = unique_path(root, relative_path, used_names)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(upload.data)
        saved.append(target)
    if not saved:
        raise ValueError("selected files were empty")
    return saved


def safe_upload_path(filename: str) -> Path | None:
    parts: list[str] = []
    for raw_part in filename.replace("\\", "/").split("/"):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_part.strip())
        safe = safe.strip("._")
        if not safe or safe in {".", ".."}:
            continue
        parts.append(safe)
    if not parts:
        return None
    return Path(*parts)


def unique_path(root: Path, relative_path: Path, used_names: set[str]) -> Path:
    parent = relative_path.parent
    filename = relative_path.name
    candidate = filename
    stem = Path(filename).stem
    suffix = "".join(Path(filename).suffixes)
    if suffix:
        stem = filename[: -len(suffix)]
    index = 1
    candidate_path = parent / candidate
    while candidate_path.as_posix().lower() in used_names or (root / candidate_path).exists():
        candidate = f"{stem}_{index}{suffix}"
        candidate_path = parent / candidate
        index += 1
    used_names.add(candidate_path.as_posix().lower())
    return root / candidate_path


def pick_local_path(mode: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    if mode != "output_dir":
        raise ValueError(f"unsupported picker mode: {mode}")

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(parent=root)
        return selected or ""
    finally:
        root.destroy()


def render_page(values: dict[str, str], result: dict[str, Any] | None) -> str:
    result_text = html.escape(json.dumps(result, ensure_ascii=False, indent=2)) if result is not None else ""
    result_hidden = "" if result is not None else " hidden"
    result_html = (
        f"<section class=\"result\" data-result{result_hidden}>"
        "<h2>Result</h2>"
        f"<pre data-result-output>{result_text}</pre>"
        "</section>"
    )
    format_value = values.get("format", "readable")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RCOL Exporter</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Segoe UI", Arial, sans-serif;
      background: #f4f7fb;
      color: #202124;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
    }}
    main {{
      width: min(1040px, calc(100% - 40px));
      margin: 0 auto;
      padding: 32px 0 44px;
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 24px;
      border-bottom: 1px solid #d8dee8;
      padding-bottom: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .panel, .result {{
      background: #ffffff;
      border: 1px solid #d8dee8;
      border-radius: 8px;
      padding: 20px;
    }}
    form {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    label {{
      display: grid;
      gap: 7px;
      font-size: 13px;
      font-weight: 650;
      color: #3f4754;
    }}
    .file-picker {{
      grid-column: 1 / -1;
      display: grid;
      gap: 10px;
    }}
    .source-buttons {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .source-status {{
      min-height: 22px;
      color: #4b5563;
      font-size: 13px;
      word-break: break-word;
    }}
    .hidden-file {{
      position: absolute;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }}
    input, select {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #bcc6d4;
      border-radius: 6px;
      padding: 10px 11px;
      font: inherit;
      background: #ffffff;
      color: #202124;
    }}
    .field-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: end;
    }}
    .field-row input {{
      min-width: 0;
    }}
    .wide {{
      grid-column: 1 / -1;
    }}
    .actions {{
      grid-column: 1 / -1;
      display: flex;
      justify-content: flex-end;
      margin-top: 4px;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      background: #155e63;
      color: white;
      font: inherit;
      font-weight: 700;
      padding: 10px 18px;
      cursor: pointer;
    }}
    button:hover {{
      background: #0f4d52;
    }}
    button.secondary {{
      background: #e7edf4;
      color: #1f2937;
      border: 1px solid #c8d2df;
    }}
    button.secondary:hover {{
      background: #dce6f1;
    }}
    button:disabled {{
      opacity: .66;
      cursor: wait;
    }}
    .progress {{
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      color: #3f4754;
      font-size: 13px;
    }}
    .progress[hidden] {{
      display: none;
    }}
    .progress-track {{
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: #dbe4ee;
    }}
    .progress-bar {{
      width: 42%;
      height: 100%;
      border-radius: inherit;
      background: #155e63;
      animation: loading-bar 1s ease-in-out infinite;
    }}
    @keyframes loading-bar {{
      0% {{ transform: translateX(-120%); }}
      50% {{ transform: translateX(55%); }}
      100% {{ transform: translateX(240%); }}
    }}
    .result {{
      margin-top: 18px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 18px;
    }}
    pre {{
      margin: 0;
      overflow: auto;
      background: #111827;
      color: #f8fafc;
      border-radius: 6px;
      padding: 14px;
      line-height: 1.45;
    }}
    @media (max-width: 720px) {{
      form {{
        grid-template-columns: 1fr;
      }}
      header {{
        display: block;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>RCOL Exporter</h1>
    </header>
    <section class="panel">
      <form method="post" action="/" enctype="multipart/form-data" data-export-form>
        {file_picker()}
        {path_field("output_dir", "Output directory", values, "output_dir")}
        {path_field("schema_path", "rszmhws.json", values, "schema_file", wide=True)}
        {path_field("il2cpp_dump_path", "il2cpp_dump.json", values, "il2cpp_file", wide=True)}
        <label>
          Format
          <select name="format">
            {option("readable", format_value)}
            {option("repack", format_value)}
            {option("both", format_value)}
          </select>
        </label>
        {input_field("limit", "Limit", values)}
        <div class="progress" data-progress hidden>
          <div class="progress-track"><div class="progress-bar"></div></div>
          <div>Exporting...</div>
        </div>
        <div class="actions"><button type="submit" data-submit>Export</button></div>
      </form>
    </section>
    {result_html}
  </main>
  <script>
    const form = document.querySelector('[data-export-form]');
    const fileInput = document.querySelector('#rcol-files');
    const dirInput = document.querySelector('#rcol-directory');
    const schemaFile = document.querySelector('#schema-file');
    const il2cppFile = document.querySelector('#il2cpp-file');
    const sourceStatus = document.querySelector('[data-source-status]');
    const progress = document.querySelector('[data-progress]');
    const submitButton = document.querySelector('[data-submit]');
    const resultPanel = document.querySelector('[data-result]');
    const resultOutput = document.querySelector('[data-result-output]');
    const syncFileList = () => {{
      const fileNames = Array.from(fileInput.files || []).map(file => file.name);
      const dirNames = Array.from(dirInput.files || []).map(file => file.webkitRelativePath || file.name);
      const names = [...fileNames, ...dirNames];
      if (names.length) {{
        sourceStatus.textContent = `${{names.length}} selected`;
      }} else {{
        sourceStatus.textContent = '';
      }}
    }};
    fileInput.addEventListener('change', () => {{
      if (fileInput.files.length) {{
        dirInput.value = '';
      }}
      syncFileList();
    }});
    dirInput.addEventListener('change', () => {{
      if (dirInput.files.length) {{
        fileInput.value = '';
      }}
      syncFileList();
    }});
    const syncMetadataFile = input => {{
      const target = document.querySelector(`#${{input.dataset.target}}`);
      if (input.files.length && target) {{
        target.value = input.files[0].name;
      }}
    }};
    const pickOutputDir = async () => {{
      const response = await fetch('/pick?mode=output_dir');
      const data = await response.json();
      if (!response.ok) {{
        throw new Error(data.error || 'Picker failed');
      }}
      if (data.path) {{
        document.querySelector('#output-dir').value = data.path;
      }}
    }};
    schemaFile.addEventListener('change', () => syncMetadataFile(schemaFile));
    il2cppFile.addEventListener('change', () => syncMetadataFile(il2cppFile));
    document.querySelectorAll('[data-trigger-file]').forEach(button => {{
      button.addEventListener('click', () => document.querySelector(`#${{button.dataset.triggerFile}}`).click());
    }});
    document.querySelector('[data-output-picker]').addEventListener('click', async () => {{
      try {{
        await pickOutputDir();
      }} catch (error) {{
        resultPanel.hidden = false;
        resultOutput.textContent = error.message;
      }}
    }});
    form.addEventListener('submit', async event => {{
      event.preventDefault();
      const payload = new FormData();
      for (const name of ['input_path', 'output_dir', 'schema_path', 'il2cpp_dump_path', 'format', 'limit']) {{
        const field = form.elements[name];
        if (field) {{
          payload.append(name, field.value || '');
        }}
      }}
      Array.from(fileInput.files || []).forEach(file => payload.append('rcol_files', file, file.name));
      Array.from(dirInput.files || []).forEach(file => payload.append('rcol_files', file, file.webkitRelativePath || file.name));
      if (schemaFile.files.length) {{
        payload.append('schema_file', schemaFile.files[0], schemaFile.files[0].name);
      }}
      if (il2cppFile.files.length) {{
        payload.append('il2cpp_file', il2cppFile.files[0], il2cppFile.files[0].name);
      }}
      progress.hidden = false;
      submitButton.disabled = true;
      resultPanel.hidden = false;
      resultOutput.textContent = 'Exporting...';
      try {{
        const response = await fetch('/api/export', {{ method: 'POST', body: payload }});
        const text = await response.text();
        let data;
        try {{
          data = JSON.parse(text);
        }} catch {{
          data = {{ error: text }};
        }}
        resultOutput.textContent = JSON.stringify(data, null, 2);
      }} catch (error) {{
        resultOutput.textContent = error.message;
      }} finally {{
        progress.hidden = true;
        submitButton.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""


def file_picker() -> str:
    return """<div class="file-picker">
          <label>RCOL source</label>
          <input id="input-path" type="hidden" name="input_path" value="">
          <input id="rcol-directory" class="hidden-file" type="file" multiple webkitdirectory directory>
          <input id="rcol-files" class="hidden-file" type="file" name="rcol_files" accept=".rcol,.28,.38">
          <div class="source-buttons">
            <button class="secondary" type="button" data-trigger-file="rcol-files">Select file</button>
            <button class="secondary" type="button" data-trigger-file="rcol-directory">Select folder</button>
          </div>
          <div class="source-status" data-source-status></div>
        </div>"""


def path_field(name: str, label: str, values: dict[str, str], picker: str, wide: bool = False) -> str:
    class_name = " class=\"wide\"" if wide else ""
    value = html.escape(values.get(name, ""))
    field_id = name.replace("_", "-")
    if picker == "output_dir":
        extra = ""
        button = '<button class="secondary" type="button" data-output-picker>Choose</button>'
    elif picker == "schema_file":
        extra = '<input id="schema-file" class="hidden-file" type="file" data-target="schema-path" accept=".json">'
        button = '<button class="secondary" type="button" data-trigger-file="schema-file">Choose</button>'
    elif picker == "il2cpp_file":
        extra = '<input id="il2cpp-file" class="hidden-file" type="file" data-target="il2cpp-dump-path" accept=".json">'
        button = '<button class="secondary" type="button" data-trigger-file="il2cpp-file">Choose</button>'
    else:
        raise ValueError(f"unsupported picker: {picker}")
    return (
        f'<label{class_name}>{html.escape(label)}'
        f'<div class="field-row"><input id="{field_id}" name="{name}" value="{value}">{button}</div>'
        f'{extra}</label>'
    )


def input_field(name: str, label: str, values: dict[str, str], wide: bool = False) -> str:
    class_name = " class=\"wide\"" if wide else ""
    value = html.escape(values.get(name, ""))
    return f"<label{class_name}>{html.escape(label)}<input name=\"{name}\" value=\"{value}\"></label>"


def option(value: str, selected: str) -> str:
    attr = " selected" if value == selected else ""
    return f"<option value=\"{value}\"{attr}>{value}</option>"


def run_server(host: str = "127.0.0.1", port: int = 8766) -> None:
    handler = make_handler()
    server = ReusableThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"RCOL Exporter Web is running at: {url}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local RCOL exporter Web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface where the local server listens.")
    parser.add_argument("--port", type=int, default=8766, help="TCP port where the local server listens.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_server(host=args.host, port=args.port)
    return 0
