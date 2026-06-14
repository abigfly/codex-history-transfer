#!/usr/bin/env python3
"""Tkinter GUI for codex-history-transfer."""

from __future__ import annotations

import argparse
import contextlib
import io
import queue
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import codex_history_transfer as cht


THREAD_COLUMNS = ("updated", "provider", "title", "cwd", "id")
PACKAGE_COLUMNS = ("path", "size")
BG = "#f5f7fb"
SURFACE = "#ffffff"
SURFACE_ALT = "#eef4ff"
BORDER = "#d9e2f1"
TEXT = "#162033"
MUTED = "#617089"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
SUCCESS = "#0f766e"
WARNING = "#b45309"


def list_threads(codex_home: Path, query: str, limit: int, include_archived: bool) -> list[dict[str, Any]]:
    _, conn = cht.connect_state_db(codex_home)
    try:
        where: list[str] = []
        params: list[Any] = []
        if not include_archived:
            where.append("archived = 0")
        if query:
            like = f"%{query}%"
            where.append("(id LIKE ? OR title LIKE ? OR preview LIKE ? OR first_user_message LIKE ?)")
            params.extend([like, like, like, like])

        sql = """
            SELECT id,title,preview,cwd,model_provider,model,archived,created_at,updated_at
            FROM threads
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [cht.row_to_dict(row) for row in conn.execute(sql, params).fetchall() if row]
    finally:
        conn.close()


def current_provider(codex_home: Path) -> str:
    _, conn = cht.connect_state_db(codex_home)
    try:
        return cht.detect_current_provider(conn) or ""
    finally:
        conn.close()


def capture_command(func: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(args)
    output = stdout.getvalue()
    errors = stderr.getvalue()
    if code:
        raise cht.TransferError((errors or output or f"Command failed with exit code {code}").strip())
    return (output + errors).strip()


def package_summary(package: Path) -> dict[str, Any]:
    manifest, rollout_text = cht.read_package(package)
    meta = cht.read_rollout_meta(rollout_text)
    payload = meta["payload"]
    with zipfile.ZipFile(package, "r") as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
    workspace_files = [
        info for info in infos if info.filename.startswith(manifest.get("workspace", {}).get("archive_prefix", "workspace/"))
    ]
    return {
        "manifest": manifest,
        "meta": meta,
        "thread_id": payload.get("id") or manifest.get("thread_id"),
        "title": manifest.get("thread", {}).get("title") or manifest.get("thread", {}).get("preview") or "(untitled)",
        "provider": payload.get("model_provider") or manifest.get("thread", {}).get("model_provider") or "",
        "cwd": payload.get("cwd") or manifest.get("thread", {}).get("cwd") or "",
        "workspace_included": bool(manifest.get("workspace", {}).get("included")),
        "workspace_files": len(workspace_files),
        "entries": infos,
    }


class CodexHistoryTransferApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"codex-history-transfer {cht.__version__}")
        self.geometry("1100x720")
        self.minsize(860, 560)
        self.configure(bg=BG)

        self.task_queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self.threads: dict[str, dict[str, Any]] = {}
        self.selected_thread_id: str | None = None
        self.package_path: Path | None = None
        self.package_data: dict[str, Any] | None = None

        self.codex_home_var = tk.StringVar(value=str(cht.default_codex_home()))
        self.search_var = tk.StringVar()
        self.limit_var = tk.IntVar(value=50)
        self.include_archived_var = tk.BooleanVar(value=False)
        self.include_workspace_var = tk.BooleanVar(value=False)
        self.export_path_var = tk.StringVar()

        self.import_package_var = tk.StringVar()
        self.provider_var = tk.StringVar(value="current")
        self.target_cwd_var = tk.StringVar()
        self.workspace_root_var = tk.StringVar()
        self.overwrite_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")
        self.export_summary_var = tk.StringVar(value="Select a Codex conversation, then export a transfer zip.")
        self.import_summary_var = tk.StringVar(value="Open a transfer zip to inspect it before importing.")

        self._build_ui()
        self.after(100, self.refresh_threads)
        self.after(150, self._poll_tasks)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self._configure_styles(style)

        header = ttk.Frame(self, style="Hero.TFrame", padding=(14, 10, 14, 10))
        header.pack(fill="x")
        title_box = ttk.Frame(header, style="Hero.TFrame")
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="codex-history-transfer", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text=f"Version {cht.__version__} - Offline Windows transfer for Codex Desktop history",
            style="HeroSubtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Label(header, text="Local only", style="Pill.TLabel").pack(side="right")

        top = ttk.Frame(self, padding=(10, 8, 10, 6), style="App.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Codex home", style="FieldLabel.TLabel").pack(side="left")
        ttk.Entry(top, textvariable=self.codex_home_var, style="App.TEntry").pack(side="left", fill="x", expand=True, padx=(10, 8))
        ttk.Button(top, text="Browse", command=self.browse_codex_home, style="Secondary.TButton").pack(side="left")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.export_tab = ttk.Frame(self.notebook)
        self.import_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.export_tab, text=" Export ")
        self.notebook.add(self.import_tab, text=" Import ")
        self._build_export_tab()
        self._build_import_tab()

        status = ttk.Frame(self, padding=(10, 0, 10, 6), style="App.TFrame")
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")

    def _configure_styles(self, style: ttk.Style) -> None:
        default_font = ("Segoe UI", 10)
        self.option_add("*Font", default_font)
        self.option_add("*Text.Font", ("Segoe UI", 10))

        style.configure("App.TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE, relief="solid", borderwidth=1)
        style.configure("Hero.TFrame", background="#111827")
        style.configure("HeroTitle.TLabel", background="#111827", foreground="#ffffff", font=("Segoe UI Semibold", 16))
        style.configure("HeroSubtitle.TLabel", background="#111827", foreground="#cbd5e1", font=("Segoe UI", 10))
        style.configure("Pill.TLabel", background="#dbeafe", foreground=ACCENT_DARK, padding=(12, 6), font=("Segoe UI Semibold", 9))
        style.configure("FieldLabel.TLabel", background=BG, foreground=MUTED, font=("Segoe UI Semibold", 9))
        style.configure("CardTitle.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI Semibold", 12))
        style.configure("CardText.TLabel", background=SURFACE, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Summary.TLabel", background=SURFACE_ALT, foreground=TEXT, padding=(12, 8), font=("Segoe UI", 10))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI Semibold", 10))
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), foreground=TEXT)
        style.configure("Primary.TButton", padding=(12, 5), font=("Segoe UI Semibold", 10))
        style.configure("Secondary.TButton", padding=(10, 5), font=("Segoe UI", 10))
        style.map("Primary.TButton", foreground=[("active", "#ffffff")])

    def _build_export_tab(self) -> None:
        self.export_tab.configure(style="App.TFrame")
        intro = ttk.Frame(self.export_tab, padding=(10, 8, 10, 5), style="App.TFrame")
        intro.pack(fill="x")
        card = ttk.Frame(intro, style="Card.TFrame", padding=(12, 8, 12, 8))
        card.pack(fill="x")
        ttk.Label(card, text="Export conversations", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.export_summary_var, style="Summary.TLabel").pack(fill="x", pady=(8, 0))

        controls = ttk.Frame(self.export_tab, padding=(10, 3, 10, 5), style="App.TFrame")
        controls.pack(fill="x")
        ttk.Label(controls, text="Search", style="FieldLabel.TLabel").pack(side="left")
        search = ttk.Entry(controls, textvariable=self.search_var, width=32)
        search.pack(side="left", padx=(8, 8))
        search.bind("<Return>", lambda _event: self.refresh_threads())
        ttk.Label(controls, text="Limit", style="FieldLabel.TLabel").pack(side="left")
        ttk.Spinbox(controls, from_=10, to=500, textvariable=self.limit_var, width=6).pack(side="left", padx=(8, 8))
        ttk.Checkbutton(controls, text="Archived", variable=self.include_archived_var).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Refresh", command=self.refresh_threads, style="Secondary.TButton").pack(side="left")
        ttk.Button(controls, text="Verify selected", command=self.verify_selected_thread, style="Secondary.TButton").pack(side="right")

        export_bar = ttk.Frame(self.export_tab, padding=(10, 4, 10, 6), style="App.TFrame")
        export_bar.pack(fill="x")
        ttk.Checkbutton(export_bar, text="Include workspace", variable=self.include_workspace_var).pack(side="left")
        ttk.Entry(export_bar, textvariable=self.export_path_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(export_bar, text="Save as", command=self.browse_export_path, style="Secondary.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(export_bar, text="Export zip", command=self.export_selected_thread, style="Primary.TButton").pack(side="left")

        body = ttk.PanedWindow(self.export_tab, orient="vertical")
        body.pack(fill="both", expand=True, padx=10)

        table_frame = ttk.Frame(body, style="Card.TFrame", padding=(8, 8, 8, 8))
        self.thread_tree = ttk.Treeview(table_frame, columns=THREAD_COLUMNS, show="headings", selectmode="browse")
        self.thread_tree.heading("updated", text="Updated")
        self.thread_tree.heading("provider", text="Provider")
        self.thread_tree.heading("title", text="Title")
        self.thread_tree.heading("cwd", text="Working directory")
        self.thread_tree.heading("id", text="Thread id")
        self.thread_tree.column("updated", width=135, anchor="w")
        self.thread_tree.column("provider", width=85, anchor="w")
        self.thread_tree.column("title", width=360, anchor="w")
        self.thread_tree.column("cwd", width=320, anchor="w")
        self.thread_tree.column("id", width=260, anchor="w")
        self.thread_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.thread_tree.yview)
        scroll.pack(side="right", fill="y")
        self.thread_tree.configure(yscrollcommand=scroll.set)
        self.thread_tree.tag_configure("odd", background="#f8fafc")
        self.thread_tree.tag_configure("even", background="#ffffff")
        self.thread_tree.bind("<<TreeviewSelect>>", self.on_thread_select)
        body.add(table_frame, weight=3)

        details = ttk.Frame(body, style="Card.TFrame", padding=(8, 8, 8, 8))
        self.thread_details = tk.Text(details, height=5, wrap="word", bg=SURFACE, fg=TEXT, relief="flat", padx=10, pady=8)
        self.thread_details.pack(fill="both", expand=True)
        self.thread_details.configure(state="disabled")
        body.add(details, weight=1)

        self.export_log = tk.Text(self.export_tab, height=6, wrap="word", bg="#0f172a", fg="#dbeafe", insertbackground="#dbeafe", relief="flat", padx=10, pady=8)
        self.export_log.pack(fill="x", padx=10, pady=(8, 8))
        self.export_log.configure(state="disabled")

    def _build_import_tab(self) -> None:
        self.import_tab.configure(style="App.TFrame")
        intro = ttk.Frame(self.import_tab, padding=(10, 8, 10, 5), style="App.TFrame")
        intro.pack(fill="x")
        card = ttk.Frame(intro, style="Card.TFrame", padding=(12, 8, 12, 8))
        card.pack(fill="x")
        ttk.Label(card, text="Import transfer package", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.import_summary_var, style="Summary.TLabel").pack(fill="x", pady=(8, 0))

        package_bar = ttk.Frame(self.import_tab, padding=(10, 3, 10, 5), style="App.TFrame")
        package_bar.pack(fill="x")
        ttk.Label(package_bar, text="Package", style="FieldLabel.TLabel").pack(side="left")
        ttk.Entry(package_bar, textvariable=self.import_package_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(package_bar, text="Open zip", command=self.browse_import_package, style="Primary.TButton").pack(side="left")

        options = ttk.LabelFrame(self.import_tab, text="Import options", padding=(10, 7, 10, 7))
        options.pack(fill="x", padx=10, pady=(0, 6))

        row1 = ttk.Frame(options)
        row1.pack(fill="x", pady=(0, 6))
        ttk.Label(row1, text="Provider").pack(side="left")
        provider = ttk.Combobox(row1, textvariable=self.provider_var, values=["current", "keep", "custom", "openai"], width=12)
        provider.pack(side="left", padx=(8, 18))
        ttk.Checkbutton(row1, text="Overwrite existing thread", variable=self.overwrite_var).pack(side="left")
        ttk.Button(row1, text="Dry run", command=self.import_dry_run, style="Secondary.TButton").pack(side="right", padx=(8, 0))
        ttk.Button(row1, text="Import", command=self.import_package, style="Primary.TButton").pack(side="right")

        row2 = ttk.Frame(options)
        row2.pack(fill="x", pady=(0, 6))
        ttk.Label(row2, text="Target cwd").pack(side="left")
        ttk.Entry(row2, textvariable=self.target_cwd_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row2, text="Browse", command=self.browse_target_cwd, style="Secondary.TButton").pack(side="left")

        row3 = ttk.Frame(options)
        row3.pack(fill="x")
        ttk.Label(row3, text="Workspace root").pack(side="left")
        ttk.Entry(row3, textvariable=self.workspace_root_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row3, text="Browse", command=self.browse_workspace_root, style="Secondary.TButton").pack(side="left")

        body = ttk.PanedWindow(self.import_tab, orient="vertical")
        body.pack(fill="both", expand=True, padx=10)

        info_frame = ttk.Frame(body, style="Card.TFrame", padding=(8, 8, 8, 8))
        self.package_info = tk.Text(info_frame, height=6, wrap="word", bg=SURFACE, fg=TEXT, relief="flat", padx=10, pady=8)
        self.package_info.pack(side="left", fill="both", expand=True)
        info_scroll = ttk.Scrollbar(info_frame, orient="vertical", command=self.package_info.yview)
        info_scroll.pack(side="right", fill="y")
        self.package_info.configure(yscrollcommand=info_scroll.set, state="disabled")
        body.add(info_frame, weight=1)

        entries_frame = ttk.Frame(body, style="Card.TFrame", padding=(8, 8, 8, 8))
        self.package_tree = ttk.Treeview(entries_frame, columns=PACKAGE_COLUMNS, show="headings")
        self.package_tree.heading("path", text="Package entry")
        self.package_tree.heading("size", text="Size")
        self.package_tree.column("path", width=900, anchor="w")
        self.package_tree.column("size", width=100, anchor="e")
        self.package_tree.pack(side="left", fill="both", expand=True)
        entry_scroll = ttk.Scrollbar(entries_frame, orient="vertical", command=self.package_tree.yview)
        entry_scroll.pack(side="right", fill="y")
        self.package_tree.configure(yscrollcommand=entry_scroll.set)
        self.package_tree.tag_configure("odd", background="#f8fafc")
        self.package_tree.tag_configure("even", background="#ffffff")
        body.add(entries_frame, weight=2)

        self.import_log = tk.Text(self.import_tab, height=6, wrap="word", bg="#0f172a", fg="#dbeafe", insertbackground="#dbeafe", relief="flat", padx=10, pady=8)
        self.import_log.pack(fill="x", padx=10, pady=(8, 8))
        self.import_log.configure(state="disabled")

    def browse_codex_home(self) -> None:
        path = filedialog.askdirectory(title="Select Codex home", initialdir=str(Path(self.codex_home_var.get()).parent))
        if path:
            self.codex_home_var.set(path)
            self.refresh_threads()

    def browse_export_path(self) -> None:
        initial = self.default_export_name()
        path = filedialog.asksaveasfilename(
            title="Save transfer package",
            defaultextension=".zip",
            filetypes=[("Zip packages", "*.zip"), ("All files", "*.*")],
            initialfile=initial.name,
            initialdir=str(initial.parent),
        )
        if path:
            self.export_path_var.set(path)

    def browse_import_package(self) -> None:
        path = filedialog.askopenfilename(
            title="Open transfer package",
            filetypes=[("Zip packages", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.import_package_var.set(path)
            self.load_package_preview()

    def browse_target_cwd(self) -> None:
        path = filedialog.askdirectory(title="Select target working directory")
        if path:
            self.target_cwd_var.set(path)

    def browse_workspace_root(self) -> None:
        path = filedialog.askdirectory(title="Select workspace root")
        if path:
            self.workspace_root_var.set(path)

    def codex_home(self) -> Path:
        return Path(self.codex_home_var.get()).expanduser()

    def selected_thread(self) -> dict[str, Any] | None:
        if self.selected_thread_id:
            return self.threads.get(self.selected_thread_id)
        return None

    def default_export_name(self) -> Path:
        thread = self.selected_thread()
        if not thread:
            return Path.home() / "Desktop" / "codex-thread.zip"
        title = thread.get("title") or thread.get("preview") or thread.get("id") or "codex-thread"
        return Path.home() / "Desktop" / f"{cht.slugify(str(title))}.zip"

    def refresh_threads(self) -> None:
        try:
            rows = list_threads(
                self.codex_home(),
                self.search_var.get().strip(),
                int(self.limit_var.get()),
                self.include_archived_var.get(),
            )
            self._fill_thread_table(rows)
            self.append_log(self.export_log, f"Loaded {len(rows)} thread(s).")
        except Exception as exc:  # noqa: BLE001 - GUI should show discovery failures.
            messagebox.showerror("Cannot load threads", str(exc))

    def _fill_thread_table(self, rows: list[dict[str, Any]]) -> None:
        self.threads = {str(row["id"]): row for row in rows}
        self.thread_tree.delete(*self.thread_tree.get_children())
        for index, row in enumerate(rows):
            thread_id = str(row["id"])
            values = (
                cht.format_time(row.get("updated_at")),
                row.get("model_provider") or "",
                cht.truncate(row.get("title") or row.get("preview") or "(untitled)", 80),
                cht.truncate(cht.strip_extended_prefix(row.get("cwd")) or "", 80),
                thread_id,
            )
            self.thread_tree.insert("", "end", iid=thread_id, values=values, tags=("even" if index % 2 == 0 else "odd",))
        self.selected_thread_id = None
        self.export_summary_var.set(f"Loaded {len(rows)} conversation(s). Select one to inspect and export.")
        self.set_text(self.thread_details, "Select a thread to see details.")

    def on_thread_select(self, _event: tk.Event | None = None) -> None:
        selected = self.thread_tree.selection()
        if not selected:
            return
        self.selected_thread_id = selected[0]
        thread = self.threads.get(self.selected_thread_id)
        if not thread:
            return
        detail = [
            f"Title: {thread.get('title') or thread.get('preview') or '(untitled)'}",
            f"Thread id: {thread.get('id')}",
            f"Provider: {thread.get('model_provider') or ''}",
            f"Model: {thread.get('model') or ''}",
            f"Updated: {cht.format_time(thread.get('updated_at'))}",
            f"Cwd: {cht.strip_extended_prefix(thread.get('cwd')) or ''}",
            "",
            str(thread.get("preview") or ""),
        ]
        self.set_text(self.thread_details, "\n".join(detail))
        self.export_summary_var.set(
            f"Selected: {cht.truncate(thread.get('title') or thread.get('preview') or '(untitled)', 120)}"
        )
        if not self.export_path_var.get().strip():
            self.export_path_var.set(str(self.default_export_name()))

    def verify_selected_thread(self) -> None:
        thread = self.selected_thread()
        if not thread:
            messagebox.showinfo("No thread selected", "Select a thread first.")
            return

        def task() -> tuple[str, str]:
            args = argparse.Namespace(codex_home=str(self.codex_home()), id=str(thread["id"]), limit=20)
            return "export", capture_command(cht.command_verify, args)

        self.run_task(task)

    def export_selected_thread(self) -> None:
        thread = self.selected_thread()
        if not thread:
            messagebox.showinfo("No thread selected", "Select a thread first.")
            return
        out = self.export_path_var.get().strip()
        if not out:
            self.browse_export_path()
            out = self.export_path_var.get().strip()
        if not out:
            return

        def task() -> tuple[str, str]:
            args = argparse.Namespace(
                codex_home=str(self.codex_home()),
                id=str(thread["id"]),
                out=out,
                include_workspace=self.include_workspace_var.get(),
                exclude=list(cht.DEFAULT_EXCLUDES),
            )
            return "export", capture_command(cht.command_export, args)

        self.run_task(task)

    def load_package_preview(self) -> None:
        path = Path(self.import_package_var.get()).expanduser()
        if not path.exists():
            messagebox.showerror("Package missing", f"File not found:\n{path}")
            return
        try:
            data = package_summary(path)
            provider = current_provider(self.codex_home())
        except Exception as exc:  # noqa: BLE001 - show GUI errors as dialogs.
            messagebox.showerror("Cannot read package", str(exc))
            return
        self.package_path = path
        self.package_data = data
        entries = data["entries"]
        info = [
            f"Package: {path}",
            f"Thread id: {data['thread_id']}",
            f"Title: {data['title']}",
            f"Provider in package: {data['provider']}",
            f"Current provider on this machine: {provider or '(unknown)'}",
            f"Original cwd: {data['cwd']}",
            f"Workspace included: {'yes' if data['workspace_included'] else 'no'}",
            f"Workspace file count: {data['workspace_files']}",
            f"Total package entries: {len(entries)}",
        ]
        self.set_text(self.package_info, "\n".join(info))
        self.package_tree.delete(*self.package_tree.get_children())
        for idx, entry in enumerate(entries[:1000]):
            self.package_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(entry.filename, f"{entry.file_size:,}"),
                tags=("even" if idx % 2 == 0 else "odd",),
            )
        if len(entries) > 1000:
            self.package_tree.insert("", "end", values=(f"... {len(entries) - 1000} more entries", ""))

        self.import_summary_var.set(
            f"Package loaded: {cht.truncate(data['title'], 110)} - "
            f"{data['workspace_files']} workspace file(s) - provider {data['provider'] or 'unknown'}"
        )
        if data["workspace_included"] and not self.workspace_root_var.get().strip():
            self.workspace_root_var.set(str(Path.home() / "Documents" / "CodexImported"))
        if not self.target_cwd_var.get().strip() and not data["workspace_included"]:
            self.target_cwd_var.set(str(Path(data["cwd"]) if data["cwd"] else Path.home() / "Documents" / "CodexImported"))
        self.append_log(self.import_log, f"Loaded package for thread {data['thread_id']}.")

    def import_args(self, dry_run: bool) -> argparse.Namespace | None:
        if not self.package_path:
            path = self.import_package_var.get().strip()
            if path:
                self.package_path = Path(path).expanduser()
        if not self.package_path:
            messagebox.showinfo("No package selected", "Open a transfer zip first.")
            return None
        return argparse.Namespace(
            codex_home=str(self.codex_home()),
            package=str(self.package_path),
            provider=self.provider_var.get().strip() or "current",
            workspace_root=self.workspace_root_var.get().strip() or None,
            target_cwd=self.target_cwd_var.get().strip() or None,
            title=None,
            overwrite=self.overwrite_var.get(),
            dry_run=dry_run,
        )

    def import_dry_run(self) -> None:
        args = self.import_args(dry_run=True)
        if not args:
            return

        def task() -> tuple[str, str]:
            return "import", capture_command(cht.command_import, args)

        self.run_task(task)

    def import_package(self) -> None:
        args = self.import_args(dry_run=False)
        if not args:
            return
        if not messagebox.askyesno(
            "Confirm import",
            "This will write the rollout JSONL and update the Codex SQLite index.\n\n"
            "Backups are created before SQLite files are changed. Continue?",
        ):
            return

        def task() -> tuple[str, str]:
            return "import", capture_command(cht.command_import, args)

        self.run_task(task)

    def run_task(self, task: Callable[[], tuple[str, str]]) -> None:
        self.status_var.set("Working...")

        def worker() -> None:
            try:
                target, message = task()
                self.task_queue.put((target, message, None))
            except Exception as exc:  # noqa: BLE001 - surface all task errors to the user.
                self.task_queue.put(("error", "", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_tasks(self) -> None:
        try:
            while True:
                target, message, error = self.task_queue.get_nowait()
                if error:
                    self.status_var.set("Error")
                    messagebox.showerror("codex-history-transfer", error)
                    self.append_log(self.export_log if self.notebook.index("current") == 0 else self.import_log, error)
                elif target == "export":
                    self.status_var.set("Ready")
                    self.append_log(self.export_log, message)
                elif target == "import":
                    self.status_var.set("Ready")
                    self.append_log(self.import_log, message)
        except queue.Empty:
            pass
        self.after(150, self._poll_tasks)

    @staticmethod
    def set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    @staticmethod
    def append_log(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        if widget.get("1.0", "end").strip():
            widget.insert("end", "\n\n")
        widget.insert("end", value)
        widget.see("end")
        widget.configure(state="disabled")


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the codex-history-transfer GUI.")
    parser.add_argument("--version", action="version", version=f"codex-history-transfer-gui {cht.__version__}")
    parser.add_argument("--smoke-test", action="store_true", help="Import GUI dependencies and exit.")
    args = parser.parse_args()
    if args.smoke_test:
        print("GUI smoke test OK")
        return 0
    app = CodexHistoryTransferApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
