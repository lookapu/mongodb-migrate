from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from pymongo import MongoClient

from .archive import (
    ArchiveCancelled,
    BackupOptions,
    ExportOptions,
    ImportOptions,
    RestoreOptions,
    create_backup,
    export_data,
    import_data,
    restore_backup,
    verify_backup,
)
from .config import MigrationOptions, select_collections
from .diagnostics import create_diagnostic_bundle
from .engine import (
    MigrationCancelled,
    MigrationEngine,
    PlanApprovalRequired,
)
from .product_info import PRODUCT_VERSION
from .store import MigrationStore


def default_app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "MongoDB Migrate"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MongoDB Migrate"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "mongodb-migrate"


COLORS = {
    "bg": "#F3F6FA",
    "card": "#FFFFFF",
    "navy": "#13233A",
    "blue": "#2563EB",
    "blue_hover": "#1D4ED8",
    "text": "#172033",
    "muted": "#667085",
    "border": "#DCE3EC",
    "green": "#138A5B",
    "amber": "#B76E00",
    "red": "#C83C3C",
    "log": "#101827",
}


class FlatTabView(tk.Frame):
    """Fixed-geometry tabs whose selected state changes colors only."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, background=COLORS["bg"], borderwidth=0)
        self.tabbar = tk.Frame(self, background="#E7ECF3", height=48)
        self.tabbar.pack(fill="x")
        self.tabbar.pack_propagate(False)
        self.content = tk.Frame(self, background=COLORS["bg"])
        self.content.pack(fill="both", expand=True)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)
        self.pages: list[tk.Widget] = []
        self.buttons: list[tk.Button] = []
        self.selected_index = 0

    def add(self, page: tk.Widget, *, text: str) -> None:
        index = len(self.pages)
        self.tabbar.grid_columnconfigure(index, weight=1, uniform="tabs")
        button = tk.Button(
            self.tabbar,
            text=text,
            command=lambda selected=index: self.select(selected),
            font=("Helvetica Neue", 12, "bold"),
            height=2,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
            takefocus=True,
        )
        button.grid(row=0, column=index, sticky="nsew")
        page.grid(row=0, column=0, sticky="nsew")
        self.pages.append(page)
        self.buttons.append(button)
        self._paint()
        if index == 0:
            page.tkraise()

    def select(self, index: int | None = None) -> int:
        if index is None:
            return self.selected_index
        self.selected_index = int(index)
        self.pages[self.selected_index].tkraise()
        self._paint()
        return self.selected_index

    def index(self, value: str | int) -> int:
        return len(self.pages) if value == "end" else int(value)

    def _paint(self) -> None:
        for index, button in enumerate(self.buttons):
            selected = index == self.selected_index
            background = COLORS["card"] if selected else "#E7ECF3"
            foreground = COLORS["blue"] if selected else COLORS["muted"]
            button.configure(
                background=background,
                foreground=foreground,
                activebackground=background,
                activeforeground=foreground,
            )


class Tooltip:
    """Small non-focus-stealing help popup for option descriptions."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 350):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.after_id: str | None = None
        self.popup: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event[Any] | None = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self) -> None:
        if self.popup or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() - 4
        popup = tk.Toplevel(self.widget)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            popup,
            text=self.text,
            justify="left",
            wraplength=360,
            background="#172033",
            foreground="#F9FAFB",
            font=("Helvetica Neue", 11),
            padx=12,
            pady=9,
            relief="solid",
            borderwidth=1,
        )
        label.pack()
        self.popup = popup

    def _hide(self, _event: tk.Event[Any] | None = None) -> None:
        self._cancel()
        if self.popup:
            self.popup.destroy()
            self.popup = None


class QueueLogHandler(logging.Handler):
    def __init__(self, messages: queue.Queue[tuple[str, Any]]):
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.put(("log", self.format(record)))


class MongoMigrateApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MongoDB Migrate · All-in-One Data Mobility")
        self.geometry("1180x840")
        self.minsize(1020, 740)
        self.configure(bg=COLORS["bg"])
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.engine: MigrationEngine | None = None
        self.worker: threading.Thread | None = None
        self.collection_names: list[str] = []
        self.visible_collection_names: list[str] = []
        self.vars: dict[str, tk.Variable] = {}
        self.tooltips: list[Tooltip] = []
        self.archive_cancel = threading.Event()
        self.smoke_test = "--smoke-test" in sys.argv
        self.callback_error = ""
        self._configure_persistent_logging()
        self.report_callback_exception = self._report_callback_exception
        self._configure_style()
        self._build()
        self._load_settings()
        self._bind_summary_updates()
        self.after(100, self._drain_messages)
        self.after(500, self._poll_runtime_metrics)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_persistent_logging(self) -> None:
        log_dir = self.app_data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "mongodb-migrate.log"
        logger = logging.getLogger("mongodb_migrate")
        logger.setLevel(logging.INFO)
        if not any(
            isinstance(handler, logging.handlers.RotatingFileHandler)
            and Path(handler.baseFilename) == path
            for handler in logger.handlers
        ):
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
            )
            logger.addHandler(handler)

    def _report_callback_exception(
        self, exc_type: type[BaseException], exc: BaseException, tb: Any
    ) -> None:
        content = "".join(traceback.format_exception(exc_type, exc, tb))
        crash_dir = self.app_data_dir / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        path = crash_dir / f"crash-{time.strftime('%Y%m%d-%H%M%S')}.log"
        path.write_text(content, encoding="utf-8")
        logging.getLogger("mongodb_migrate").critical(
            "uncaught GUI exception\n%s", content
        )
        self.callback_error = content
        if self.smoke_test:
            return
        messagebox.showerror(
            "应用发生异常",
            f"已保存崩溃报告：\n{path}\n\n可在“执行与审计”中导出诊断包。",
            parent=self,
        )

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Helvetica Neue", 12), background=COLORS["bg"])
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure(
            "TLabel", background=COLORS["bg"], foreground=COLORS["text"]
        )
        style.configure(
            "Card.TLabel", background=COLORS["card"], foreground=COLORS["text"]
        )
        style.configure(
            "Card.TCheckbutton",
            background=COLORS["card"],
            foreground=COLORS["text"],
        )
        style.configure(
            "Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"]
        )
        style.configure(
            "Section.TLabelframe",
            background=COLORS["card"],
            bordercolor=COLORS["border"],
            relief="solid",
            borderwidth=1,
            padding=14,
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=COLORS["card"],
            foreground=COLORS["navy"],
            font=("Helvetica Neue", 13, "bold"),
        )
        style.configure(
            "TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(0, 8, 0, 0)
        )
        style.configure(
            "TNotebook.Tab",
            padding=(20, 10),
            background="#E7ECF3",
            foreground=COLORS["muted"],
            font=("Helvetica Neue", 12, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["card"])],
            foreground=[("selected", COLORS["blue"])],
        )
        style.configure(
            "TEntry", fieldbackground="#FFFFFF", bordercolor=COLORS["border"], padding=7
        )
        style.configure(
            "TCombobox", fieldbackground="#FFFFFF", bordercolor=COLORS["border"], padding=6
        )
        style.configure("TButton", padding=(13, 8))
        style.configure(
            "Primary.TButton",
            font=("Helvetica Neue", 12, "bold"),
            foreground="#FFFFFF",
            background=COLORS["blue"],
            bordercolor=COLORS["blue"],
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["blue_hover"]), ("disabled", "#9CB6EA")],
        )
        style.configure(
            "Danger.TButton",
            foreground=COLORS["red"],
            background="#FFF5F5",
            bordercolor="#F2C6C6",
        )
        style.configure(
            "Status.TLabel",
            font=("Helvetica Neue", 12, "bold"),
            foreground=COLORS["navy"],
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=COLORS["blue"],
            troughcolor="#DDE6F2",
            borderwidth=0,
        )

    def _var(self, name: str, value: Any = "") -> tk.Variable:
        cls = tk.BooleanVar if isinstance(value, bool) else tk.StringVar
        variable = cls(value=value)
        self.vars[name] = variable
        return variable

    def _attach_help(self, widget: tk.Widget, text: str) -> None:
        self.tooltips.append(Tooltip(widget, text))

    def _field_label(
        self,
        parent: tk.Misc,
        text: str,
        help_text: str,
        *,
        row: int,
        column: int,
        pady: int = 7,
    ) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=row, column=column, sticky="w", pady=pady)
        ttk.Label(frame, text=text, style="Card.TLabel").pack(side="left")
        help_label = tk.Label(
            frame,
            text="?",
            width=2,
            height=1,
            font=("Helvetica Neue", 10, "bold"),
            foreground=COLORS["blue"],
            background="#EAF1FF",
            cursor="question_arrow",
            borderwidth=0,
        )
        help_label.pack(side="left", padx=(5, 0))
        self._attach_help(help_label, help_text)
        return frame

    def _check_option(
        self,
        parent: tk.Misc,
        text: str,
        name: str,
        default: bool,
        help_text: str,
    ) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame")
        check = ttk.Checkbutton(
            frame,
            text=text,
            variable=self._var(name, default),
            style="Card.TCheckbutton",
        )
        check.pack(side="left")
        help_label = tk.Label(
            frame,
            text="?",
            width=2,
            font=("Helvetica Neue", 10, "bold"),
            foreground=COLORS["blue"],
            background="#EAF1FF",
            cursor="question_arrow",
        )
        help_label.pack(side="left", padx=(4, 0))
        self._attach_help(help_label, help_text)
        return frame

    def _build(self) -> None:
        header = tk.Frame(self, background=COLORS["navy"], height=104)
        header.pack(fill="x")
        header.pack_propagate(False)
        brand = tk.Frame(header, background=COLORS["navy"])
        brand.pack(side="left", padx=28, pady=18)
        logo = tk.Label(
            brand,
            text="M",
            font=("Helvetica Neue", 22, "bold"),
            foreground="#FFFFFF",
            background=COLORS["blue"],
            width=2,
            height=1,
        )
        logo.pack(side="left", padx=(0, 14))
        titles = tk.Frame(brand, background=COLORS["navy"])
        titles.pack(side="left")
        tk.Label(
            titles,
            text="MongoDB Migrate",
            font=("Helvetica Neue", 23, "bold"),
            foreground="#FFFFFF",
            background=COLORS["navy"],
        ).pack(anchor="w")
        tk.Label(
            titles,
            text=f"All-in-One Migration & Backup Console · v{PRODUCT_VERSION}",
            font=("Helvetica Neue", 11),
            foreground="#AFC0D8",
            background=COLORS["navy"],
        ).pack(anchor="w")
        badge = tk.Label(
            header,
            text="●  LOCAL & STANDALONE",
            font=("Helvetica Neue", 10, "bold"),
            foreground="#78E5B5",
            background="#203653",
            padx=12,
            pady=7,
        )
        badge.pack(side="right", padx=28)
        self._attach_help(
            badge,
            "应用自带 Python、Tcl/Tk 与 PyMongo。连接串仅驻留内存，"
            "迁移数据不会经过第三方服务。",
        )

        root = ttk.Frame(self, padding=(24, 16))
        root.pack(fill="both", expand=True)

        notebook = FlatTabView(root)
        notebook.pack(fill="both", expand=True)
        connection = ttk.Frame(notebook.content, padding=(18, 18))
        migration_page = ttk.Frame(notebook.content)
        migration_canvas = tk.Canvas(
            migration_page,
            background=COLORS["bg"],
            highlightthickness=0,
            borderwidth=0,
        )
        migration_scroll = ttk.Scrollbar(
            migration_page, orient="vertical", command=migration_canvas.yview
        )
        migration_canvas.configure(yscrollcommand=migration_scroll.set)
        migration_canvas.pack(side="left", fill="both", expand=True)
        migration_scroll.pack(side="right", fill="y")
        migration = ttk.Frame(migration_canvas, padding=(18, 18))
        migration_window = migration_canvas.create_window(
            (0, 0), window=migration, anchor="nw"
        )
        migration.bind(
            "<Configure>",
            lambda _event: migration_canvas.configure(
                scrollregion=migration_canvas.bbox("all")
            ),
        )
        migration_canvas.bind(
            "<Configure>",
            lambda event: migration_canvas.itemconfigure(
                migration_window, width=event.width
            ),
        )
        migration_canvas.bind(
            "<Enter>",
            lambda _event: migration_canvas.bind_all(
                "<MouseWheel>",
                lambda wheel: migration_canvas.yview_scroll(
                    -1 if wheel.delta > 0 else 1, "units"
                ),
            ),
        )
        migration_canvas.bind(
            "<Leave>",
            lambda _event: migration_canvas.unbind_all("<MouseWheel>"),
        )
        runtime = ttk.Frame(notebook.content, padding=(18, 18))
        backup_center = ttk.Frame(notebook.content, padding=(18, 18))
        notebook.add(connection, text="  ① 连接与集合  ")
        notebook.add(migration_page, text="  ② 迁移策略  ")
        notebook.add(runtime, text="  ③ 执行与审计  ")
        notebook.add(backup_center, text="  ④ 备份与交换  ")
        self.notebook = notebook

        self._build_connection(connection)
        self._build_migration(migration)
        self._build_runtime(runtime)
        self._build_backup_center(backup_center)

    def _labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        name: str,
        default: str = "",
        *,
        show: str = "",
        width: int = 56,
        help_text: str = "",
    ) -> ttk.Entry:
        if help_text:
            self._field_label(
                parent, label, help_text, row=row, column=0, pady=6
            )
        else:
            ttk.Label(parent, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=6
            )
        entry = ttk.Entry(
            parent, textvariable=self._var(name, default), width=width, show=show
        )
        entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(12, 0), pady=6)
        return entry

    def _build_connection(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        endpoints = ttk.LabelFrame(
            parent, text="端点（连接串仅保留在内存中）", style="Section.TLabelframe"
        )
        endpoints.grid(row=0, column=0, sticky="ew")
        endpoints.columnconfigure(1, weight=1)
        self.source_uri_entry = self._labeled_entry(
            endpoints,
            0,
            "源 MongoDB URI",
            "source_uri",
            "mongodb://localhost:27017",
            show="•",
            help_text="源集群连接串。支持 mongodb:// 与 mongodb+srv://。"
            "建议使用只读、最小权限账号；连接串不会保存到磁盘。",
        )
        self.target_uri_entry = self._labeled_entry(
            endpoints,
            1,
            "目标 MongoDB URI",
            "target_uri",
            "mongodb://localhost:27018",
            show="•",
            help_text="目标集群连接串。账号需有创建集合、写数据和创建索引权限；"
            "开启安全切换时还需要 renameCollection 权限。",
        )
        ttk.Button(
            endpoints, text="显示/隐藏", command=self._toggle_uris
        ).grid(row=0, column=4, rowspan=2, padx=(8, 0))
        self._field_label(
            endpoints,
            "源数据库",
            "要读取的源 database 名称。Views 会自动跳过。",
            row=2,
            column=0,
            pady=6,
        )
        ttk.Entry(endpoints, textvariable=self._var("source_db", "app")).grid(
            row=2, column=1, sticky="ew", padx=(12, 16)
        )
        self._field_label(
            endpoints,
            "目标数据库",
            "影子集合和最终集合所在的目标 database。切换只能在此数据库内进行。",
            row=2,
            column=2,
            pady=6,
        )
        ttk.Entry(endpoints, textvariable=self._var("target_db", "app")).grid(
            row=2, column=3, sticky="ew", padx=(12, 0)
        )
        source_badge = tk.Label(
            endpoints,
            text="SOURCE",
            foreground="#1D4ED8",
            background="#EAF1FF",
            font=("Helvetica Neue", 9, "bold"),
            padx=8,
            pady=3,
        )
        source_badge.grid(row=0, column=5, padx=(8, 0))
        target_badge = tk.Label(
            endpoints,
            text="TARGET",
            foreground="#138A5B",
            background="#E8F7F0",
            font=("Helvetica Neue", 9, "bold"),
            padx=8,
            pady=3,
        )
        target_badge.grid(row=1, column=5, padx=(8, 0))

        selector = ttk.LabelFrame(
            parent, text="集合选择", style="Section.TLabelframe"
        )
        selector.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        selector.columnconfigure(0, weight=1)
        selector.rowconfigure(2, weight=1)
        bar = ttk.Frame(selector, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(bar, text="读取源集合", command=self._fetch_collections).pack(side="left")
        ttk.Button(bar, text="全选", command=self._select_all_collections).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="清空", command=self._clear_collection_selection).pack(
            side="left"
        )
        pattern_label = ttk.Label(bar, text="匹配表达式：")
        pattern_label.pack(side="left", padx=(22, 6))
        self._attach_help(
            pattern_label,
            "支持逗号分隔的 glob，例如 users,orders_*,audit_2026。"
            "读取集合后，列表中的多选结果优先。",
        )
        ttk.Entry(
            bar, textvariable=self._var("collections", "*"), width=26
        ).pack(side="left", fill="x", expand=True)
        filter_bar = ttk.Frame(selector, style="Card.TFrame")
        filter_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(filter_bar, text="搜索集合", style="Card.TLabel").pack(side="left")
        search_var = self._var("collection_search", "")
        search_entry = ttk.Entry(filter_bar, textvariable=search_var, width=32)
        search_entry.pack(side="left", padx=(8, 12))
        search_var.trace_add("write", lambda *_: self._filter_collection_list())
        self.collection_count_var = tk.StringVar(value="尚未读取集合")
        ttk.Label(
            filter_bar, textvariable=self.collection_count_var, style="Card.TLabel"
        ).pack(side="left")
        query_label = ttk.Label(filter_bar, text="排除：", style="Card.TLabel")
        query_label.pack(side="left", padx=(24, 6))
        self._attach_help(
            query_label,
            "排除表达式同样支持逗号和 glob。system.* 始终不会迁移。",
        )
        ttk.Entry(
            filter_bar, textvariable=self._var("exclude", "system.*"), width=20
        ).pack(side="left")
        list_frame = ttk.Frame(selector, style="Card.TFrame")
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.collection_list = tk.Listbox(
            list_frame,
            selectmode="extended",
            height=12,
            borderwidth=0,
            highlightthickness=1,
            highlightcolor="#2f6fed",
            highlightbackground=COLORS["border"],
            selectbackground=COLORS["blue"],
            selectforeground="#FFFFFF",
            background="#FAFBFD",
            foreground=COLORS["text"],
            font=("Menlo", 11),
            exportselection=False,
        )
        self.collection_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.collection_list.yview
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.collection_list.configure(yscrollcommand=scroll.set)
        self.collection_list.bind(
            "<<ListboxSelect>>", lambda _event: self._update_collection_count()
        )
        query_row = ttk.Frame(selector, style="Card.TFrame")
        query_row.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        query_row.columnconfigure(1, weight=1)
        query_help = self._field_label(
            query_row,
            "文档过滤 Query",
            "可选 MongoDB Extended JSON 查询，例如 "
            '{"tenant_id": 7, "created_at": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}。'
            "过滤迁移会只校验过滤结果，不建议与最终整集合切换混用。",
            row=0,
            column=0,
            pady=0,
        )
        query_help.grid_configure(padx=(0, 8))
        ttk.Entry(
            query_row,
            textvariable=self._var("query", ""),
        ).grid(row=0, column=1, sticky="ew")
        parent.rowconfigure(1, weight=1)

    def _build_migration(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        general = ttk.LabelFrame(
            parent, text="核心策略", style="Section.TLabelframe"
        )
        general.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            general.columnconfigure(column, weight=1 if column in (1, 3) else 0)
        fields = [
            (
                "影子集合后缀",
                "target_suffix",
                "__migrating",
                (
                    "所有数据先写入“原集合名 + 后缀”。默认不覆盖现有业务集合，"
                    "这是安全发布和中断恢复的基础。"
                ),
            ),
            (
                "每批文档数",
                "batch_size",
                "1000",
                (
                    "单次 bulk_write 的最大文档数。建议从 500–2000 起步；"
                    "文档较大或目标负载较高时调小。"
                ),
            ),
            (
                "集合并发数",
                "workers",
                "2",
                (
                    "同时迁移的集合数量。建议生产初始值 2–4。它不是单集合分片并发，"
                    "因此不会破坏每个集合的 checkpoint 顺序。"
                ),
            ),
            (
                "全局 docs/s",
                "docs_per_second",
                "0",
                (
                    "所有工作线程共享的写入限速。0 表示不限制；生产环境建议先设置"
                    "保守值，观察目标 CPU、磁盘和复制延迟后再提高。"
                ),
            ),
        ]
        for idx, (label, name, default, help_text) in enumerate(fields):
            row, pair = divmod(idx, 2)
            column = pair * 2
            self._field_label(
                general,
                label,
                help_text,
                row=row,
                column=column,
            )
            ttk.Entry(general, textvariable=self._var(name, default), width=22).grid(
                row=row, column=column + 1, sticky="ew", padx=(8, 18), pady=7
            )
        self._field_label(
            general,
            "校验级别",
            "none：不校验；count：仅数量；sample：数量 + 内容抽样；"
            "full：逐文档内容校验。正式切换推荐 sample 或 full。",
            row=2,
            column=0,
        )
        ttk.Combobox(
            general,
            textvariable=self._var("verify", "sample"),
            values=("none", "count", "sample", "full"),
            state="readonly",
            width=18,
        ).grid(row=2, column=1, sticky="w", padx=(8, 18))
        self._field_label(
            general,
            "冲突策略",
            "fail：发现同名影子集合立即停止，防止接管未知数据；"
            "resume：确认影子集合属于本任务后继续。填写 Job ID 时自动使用 resume。",
            row=2,
            column=2,
        )
        ttk.Combobox(
            general,
            textvariable=self._var("conflict", "fail"),
            values=("fail", "resume"),
            state="readonly",
            width=18,
        ).grid(row=2, column=3, sticky="w", padx=(8, 18))

        flags = ttk.Frame(general, style="Card.TFrame")
        flags.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        for text, name, default, help_text in (
            (
                "复制索引",
                "copy_indexes",
                True,
                (
                    "数据写完并校验后复制除 _id_ 外的二级索引。"
                    "后置建索引通常比边写边维护更快，唯一索引冲突会使任务失败。"
                ),
            ),
            (
                "只做预演（Dry Run）",
                "dry_run",
                False,
                "只执行连接、权限、集合选择和计数，不创建集合、不写数据、不切换。",
            ),
            (
                "校验成功后安全切换",
                "cutover",
                False,
                (
                    "将目标现有集合改名为时间戳 backup，再把影子集合改为正式名称。"
                    "不会自动删除 backup，但两次 rename 之间存在极短窗口。"
                ),
            ),
        ):
            option = self._check_option(flags, text, name, default, help_text)
            option.pack(side="left", padx=(0, 22))

        advanced = ttk.LabelFrame(
            parent, text="可靠性与负载保护", style="Section.TLabelframe"
        )
        advanced.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        for column in range(4):
            advanced.columnconfigure(column, weight=1 if column % 2 else 0)
        advanced_fields = [
            (
                "批次上限 MiB",
                "batch_mib",
                "12",
                (
                    "除文档数外，再按 BSON 编码字节数切批。最大允许 15 MiB，"
                    "用于避开单文档/消息 16 MiB 边界。"
                ),
            ),
            (
                "内容抽样数",
                "sample_size",
                "200",
                (
                    "sample 校验时逐条比较的文档数。关键集合建议 1000 以上；"
                    "full 模式会忽略该值并校验全部文档。"
                ),
            ),
            (
                "最大重试次数",
                "max_retries",
                "6",
                "网络断开、选主、超时等瞬时错误的批次重试次数。不可恢复写入错误不会盲目重试。",
            ),
            (
                "初始退避秒数",
                "retry_backoff",
                "0.5",
                "指数退避起点。第 n 次等待约为该值 × 2ⁿ，降低故障期间对集群的持续冲击。",
            ),
        ]
        for idx, (label, name, default, help_text) in enumerate(advanced_fields):
            row, pair = divmod(idx, 2)
            column = pair * 2
            self._field_label(
                advanced, label, help_text, row=row, column=column
            )
            ttk.Entry(
                advanced, textvariable=self._var(name, default), width=18
            ).grid(row=row, column=column + 1, sticky="ew", padx=(7, 16), pady=7)

        incremental = ttk.LabelFrame(
            parent, text="在线追平（不捕获物理删除）", style="Section.TLabelframe"
        )
        incremental.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        for column in range(6):
            incremental.columnconfigure(column, weight=1 if column % 2 else 0)
        incremental_fields = [
            (
                "更新时间字段",
                "incremental_field",
                "",
                (
                    "业务每次新增和更新都必须维护的 BSON Date 或数值字段，"
                    "例如 updated_at，并建议建立索引。"
                ),
            ),
            (
                "追平轮数",
                "incremental_rounds",
                "0",
                "全量完成后的最大增量扫描轮数。0 表示离线迁移，不执行追平。",
            ),
            (
                "回看秒数",
                "incremental_overlap_seconds",
                "120",
                "每轮水位向前回看，覆盖乱序写入、时钟偏差和延迟更新。重复文档会幂等覆盖。",
            ),
            (
                "轮次间隔秒",
                "incremental_interval",
                "3",
                "两轮追平之间等待时间，为业务新写入和复制延迟留出观察窗口。",
            ),
            (
                "连续收敛轮数",
                "convergence_rounds",
                "2",
                "源/目标数量连续相等且源数量稳定达到该轮数时提前结束追平。",
            ),
        ]
        for idx, (label, name, default, help_text) in enumerate(incremental_fields):
            row, pair = divmod(idx, 3)
            column = pair * 2
            self._field_label(
                incremental, label, help_text, row=row, column=column
            )
            ttk.Entry(incremental, textvariable=self._var(name, default), width=18).grid(
                row=row, column=column + 1, sticky="ew", padx=(7, 14)
            )
        warning = ttk.Label(
            incremental,
            text="⚠  水位模式覆盖新增和更新，但不会捕获物理删除；严格零停机请使用 Change Streams / CDC。",
            foreground="#a15c00",
            style="Card.TLabel",
        )
        warning.grid(row=2, column=0, columnspan=6, sticky="w", pady=(9, 0))
        self._attach_help(
            warning,
            "迁移期间删除文档不会更新水位字段，因此目标可能保留已删除记录。"
            "此时必须停写后做最终同步，或在全量前启动 Change Streams/CDC。",
        )
        cdc_row = ttk.Frame(incremental, style="Card.TFrame")
        cdc_row.grid(
            row=3, column=0, columnspan=6, sticky="ew", pady=(10, 0)
        )
        cdc_option = self._check_option(
            cdc_row,
            "启用 Change Streams CDC",
            "cdc_enabled",
            False,
            "在全量开始前记录 cluster time，全量完成后从该位置追平 insert、update、"
            "replace 和 delete，并持久化 resume token。源端必须是 Replica Set 或 Sharded Cluster。",
        )
        cdc_option.pack(side="left", padx=(0, 20))
        quiet_label = ttk.Label(
            cdc_row, text="静默收敛秒数", style="Card.TLabel"
        )
        quiet_label.pack(side="left")
        self._attach_help(
            quiet_label,
            "Change Stream 连续无新事件达到该时长后认为已追平。"
            "切换前仍建议业务短暂停写，再完成最终静默窗口。",
        )
        ttk.Entry(
            cdc_row,
            textvariable=self._var("cdc_quiet_seconds", "5"),
            width=8,
        ).pack(side="left", padx=(7, 18))
        max_label = ttk.Label(
            cdc_row, text="最长追平秒数", style="Card.TLabel"
        )
        max_label.pack(side="left")
        self._attach_help(
            max_label,
            "CDC 无法在此时间内进入静默窗口时任务失败，防止在持续高写入下误切换。",
        )
        ttk.Entry(
            cdc_row,
            textvariable=self._var("cdc_max_seconds", "600"),
            width=9,
        ).pack(side="left", padx=(7, 0))

        storage = ttk.LabelFrame(
            parent, text="任务持久化与审计", style="Section.TLabelframe"
        )
        storage.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        storage.columnconfigure(1, weight=1)
        storage.columnconfigure(3, weight=1)
        self._field_label(
            storage,
            "SQLite 状态库",
            "保存 Job、集合状态、checkpoint、租约与审计事件。启用 WAL 和 synchronous=FULL。"
            "恢复时必须保留此文件。",
            row=0,
            column=0,
        )
        ttk.Entry(
            storage,
            textvariable=self._var(
                "state_db", str(self.app_data_dir / "state.sqlite3")
            ),
        ).grid(row=0, column=1, sticky="ew", padx=(8, 18))
        self._field_label(
            storage,
            "恢复任务 ID",
            "填写上次输出的 Job ID 即可恢复。端点和数据语义参数必须与原任务一致，"
            "已完成集合会自动跳过。",
            row=0,
            column=2,
        )
        ttk.Entry(storage, textvariable=self._var("job_id", "")).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )
        self._field_label(
            storage,
            "DLQ 目录",
            "不可恢复写入失败的原文档以 MongoDB Extended JSON 写入此目录。"
            "任务同时会失败，不会静默跳过。",
            row=1,
            column=0,
        )
        ttk.Entry(
            storage,
            textvariable=self._var("dlq_dir", str(self.app_data_dir / "dlq")),
        ).grid(
            row=1, column=1, sticky="ew", padx=(8, 18)
        )
        self._field_label(
            storage,
            "任务租约秒数",
            "运行进程会周期续租，阻止另一个进程同时恢复同一 Job。"
            "仅在确认旧进程已停止且租约过期后接管。",
            row=1,
            column=2,
        )
        ttk.Entry(storage, textvariable=self._var("lease_ttl", "60")).grid(
            row=1, column=3, sticky="ew", padx=(8, 0)
        )
        self._field_label(
            storage,
            "报告目录",
            "保存执行计划和校验证据。执行计划不含连接凭据，也不保存 Query 明文。",
            row=2,
            column=0,
        )
        ttk.Entry(
            storage,
            textvariable=self._var("report_dir", str(self.app_data_dir / "reports")),
        ).grid(row=2, column=1, sticky="ew", padx=(8, 18))

        safety = ttk.LabelFrame(
            parent, text="生产安全与运行时熔断", style="Section.TLabelframe"
        )
        safety.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        for column in range(6):
            safety.columnconfigure(column, weight=1 if column % 2 else 0)
        safe_var = self._var("production_safe_mode", False)
        safe = ttk.Checkbutton(
            safety,
            text="生产安全模式",
            variable=safe_var,
            style="Card.TCheckbutton",
            command=self._apply_safe_defaults,
        )
        safe.grid(row=0, column=0, sticky="w")
        self._attach_help(
            safe,
            "强制 full 内容校验、fail 冲突策略、目标集合互斥租约和执行计划审批。"
            "目标资源指标无法读取时会阻止写入。",
        )
        self._check_option(
            safety,
            "源端持续写入",
            "continuous_writes",
            False,
            "声明迁移期间业务仍会写入。生产安全模式下必须启用 Change Streams，"
            "且持续写入时禁止自动切换。",
        ).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self._check_option(
            safety,
            "运行时熔断",
            "runtime_guard",
            True,
            "每 5 秒检查目标连接占用、WiredTiger Cache 与磁盘空间；超阈值时在批次边界暂停。",
        ).grid(row=0, column=2, sticky="w", padx=(16, 0))
        self._check_option(
            safety,
            "仅生成计划",
            "plan_only",
            False,
            "只连接、预检并生成审批计划，不创建集合、不写文档、不复制索引。",
        ).grid(row=0, column=3, sticky="w", padx=(16, 0))
        guard_fields = [
            ("Cache 上限 %", "max_cache_percent", "85", "WiredTiger Cache 使用率达到该值时暂停。"),
            ("连接上限 %", "max_connections_percent", "85", "MongoDB 连接槽位使用率达到该值时暂停。"),
            ("磁盘最低空闲 %", "min_disk_free_percent", "10", "目标文件系统空闲率低于该值时暂停。"),
        ]
        for idx, (label, name, default, help_text) in enumerate(guard_fields):
            column = idx * 2
            self._field_label(
                safety, label, help_text, row=1, column=column
            )
            ttk.Entry(
                safety, textvariable=self._var(name, default), width=10
            ).grid(row=1, column=column + 1, sticky="ew", padx=(7, 14))
        self._field_label(
            safety,
            "熔断超时秒",
            "资源持续不安全超过该时间后任务失败，不会无限等待。",
            row=2,
            column=0,
        )
        ttk.Entry(
            safety, textvariable=self._var("safety_pause_timeout", "300"), width=10
        ).grid(row=2, column=1, sticky="w", padx=(7, 14))
        self._field_label(
            safety,
            "计划审批码",
            "生产安全模式执行前必须填写计划文件中的 8 位 approval_code。"
            "GUI 首次运行会先展示审批码并可由你确认后继续。",
            row=2,
            column=2,
        )
        ttk.Entry(
            safety, textvariable=self._var("approval_token", ""), width=16
        ).grid(row=2, column=3, sticky="w", padx=(7, 14))

    def _build_runtime(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        summary = ttk.Frame(parent)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        for column in range(4):
            summary.columnconfigure(column, weight=1)
        self.summary_vars: dict[str, tk.StringVar] = {}
        summary_items = [
            ("集合范围", "summary_collections", "未读取", COLORS["blue"]),
            ("同步模式", "summary_mode", "离线全量", COLORS["green"]),
            ("校验策略", "summary_verify", "SAMPLE", COLORS["amber"]),
            ("发布动作", "summary_cutover", "仅写影子集合", COLORS["red"]),
        ]
        for column, (title, name, value, accent) in enumerate(summary_items):
            card = tk.Frame(
                summary,
                background=COLORS["card"],
                highlightbackground=COLORS["border"],
                highlightthickness=1,
                padx=14,
                pady=10,
            )
            card.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(0, 10) if column < 3 else 0,
            )
            tk.Frame(card, background=accent, width=4, height=38).pack(side="left")
            values = tk.Frame(card, background=COLORS["card"])
            values.pack(side="left", padx=(10, 0))
            tk.Label(
                values,
                text=title,
                foreground=COLORS["muted"],
                background=COLORS["card"],
                font=("Helvetica Neue", 10),
            ).pack(anchor="w")
            variable = tk.StringVar(value=value)
            self.summary_vars[name] = variable
            tk.Label(
                values,
                textvariable=variable,
                foreground=COLORS["text"],
                background=COLORS["card"],
                font=("Helvetica Neue", 12, "bold"),
            ).pack(anchor="w")

        controls = ttk.Frame(parent)
        controls.grid(row=1, column=0, sticky="ew")
        self.preflight_button = ttk.Button(
            controls, text="✓ 连接预检", command=self._preflight
        )
        self.preflight_button.pack(side="left")
        self.start_button = ttk.Button(
            controls, text="▶ 开始迁移", style="Primary.TButton", command=self._start
        )
        self.start_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(
            controls,
            text="■ 安全停止",
            style="Danger.TButton",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.pack(side="left")
        ttk.Button(
            controls, text="导出日志", command=self._export_log
        ).pack(side="right")
        ttk.Button(
            controls, text="导出诊断包", command=self._export_diagnostics
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            controls, text="清空日志", command=self._clear_log
        ).pack(side="right", padx=(0, 8))
        self.status_var = tk.StringVar(value="就绪")
        status_frame = ttk.Frame(parent)
        status_frame.grid(row=2, column=0, sticky="ew", pady=(14, 7))
        self.status_dot = tk.Label(
            status_frame,
            text="●",
            foreground=COLORS["green"],
            background=COLORS["bg"],
            font=("Helvetica Neue", 13),
        )
        self.status_dot.pack(side="left")
        ttk.Label(
            status_frame, textvariable=self.status_var, style="Status.TLabel"
        ).pack(side="left", padx=(5, 0))
        self.progress = ttk.Progressbar(parent, mode="indeterminate")
        self.progress.grid(row=2, column=0, sticky="e", padx=(360, 0))
        log_frame = ttk.Frame(parent)
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            font=("Menlo", 11),
            background=COLORS["log"],
            foreground="#d1d5db",
            insertbackground="white",
            borderwidth=0,
            padx=12,
            pady=12,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set, state="disabled")
        self._append_log("MongoDB Migrate 已就绪。请先读取集合并执行连接预检。")

    def _build_backup_center(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(3, weight=1)

        intro = ttk.Frame(parent)
        intro.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(
            intro,
            text="本地 BSON 备份 · 校验恢复 · JSONL / CSV 数据交换",
            style="Status.TLabel",
        ).pack(side="left")
        self.archive_status_var = tk.StringVar(value="备份中心就绪")
        ttk.Label(
            intro, textvariable=self.archive_status_var, style="Muted.TLabel"
        ).pack(side="right")

        backup = ttk.LabelFrame(
            parent, text="创建可验证备份", style="Section.TLabelframe"
        )
        backup.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        backup.columnconfigure(1, weight=1)
        self._field_label(
            backup, "输出文件", "原子生成 .mmbackup；包含原始 BSON、集合选项、"
            "索引、计数和每段 SHA-256，未成功时不会留下半成品。",
            row=0, column=0,
        )
        default_backup = self.app_data_dir / "backups" / time.strftime(
            "mongodb-%Y%m%d-%H%M%S.mmbackup"
        )
        ttk.Entry(
            backup, textvariable=self._var("backup_output", str(default_backup))
        ).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(
            backup, text="选择…", command=self._choose_backup_output
        ).grid(row=0, column=2)
        self._field_label(
            backup, "集合范围", "默认沿用第一页的多选集合；为空时使用此处 glob。"
            "View 与 system.* 不进入逻辑备份。", row=1, column=0,
        )
        ttk.Entry(
            backup, textvariable=self._var("backup_collections", "*")
        ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        backup_flags = ttk.Frame(backup, style="Card.TFrame")
        backup_flags.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        self._check_option(
            backup_flags, "AES-256 加密", "backup_encrypt", False,
            "使用 PBKDF2-SHA256 派生密钥和 AES-256-GCM 认证加密。密码不保存到设置、"
            "状态库或备份清单，丢失后无法恢复。",
        ).pack(side="left")
        ttk.Label(backup_flags, text="密码", style="Card.TLabel").pack(
            side="left", padx=(14, 5)
        )
        self.backup_password_var = tk.StringVar()
        ttk.Entry(
            backup_flags, textvariable=self.backup_password_var, show="•", width=18
        ).pack(side="left")
        ttk.Label(backup_flags, text="压缩级别", style="Card.TLabel").pack(
            side="left", padx=(14, 5)
        )
        ttk.Combobox(
            backup_flags,
            textvariable=self._var("backup_compression", "6"),
            values=tuple(str(value) for value in range(10)),
            state="readonly",
            width=4,
        ).pack(side="left")
        ttk.Label(backup_flags, text="保留天数", style="Card.TLabel").pack(
            side="left", padx=(14, 5)
        )
        ttk.Entry(
            backup_flags, textvariable=self._var("backup_retention_days", "30"), width=6
        ).pack(side="left")
        backup_controls = ttk.Frame(backup, style="Card.TFrame")
        backup_controls.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.backup_start_button = ttk.Button(
            backup_controls, text="创建备份", style="Primary.TButton",
            command=self._start_backup,
        )
        self.backup_start_button.pack(side="left")
        ttk.Button(
            backup_controls, text="校验文件", command=self._verify_backup_file
        ).pack(side="left", padx=7)

        restore = ttk.LabelFrame(
            parent, text="校验与恢复", style="Section.TLabelframe"
        )
        restore.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        restore.columnconfigure(1, weight=1)
        self._field_label(
            restore, "备份文件", "恢复前默认验证 Manifest、元数据和每个 BSON 数据段的"
            "SHA-256；损坏时在任何目标写入前失败。", row=0, column=0,
        )
        ttk.Entry(
            restore, textvariable=self._var("restore_input", "")
        ).grid(row=0, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(
            restore, text="选择…", command=self._choose_restore_input
        ).grid(row=0, column=2)
        restore_flags = ttk.Frame(restore, style="Card.TFrame")
        restore_flags.grid(row=1, column=0, columnspan=3, sticky="ew", pady=7)
        ttk.Label(restore_flags, text="冲突策略", style="Card.TLabel").pack(side="left")
        ttk.Combobox(
            restore_flags,
            textvariable=self._var("restore_conflict", "fail"),
            values=("fail", "merge", "drop"), state="readonly", width=8,
        ).pack(side="left", padx=(5, 12))
        self._attach_help(
            restore_flags,
            "fail：目标集合存在即停止；merge：按 _id 幂等覆盖；drop：删除目标集合后重建。"
            "drop 属于破坏性操作，界面会再次确认。",
        )
        self._check_option(
            restore_flags, "恢复索引", "restore_indexes", True,
            "文档恢复完成后重建备份记录的二级索引；_id_ 索引由 MongoDB 自动创建。",
        ).pack(side="left")
        ttk.Label(restore_flags, text="解密密码", style="Card.TLabel").pack(
            side="left", padx=(12, 5)
        )
        self.restore_password_var = tk.StringVar()
        ttk.Entry(
            restore_flags, textvariable=self.restore_password_var, show="•", width=18
        ).pack(side="left")
        restore_controls = ttk.Frame(restore, style="Card.TFrame")
        restore_controls.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.restore_start_button = ttk.Button(
            restore_controls, text="恢复到目标库", style="Primary.TButton",
            command=self._start_restore,
        )
        self.restore_start_button.pack(side="left")
        self.archive_stop_button = ttk.Button(
            restore_controls, text="安全停止", style="Danger.TButton",
            command=self._stop_archive, state="disabled",
        )
        self.archive_stop_button.pack(side="left", padx=7)

        exchange = ttk.LabelFrame(
            parent, text="数据交换（不是备份）", style="Section.TLabelframe"
        )
        exchange.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        exchange.columnconfigure(3, weight=1)
        ttk.Label(exchange, text="集合", style="Card.TLabel").grid(row=0, column=0)
        ttk.Entry(
            exchange, textvariable=self._var("exchange_collection", "")
        ).grid(row=0, column=1, padx=(6, 14))
        ttk.Label(exchange, text="文件", style="Card.TLabel").grid(row=0, column=2)
        ttk.Entry(
            exchange, textvariable=self._var("exchange_path", "")
        ).grid(row=0, column=3, sticky="ew", padx=(6, 6))
        ttk.Button(exchange, text="选择…", command=self._choose_exchange_path).grid(
            row=0, column=4
        )
        ttk.Label(exchange, text="格式", style="Card.TLabel").grid(
            row=1, column=0, pady=(8, 0)
        )
        ttk.Combobox(
            exchange,
            textvariable=self._var("exchange_format", "jsonl"),
            values=("jsonl", "csv"), state="readonly", width=8,
        ).grid(row=1, column=1, sticky="w", padx=(6, 14), pady=(8, 0))
        fields_label = ttk.Label(exchange, text="CSV 字段", style="Card.TLabel")
        fields_label.grid(row=1, column=2, pady=(8, 0))
        self._attach_help(
            fields_label,
            "CSV 必须显式列出逗号分隔字段；嵌套字段支持 profile.name。"
            "CSV 导入按字符串处理，因此正式备份请使用 BSON，数据交换优先 JSONL。",
        )
        ttk.Entry(
            exchange, textvariable=self._var("exchange_fields", "")
        ).grid(row=1, column=3, sticky="ew", padx=(6, 6), pady=(8, 0))
        ttk.Button(exchange, text="从源库导出", command=self._start_export).grid(
            row=1, column=4, padx=(0, 6), pady=(8, 0)
        )
        ttk.Button(exchange, text="导入目标库", command=self._start_import).grid(
            row=1, column=5, pady=(8, 0)
        )

        assets = ttk.LabelFrame(
            parent, text="备份资产目录", style="Section.TLabelframe"
        )
        assets.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        assets.columnconfigure(0, weight=1)
        assets.rowconfigure(0, weight=1)
        columns = ("kind", "created", "path", "documents", "size", "encrypted", "verified")
        self.asset_tree = ttk.Treeview(
            assets, columns=columns, show="headings", height=7, selectmode="browse"
        )
        headings = {
            "kind": "类型", "created": "创建时间", "path": "文件",
            "documents": "文档", "size": "大小", "encrypted": "加密", "verified": "已校验",
        }
        widths = {
            "kind": 70, "created": 135, "path": 420, "documents": 80,
            "size": 80, "encrypted": 60, "verified": 90,
        }
        for name in columns:
            self.asset_tree.heading(name, text=headings[name])
            self.asset_tree.column(name, width=widths[name], anchor="w")
        self.asset_tree.grid(row=0, column=0, sticky="nsew")
        asset_scroll = ttk.Scrollbar(
            assets, orient="vertical", command=self.asset_tree.yview
        )
        asset_scroll.grid(row=0, column=1, sticky="ns")
        self.asset_tree.configure(yscrollcommand=asset_scroll.set)
        asset_controls = ttk.Frame(assets, style="Card.TFrame")
        asset_controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(asset_controls, text="刷新目录", command=self._refresh_assets).pack(side="left")
        ttk.Button(
            asset_controls, text="选中用于恢复", command=self._select_asset_for_restore
        ).pack(side="left", padx=7)
        ttk.Button(
            asset_controls, text="从目录移除", command=self._forget_selected_asset
        ).pack(side="left")
        self.after(50, self._refresh_assets)

    def _toggle_uris(self) -> None:
        show = "" if self.source_uri_entry.cget("show") else "•"
        self.source_uri_entry.configure(show=show)
        self.target_uri_entry.configure(show=show)

    def _choose_backup_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            parent=self, title="创建 MongoDB BSON 备份",
            defaultextension=".mmbackup",
            filetypes=[("MongoDB Migrate Backup", "*.mmbackup"), ("所有文件", "*")],
        )
        if filename:
            self.vars["backup_output"].set(filename)

    def _choose_restore_input(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self, title="选择 MongoDB BSON 备份",
            filetypes=[("MongoDB Migrate Backup", "*.mmbackup"), ("所有文件", "*")],
        )
        if filename:
            self.vars["restore_input"].set(filename)

    def _choose_exchange_path(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self, title="选择 JSONL / CSV 文件",
            filetypes=[("数据交换文件", "*.jsonl *.json *.csv"), ("所有文件", "*")],
        )
        if filename:
            self.vars["exchange_path"].set(filename)

    def _archive_query(self) -> dict[str, Any] | None:
        from bson import json_util

        raw = self.vars["query"].get().strip()
        value = json_util.loads(raw) if raw else None
        if value is not None and not isinstance(value, dict):
            raise ValueError("文档过滤 Query 必须是 Extended JSON 对象")
        return value

    def _backup_collection_pattern(self) -> str:
        selected = [
            self.visible_collection_names[index]
            for index in self.collection_list.curselection()
        ]
        return ",".join(selected) if selected else self.vars["backup_collections"].get()

    def _archive_progress(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", "progress"))
        collection = event.get("collection", "")
        documents = event.get("documents")
        detail = f" · {collection}" if collection else ""
        if documents is not None:
            detail += f" · {int(documents):,} 文档"
        self.messages.put(("archive_log", f"{kind}{detail}"))

    def _run_archive_task(self, operation: str, function: Any, status: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("已有任务", "请等待当前任务完成或安全停止。", parent=self)
            return
        self.archive_cancel.clear()
        self._set_archive_running(True, status)

        def work() -> None:
            try:
                result = function()
                self.messages.put(("archive_completed", {"operation": operation, "result": result}))
            except ArchiveCancelled:
                self.messages.put(("archive_cancelled", operation))
            except Exception as exc:  # noqa: BLE001 - GUI process boundary
                self.messages.put(("archive_error", str(exc)))

        self.worker = threading.Thread(
            target=work, daemon=True, name=f"mongodb-{operation}"
        )
        self.worker.start()

    def _start_backup(self) -> None:
        try:
            encrypted = bool(self.vars["backup_encrypt"].get())
            password = self.backup_password_var.get() if encrypted else ""
            options = BackupOptions(
                source_uri=self.vars["source_uri"].get().strip(),
                source_db=self.vars["source_db"].get().strip(),
                output=self.vars["backup_output"].get().strip(),
                collections=self._backup_collection_pattern(),
                exclude=self.vars["exclude"].get(),
                query=self._archive_query(),
                compression_level=int(self.vars["backup_compression"].get()),
                encryption_password=password,
            )
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("备份参数错误", str(exc), parent=self)
            return
        self._run_archive_task(
            "backup",
            lambda: create_backup(
                options, hook=self._archive_progress, cancel=self.archive_cancel
            ),
            "正在创建 BSON 备份…",
        )

    def _verify_backup_file(self) -> None:
        path = self.vars["restore_input"].get().strip()
        if not path:
            self._choose_restore_input()
            path = self.vars["restore_input"].get().strip()
        if not path:
            return
        password = self.restore_password_var.get()
        self._run_archive_task(
            "verify",
            lambda: verify_backup(
                path, password=password, hook=self._archive_progress,
                cancel=self.archive_cancel,
            ),
            "正在逐段校验备份…",
        )

    def _start_restore(self) -> None:
        try:
            options = RestoreOptions(
                target_uri=self.vars["target_uri"].get().strip(),
                target_db=self.vars["target_db"].get().strip(),
                input=self.vars["restore_input"].get().strip(),
                encryption_password=self.restore_password_var.get(),
                conflict=self.vars["restore_conflict"].get(),
                restore_indexes=bool(self.vars["restore_indexes"].get()),
            )
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("恢复参数错误", str(exc), parent=self)
            return
        if options.conflict == "drop" and not messagebox.askyesno(
            "确认删除目标集合",
            "DROP 模式会删除备份中同名的目标集合，再按备份重建。此操作不可自动撤销，继续吗？",
            parent=self,
        ):
            return
        if not messagebox.askyesno(
            "确认恢复",
            f"将校验备份后恢复到目标数据库：{options.target_db}\n"
            f"冲突策略：{options.conflict}\n\n继续吗？",
            parent=self,
        ):
            return
        self._run_archive_task(
            "restore",
            lambda: restore_backup(
                options, hook=self._archive_progress, cancel=self.archive_cancel
            ),
            "正在校验并恢复备份…",
        )

    def _exchange_output(self, format_name: str) -> str:
        path = self.vars["exchange_path"].get().strip()
        if path:
            return path
        filename = filedialog.asksaveasfilename(
            parent=self, title="选择导出文件",
            defaultextension=f".{format_name}",
            filetypes=[(format_name.upper(), f"*.{format_name}"), ("所有文件", "*")],
        )
        if filename:
            self.vars["exchange_path"].set(filename)
        return filename

    def _start_export(self) -> None:
        try:
            format_name = self.vars["exchange_format"].get()
            options = ExportOptions(
                source_uri=self.vars["source_uri"].get().strip(),
                source_db=self.vars["source_db"].get().strip(),
                collection=self.vars["exchange_collection"].get().strip(),
                output=self._exchange_output(format_name),
                format=format_name,
                fields=tuple(
                    field.strip()
                    for field in self.vars["exchange_fields"].get().split(",")
                    if field.strip()
                ),
                query=self._archive_query(),
            )
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("导出参数错误", str(exc), parent=self)
            return
        self._run_archive_task(
            "export",
            lambda: export_data(
                options, hook=self._archive_progress, cancel=self.archive_cancel
            ),
            "正在导出业务数据…",
        )

    def _start_import(self) -> None:
        try:
            options = ImportOptions(
                target_uri=self.vars["target_uri"].get().strip(),
                target_db=self.vars["target_db"].get().strip(),
                collection=self.vars["exchange_collection"].get().strip(),
                input=self.vars["exchange_path"].get().strip(),
                format=self.vars["exchange_format"].get(),
                conflict="merge",
            )
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("导入参数错误", str(exc), parent=self)
            return
        if not messagebox.askyesno(
            "确认导入",
            f"将按 _id 合并写入 {options.target_db}.{options.collection}。\n"
            "CSV 字段会按字符串处理；确认继续吗？",
            parent=self,
        ):
            return
        self._run_archive_task(
            "import",
            lambda: import_data(
                options, hook=self._archive_progress, cancel=self.archive_cancel
            ),
            "正在导入业务数据…",
        )

    def _stop_archive(self) -> None:
        if messagebox.askyesno(
            "安全停止", "将在当前文档批次或校验段边界停止。", parent=self
        ):
            self.archive_cancel.set()
            self.archive_status_var.set("正在安全停止…")

    def _set_archive_running(self, running: bool, status: str) -> None:
        self.archive_status_var.set(status)
        self.backup_start_button.configure(state="disabled" if running else "normal")
        self.restore_start_button.configure(state="disabled" if running else "normal")
        self.archive_stop_button.configure(state="normal" if running else "disabled")

    def _catalog_asset(self, kind: str, result: dict[str, Any]) -> None:
        manifest = result.get("manifest") or {}
        source = manifest.get("source") or {}
        store = MigrationStore(self.vars["state_db"].get())
        try:
            store.register_asset(
                kind=kind,
                path=result["path"],
                source_endpoint=source.get("endpoint", ""),
                source_db=source.get("database", self.vars["source_db"].get()),
                collections=int(result.get("collections", 0)),
                documents=int(result.get("documents", 0)),
                size_bytes=int(result.get("size", 0)),
                sha256=str(result.get("sha256", "")),
                encrypted=bool(result.get("encrypted", False)),
                verified=kind == "backup",
                retention_days=max(0, int(self.vars["backup_retention_days"].get())),
                metadata={"format": result.get("format", kind)},
            )
        finally:
            store.close()

    def _refresh_assets(self) -> None:
        if not hasattr(self, "asset_tree"):
            return
        store = MigrationStore(self.vars["state_db"].get())
        try:
            assets = store.list_assets()
        finally:
            store.close()
        self.asset_tree.delete(*self.asset_tree.get_children())
        for asset in assets:
            created = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(float(asset["created_at"]))
            )
            verified = (
                time.strftime("%Y-%m-%d", time.localtime(float(asset["verified_at"])))
                if asset["verified_at"] else "否"
            )
            self.asset_tree.insert(
                "", "end", iid=asset["id"],
                values=(
                    asset["kind"], created, asset["path"],
                    f"{int(asset['documents']):,}", self._human_size(int(asset["size_bytes"])),
                    "是" if asset["encrypted"] else "否", verified,
                ),
            )

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TiB"

    def _selected_asset(self) -> tuple[str, str] | None:
        selected = self.asset_tree.selection()
        if not selected:
            messagebox.showwarning("未选择", "请先选择一个备份资产。", parent=self)
            return None
        asset_id = selected[0]
        values = self.asset_tree.item(asset_id, "values")
        return asset_id, str(values[2])

    def _select_asset_for_restore(self) -> None:
        selected = self._selected_asset()
        if selected:
            self.vars["restore_input"].set(selected[1])

    def _forget_selected_asset(self) -> None:
        selected = self._selected_asset()
        if not selected:
            return
        if not messagebox.askyesno(
            "从目录移除",
            "只移除资产目录记录，不会删除备份文件。继续吗？",
            parent=self,
        ):
            return
        store = MigrationStore(self.vars["state_db"].get())
        try:
            store.forget_asset(selected[0])
        finally:
            store.close()
        self._refresh_assets()

    def _filter_collection_list(self) -> None:
        if not hasattr(self, "collection_list"):
            return
        term = self.vars.get("collection_search")
        needle = str(term.get()).strip().lower() if term else ""
        self.visible_collection_names = [
            name for name in self.collection_names if needle in name.lower()
        ]
        self.collection_list.delete(0, "end")
        for name in self.visible_collection_names:
            self.collection_list.insert("end", name)
        self._update_collection_count()

    def _select_all_collections(self) -> None:
        self.collection_list.selection_set(0, "end")
        self._update_collection_count()

    def _clear_collection_selection(self) -> None:
        self.collection_list.selection_clear(0, "end")
        self._update_collection_count()

    def _update_collection_count(self) -> None:
        if not hasattr(self, "collection_count_var"):
            return
        selected = len(self.collection_list.curselection())
        total = len(self.collection_names)
        visible = len(self.visible_collection_names)
        if not total:
            text = "尚未读取集合"
        elif selected:
            text = f"已选 {selected} · 当前显示 {visible} · 共 {total}"
        else:
            text = f"当前显示 {visible} · 共 {total}"
        self.collection_count_var.set(text)
        if hasattr(self, "summary_vars"):
            self.summary_vars["summary_collections"].set(
                f"{selected} 个已选" if selected else f"{total} 个匹配"
            )

    def _fetch_collections(self) -> None:
        source_uri = self.vars["source_uri"].get()
        source_db = self.vars["source_db"].get()
        patterns = self.vars["collections"].get()
        exclude = self.vars["exclude"].get()
        self._run_background(
            lambda: self._fetch_collections_worker(
                source_uri, source_db, patterns, exclude
            ),
            "正在读取源集合…",
        )

    def _fetch_collections_worker(
        self, source_uri: str, source_db: str, patterns: str, exclude: str
    ) -> tuple[str, Any]:
        client = MongoClient(source_uri, serverSelectionTimeoutMS=8000)
        try:
            client.admin.command("ping")
            names = select_collections(
                client[source_db].list_collection_names(),
                patterns,
                exclude,
            )
            return "collections", names
        finally:
            client.close()

    def _preflight(self) -> None:
        try:
            options = self._migration_options()
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        self._refresh_summary(options)
        self._run_background(
            lambda: self._preflight_worker(options),
            "正在执行连接和权限预检…",
        )

    def _preflight_worker(self, options: MigrationOptions) -> tuple[str, Any]:
        engine = MigrationEngine(options)
        try:
            names = engine.preflight()
            return "preflight", names
        finally:
            engine.close()

    def _selected_pattern(self) -> str:
        selected = [
            self.visible_collection_names[i]
            for i in self.collection_list.curselection()
        ]
        return ",".join(selected) if selected else self.vars["collections"].get()

    def _migration_options(self) -> MigrationOptions:
        from bson import json_util

        job_id = self.vars["job_id"].get().strip()
        conflict = self.vars["conflict"].get()
        if job_id and not self.vars["production_safe_mode"].get():
            conflict = "resume"
        query_text = self.vars["query"].get().strip()
        query = json_util.loads(query_text) if query_text else None
        if query is not None and not isinstance(query, dict):
            raise ValueError("文档过滤 Query 必须解析为 JSON 对象")
        return MigrationOptions(
            source_uri=self.vars["source_uri"].get().strip(),
            target_uri=self.vars["target_uri"].get().strip(),
            source_db=self.vars["source_db"].get().strip(),
            target_db=self.vars["target_db"].get().strip(),
            collections=self._selected_pattern(),
            exclude=self.vars["exclude"].get(),
            target_suffix=self.vars["target_suffix"].get(),
            query=query,
            batch_size=int(self.vars["batch_size"].get()),
            batch_bytes=int(float(self.vars["batch_mib"].get()) * 1024 * 1024),
            workers=int(self.vars["workers"].get()),
            docs_per_second=float(self.vars["docs_per_second"].get()),
            max_retries=int(self.vars["max_retries"].get()),
            retry_backoff=float(self.vars["retry_backoff"].get()),
            incremental_field=self.vars["incremental_field"].get().strip(),
            incremental_rounds=int(self.vars["incremental_rounds"].get()),
            incremental_overlap_seconds=float(
                self.vars["incremental_overlap_seconds"].get()
            ),
            incremental_interval=float(self.vars["incremental_interval"].get()),
            convergence_rounds=int(self.vars["convergence_rounds"].get()),
            cdc_enabled=bool(self.vars["cdc_enabled"].get()),
            cdc_quiet_seconds=float(self.vars["cdc_quiet_seconds"].get()),
            cdc_max_seconds=float(self.vars["cdc_max_seconds"].get()),
            verify=self.vars["verify"].get(),
            sample_size=int(self.vars["sample_size"].get()),
            copy_indexes=bool(self.vars["copy_indexes"].get()),
            cutover=bool(self.vars["cutover"].get()),
            dry_run=bool(self.vars["dry_run"].get()),
            conflict=conflict,
            state_db=self.vars["state_db"].get(),
            dlq_dir=self.vars["dlq_dir"].get(),
            job_id=job_id,
            lease_ttl=int(self.vars["lease_ttl"].get()),
            report_dir=self.vars["report_dir"].get(),
            production_safe_mode=bool(self.vars["production_safe_mode"].get()),
            continuous_writes=bool(self.vars["continuous_writes"].get()),
            runtime_guard=bool(self.vars["runtime_guard"].get()),
            max_cache_percent=float(self.vars["max_cache_percent"].get()),
            max_connections_percent=float(
                self.vars["max_connections_percent"].get()
            ),
            min_disk_free_percent=float(
                self.vars["min_disk_free_percent"].get()
            ),
            safety_pause_timeout=float(
                self.vars["safety_pause_timeout"].get()
            ),
            approval_token=self.vars["approval_token"].get().strip(),
            plan_only=bool(self.vars["plan_only"].get()),
        )

    def _apply_safe_defaults(self) -> None:
        if self.vars["production_safe_mode"].get():
            self.vars["verify"].set("full")
            self.vars["conflict"].set("fail")
            self.vars["runtime_guard"].set(True)
            self._append_log(
                "INFO  生产安全模式已应用：FULL 校验、fail 冲突策略、"
                "运行时熔断和计划审批"
            )

    def _refresh_summary(self, options: MigrationOptions) -> None:
        if not hasattr(self, "summary_vars"):
            return
        selected_count = len(self.collection_list.curselection())
        collection_label = (
            f"{selected_count} 个已选"
            if selected_count
            else options.collections
        )
        self.summary_vars["summary_collections"].set(collection_label)
        self.summary_vars["summary_mode"].set(
            "Change Streams CDC"
            if options.cdc_enabled
            else (
                f"追平 {options.incremental_rounds} 轮"
                if options.incremental_rounds
                else "离线全量"
            )
        )
        self.summary_vars["summary_verify"].set(options.verify.upper())
        self.summary_vars["summary_cutover"].set(
            "校验后切换" if options.cutover else "仅写影子集合"
        )

    def _bind_summary_updates(self) -> None:
        def refresh(_name: str = "", _index: str = "", _mode: str = "") -> None:
            if not hasattr(self, "summary_vars"):
                return
            rounds = self.vars["incremental_rounds"].get().strip()
            self.summary_vars["summary_mode"].set(
                "Change Streams CDC"
                if self.vars["cdc_enabled"].get()
                else (
                    f"追平 {rounds} 轮"
                    if rounds not in {"", "0"}
                    else "离线全量"
                )
            )
            self.summary_vars["summary_verify"].set(
                self.vars["verify"].get().upper()
            )
            self.summary_vars["summary_cutover"].set(
                "校验后切换"
                if self.vars["cutover"].get()
                else "仅写影子集合"
            )

        for name in ("incremental_rounds", "cdc_enabled", "verify", "cutover"):
            self.vars[name].trace_add("write", refresh)
        refresh()

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            options = self._migration_options()
            options.validate()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        self._refresh_summary(options)
        if options.query and options.cutover and not messagebox.askyesno(
            "过滤迁移风险",
            "当前设置了文档过滤 Query，同时开启了集合切换。切换后目标正式集合"
            "只包含过滤结果，确认这是你的预期吗？",
            parent=self,
        ):
            return
        if options.cutover and not messagebox.askyesno(
            "确认安全切换",
            "校验成功后将改名目标现有集合并切换影子集合。旧集合会保留为 backup。继续吗？",
            parent=self,
        ):
            return
        self.notebook.select(2)
        self.engine = MigrationEngine(options)
        self._set_running(True, "迁移正在运行…")
        self.worker = threading.Thread(target=self._migration_worker, daemon=True)
        self.worker.start()

    def _migration_worker(self) -> None:
        handler = QueueLogHandler(self.messages)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
        logger = logging.getLogger("mongodb_migrate")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            assert self.engine is not None
            job_id = self.engine.run()
            kind = "planned" if self.engine.options.plan_only else "completed"
            self.messages.put((kind, job_id))
        except MigrationCancelled:
            self.messages.put(("cancelled", None))
        except PlanApprovalRequired as exc:
            self.messages.put(("approval_required", {
                "job_id": exc.plan["job_id"],
                "approval_code": exc.plan["approval_code"],
                "path": str(exc.path),
            }))
        except Exception as exc:  # noqa: BLE001 - GUI boundary
            self.messages.put(("error", str(exc)))
        finally:
            logger.removeHandler(handler)
            self.engine = None

    def _stop(self) -> None:
        if self.engine and messagebox.askyesno(
            "安全停止", "将在当前批次边界停止，可使用任务 ID 恢复。", parent=self
        ):
            self.engine.cancel()
            self.status_var.set("正在安全停止…")

    def _run_background(self, function: Any, status: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._set_running(True, status)

        def work() -> None:
            try:
                self.messages.put(function())
            except Exception as exc:  # noqa: BLE001 - GUI boundary
                self.messages.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "collections":
                    self.collection_names = list(payload)
                    self._filter_collection_list()
                    self._set_running(False, f"已读取 {len(payload)} 个集合")
                    self._append_log(f"INFO  已读取 {len(payload)} 个源集合")
                elif kind == "preflight":
                    self._set_running(False, f"预检通过：{len(payload)} 个集合")
                    self._append_log(
                        f"INFO  连接、权限与集合预检通过，共匹配 {len(payload)} 个集合"
                    )
                    messagebox.showinfo(
                        "预检通过", f"源端、目标端可连接，共匹配 {len(payload)} 个集合。"
                    )
                elif kind == "completed":
                    self.vars["job_id"].set(str(payload))
                    self._set_running(False, f"迁移完成 · Job {payload}")
                    self._save_settings()
                    self._append_log(f"INFO  迁移完成，Job ID: {payload}")
                    messagebox.showinfo("迁移完成", f"任务 ID：{payload}", parent=self)
                elif kind == "planned":
                    self.vars["job_id"].set(str(payload))
                    self._set_running(False, f"执行计划已生成 · Job {payload}")
                    self._save_settings()
                    self._append_log(f"INFO  执行计划已生成，Job ID: {payload}")
                    messagebox.showinfo(
                        "计划已生成",
                        f"未写入 MongoDB。\n任务 ID：{payload}\n"
                        f"计划目录：{self.vars['report_dir'].get()}",
                        parent=self,
                    )
                elif kind == "approval_required":
                    details = dict(payload)
                    self.vars["job_id"].set(details["job_id"])
                    self._set_running(False, "等待生产计划审批")
                    self._append_log(
                        f"WARNING  计划等待审批：{details['approval_code']} · "
                        f"{details['path']}"
                    )
                    if messagebox.askyesno(
                        "审批生产执行计划",
                        f"请审阅计划文件：\n{details['path']}\n\n"
                        f"计划审批码：{details['approval_code']}\n\n"
                        "确认该源端、目标端、集合范围与安全策略无误，并执行迁移吗？",
                        parent=self,
                    ):
                        self.vars["approval_token"].set(details["approval_code"])
                        self.vars["plan_only"].set(False)
                        self.after(50, self._start)
                elif kind == "cancelled":
                    self._set_running(False, "已安全停止，可使用任务 ID 恢复")
                    self._append_log("WARNING  任务已在安全批次边界停止")
                elif kind == "archive_log":
                    self.archive_status_var.set(str(payload))
                    self._append_log(f"INFO  {payload}")
                elif kind == "archive_completed":
                    details = dict(payload)
                    operation = details["operation"]
                    result = dict(details["result"])
                    self._set_archive_running(False, f"{operation} 已完成")
                    if operation == "backup":
                        self._catalog_asset("backup", result)
                        self.vars["restore_input"].set(result["path"])
                        self.backup_password_var.set("")
                    elif operation == "export":
                        self._catalog_asset("export", result)
                    elif operation == "verify":
                        store = MigrationStore(self.vars["state_db"].get())
                        try:
                            try:
                                store.mark_asset_verified(result["path"])
                            except KeyError:
                                self._catalog_asset("backup", result)
                        finally:
                            store.close()
                    self.restore_password_var.set("")
                    self._refresh_assets()
                    self._append_log(
                        f"INFO  {operation} 完成：{result.get('path', '')} · "
                        f"{int(result.get('documents', 0)):,} 文档"
                    )
                    messagebox.showinfo(
                        "操作完成",
                        f"{operation} 已完成。\n"
                        f"文档：{int(result.get('documents', 0)):,}\n"
                        f"文件：{result.get('path', '—')}",
                        parent=self,
                    )
                elif kind == "archive_cancelled":
                    self._set_archive_running(False, "操作已安全停止")
                    self._append_log(f"WARNING  {payload} 已安全停止")
                elif kind == "archive_error":
                    self._set_archive_running(False, "备份中心操作失败")
                    self.backup_password_var.set("")
                    self.restore_password_var.set("")
                    self._append_log(f"ERROR  备份中心：{payload}")
                    messagebox.showerror("备份中心操作失败", str(payload), parent=self)
                elif kind == "error":
                    self._append_log(f"ERROR  {payload}")
                    self._set_running(False, "操作失败")
                    messagebox.showerror("操作失败", str(payload), parent=self)
        except queue.Empty:
            pass
        self.after(100, self._drain_messages)

    def _set_running(self, running: bool, status: str) -> None:
        self.status_var.set(status)
        self.status_dot.configure(
            foreground=COLORS["blue"] if running else COLORS["green"]
        )
        self.start_button.configure(state="disabled" if running else "normal")
        self.preflight_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running and self.engine else "disabled")
        if running:
            self.progress.configure(mode="indeterminate", maximum=100, value=0)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)

    def _poll_runtime_metrics(self) -> None:
        engine = self.engine
        if engine and engine.job_id:
            try:
                report = engine.store.report(engine.job_id)
                tasks = report["collections"]
                total = sum(int(task["source_docs"]) for task in tasks)
                copied = sum(int(task["copied_docs"]) for task in tasks)
                completed = sum(
                    task["status"] == "completed" for task in tasks
                )
                if total > 0:
                    self.progress.stop()
                    self.progress.configure(
                        mode="determinate",
                        maximum=total,
                        value=min(copied, total),
                    )
                    percent = min(100, copied * 100 / total)
                    self.status_var.set(
                        f"全量迁移 {percent:.1f}% · {copied:,}/{total:,} 文档"
                        f" · {completed}/{len(tasks)} 集合完成"
                    )
            except (sqlite3.Error, KeyError):
                # The engine may close its SQLite connection between this
                # timer tick and the GUI completion message.
                pass
        self.after(500, self._poll_runtime_metrics)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _export_log(self) -> None:
        filename = filedialog.asksaveasfilename(
            parent=self,
            title="导出日志",
            defaultextension=".log",
            filetypes=[("日志", "*.log"), ("所有文件", "*")],
        )
        if filename:
            Path(filename).write_text(
                self.log_text.get("1.0", "end-1c"), encoding="utf-8"
            )

    def _export_diagnostics(self) -> None:
        job_id = self.vars.get("job_id", tk.StringVar()).get().strip()
        if not job_id:
            messagebox.showwarning(
                "没有任务", "请先运行或填写一个任务 ID。", parent=self
            )
            return
        filename = filedialog.asksaveasfilename(
            parent=self,
            title="导出脱敏诊断包",
            initialfile=f"mongodb-migrate-{job_id[:8]}-diagnostics.zip",
            defaultextension=".zip",
            filetypes=[("ZIP 诊断包", "*.zip")],
        )
        if not filename:
            return
        try:
            path = create_diagnostic_bundle(
                self.vars["state_db"].get(),
                job_id,
                filename,
                config_path=self.settings_path,
            )
            self._append_log(f"INFO  已导出脱敏诊断包：{path}")
            messagebox.showinfo(
                "诊断包已导出",
                "已排除连接凭据、Query、源文档、DLQ 与 SQLite 原库。",
                parent=self,
            )
        except Exception as exc:  # noqa: BLE001 - GUI boundary
            messagebox.showerror("导出失败", str(exc), parent=self)

    @property
    def app_data_dir(self) -> Path:
        return default_app_data_dir()

    @property
    def settings_path(self) -> Path:
        return self.app_data_dir / "settings.json"

    def _load_settings(self) -> None:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            for name, value in data.items():
                if name in self.vars and name not in {"source_uri", "target_uri"}:
                    self.vars[name].set(value)
        except (OSError, ValueError):
            return

    def _save_settings(self) -> None:
        data = {
            name: variable.get()
            for name, variable in self.vars.items()
            if name not in {"source_uri", "target_uri", "approval_token"}
        }
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logging.getLogger("mongodb_migrate").warning("cannot save GUI settings")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "任务仍在运行", "先安全停止任务并保持窗口打开？", parent=self
            ):
                return
            if self.engine:
                self.engine.cancel()
            else:
                self.archive_cancel.set()
            return
        self._save_settings()
        self.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = MongoMigrateApp()
    logger = logging.getLogger("mongodb_migrate")
    logger.info(
        "GUI initialized: version=%s pages=%s smoke_test=%s argv=%r",
        PRODUCT_VERSION,
        app.notebook.index("end"),
        app.smoke_test,
        sys.argv,
    )
    if "--smoke-test" in sys.argv:
        app.update_idletasks()
        app.update()
        error = app.callback_error
        pages = app.notebook.index("end")
        app.destroy()
        if error:
            raise RuntimeError(f"GUI callback smoke test failed:\n{error}")
        if pages != 4:
            raise RuntimeError(f"GUI smoke test expected 4 pages, got {pages}")
        logger.info("GUI smoke test passed")
        return
    logger.info("GUI mainloop entering")
    try:
        app.mainloop()
    except Exception:
        logger.exception("GUI mainloop failed")
        raise
    logger.info("GUI mainloop exited normally")


if __name__ == "__main__":
    main()
