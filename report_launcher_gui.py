from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml
from app_paths import prepare_runtime_config, resolve_app_paths
from auth_repair import classify_auth_failure
from browser_auth_refresh import BrowserRefreshSettings, refresh_extra_auth_from_browser
from autodatareport.atomic_io import atomic_write_text
from autodatareport.gui_runtime import GuiEvent, parse_event_line
from autodatareport.models import PublishResolution
from autodatareport.gui_task_controller import GuiTaskController
from autodatareport.process_runner import TaskProcessRunner
from fenxi_auth_from_har import refresh_fenxi_auth_from_hars
from pc_auth_from_har import refresh_pc_auth_from_hars
from readiness import ReadinessState, classify_failure, validate_configuration
from publish_state import PublishStateStore


PROGRESS_RE = re.compile(r"\[PROGRESS\]\s*(\d{1,3})\|(.+)")
FEISHU_MAIN_URL_RE = re.compile(r"Feishu doc published:\s*(https?://\S+)")
FEISHU_PC_URL_RE = re.compile(r"Feishu PC doc published:\s*(https?://\S+)")


class ReportLauncherApp:
    LAUNCHD_LABEL = "com.starfish.autodatareport.daily"
    APP_VERSION = "1.5"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"云游戏日报 · 一键工作台 V{self.APP_VERSION}")
        self.root.geometry("1120x720")
        self.root.minsize(980, 660)

        self.bundle_root = self._resolve_bundle_root()
        self.app_paths = resolve_app_paths()
        self.config_migration = prepare_runtime_config(self.app_paths)
        self.project_root = self.app_paths.data
        self.config_path = self.app_paths.config
        self.script_path = self.bundle_root / "generate_daily_report.py"
        self.cli_exe_path = self.bundle_root / "autodatareport-cli.exe"
        self.browser_auth_script = self.bundle_root / "browser_auth_refresh.py"
        self.fenxi_auth_script = self.bundle_root / "fenxi_auth_from_har.py"
        self.pc_auth_script = self.bundle_root / "pc_auth_from_har.py"
        self.playwright_recover_script = self.bundle_root / "auth_recovery_playwright.py"
        self.python_bin = self._resolve_python_bin()

        self.process_runner = TaskProcessRunner()
        self.task_controller = GuiTaskController(self.process_runner)
        self.worker_thread: Optional[threading.Thread] = None
        self.aux_running = False
        self.queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.date_mode = tk.StringVar(value="yesterday")
        self.date_value = tk.StringVar(value=(date.today() - timedelta(days=1)).isoformat())
        self.with_extra = tk.BooleanVar(value=True)
        self.verify_feishu = tk.BooleanVar(value=False)
        self.disable_feishu = tk.BooleanVar(value=False)
        self.auto_auth_recover = tk.BooleanVar(value=True)

        self.status_text = tk.StringVar(value="准备就绪")
        self.progress_value = tk.IntVar(value=0)
        self.progress_pct_text = tk.StringVar(value="0%")
        self.run_button_text = tk.StringVar(value="生成并发送昨天日报")
        self.result_text = tk.StringVar(value="任务完成后，可在这里直接打开日报和输出文件。")
        self.details_button_text = tk.StringVar(value="展开详细日志")
        self.feishu_url_main = tk.StringVar(value="")
        self.feishu_url_pc = tk.StringVar(value="")
        self.log_history: list[str] = []
        self.last_failure_kind = ""
        self.last_failure_source = ""
        self.details_visible = False
        self.has_auto_retried = False
        self.has_870_cookie_retried = False

        self.schedule_hour = tk.StringVar(value="09")
        self.schedule_minute = tk.StringVar(value="00")
        self.schedule_status = tk.StringVar(value="定时任务：未检测")
        self.tools_menubutton: Optional[ttk.Menubutton] = None
        self.schedule_menu: Optional[tk.Menu] = None
        self.document_menu: Optional[tk.Menu] = None
        self.schedule_time_menu_index: Optional[int] = None
        self.schedule_status_menu_index: Optional[int] = None
        self.feishu_main_menu_index: Optional[int] = None
        self.feishu_pc_menu_index: Optional[int] = None
        self.option_summary_text = tk.StringVar(value="")
        self.source_status_text = tk.StringVar(value="平台状态：正在检查配置…")
        self.status_badge: Optional[ttk.Label] = None
        self.log_card: Optional[ttk.Frame] = None
        self.btn_main_doc: Optional[ttk.Button] = None
        self.btn_pc_doc: Optional[ttk.Button] = None

        self._configure_style()
        for option_var in (self.with_extra, self.verify_feishu, self.disable_feishu, self.auto_auth_recover):
            option_var.trace_add("write", lambda *_args: self._update_option_summary())
        self.date_value.trace_add("write", lambda *_args: self._update_option_summary())
        self._build_ui()
        self._update_option_summary()
        self._refresh_source_status()
        self._refresh_schedule_status()
        self.root.bind("<Control-Return>", lambda _event: self.start_run())
        self.root.bind("<Escape>", lambda _event: self.stop_run())
        self.root.after(120, self._drain_queue)

    def _resolve_bundle_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _resolve_python_bin(self) -> str:
        if getattr(sys, "frozen", False):
            return ""
        for venv_bin in (
            self.bundle_root / ".venv" / "Scripts" / "python.exe",
            self.bundle_root / ".venv" / "bin" / "python",
        ):
            if venv_bin.exists():
                return str(venv_bin)
        return "python3"

    def _resolve_runtime_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return self.bundle_root
        return self.bundle_root

    def _resolve_runtime_file(self, filename: str) -> Path:
        runtime_path = self.project_root / filename
        if runtime_path.exists():
            return runtime_path
        bundle_path = self.bundle_root / filename
        if bundle_path.exists():
            return bundle_path
        return runtime_path

    def _extra_auth_path(self) -> Path:
        return self.app_paths.extra_auth

    def _build_cli_command(self, *args: str) -> list[str]:
        if getattr(sys, "frozen", False):
            if not self.cli_exe_path.exists():
                raise FileNotFoundError(f"未找到打包后的 CLI：{self.cli_exe_path}")
            base = [str(self.cli_exe_path)]
        else:
            base = [self.python_bin, str(self.script_path)]
        return [*base, "--data-dir", str(self.project_root), *args]

    def _append_auth_repair_args(self, cmd: list[str], *, target: str = "auto") -> None:
        if not self.auto_auth_recover.get():
            return
        cmd.extend(
            [
                "--repair-auth-on-failure",
                "--auth-repair-browser",
                "chrome",
                "--auth-repair-profile",
                str(self.project_root / "output" / "auth_profiles" / "chrome_daily_report"),
                "--auth-repair-timeout-seconds",
                "300",
                "--auth-repair-target",
                target,
            ]
        )

    def _configure_style(self) -> None:
        self.font_family = "Microsoft YaHei UI" if os.name == "nt" else "PingFang SC"
        self.mono_font = "Consolas" if os.name == "nt" else "Menlo"
        self.root.configure(bg="#F3F6FA")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Root.TFrame", background="#F3F6FA")
        style.configure("Header.TFrame", background="#0F172A")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("Hero.TFrame", background="#FFFFFF")
        style.configure("Subtle.TFrame", background="#F8FAFC")
        style.configure("CardTitle.TLabel", background="#FFFFFF", foreground="#0F172A", font=(self.font_family, 11, "bold"))
        style.configure("SectionTitle.TLabel", background="#FFFFFF", foreground="#0F172A", font=(self.font_family, 13, "bold"))
        style.configure("Muted.TLabel", background="#FFFFFF", foreground="#64748B", font=(self.font_family, 9))
        style.configure("Body.TLabel", background="#FFFFFF", foreground="#334155", font=(self.font_family, 10))
        style.configure("Strong.TLabel", background="#FFFFFF", foreground="#0F172A", font=(self.font_family, 11, "bold"))
        style.configure("HeaderEyebrow.TLabel", background="#0F172A", foreground="#60A5FA", font=(self.font_family, 9, "bold"))
        style.configure("HeaderTitle.TLabel", background="#0F172A", foreground="#F8FAFC", font=(self.font_family, 20, "bold"))
        style.configure("HeaderSub.TLabel", background="#0F172A", foreground="#CBD5E1", font=(self.font_family, 10))
        style.configure("HeaderMeta.TLabel", background="#0F172A", foreground="#94A3B8", font=(self.font_family, 8))
        style.configure("StatusPill.TLabel", background="#1E293B", foreground="#E2E8F0", font=(self.font_family, 9, "bold"), padding=(12, 6))
        style.configure("Running.StatusPill.TLabel", background="#1D4ED8", foreground="#EFF6FF", font=(self.font_family, 9, "bold"), padding=(12, 6))
        style.configure("Success.StatusPill.TLabel", background="#047857", foreground="#ECFDF5", font=(self.font_family, 9, "bold"), padding=(12, 6))
        style.configure("Error.StatusPill.TLabel", background="#B45309", foreground="#FFF7ED", font=(self.font_family, 9, "bold"), padding=(12, 6))
        style.configure("OptionSummary.TLabel", background="#EFF6FF", foreground="#1D4ED8", font=(self.font_family, 9, "bold"), padding=(10, 5))
        style.configure("ResultSummary.TLabel", background="#F8FAFC", foreground="#334155", font=(self.font_family, 10), padding=(12, 9))
        style.configure("Date.TRadiobutton", background="#FFFFFF", foreground="#334155", font=(self.font_family, 10), padding=(6, 3))
        style.map("Date.TRadiobutton", background=[("active", "#FFFFFF")], foreground=[("selected", "#1D4ED8")])

        style.configure("Hero.TButton", font=(self.font_family, 13, "bold"), padding=(26, 16), borderwidth=0)
        style.map(
            "Hero.TButton",
            background=[("disabled", "#94A3B8"), ("pressed", "#1E40AF"), ("active", "#3B82F6"), ("!disabled", "#2563EB")],
            foreground=[("disabled", "#E2E8F0"), ("!disabled", "#FFFFFF")],
        )

        style.configure("Primary.TButton", font=(self.font_family, 10, "bold"), padding=(14, 9), borderwidth=0)
        style.map(
            "Primary.TButton",
            background=[("disabled", "#CBD5E1"), ("pressed", "#1E40AF"), ("active", "#3B82F6"), ("!disabled", "#2563EB")],
            foreground=[("disabled", "#64748B"), ("!disabled", "#FFFFFF")],
        )

        style.configure("Warn.TButton", font=(self.font_family, 10), padding=(12, 9), borderwidth=0)
        style.map(
            "Warn.TButton",
            background=[("disabled", "#F1F5F9"), ("pressed", "#FED7AA"), ("active", "#FFEDD5"), ("!disabled", "#FFF7ED")],
            foreground=[("disabled", "#94A3B8"), ("!disabled", "#C2410C")],
        )

        style.configure("Ghost.TButton", font=(self.font_family, 9), padding=(11, 8), borderwidth=1)
        style.map(
            "Ghost.TButton",
            background=[("disabled", "#F8FAFC"), ("pressed", "#E2E8F0"), ("active", "#F1F5F9"), ("!disabled", "#FFFFFF")],
            foreground=[("disabled", "#94A3B8"), ("!disabled", "#334155")],
        )
        style.configure("Header.TMenubutton", font=(self.font_family, 9, "bold"), padding=(11, 7), borderwidth=0)
        style.map(
            "Header.TMenubutton",
            background=[("pressed", "#334155"), ("active", "#334155"), ("!disabled", "#1E293B")],
            foreground=[("!disabled", "#E2E8F0")],
        )

        style.configure("Run.Horizontal.TProgressbar", troughcolor="#E2E8F0", background="#2563EB", bordercolor="#E2E8F0", lightcolor="#2563EB", darkcolor="#2563EB", thickness=9)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, style="Root.TFrame", padding=18)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container, style="Header.TFrame", padding=(20, 16))
        header.pack(fill=tk.X)
        self._build_header(header)

        run_card = ttk.Frame(container, style="Hero.TFrame", padding=(20, 18))
        run_card.pack(fill=tk.X, pady=(12, 0))
        self._build_run_controls(run_card)

        progress_card = ttk.Frame(container, style="Card.TFrame", padding=(18, 14))
        progress_card.pack(fill=tk.X, pady=(10, 0))
        self._build_progress(progress_card)

        result_card = ttk.Frame(container, style="Card.TFrame", padding=(18, 14))
        result_card.pack(fill=tk.X, pady=(10, 0))
        self._build_results(result_card)

        details_bar = ttk.Frame(container, style="Root.TFrame")
        details_bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(
            details_bar,
            text="详细日志只在排查问题时需要",
            background="#F3F6FA",
            foreground="#64748B",
            font=(self.font_family, 9),
        ).pack(side=tk.LEFT)
        ttk.Button(
            details_bar,
            textvariable=self.details_button_text,
            style="Ghost.TButton",
            command=self._toggle_logs,
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        self.log_card = ttk.Frame(container, style="Card.TFrame", padding=12)
        self._build_logs(self.log_card)

    def _build_header(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)

        title_area = ttk.Frame(parent, style="Header.TFrame")
        title_area.grid(row=0, column=0, sticky="w")
        ttk.Label(title_area, text=f"AUTODATAREPORT · V{self.APP_VERSION}", style="HeaderEyebrow.TLabel").pack(anchor="w")
        ttk.Label(title_area, text="云游戏日报 · 一键工作台", style="HeaderTitle.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Label(
            title_area,
            text="默认生成并发送昨天日报；登录失效时会直接告诉你下一步。",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            title_area,
            text=f"运行数据：{self.project_root}",
            style="HeaderMeta.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        command_area = ttk.Frame(parent, style="Header.TFrame")
        command_area.grid(row=0, column=1, sticky="e")
        self.tools_menubutton = ttk.Menubutton(
            command_area,
            text="设置与修复  ▾",
            style="Header.TMenubutton",
            cursor="hand2",
        )
        self.tools_menubutton.pack(side=tk.LEFT, padx=(0, 10))
        self._build_tools_menu()
        self.status_badge = ttk.Label(command_area, textvariable=self.status_text, style="StatusPill.TLabel")
        self.status_badge.pack(side=tk.LEFT)

    def _build_run_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)

        setup_area = ttk.Frame(parent, style="Hero.TFrame")
        setup_area.grid(row=0, column=0, sticky="nsew", padx=(0, 24))
        ttk.Label(setup_area, text="今天要做什么？", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            setup_area,
            text="默认已经选好昨天。直接点右侧按钮即可生成并发送。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        date_row = ttk.Frame(setup_area, style="Hero.TFrame")
        date_row.pack(anchor="w", pady=(14, 0))
        ttk.Label(date_row, text="日报日期", style="Strong.TLabel").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            date_row,
            text="昨天",
            value="yesterday",
            variable=self.date_mode,
            command=self._update_date_from_mode,
            style="Date.TRadiobutton",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            date_row,
            text="今天",
            value="today",
            variable=self.date_mode,
            command=self._update_date_from_mode,
            style="Date.TRadiobutton",
        ).pack(side=tk.LEFT, padx=(4, 0))
        self.date_entry = ttk.Entry(date_row, textvariable=self.date_value, width=13, font=(self.font_family, 10))
        self.date_entry.pack(side=tk.LEFT, padx=(10, 0), ipady=3)
        self.date_entry.bind("<KeyRelease>", lambda _event: self.date_mode.set("custom"))

        summary_row = ttk.Frame(setup_area, style="Hero.TFrame")
        summary_row.pack(anchor="w", pady=(14, 0))
        ttk.Label(summary_row, textvariable=self.option_summary_text, style="OptionSummary.TLabel").pack(side=tk.LEFT)
        ttk.Label(
            summary_row,
            text="可在右上角“设置与修复”里调整",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(setup_area, textvariable=self.source_status_text, style="Muted.TLabel").pack(anchor="w", pady=(9, 0))

        action_area = ttk.Frame(parent, style="Hero.TFrame")
        action_area.grid(row=0, column=1, sticky="e")
        self.btn_start = ttk.Button(
            action_area,
            textvariable=self.run_button_text,
            style="Hero.TButton",
            command=self.start_run,
            cursor="hand2",
        )
        self.btn_start.pack(fill=tk.X)
        self.btn_stop = ttk.Button(
            action_area,
            text="停止当前任务",
            style="Warn.TButton",
            command=self.stop_run,
            state=tk.DISABLED,
            cursor="hand2",
        )
        self.btn_stop.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(
            action_area,
            text="Ctrl + Enter 可直接开始",
            style="Muted.TLabel",
        ).pack(anchor="center", pady=(7, 0))

    def _build_tools_menu(self) -> None:
        if self.tools_menubutton is None:
            return
        tools_menu = tk.Menu(self.tools_menubutton, tearoff=False)
        self.tools_menubutton.configure(menu=tools_menu)

        tools_menu.add_command(label="首次设置（推荐）", command=self.start_first_time_setup)
        tools_menu.add_separator()

        auth_menu = tk.Menu(tools_menu, tearoff=False)
        auth_menu.add_command(label="打开 870 登录页", command=self.open_870_login)
        auth_menu.add_command(label="更新 870 登录态", command=self.open_870_cookie_repair_dialog)
        auth_menu.add_separator()
        auth_menu.add_command(label="自动刷新 PC 登录态", command=self.refresh_pc_auth)
        auth_menu.add_command(label="从 HAR 更新 PC 登录态", command=self.import_pc_har)
        auth_menu.add_command(label="从 HAR 更新 Fenxi 登录态", command=self.import_fenxi_har)
        tools_menu.add_cascade(label="登录态修复", menu=auth_menu)

        run_menu = tk.Menu(tools_menu, tearoff=False)
        run_menu.add_checkbutton(label="抓取完整数据（分析后台 + 505）", variable=self.with_extra)
        run_menu.add_checkbutton(label="发送后校验飞书内容", variable=self.verify_feishu)
        run_menu.add_checkbutton(label="本次仅生成，不发送", variable=self.disable_feishu)
        run_menu.add_checkbutton(label="登录失效时自动修复并重试", variable=self.auto_auth_recover)
        tools_menu.add_cascade(label="本次运行选项", menu=run_menu)

        if self._ensure_macos_silent():
            self.schedule_menu = tk.Menu(tools_menu, tearoff=False)
            self.schedule_menu.add_command(label="", command=self.edit_schedule_time)
            self.schedule_time_menu_index = 0
            self.schedule_menu.add_command(label="安装/更新定时任务", command=self.install_schedule)
            self.schedule_menu.add_command(label="立即触发定时任务", command=self.trigger_schedule_now)
            self.schedule_menu.add_command(label="取消定时任务", command=self.disable_schedule)
            self.schedule_menu.add_command(label="刷新状态", command=self._refresh_schedule_status)
            self.schedule_menu.add_separator()
            self.schedule_menu.add_command(label=self.schedule_status.get(), state=tk.DISABLED)
            self.schedule_status_menu_index = 6
            tools_menu.add_cascade(label="定时任务", menu=self.schedule_menu)

        self.document_menu = tk.Menu(tools_menu, tearoff=False)
        self.document_menu.add_command(label="打开主日报飞书文档", command=self.open_main_feishu, state=tk.DISABLED)
        self.feishu_main_menu_index = 0
        self.document_menu.add_command(label="打开 PC 飞书文档", command=self.open_pc_feishu, state=tk.DISABLED)
        self.feishu_pc_menu_index = 1
        self.document_menu.add_separator()
        self.document_menu.add_command(label="打开输出文件夹", command=self.open_output_dir)
        self.document_menu.add_command(label="打开定时日志文件夹", command=self.open_log_dir)
        tools_menu.add_cascade(label="结果与文件", menu=self.document_menu)

        push_menu = tk.Menu(tools_menu, tearoff=False)
        push_menu.add_command(label="重新发送主日报到飞书", command=self.repush_main_feishu)
        push_menu.add_command(label="重新发送 PC 日报到飞书", command=self.repush_pc_feishu)
        push_menu.add_command(label="发送到企业微信个人", command=self.push_wecom_single)
        push_menu.add_command(label="发送到企业微信群", command=self.push_wecom_group)
        tools_menu.add_cascade(label="手动补发", menu=push_menu)
        self._update_schedule_menu_labels()

    def _build_progress(self, parent: ttk.Frame) -> None:
        title_row = ttk.Frame(parent, style="Card.TFrame")
        title_row.pack(fill=tk.X)
        ttk.Label(title_row, text="执行进度", style="CardTitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(title_row, textvariable=self.progress_pct_text, style="Strong.TLabel").pack(side=tk.RIGHT)

        ttk.Label(parent, textvariable=self.status_text, style="Body.TLabel").pack(anchor="w", pady=(7, 0))

        self.progress = ttk.Progressbar(
            parent,
            orient=tk.HORIZONTAL,
            mode="determinate",
            variable=self.progress_value,
            maximum=100,
            style="Run.Horizontal.TProgressbar",
        )
        self.progress.pack(fill=tk.X, pady=(9, 0))

    def _build_results(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Card.TFrame")
        header.pack(fill=tk.X)
        ttk.Label(header, text="完成后", style="CardTitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, text="不用再去文件夹里找", style="Muted.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(parent, textvariable=self.result_text, style="ResultSummary.TLabel").pack(fill=tk.X, pady=(9, 0))

        actions = ttk.Frame(parent, style="Card.TFrame")
        actions.pack(fill=tk.X, pady=(9, 0))
        self.btn_main_doc = ttk.Button(
            actions,
            text="打开主日报",
            style="Primary.TButton",
            command=self.open_main_feishu,
            state=tk.DISABLED,
            cursor="hand2",
        )
        self.btn_main_doc.pack(side=tk.LEFT)
        self.btn_pc_doc = ttk.Button(
            actions,
            text="打开 PC 日报",
            style="Ghost.TButton",
            command=self.open_pc_feishu,
            state=tk.DISABLED,
            cursor="hand2",
        )
        self.btn_pc_doc.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="打开输出文件夹",
            style="Ghost.TButton",
            command=self.open_output_dir,
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="登录失效？更新 870 登录态",
            style="Ghost.TButton",
            command=self.open_870_cookie_repair_dialog,
            cursor="hand2",
        ).pack(side=tk.RIGHT)

    def _build_logs(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="详细运行日志", style="CardTitle.TLabel").pack(anchor="w")
        wrapper = ttk.Frame(parent, style="Card.TFrame")
        wrapper.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.log_text = tk.Text(
            wrapper,
            wrap="word",
            height=13,
            bg="#0F172A",
            fg="#CBD5E1",
            insertbackground="#CBD5E1",
            selectbackground="#1D4ED8",
            relief="flat",
            font=(self.mono_font, 10),
            padx=12,
            pady=10,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _toggle_logs(self, force: Optional[bool] = None) -> None:
        if self.log_card is None:
            return
        should_show = (not self.details_visible) if force is None else bool(force)
        if should_show == self.details_visible:
            return
        self.details_visible = should_show
        if should_show:
            self.log_card.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
            self.details_button_text.set("收起详细日志")
            target_height = min(900, self.root.winfo_screenheight() - 80)
            if self.root.winfo_height() < target_height:
                self.root.geometry(f"{max(self.root.winfo_width(), 980)}x{target_height}")
        else:
            self.log_card.pack_forget()
            self.details_button_text.set("展开详细日志")
            if self.root.winfo_height() > 720:
                self.root.geometry(f"{max(self.root.winfo_width(), 980)}x720")

    def _update_date_from_mode(self) -> None:
        if self.date_mode.get() == "yesterday":
            target = date.today() - timedelta(days=1)
        else:
            target = date.today()
        self.date_value.set(target.isoformat())
        self._update_option_summary()

    def _append_log(self, line: str) -> None:
        self.log_history.append(str(line))
        if len(self.log_history) > 600:
            self.log_history = self.log_history[-600:]
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line)
        if not line.endswith("\n"):
            self.log_text.insert(tk.END, "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _load_scheduler_env(self) -> Dict[str, str]:
        env_file = self.project_root / ".env.scheduler"
        env: Dict[str, str] = {}
        if not env_file.exists():
            return env
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'").strip('"')
        return env

    def _build_command(self) -> list[str]:
        extra_auth_file = self._extra_auth_path()
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--extra-auth-file",
            str(extra_auth_file),
            "--event-stream",
            "jsonl",
            "--max-total-concurrency",
            "8",
        )
        if self.with_extra.get():
            cmd.append("--with-extra-metrics")
        if self.verify_feishu.get():
            cmd.append("--verify-feishu-content")
        if self.disable_feishu.get():
            cmd.append("--no-publish")
        self._append_auth_repair_args(cmd, target="auto")
        return cmd

    def _update_option_summary(self) -> None:
        parts = ["完整数据" if self.with_extra.get() else "基础数据"]
        parts.append("仅生成，不发送" if self.disable_feishu.get() else "自动发送飞书 / 企微")
        if self.verify_feishu.get():
            parts.append("发送后校验")
        if self.auto_auth_recover.get():
            parts.append("登录失效自动修复")
        self.option_summary_text.set(" · ".join(parts))

        value = self.date_value.get().strip()
        if value == date.today().isoformat():
            date_label = "今天"
        elif value == (date.today() - timedelta(days=1)).isoformat():
            date_label = "昨天"
        else:
            date_label = value or "所选日期"
        action = "仅生成" if self.disable_feishu.get() else "生成并发送"
        self.run_button_text.set(f"{action}{date_label}日报")

    def _refresh_source_status(self) -> None:
        states = validate_configuration(self._load_config())
        labels = []
        for item in states:
            symbol = "—" if item.state == ReadinessState.DISABLED else ("!" if item.state == ReadinessState.CONFIG_MISSING else "○")
            labels.append(f"{item.source} {symbol}")
        missing = any(item.state == ReadinessState.CONFIG_MISSING for item in states)
        suffix = "（请先点首次设置）" if missing else "（运行时自动验证）"
        self.source_status_text.set("平台状态：" + "  ".join(labels) + suffix)

    def _set_feishu_menu_state(self) -> None:
        if self.document_menu is None:
            return
        if self.feishu_main_menu_index is not None:
            state = tk.NORMAL if self.feishu_url_main.get().strip() else tk.DISABLED
            self.document_menu.entryconfig(self.feishu_main_menu_index, state=state)
            if self.btn_main_doc is not None:
                self.btn_main_doc.configure(state=state)
        if self.feishu_pc_menu_index is not None:
            state = tk.NORMAL if self.feishu_url_pc.get().strip() else tk.DISABLED
            self.document_menu.entryconfig(self.feishu_pc_menu_index, state=state)
            if self.btn_pc_doc is not None:
                self.btn_pc_doc.configure(state=state)

    def _update_schedule_menu_labels(self) -> None:
        if self.schedule_menu is None:
            return
        if self.schedule_time_menu_index is not None:
            self.schedule_menu.entryconfig(
                self.schedule_time_menu_index,
                label=f"◷ 设置执行时间（当前 {self.schedule_hour.get().strip()}:{self.schedule_minute.get().strip()}）",
            )
        if self.schedule_status_menu_index is not None:
            self.schedule_menu.entryconfig(self.schedule_status_menu_index, label=self.schedule_status.get())

    def _set_schedule_status(self, status: str) -> None:
        self.schedule_status.set(status)
        self._update_schedule_menu_labels()

    def _set_visual_state(self, state: str) -> None:
        if self.status_badge is None:
            return
        styles = {
            "idle": "StatusPill.TLabel",
            "running": "Running.StatusPill.TLabel",
            "success": "Success.StatusPill.TLabel",
            "error": "Error.StatusPill.TLabel",
        }
        self.status_badge.configure(style=styles.get(state, "StatusPill.TLabel"))

    def _set_progress(self, value: int, status: Optional[str] = None) -> None:
        pct = max(0, min(100, int(value)))
        self.progress_value.set(pct)
        self.progress_pct_text.set(f"{pct}%")
        if status is not None:
            self.status_text.set(status)

    def start_run(self, from_auto_retry: bool = False) -> None:
        if self._is_busy():
            if not from_auto_retry:
                messagebox.showinfo("任务正在执行", "请等待当前任务结束后再开始。")
            return
        if not from_auto_retry:
            self.has_auto_retried = False
        self.log_history = []
        self.last_failure_kind = ""
        self.last_failure_source = ""
        run_date = self.date_value.get().strip()
        if not from_auto_retry:
            self.has_870_cookie_retried = False
        try:
            datetime.strptime(run_date, "%Y-%m-%d")
        except ValueError:
            messagebox.showerror("日期格式不正确", "请按 YYYY-MM-DD 填写，例如 2026-07-16。")
            self.date_entry.focus_set()
            return
        if not self.config_path.exists():
            messagebox.showerror("缺少配置文件", f"未找到配置文件：\n{self.config_path}")
            return

        cmd = self._build_command()
        env = os.environ.copy()
        env.update(self._load_scheduler_env())

        self._set_progress(0, "正在准备任务")
        self._set_visual_state("running")
        self.result_text.set("任务进行中。完成后，日报链接和本地文件会出现在这里。")
        self.feishu_url_main.set("")
        self.feishu_url_pc.set("")
        self._set_feishu_menu_state()
        self._append_log("=" * 84)
        self._append_log("Start command: " + " ".join(cmd))

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)

        def handle_line(task_id: int, line: str) -> None:
            event = parse_event_line(line)
            if event is not None:
                self.queue.put(("task_event", (task_id, event)))
                return
            self.queue.put(("task_line", (task_id, line)))
            progress_match = PROGRESS_RE.search(line)
            if progress_match:
                self.queue.put(("task_progress", (task_id, int(progress_match.group(1)), progress_match.group(2).strip())))

        try:
            self.task_controller.start(
                cmd,
                cwd=self.project_root,
                env=env,
                kind="report",
                label="日报任务",
                on_line=handle_line,
                on_done=lambda task_id, rc: self.queue.put(("done", (task_id, rc))),
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[GUI ERROR] {exc}")
            self.btn_start.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            self._set_visual_state("error")
            messagebox.showerror("启动失败", str(exc))

    def stop_run(self) -> None:
        active = self.task_controller.active
        if active is None:
            return
        if active.publishing and not messagebox.askyesno(
            "发送阶段仍在进行",
            "停止后可能需要确认飞书或企微是否已经发送。仍要停止吗？",
        ):
            return
        try:
            self.task_controller.stop()
        except Exception:  # noqa: BLE001
            pass
        self.status_text.set("已请求停止")
        self._append_log("[GUI] 已请求停止任务")

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, value = self.queue.get_nowait()
            except queue.Empty:
                break

            if kind == "task_line":
                task_id, text = value  # type: ignore[misc]
                if not self.task_controller.is_current(int(task_id)):
                    continue
                self._append_log(str(text))
            elif kind == "task_event":
                task_id, event = value  # type: ignore[misc]
                if not self.task_controller.is_current(int(task_id)) or not isinstance(event, GuiEvent):
                    continue
                self.task_controller.set_stage(int(task_id), event.stage)
                if event.kind == "run_finished" and event.failure_kind:
                    self.last_failure_kind = event.failure_kind
                    self.last_failure_source = event.failure_source
                self._append_log("[EVENT] " + event.log_line())
                if event.progress is not None:
                    self._set_progress(event.progress, event.message or None)
                elif event.message and event.kind in {"stage_started", "artifact", "publish_finished"}:
                    self.status_text.set(event.message)
                if event.url and event.target == "pc":
                    self.feishu_url_pc.set(event.url)
                    self._set_feishu_menu_state()
                elif event.url and event.target == "main":
                    self.feishu_url_main.set(event.url)
                    self._set_feishu_menu_state()
            elif kind == "task_progress":
                task_id, pct, message = value  # type: ignore[misc]
                if not self.task_controller.is_current(int(task_id)):
                    continue
                self._set_progress(int(pct), str(message))
            elif kind == "line":
                text = str(value)
                self._append_log(text)
                pc_url_match = FEISHU_PC_URL_RE.search(text)
                if pc_url_match:
                    self.feishu_url_pc.set(pc_url_match.group(1).strip())
                    self._set_feishu_menu_state()
                else:
                    main_url_match = FEISHU_MAIN_URL_RE.search(text)
                    if main_url_match:
                        self.feishu_url_main.set(main_url_match.group(1).strip())
                        self._set_feishu_menu_state()
            elif kind == "event":
                event = value
                if not isinstance(event, GuiEvent):
                    continue
                self._append_log("[EVENT] " + event.log_line())
                if event.progress is not None:
                    self._set_progress(event.progress, event.message or None)
                elif event.message and event.kind in {"stage_started", "artifact", "publish_finished"}:
                    self.status_text.set(event.message)
                if event.url and event.target == "pc":
                    self.feishu_url_pc.set(event.url)
                    self._set_feishu_menu_state()
                elif event.url and event.target == "main":
                    self.feishu_url_main.set(event.url)
                    self._set_feishu_menu_state()
            elif kind == "progress":
                self._set_progress(int(value))
            elif kind == "status":
                self.status_text.set(str(value))
            elif kind == "feishu_main":
                self.feishu_url_main.set(str(value))
                self._set_feishu_menu_state()
            elif kind == "feishu_pc":
                self.feishu_url_pc.set(str(value))
                self._set_feishu_menu_state()
            elif kind == "done":
                task_id, rc = value  # type: ignore[misc]
                if not self.task_controller.finish(int(task_id)):
                    continue
                rc = int(rc)
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                if rc == 0:
                    self._set_progress(100, "任务完成")
                    self._set_visual_state("success")
                    delivered = []
                    if self.feishu_url_main.get().strip():
                        delivered.append("主日报已发送")
                    if self.feishu_url_pc.get().strip():
                        delivered.append("PC 日报已发送")
                    if delivered:
                        self.result_text.set("任务完成：" + "，".join(delivered) + "。可直接打开查看。")
                    else:
                        self.result_text.set("任务完成：日报文件已生成，可在输出文件夹中查看。")
                    self._append_log("[GUI] 任务完成")
                else:
                    if self._handle_uncertain_publish():
                        continue
                    if (not self.has_870_cookie_retried) and self._looks_like_870_cookie_failure():
                        if self.prompt_870_cookie_repair(retry_after_save=True):
                            continue
                    if (
                        self.auto_auth_recover.get()
                        and (not self.has_auto_retried)
                        and self._looks_like_auth_failure()
                    ):
                        should_recover = messagebox.askyesno(
                            "检测到登录态问题",
                            "任务失败且日志命中登录态失效特征。\n是否自动执行登录修复并重试一次？",
                        )
                        if should_recover:
                            self.has_auto_retried = True
                            self.start_auth_recovery_and_retry()
                            continue
                    self.status_text.set("任务失败")
                    self._set_visual_state("error")
                    self.result_text.set("任务没有完成。请展开详细日志查看原因；若提示登录失效，可直接更新 870 登录态。")
                    self._toggle_logs(force=True)
                    self._append_log(f"[GUI] 任务失败，退出码={rc}")
                    messagebox.showerror("任务未完成", "详细日志已经展开。请按最后一条提示处理后重试。")
            elif kind == "aux_done":
                if isinstance(value, tuple) and len(value) == 3:
                    task_id, rc, label = value
                    if not self.task_controller.finish(int(task_id)):
                        continue
                else:
                    rc, label = value  # type: ignore[misc]
                self.aux_running = False
                self.btn_stop.configure(state=tk.DISABLED)
                if int(rc) == 0:
                    self._append_log(f"[GUI] {label}完成")
                    if label == "首次设置":
                        self.source_status_text.set("平台状态：870 ✓  Fenxi ✓  505 ✓  PC ✓（真实查询已通过）")
                    messagebox.showinfo("完成", f"{label}成功。")
                else:
                    self._append_log(f"[GUI] {label}失败，退出码={rc}")
                    messagebox.showerror("失败", f"{label}失败，请查看日志。")
                self._refresh_schedule_status()
            elif kind == "recover_done":
                task_id, ok, reason = value  # type: ignore[misc]
                if not self.task_controller.finish(int(task_id)):
                    continue
                self.aux_running = False
                self.btn_stop.configure(state=tk.DISABLED)
                if ok:
                    self._append_log("[GUI] 自动登录修复完成，开始重试任务")
                    self.status_text.set("自动修复成功，任务重试中")
                    self._set_visual_state("running")
                    self.start_run(from_auto_retry=True)
                else:
                    self.status_text.set("自动修复失败")
                    self._set_visual_state("error")
                    self._toggle_logs(force=True)
                    self._append_log(f"[GUI] 自动修复失败: {reason}")
                    messagebox.showerror("自动修复失败", str(reason))
                self._refresh_schedule_status()

        self.root.after(120, self._drain_queue)

    def _is_busy(self) -> bool:
        return self.task_controller.busy or self.aux_running

    def _run_aux_command(self, label: str, cmd: list[str]) -> None:
        if self._is_busy():
            messagebox.showinfo("提示", "当前有任务在执行，请稍后再试。")
            return
        env = os.environ.copy()
        env.update(self._load_scheduler_env())
        self.aux_running = True
        self._append_log("=" * 84)
        self._append_log(f"[GUI] {label}")
        self._append_log("执行命令: " + " ".join(cmd))

        try:
            self.task_controller.start(
                cmd,
                cwd=self.project_root,
                env=env,
                kind="aux",
                label=label,
                on_line=lambda task_id, line: self.queue.put(("task_line", (task_id, line))),
                on_done=lambda task_id, rc: self.queue.put(("aux_done", (task_id, rc, label))),
            )
            self.btn_stop.configure(state=tk.NORMAL)
        except Exception as exc:  # noqa: BLE001
            self.aux_running = False
            self._append_log(f"[GUI ERROR] {exc}")
            messagebox.showerror("启动失败", str(exc))

    def _run_aux_callable(self, label: str, worker_fn) -> None:
        if self.aux_running:
            messagebox.showinfo("提示", "当前有辅助任务在执行，请稍后再试。")
            return
        self.aux_running = True
        self._append_log("=" * 84)
        self._append_log(f"[GUI] {label}")

        def _worker() -> None:
            rc = 0
            try:
                result = worker_fn()
                if result is not None:
                    self.queue.put(("line", json.dumps(result, ensure_ascii=False)))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("line", f"[GUI ERROR] {type(exc).__name__}: {exc}"))
                rc = 1
            self.queue.put(("aux_done", (rc, label)))

        threading.Thread(target=_worker, daemon=True).start()

    def _prepare_har_update(self, label: str) -> bool:
        if self.aux_running:
            messagebox.showinfo("提示", "当前有辅助任务在执行，请稍后再试。")
            return False
        if not self.task_controller.busy:
            return True
        should_stop = messagebox.askyesno(
            "当前任务仍在执行",
            f"{label}前需要先停止当前主流程，否则会继续占用登录态与文件。\n是否先停止当前任务，再继续上传 HAR？",
        )
        if not should_stop:
            return False
        self._append_log(f"[GUI] 为执行“{label}”，先停止当前任务")
        try:
            self.task_controller.stop()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("停止失败", f"无法停止当前任务：{exc}")
            return False
        active = self.task_controller.active
        if active is not None:
            self.task_controller.finish(active.task_id)
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_text.set("主流程已停止，可更新登录态")
        self._append_log("[GUI] 当前任务已停止，开始更新登录态")
        return True

    def _recent_failure_text(self) -> str:
        haystack = "\n".join(self.log_history[-220:])
        marker = "Failed to generate report:"
        if marker in haystack:
            return haystack.rsplit(marker, 1)[-1]
        return haystack

    def _handle_uncertain_publish(self) -> bool:
        legacy_match = "发送结果待确认" in self._recent_failure_text() or "已阻止自动重发" in self._recent_failure_text()
        if self.last_failure_kind != "publish_uncertain" and not legacy_match:
            return False
        try:
            report_date = date.fromisoformat(self.date_value.get().strip())
            store = PublishStateStore(self.project_root / "output", report_date)
            entries = store.uncertain_entries()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[GUI] 无法读取待确认发布状态: {exc}")
            return False
        if not entries:
            return False
        urls = [str(entry.result.get("url") or "").strip() for entry in entries]
        urls = [url for url in urls if url]
        if urls:
            self.feishu_url_main.set(urls[0])
            self._set_feishu_menu_state()
        targets = "、".join(entry.target for entry in entries)
        choice = messagebox.askyesnocancel(
            "发送结果待确认",
            f"以下目标可能已经发送：{targets}\n\n"
            "请先到飞书或企微检查。\n"
            "选择“是”：已经看到，标记为完成。\n"
            "选择“否”：确认没有发送，立即重试。\n"
            "选择“取消”：暂不处理，并继续阻止自动重发。",
        )
        if choice is True:
            for entry in entries:
                store.resolve_uncertain(entry.target, PublishResolution.COMPLETED)
            self.status_text.set("待确认发送已标记完成")
            self._set_visual_state("success")
            self._append_log("[GUI] 用户确认远端已发送，状态已标记完成")
        elif choice is False:
            for entry in entries:
                store.resolve_uncertain(entry.target, PublishResolution.RETRY)
            self._append_log("[GUI] 用户确认远端未发送，开始重试")
            self.start_run(from_auto_retry=True)
        else:
            self.status_text.set("发送结果待确认")
            self._set_visual_state("error")
            self._toggle_logs(force=True)
        return True

    def _looks_like_870_cookie_failure(self) -> bool:
        if self.last_failure_kind == "login_required" and self.last_failure_source == "870":
            return True
        haystack = self._recent_failure_text()
        return classify_failure(haystack) == ReadinessState.LOGIN_REQUIRED

    def _looks_like_auth_failure(self) -> bool:
        if self.last_failure_kind == "login_required":
            return True
        haystack = self._recent_failure_text()
        lowered = haystack.lower()
        non_repairable_870_markers = [
            "870 network mode",
            "抓取870数据",
            "870登录态不可用",
            "870登录态预检失败",
            "admin.buke999.com",
            "session cookie may be invalid",
            "session_cookie",
            "response is not valid json",
        ]
        if any(marker in lowered for marker in non_repairable_870_markers):
            return False
        return bool(classify_auth_failure(haystack))

    def open_870_cookie_repair_dialog(self) -> None:
        if self._is_busy():
            messagebox.showinfo("提示", "当前有任务在执行，请稍后再修复 870 PHPSESSID。")
            return
        self.prompt_870_cookie_repair(retry_after_save=False)

    def _current_870_session_cookie(self) -> str:
        cfg = self._load_config()
        return str(cfg.get("session_cookie") or "").strip()

    def _normalize_870_session_cookie(self, value: str) -> str:
        raw = str(value or "").strip()
        if raw.lower().startswith("session_cookie:"):
            raw = raw.split(":", 1)[1].strip()
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            raise ValueError("PHPSESSID 不能为空。")
        if "=" not in raw:
            raw = f"PHPSESSID={raw}"
        if "phpsessid=" not in raw.lower():
            raise ValueError("请填写 PHPSESSID=... 或 PHPSESSID 的值。")
        return raw

    def _write_870_session_cookie(self, cookie: str) -> Path:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        original = self.config_path.read_text(encoding="utf-8")
        backup_path = self.config_path.with_name(
            f"{self.config_path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        line = f"session_cookie: {json.dumps(cookie, ensure_ascii=False)}"
        if re.search(r"(?m)^session_cookie\s*:", original):
            updated = re.sub(r"(?m)^session_cookie\s*:.*$", line, original, count=1)
        else:
            suffix = "" if original.endswith("\n") else "\n"
            updated = f"{original}{suffix}{line}\n"

        parsed = yaml.safe_load(updated)
        if not isinstance(parsed, dict):
            raise ValueError("config.yaml 结构异常，未写入 PHPSESSID。")

        shutil.copy2(self.config_path, backup_path)
        atomic_write_text(self.config_path, updated)
        backups = sorted(
            self.config_path.parent.glob(f"{self.config_path.name}.bak-*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_backup in backups[3:]:
            stale_backup.unlink()
        return backup_path

    def prompt_870_cookie_repair(self, *, retry_after_save: bool) -> bool:
        current_cookie = self._current_870_session_cookie()
        initial_value = current_cookie or "PHPSESSID="
        dialog = tk.Toplevel(self.root)
        dialog.title("修复 870 PHPSESSID")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        cookie_var = tk.StringVar(value=initial_value)
        result: Dict[str, str] = {}

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="检测到 870 数据请求疑似登录态失效。", style="Body.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(frame, text='请检查并更新 session_cookie: "PHPSESSID=..."', style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 12)
        )
        ttk.Label(frame, text="session_cookie", style="Body.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(frame, width=58, textvariable=cookie_var)
        entry.grid(row=2, column=1, sticky="ew")
        ttk.Label(
            frame,
            text="可粘贴完整 PHPSESSID=xxx，也可以只填 session 值；日志不会打印完整 Cookie。",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(16, 0))

        def _confirm() -> None:
            try:
                result["cookie"] = self._normalize_870_session_cookie(cookie_var.get())
            except ValueError as exc:
                messagebox.showerror("输入错误", str(exc), parent=dialog)
                return
            dialog.destroy()

        def _cancel() -> None:
            dialog.destroy()

        ttk.Button(button_row, text="取消", style="Ghost.TButton", command=_cancel).pack(side=tk.LEFT)
        ttk.Button(button_row, text="确认并写回", style="Primary.TButton", command=_confirm).pack(side=tk.LEFT, padx=(8, 0))

        entry.focus_set()
        entry.selection_range(0, tk.END)
        dialog.bind("<Return>", lambda _event: _confirm())
        dialog.bind("<Escape>", lambda _event: _cancel())
        self.root.wait_window(dialog)

        cookie = result.get("cookie")
        if not cookie:
            return False
        try:
            backup_path = self._write_870_session_cookie(cookie)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("写入失败", f"写回 config.yaml 失败：{exc}")
            return False

        self._append_log(f"[GUI] 已更新 870 PHPSESSID，原配置已备份：{backup_path.name}")
        messagebox.showinfo("修复完成", "870 PHPSESSID 已写回 config.yaml。")
        if retry_after_save:
            self.has_870_cookie_retried = True
            self.status_text.set("870 PHPSESSID 已更新，任务重试中")
            self._append_log("[GUI] 870 PHPSESSID 修复完成，开始自动重试任务")
            self.start_run(from_auto_retry=True)
        return True

    def start_first_time_setup(self) -> None:
        """Open one guided Chrome session for all logins and verify real platform queries."""
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            date.today().isoformat(),
            "--no-runtime-gui",
            "--no-publish",
            "--with-extra-metrics",
            "--extra-auth-file",
            str(self._extra_auth_path()),
            "--repair-auth-only",
            "--auth-repair-browser",
            "chrome",
            "--auth-repair-profile",
            str(self.project_root / "output" / "auth_profiles" / "chrome_daily_report"),
            "--auth-repair-timeout-seconds",
            "300",
            "--auth-repair-target",
            "all",
        )
        self.status_text.set("首次设置：仅登录并检查连接，不会生成或发送日报")
        self._run_aux_command("首次设置", cmd)

    def start_auth_recovery_and_retry(self) -> None:
        if self._is_busy():
            messagebox.showinfo("Busy", "A task is already running. Try again later.")
            return
        self.aux_running = True
        self.status_text.set("Running auth repair")
        extra_auth_file = self._extra_auth_path()
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--with-extra-metrics",
            "--extra-auth-file",
            str(extra_auth_file),
            "--repair-auth-only",
            "--auth-repair-browser",
            "chrome",
            "--auth-repair-profile",
            str(self.project_root / "output" / "auth_profiles" / "chrome_daily_report"),
            "--auth-repair-timeout-seconds",
            "300",
            "--auth-repair-target",
            "both",
        )

        self._append_log("=" * 84)
        self._append_log("[GUI] Running thin auth repair")
        self._append_log("执行命令: " + " ".join(cmd))
        try:
            self.task_controller.start(
                cmd,
                cwd=self.project_root,
                env={**os.environ.copy(), **self._load_scheduler_env()},
                kind="auth_repair",
                label="自动登录修复",
                on_line=lambda task_id, line: self.queue.put(("task_line", (task_id, line))),
                on_done=lambda task_id, rc: self.queue.put(
                    ("recover_done", (task_id, rc == 0, "" if rc == 0 else f"退出码={rc}"))
                ),
            )
            self.btn_stop.configure(state=tk.NORMAL)
        except Exception as exc:  # noqa: BLE001
            self.aux_running = False
            self._append_log(f"[GUI ERROR] {exc}")
            messagebox.showerror("自动修复启动失败", str(exc))

    def _load_config(self) -> Dict[str, object]:
        if not self.config_path.exists():
            return {}
        try:
            cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def _resolve_pc_hosts_yaml(self) -> str:
        cfg = self._load_config()
        pc_cfg = cfg.get("pc_web_metrics") if isinstance(cfg.get("pc_web_metrics"), dict) else {}
        extra_cfg = cfg.get("extra_metrics") if isinstance(cfg.get("extra_metrics"), dict) else {}
        path = ""
        if isinstance(pc_cfg, dict):
            path = str(pc_cfg.get("hosts_yaml_path") or "").strip()
        if (not path) and isinstance(extra_cfg, dict):
            path = str(extra_cfg.get("hosts_yaml_path") or "").strip()
        if not path:
            path = str(self.project_root / "hosts_505.yaml")
        return path

    def refresh_pc_auth(self) -> None:
        extra_auth_file = self._extra_auth_path()
        self._run_aux_callable(
            "自动刷新PC登录态",
            lambda: refresh_extra_auth_from_browser(
                BrowserRefreshSettings(
                    browser="atlas",
                    extra_auth_path=extra_auth_file,
                    output_path=extra_auth_file,
                    hosts_yaml_path=self._resolve_pc_hosts_yaml(),
                    pc_only=True,
                )
            ),
        )

    def import_fenxi_har(self) -> None:
        if not self._prepare_har_update("上传Fenxi HAR并更新登录态"):
            return
        files = filedialog.askopenfilenames(
            title="选择 Fenxi HAR 文件",
            filetypes=[("HAR files", "*.har"), ("All files", "*.*")],
        )
        if not files:
            return
        extra_auth_file = self._extra_auth_path()
        self._run_aux_callable(
            "上传Fenxi HAR并更新登录态",
            lambda: refresh_fenxi_auth_from_hars(
                fenxi_hars=[Path(f) for f in files],
                extra_auth_file=extra_auth_file,
                output=extra_auth_file,
            ),
        )

    def import_pc_har(self) -> None:
        if not self._prepare_har_update("上传PC HAR并更新登录态"):
            return
        files = filedialog.askopenfilenames(
            title="选择 PC HAR 文件",
            filetypes=[("HAR files", "*.har"), ("All files", "*.*")],
        )
        if not files:
            return
        extra_auth_file = self._extra_auth_path()
        self._run_aux_callable(
            "上传PC HAR并更新登录态",
            lambda: refresh_pc_auth_from_hars(
                pc_hars=[Path(f) for f in files],
                extra_auth_file=extra_auth_file,
                output=extra_auth_file,
            ),
        )

    def _resolve_output_report_path(self, suffix: str) -> Path:
        run_date = self.date_value.get().strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
            raise ValueError("请输入 YYYY-MM-DD 格式日期。")
        report_date = date.fromisoformat(run_date)
        date_for_filename = f"{report_date.year}{report_date.month}{report_date.day}"
        return self.project_root / "output" / f"{date_for_filename}{suffix}"

    def repush_main_feishu(self) -> None:
        try:
            report_path = self._resolve_output_report_path("_report.txt")
        except ValueError as exc:
            messagebox.showerror("日期错误", str(exc))
            return
        if not report_path.exists():
            messagebox.showerror("文件不存在", f"未找到已生成的主日报文件：{report_path}")
            return
        self.feishu_url_main.set("")
        self._set_feishu_menu_state()
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--push-report-file",
            str(report_path),
            "--force-publish",
        )
        self._run_aux_command("补推主日报飞书", cmd)

    def repush_pc_feishu(self) -> None:
        try:
            report_path = self._resolve_output_report_path("_pc_report.txt")
        except ValueError as exc:
            messagebox.showerror("日期错误", str(exc))
            return
        if not report_path.exists():
            messagebox.showerror("文件不存在", f"未找到已生成的PC日报文件：{report_path}")
            return
        self.feishu_url_pc.set("")
        self._set_feishu_menu_state()
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--push-pc-report-file",
            str(report_path),
            "--force-publish",
        )
        self._run_aux_command("补推PC日报飞书", cmd)

    def _existing_report_paths(self) -> list[Path]:
        paths: list[Path] = []
        for suffix in ("_report.txt", "_pc_report.txt"):
            try:
                path = self._resolve_output_report_path(suffix)
            except ValueError:
                continue
            if path.exists():
                paths.append(path)
        return paths

    def _push_wecom_reports(self, target: str) -> None:
        report_paths = self._existing_report_paths()
        if not report_paths:
            messagebox.showerror("文件不存在", "当前日期没有已生成的主日报或PC日报文件。")
            return
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--push-wecom-reports",
            "--wecom-target",
            target,
            "--force-publish",
        )
        label = "推送企业微信-单人" if target == "single" else "推送企业微信-群"
        self._run_aux_command(label, cmd)

    def push_wecom_single(self) -> None:
        self._push_wecom_reports("single")

    def push_wecom_group(self) -> None:
        self._push_wecom_reports("group")

    def _resolve_870_login_url(self) -> str:
        if not self.config_path.exists():
            return ""
        try:
            cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            cfg = {}
        if isinstance(cfg, dict):
            explicit = str(cfg.get("login_url_870") or "").strip()
            if explicit:
                return explicit
            base = str(cfg.get("base_url") or "").strip()
            if base:
                parsed = urlparse(base)
                scheme = parsed.scheme or "http"
                netloc = parsed.netloc
                if netloc:
                    return f"{scheme}://{netloc}/?m=user&ac=login"
        return ""

    def open_870_login(self) -> None:
        url = self._resolve_870_login_url()
        if not url:
            messagebox.showwarning("缺少登录地址", "请先在 config.yaml 中配置 base_url 或 login_url_870。")
            return
        webbrowser.open(url, new=2)
        self._append_log(f"[GUI] 已打开870登录页: {url}")

    def open_main_feishu(self) -> None:
        url = self.feishu_url_main.get().strip()
        if not url:
            messagebox.showinfo("提示", "当前没有可打开的日报飞书链接。")
            return
        webbrowser.open(url, new=2)

    def open_pc_feishu(self) -> None:
        url = self.feishu_url_pc.get().strip()
        if not url:
            messagebox.showinfo("提示", "当前没有可打开的PC飞书链接。")
            return
        webbrowser.open(url, new=2)

    def open_log_dir(self) -> None:
        log_dir = self.project_root / "output" / "scheduler_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(log_dir.as_uri(), new=2)

    def open_output_dir(self) -> None:
        output_dir = self.project_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(output_dir.as_uri(), new=2)

    def edit_schedule_time(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("设置定时任务时间")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        hour_var = tk.StringVar(value=self.schedule_hour.get().strip() or "09")
        minute_var = tk.StringVar(value=self.schedule_minute.get().strip() or "00")

        frame = ttk.Frame(dialog, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text="每天执行时间", style="Body.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        hour_spin = ttk.Spinbox(frame, from_=0, to=23, width=4, textvariable=hour_var, format="%02.0f")
        hour_spin.grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, text=":", style="Body.TLabel").grid(row=1, column=1, padx=6, pady=(10, 0))
        ttk.Spinbox(frame, from_=0, to=59, width=4, textvariable=minute_var, format="%02.0f").grid(
            row=1, column=2, sticky="w", pady=(10, 0)
        )

        button_row = ttk.Frame(frame)
        button_row.grid(row=2, column=0, columnspan=4, sticky="e", pady=(16, 0))

        def _save() -> None:
            hour_text = hour_var.get().strip()
            minute_text = minute_var.get().strip()
            if not hour_text.isdigit() or not minute_text.isdigit():
                messagebox.showerror("时间错误", "小时和分钟必须是数字。", parent=dialog)
                return
            hour = int(hour_text)
            minute = int(minute_text)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                messagebox.showerror("时间错误", "请填写有效时间（小时0-23，分钟0-59）。", parent=dialog)
                return
            self.schedule_hour.set(f"{hour:02d}")
            self.schedule_minute.set(f"{minute:02d}")
            self._update_schedule_menu_labels()
            self._append_log(f"[GUI] 定时任务执行时间已设置为 {hour:02d}:{minute:02d}")
            dialog.destroy()

        ttk.Button(button_row, text="取消", style="Ghost.TButton", command=dialog.destroy).pack(side=tk.LEFT)
        ttk.Button(button_row, text="保存", style="Primary.TButton", command=_save).pack(side=tk.LEFT, padx=(8, 0))
        hour_spin.focus_set()
        dialog.bind("<Return>", lambda _event: _save())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self.root.wait_window(dialog)

    def _launchd_plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.LAUNCHD_LABEL}.plist"

    def _ensure_macos(self) -> bool:
        if os.name != "posix" or "darwin" not in os.uname().sysname.lower():
            messagebox.showinfo("提示", "定时任务按钮当前仅支持 macOS（launchd）。")
            return False
        return True

    def _refresh_schedule_status(self) -> None:
        plist = self._launchd_plist_path()
        exists = plist.exists()
        if not self._ensure_macos_silent():
            self._set_schedule_status("定时任务：仅 macOS 支持此区域")
            return
        result = subprocess.run(["launchctl", "list", self.LAUNCHD_LABEL], capture_output=True, text=True)
        if result.returncode == 0:
            self._set_schedule_status(f"定时任务：已加载（{self.LAUNCHD_LABEL}）")
            return
        if exists:
            self._set_schedule_status("定时任务：已配置但未加载（可点安装/更新）")
        else:
            self._set_schedule_status("定时任务：未安装")

    def _ensure_macos_silent(self) -> bool:
        return os.name == "posix" and "darwin" in os.uname().sysname.lower()

    def install_schedule(self) -> None:
        if not self._ensure_macos():
            return
        hour_text = self.schedule_hour.get().strip()
        minute_text = self.schedule_minute.get().strip()
        if not hour_text.isdigit() or not minute_text.isdigit():
            messagebox.showerror("时间错误", "小时和分钟必须是数字。")
            return
        hour = int(hour_text)
        minute = int(minute_text)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            messagebox.showerror("时间错误", "请填写有效时间（小时0-23，分钟0-59）。")
            return

        script = self.project_root / "scripts" / "install_macos_launchd.sh"
        if not script.exists():
            messagebox.showerror("缺少脚本", f"未找到安装脚本：{script}")
            return

        self._append_log(f"[GUI] 安装/更新定时任务: {hour:02d}:{minute:02d}")
        result = subprocess.run(["bash", str(script), str(hour), str(minute)], cwd=str(self.project_root), capture_output=True, text=True)
        if result.stdout:
            self._append_log(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr:
                self._append_log(result.stderr.strip())
            messagebox.showerror("安装失败", "定时任务安装失败，请看日志。")
            return
        self._refresh_schedule_status()
        messagebox.showinfo("成功", f"定时任务已更新：每天 {hour:02d}:{minute:02d}")

    def trigger_schedule_now(self) -> None:
        if not self._ensure_macos():
            return
        uid = str(os.getuid())
        target = f"gui/{uid}/{self.LAUNCHD_LABEL}"
        self._append_log(f"[GUI] 触发定时任务: {target}")
        result = subprocess.run(["launchctl", "kickstart", "-k", target], capture_output=True, text=True)
        if result.returncode != 0:
            if result.stderr:
                self._append_log(result.stderr.strip())
            messagebox.showerror("触发失败", "立即触发失败，请先安装定时任务。")
            return
        if result.stdout:
            self._append_log(result.stdout.strip())
        messagebox.showinfo("成功", "已触发定时任务执行。")

    def disable_schedule(self) -> None:
        if not self._ensure_macos():
            return
        plist = self._launchd_plist_path()
        if not plist.exists():
            messagebox.showinfo("提示", "当前没有已安装的定时任务。")
            self._refresh_schedule_status()
            return
        self._append_log("[GUI] 取消定时任务")
        result = subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True)
        if result.returncode != 0:
            if result.stderr:
                self._append_log(result.stderr.strip())
            messagebox.showerror("取消失败", "取消定时任务失败，请看日志。")
            return
        if result.stdout:
            self._append_log(result.stdout.strip())
        self._refresh_schedule_status()
        messagebox.showinfo("成功", "定时任务已取消。")

    def _on_close(self) -> None:
        active = self.task_controller.active
        if active is not None:
            detail = "当前正在发送，关闭后下次运行可能要求确认是否已发送。" if active.publishing else "当前任务仍在运行。"
            if not messagebox.askyesno("关闭一键工作台", f"{detail}\n是否停止任务并关闭？"):
                return
            try:
                self.task_controller.stop()
            except Exception as exc:  # noqa: BLE001
                if not messagebox.askyesno("停止失败", f"无法完整停止任务：{exc}\n仍要关闭吗？"):
                    return
        elif self.aux_running:
            if not messagebox.askyesno("关闭一键工作台", "辅助任务仍在运行，关闭后任务会中断。仍要关闭吗？"):
                return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ReportLauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
