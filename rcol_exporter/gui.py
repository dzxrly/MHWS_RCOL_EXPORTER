from __future__ import annotations

import json
import queue
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .api import RCOLConverter, discover_rcol_files, find_il2cpp_dump, find_schema
from .binary import display_path


FORMAT_VALUES = ("readable", "readable-debug", "repack", "both")


@dataclass(frozen=True)
class ExportOptions:
    input_path: str
    output_dir: str = ""
    schema_path: str = ""
    il2cpp_dump_path: str = ""
    json_format: str = "readable"
    limit: int = 0


@dataclass(frozen=True)
class ResolvedExportOptions:
    input_path: Path
    output_dir: Path | None
    schema_path: Path
    il2cpp_dump_path: Path | None
    json_format: str
    limit: int


def resolve_export_options(options: ExportOptions) -> ResolvedExportOptions:
    if options.limit < 0:
        raise ValueError("Limit 不能小于 0")
    if options.json_format not in FORMAT_VALUES:
        raise ValueError(f"不支持的输出格式: {options.json_format}")

    input_text = options.input_path.strip()
    if not input_text:
        raise ValueError("请选择 RCOL 文件或目录")
    input_path = Path(input_text).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"RCOL 输入不存在: {input_path}")
    if not discover_rcol_files(input_path):
        raise ValueError(f"没有找到 *.rcol.<version> 文件: {input_path}")

    schema_text = options.schema_path.strip()
    if not schema_text:
        raise ValueError("请选择 RSZ 模板 JSON")
    schema_path = find_schema(input_path, schema_text)
    il2cpp_text = options.il2cpp_dump_path.strip()
    il2cpp_path = find_il2cpp_dump(input_path, il2cpp_text) if il2cpp_text else None
    output_dir = Path(options.output_dir.strip()).expanduser() if options.output_dir.strip() else None
    if output_dir is not None and output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"输出路径不是目录: {output_dir}")

    return ResolvedExportOptions(
        input_path=input_path,
        output_dir=output_dir,
        schema_path=schema_path,
        il2cpp_dump_path=il2cpp_path,
        json_format=options.json_format,
        limit=options.limit,
    )


def export_with_options(options: ExportOptions) -> dict[str, Any]:
    resolved = resolve_export_options(options)
    converter = RCOLConverter(
        schema_path=resolved.schema_path,
        il2cpp_dump_path=resolved.il2cpp_dump_path,
    )
    result = converter.export_path(
        resolved.input_path,
        output_root=resolved.output_dir,
        json_format=resolved.json_format,
        limit=resolved.limit,
    )
    result["schema"] = display_path(resolved.schema_path)
    result["il2cpp_dump"] = display_path(resolved.il2cpp_dump_path)
    return result


def format_export_summary(result: dict[str, Any], max_paths: int = 100) -> str:
    lines = [
        f"总文件数: {result.get('total', 0)}",
        f"成功: {result.get('exported', 0)}",
        f"失败: {result.get('failed', 0)}",
        f"RSZ 模板: {result.get('schema') or '-'}",
        f"IL2CPP: {result.get('il2cpp_dump') or '-'}",
    ]
    consensus = result.get("layout_consensus")
    if consensus:
        lines.append(
            "目录布局共识: "
            + json.dumps(consensus, ensure_ascii=False, separators=(", ", ": "))
        )

    written = list(result.get("written") or [])
    if written:
        lines.extend(("", "输出文件:"))
        lines.extend(f"  {path}" for path in written[:max_paths])
        if len(written) > max_paths:
            lines.append(f"  ... 另有 {len(written) - max_paths} 个文件")

    errors = list(result.get("errors") or [])
    if errors:
        lines.extend(("", "错误:"))
        for item in errors[:max_paths]:
            lines.append(f"  {item.get('source', '-')}: {item.get('error', 'unknown error')}")
        if len(errors) > max_paths:
            lines.append(f"  ... 另有 {len(errors) - max_paths} 个错误")
    return "\n".join(lines)


class RCOLExporterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.busy = False
        self.browse_buttons: list[ttk.Button] = []

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar(value="output")
        self.schema_var = tk.StringVar()
        self.il2cpp_var = tk.StringVar()
        self.format_var = tk.StringVar(value="readable")
        self.limit_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="请选择输入文件或目录")

        self._configure_window()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("RCOL Exporter")
        self.root.geometry("900x680")
        self.root.minsize(760, 560)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#52606d")

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=18)
        container.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        ttk.Label(container, text="RCOL Exporter", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            container,
            text="将 RE Engine RCOL 文件转换为 readable 或 repack JSON",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 14))

        form = ttk.LabelFrame(container, text="导出设置", padding=12)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        self._add_path_row(
            form,
            0,
            "RCOL 输入",
            self.input_var,
            (("选择文件", self._choose_input_file), ("选择目录", self._choose_input_directory)),
        )
        self._add_path_row(
            form,
            1,
            "输出目录",
            self.output_var,
            (("选择", self._choose_output_directory),),
        )
        self._add_path_row(
            form,
            2,
            "RSZ 模板",
            self.schema_var,
            (("选择", self._choose_schema),),
        )
        self._add_path_row(
            form,
            3,
            "IL2CPP",
            self.il2cpp_var,
            (("选择", self._choose_il2cpp),),
        )

        options = ttk.Frame(form)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="输出格式").grid(row=0, column=0, sticky="w")
        self.format_box = ttk.Combobox(
            options,
            textvariable=self.format_var,
            values=FORMAT_VALUES,
            state="readonly",
            width=14,
        )
        self.format_box.grid(row=0, column=1, sticky="w", padx=(10, 28))
        ttk.Label(options, text="Limit（0 表示全部）").grid(row=0, column=2, sticky="e")
        self.limit_spinbox = ttk.Spinbox(
            options,
            from_=0,
            to=1_000_000,
            textvariable=self.limit_var,
            width=10,
        )
        self.limit_spinbox.grid(row=0, column=3, sticky="w", padx=(10, 0))

        result_frame = ttk.LabelFrame(container, text="运行结果", padding=8)
        result_frame.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        self.result_text = tk.Text(
            result_frame,
            wrap="none",
            height=14,
            font=("Consolas", 10),
            relief="flat",
            padx=8,
            pady=8,
        )
        vertical = ttk.Scrollbar(result_frame, orient="vertical", command=self.result_text.yview)
        horizontal = ttk.Scrollbar(result_frame, orient="horizontal", command=self.result_text.xview)
        self.result_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        action_row = ttk.Frame(container)
        action_row.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        action_row.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(action_row, mode="indeterminate", length=150)
        self.progress.grid(row=0, column=0, sticky="w")
        ttk.Label(action_row, textvariable=self.status_var, style="Hint.TLabel").grid(
            row=0, column=1, sticky="w", padx=12
        )
        self.export_button = ttk.Button(action_row, text="开始导出", command=self._start_export)
        self.export_button.grid(row=0, column=2, sticky="e")

    def _add_path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        buttons: tuple[tuple[str, Any], ...],
    ) -> None:
        ttk.Label(parent, text=label, width=11).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=(8, 8), pady=4
        )
        button_frame = ttk.Frame(parent)
        button_frame.grid(row=row, column=2, sticky="e", pady=4)
        for index, (text, command) in enumerate(buttons):
            button = ttk.Button(button_frame, text=text, command=command)
            button.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0))
            self.browse_buttons.append(button)

    def _choose_input_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择 RCOL 文件",
            initialdir=self._initial_directory(self.input_var.get()),
            filetypes=(("RCOL files", "*.rcol.*"), ("All files", "*.*")),
        )
        if selected:
            self.input_var.set(selected)

    def _choose_input_directory(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择包含 RCOL 文件的目录",
            initialdir=self._initial_directory(self.input_var.get()),
        )
        if selected:
            self.input_var.set(selected)

    def _choose_output_directory(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择输出目录",
            initialdir=self._initial_directory(self.output_var.get()),
        )
        if selected:
            self.output_var.set(selected)

    def _choose_schema(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择 RSZ 模板",
            initialdir=self._initial_directory(self.schema_var.get()),
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if selected:
            self.schema_var.set(selected)

    def _choose_il2cpp(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择 IL2CPP dump",
            initialdir=self._initial_directory(self.il2cpp_var.get()),
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if selected:
            self.il2cpp_var.set(selected)

    @staticmethod
    def _initial_directory(value: str) -> str:
        path = Path(value.strip()).expanduser() if value.strip() else Path.cwd()
        if path.is_file():
            path = path.parent
        elif not path.is_dir():
            path = path.parent if path.parent.is_dir() else Path.cwd()
        return str(path)

    def _start_export(self) -> None:
        if self.busy:
            return
        try:
            limit = int(self.limit_var.get().strip() or "0")
        except ValueError:
            messagebox.showerror("参数错误", "Limit 必须是整数", parent=self.root)
            return
        options = ExportOptions(
            input_path=self.input_var.get(),
            output_dir=self.output_var.get(),
            schema_path=self.schema_var.get(),
            il2cpp_dump_path=self.il2cpp_var.get(),
            json_format=self.format_var.get(),
            limit=limit,
        )
        self._set_busy(True)
        self._set_result("正在加载 metadata 并探测 RCOL 布局...\n")
        thread = threading.Thread(target=self._export_worker, args=(options,), daemon=True)
        thread.start()
        self.root.after(100, self._poll_worker)

    def _export_worker(self, options: ExportOptions) -> None:
        try:
            self.events.put(("success", export_with_options(options)))
        except Exception as exc:
            self.events.put(
                (
                    "error",
                    {
                        "message": f"{exc.__class__.__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    },
                )
            )

    def _poll_worker(self) -> None:
        try:
            kind, payload = self.events.get_nowait()
        except queue.Empty:
            if self.busy:
                self.root.after(100, self._poll_worker)
            return

        self._set_busy(False)
        if kind == "success":
            result = payload
            self._set_result(format_export_summary(result))
            failed = int(result.get("failed") or 0)
            if failed:
                self.status_var.set(f"导出完成，{failed} 个文件失败")
                messagebox.showwarning(
                    "导出完成",
                    f"成功 {result.get('exported', 0)}，失败 {failed}。请查看运行结果。",
                    parent=self.root,
                )
            else:
                self.status_var.set(f"导出完成：{result.get('exported', 0)} 个文件")
            return

        self._set_result(payload["traceback"])
        self.status_var.set("导出失败")
        messagebox.showerror("导出失败", payload["message"], parent=self.root)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.export_button.configure(state="disabled" if busy else "normal")
        for button in self.browse_buttons:
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.status_var.set("正在导出...")
            self.progress.start(12)
        else:
            self.progress.stop()

    def _set_result(self, text: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", text)
        self.result_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "退出",
            "导出仍在进行，确定要退出吗？",
            parent=self.root,
        ):
            return
        self.root.destroy()


def run_gui() -> None:
    root = tk.Tk()
    RCOLExporterApp(root)
    root.mainloop()


def main() -> int:
    run_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
