from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml
from auth_recovery_playwright import RecoverySettings, recover_auth
from browser_auth_refresh import BrowserRefreshSettings, refresh_extra_auth_from_browser
from fenxi_auth_from_har import refresh_fenxi_auth_from_hars
from pc_auth_from_har import refresh_pc_auth_from_hars


PROGRESS_RE = re.compile(r"\[PROGRESS\]\s*(\d{1,3})\|(.+)")
FEISHU_MAIN_URL_RE = re.compile(r"Feishu doc published:\s*(https?://\S+)")
FEISHU_PC_URL_RE = re.compile(r"Feishu PC doc published:\s*(https?://\S+)")


class ReportLauncherApp:
    LAUNCHD_LABEL = "com.starfish.autodatareport.daily"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("云游戏日报控制台")
        self.root.geometry("1100x760")
        self.root.minsize(980, 700)

        self.bundle_root = self._resolve_bundle_root()
        self.project_root = self._resolve_runtime_root()
        self.config_path = self._resolve_runtime_file("config.yaml")
        self.script_path = self.bundle_root / "generate_daily_report.py"
        self.cli_exe_path = self.bundle_root / "autodatareport-cli.exe"
        self.browser_auth_script = self.bundle_root / "browser_auth_refresh.py"
        self.fenxi_auth_script = self.bundle_root / "fenxi_auth_from_har.py"
        self.pc_auth_script = self.bundle_root / "pc_auth_from_har.py"
        self.playwright_recover_script = self.bundle_root / "auth_recovery_playwright.py"
        self.python_bin = self._resolve_python_bin()

        self.process: Optional[subprocess.Popen[str]] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.aux_running = False
        self.queue: "queue.Queue[tuple[str, str | int]]" = queue.Queue()

        self.date_mode = tk.StringVar(value="today")
        self.date_value = tk.StringVar(value=date.today().isoformat())
        self.with_extra = tk.BooleanVar(value=True)
        self.verify_feishu = tk.BooleanVar(value=False)
        self.disable_feishu = tk.BooleanVar(value=False)
        self.auto_auth_recover = tk.BooleanVar(value=True)

        self.status_text = tk.StringVar(value="待命")
        self.progress_value = tk.IntVar(value=0)
        self.progress_pct_text = tk.StringVar(value="0%")
        self.feishu_url_main = tk.StringVar(value="")
        self.feishu_url_pc = tk.StringVar(value="")
        self.log_history: list[str] = []
        self.has_auto_retried = False

        self.schedule_hour = tk.StringVar(value="09")
        self.schedule_minute = tk.StringVar(value="10")
        self.schedule_status = tk.StringVar(value="定时任务：未检测")

        self._configure_style()
        self._build_ui()
        self._refresh_schedule_status()
        self.root.after(120, self._drain_queue)

    def _resolve_bundle_root(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _resolve_python_bin(self) -> str:
        if getattr(sys, "frozen", False):
            return ""
        venv_bin = self.project_root / ".venv" / "bin" / "python"
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
        return self.project_root / "extra_auth.json"

    def _build_cli_command(self, *args: str) -> list[str]:
        if getattr(sys, "frozen", False):
            if not self.cli_exe_path.exists():
                raise FileNotFoundError(f"未找到打包后的 CLI：{self.cli_exe_path}")
            return [str(self.cli_exe_path), *args]
        return [self.python_bin, str(self.script_path), *args]

    def _configure_style(self) -> None:
        self.root.configure(bg="#E8EEF3")
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Root.TFrame", background="#E8EEF3")
        style.configure("Card.TFrame", background="#F6FAFD")
        style.configure("CardTitle.TLabel", background="#F6FAFD", foreground="#213547", font=("PingFang SC", 11, "bold"))
        style.configure("Muted.TLabel", background="#F6FAFD", foreground="#5D6C7A", font=("PingFang SC", 10))
        style.configure("Body.TLabel", background="#F6FAFD", foreground="#22313F", font=("PingFang SC", 10))
        style.configure("HeaderTitle.TLabel", background="#DDEAF5", foreground="#102A43", font=("PingFang SC", 17, "bold"))
        style.configure("HeaderSub.TLabel", background="#DDEAF5", foreground="#334E68", font=("PingFang SC", 10))

        style.configure("Primary.TButton", font=("PingFang SC", 10, "bold"), padding=(12, 7))
        style.map("Primary.TButton", background=[("!disabled", "#1F7AE0")], foreground=[("!disabled", "#FFFFFF")])

        style.configure("Warn.TButton", font=("PingFang SC", 10), padding=(12, 7))
        style.map("Warn.TButton", background=[("!disabled", "#FFF1E8")], foreground=[("!disabled", "#B54708")])

        style.configure("Ghost.TButton", font=("PingFang SC", 10), padding=(10, 6))
        style.map("Ghost.TButton", background=[("!disabled", "#EDF3F8")], foreground=[("!disabled", "#2F4858")])

        style.configure("Run.Horizontal.TProgressbar", troughcolor="#D9E2EC", background="#1F7AE0", bordercolor="#D9E2EC", lightcolor="#1F7AE0", darkcolor="#1F7AE0")

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, style="Root.TFrame", padding=14)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container, style="Card.TFrame", padding=(16, 12))
        header.pack(fill=tk.X)
        ttk.Label(header, text="云游戏日报控制台", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="一键登录跳转、立即执行、进度跟踪、定时任务管理（飞书默认推送）",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        top_grid = ttk.Frame(container, style="Root.TFrame")
        top_grid.pack(fill=tk.X, pady=(10, 0))
        top_grid.columnconfigure(0, weight=1)
        top_grid.columnconfigure(1, weight=1)

        run_card = ttk.Frame(top_grid, style="Card.TFrame", padding=12)
        run_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._build_run_controls(run_card)

        schedule_card = ttk.Frame(top_grid, style="Card.TFrame", padding=12)
        schedule_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._build_schedule_controls(schedule_card)

        action_card = ttk.Frame(container, style="Card.TFrame", padding=12)
        action_card.pack(fill=tk.X, pady=(10, 0))
        self._build_action_controls(action_card)

        progress_card = ttk.Frame(container, style="Card.TFrame", padding=12)
        progress_card.pack(fill=tk.X, pady=(10, 0))
        self._build_progress(progress_card)

        log_card = ttk.Frame(container, style="Card.TFrame", padding=12)
        log_card.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._build_logs(log_card)

    def _build_run_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="运行参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")

        ttk.Label(parent, text="日期模式", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Radiobutton(parent, text="今天", value="today", variable=self.date_mode, command=self._update_date_from_mode).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0)
        )
        ttk.Radiobutton(parent, text="昨天", value="yesterday", variable=self.date_mode, command=self._update_date_from_mode).grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(10, 0)
        )

        ttk.Label(parent, text="执行日期", style="Body.TLabel").grid(row=1, column=3, sticky="e", padx=(16, 6), pady=(10, 0))
        ttk.Entry(parent, textvariable=self.date_value, width=14).grid(row=1, column=4, sticky="w", pady=(10, 0))

        ttk.Checkbutton(parent, text="抓取扩展数据（分析后台+505）", variable=self.with_extra).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(parent, text="推送后校验飞书内容", variable=self.verify_feishu).grid(
            row=2, column=3, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(parent, text="本次禁用飞书推送", variable=self.disable_feishu).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        ttk.Checkbutton(parent, text="登录态失败时自动修复并重试一次", variable=self.auto_auth_recover).grid(
            row=3, column=3, columnspan=3, sticky="w", pady=(6, 0)
        )

        ttk.Label(
            parent,
            text="提示：定时任务会读取 .env.scheduler 里的飞书凭证。",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(10, 0))

    def _build_schedule_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="定时任务", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")

        ttk.Label(parent, text="每天执行时间", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(parent, from_=0, to=23, width=4, textvariable=self.schedule_hour, format="%02.0f").grid(
            row=1, column=1, sticky="w", pady=(10, 0)
        )
        ttk.Label(parent, text=":", style="Body.TLabel").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Spinbox(parent, from_=0, to=59, width=4, textvariable=self.schedule_minute, format="%02.0f").grid(
            row=1, column=3, sticky="w", pady=(10, 0)
        )

        ttk.Button(parent, text="安装/更新定时任务", style="Primary.TButton", command=self.install_schedule).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )
        ttk.Button(parent, text="立即触发定时任务", style="Ghost.TButton", command=self.trigger_schedule_now).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=(8, 0), pady=(12, 0)
        )

        ttk.Button(parent, text="取消定时任务", style="Warn.TButton", command=self.disable_schedule).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Button(parent, text="刷新状态", style="Ghost.TButton", command=self._refresh_schedule_status).grid(
            row=3, column=2, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        ttk.Label(parent, textvariable=self.schedule_status, style="Muted.TLabel").grid(
            row=4, column=0, columnspan=6, sticky="w", pady=(10, 0)
        )

    def _build_action_controls(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="快捷操作", style="CardTitle.TLabel").pack(anchor="w")

        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill=tk.X, pady=(10, 0))

        self.btn_open_login = ttk.Button(bar, text="打开870登录页", style="Ghost.TButton", command=self.open_870_login)
        self.btn_open_login.pack(side=tk.LEFT)

        self.btn_start = ttk.Button(bar, text="立即启动任务", style="Primary.TButton", command=self.start_run)
        self.btn_start.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_stop = ttk.Button(bar, text="停止任务", style="Warn.TButton", command=self.stop_run, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_open_feishu_main = ttk.Button(
            bar, text="打开日报飞书文档", style="Ghost.TButton", command=self.open_main_feishu, state=tk.DISABLED
        )
        self.btn_open_feishu_main.pack(side=tk.LEFT, padx=(10, 0))

        self.btn_open_feishu_pc = ttk.Button(
            bar, text="打开PC飞书文档", style="Ghost.TButton", command=self.open_pc_feishu, state=tk.DISABLED
        )
        self.btn_open_feishu_pc.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Button(bar, text="打开日志目录", style="Ghost.TButton", command=self.open_log_dir).pack(side=tk.LEFT, padx=(10, 0))

        push_bar = ttk.Frame(parent, style="Card.TFrame")
        push_bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(push_bar, text="补推主日报飞书", style="Ghost.TButton", command=self.repush_main_feishu).pack(side=tk.LEFT)
        ttk.Button(push_bar, text="补推PC日报飞书", style="Ghost.TButton", command=self.repush_pc_feishu).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(push_bar, text="推企业微信-单人", style="Ghost.TButton", command=self.push_wecom_single).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(push_bar, text="推企业微信-群", style="Ghost.TButton", command=self.push_wecom_group).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(push_bar, text="仅推送已有 output 下的 txt 到飞书，不重跑数据", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        auth_bar = ttk.Frame(parent, style="Card.TFrame")
        auth_bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(auth_bar, text="自动刷新PC登录态", style="Ghost.TButton", command=self.refresh_pc_auth).pack(side=tk.LEFT)
        ttk.Button(auth_bar, text="上传PC HAR并更新", style="Ghost.TButton", command=self.import_pc_har).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(auth_bar, text="上传Fenxi HAR并更新", style="Ghost.TButton", command=self.import_fenxi_har).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(auth_bar, text="登录态维护：PC自动刷新/PC HAR导入/Fenxi HAR导入", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))

    def _build_progress(self, parent: ttk.Frame) -> None:
        title_row = ttk.Frame(parent, style="Card.TFrame")
        title_row.pack(fill=tk.X)
        ttk.Label(title_row, text="任务进度", style="CardTitle.TLabel").pack(side=tk.LEFT)
        ttk.Label(title_row, textvariable=self.progress_pct_text, style="Body.TLabel").pack(side=tk.RIGHT)

        ttk.Label(parent, textvariable=self.status_text, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        self.progress = ttk.Progressbar(
            parent,
            orient=tk.HORIZONTAL,
            mode="determinate",
            variable=self.progress_value,
            maximum=100,
            style="Run.Horizontal.TProgressbar",
        )
        self.progress.pack(fill=tk.X, pady=(8, 0))

    def _build_logs(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="运行日志", style="CardTitle.TLabel").pack(anchor="w")
        wrapper = ttk.Frame(parent, style="Card.TFrame")
        wrapper.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.log_text = tk.Text(
            wrapper,
            wrap="word",
            height=24,
            bg="#0E1A26",
            fg="#D5E1ED",
            insertbackground="#D5E1ED",
            relief="flat",
            font=("Menlo", 11),
            padx=10,
            pady=10,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _update_date_from_mode(self) -> None:
        if self.date_mode.get() == "yesterday":
            target = date.today() - timedelta(days=1)
        else:
            target = date.today()
        self.date_value.set(target.isoformat())

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
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
        )
        if self.with_extra.get():
            cmd.append("--with-extra-metrics")
        if self.verify_feishu.get():
            cmd.append("--verify-feishu-content")
        if self.disable_feishu.get():
            cmd.append("--no-push-feishu-doc")
        return cmd

    def _set_progress(self, value: int, status: Optional[str] = None) -> None:
        pct = max(0, min(100, int(value)))
        self.progress_value.set(pct)
        self.progress_pct_text.set(f"{pct}%")
        if status is not None:
            self.status_text.set(status)

    def start_run(self, from_auto_retry: bool = False) -> None:
        if self.process is not None:
            return
        if not from_auto_retry:
            self.has_auto_retried = False
        self.log_history = []
        run_date = self.date_value.get().strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
            messagebox.showerror("日期格式错误", "请输入 YYYY-MM-DD 格式日期。")
            return
        if not self.config_path.exists():
            messagebox.showerror("配置缺失", f"未找到配置文件：{self.config_path}")
            return

        cmd = self._build_command()
        preflight_cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            run_date,
            "--no-runtime-gui",
            "--check-extra-auth",
        )
        env = os.environ.copy()
        env.update(self._load_scheduler_env())

        self._set_progress(0, "登录态预检中")
        self.feishu_url_main.set("")
        self.feishu_url_pc.set("")
        self.btn_open_feishu_main.configure(state=tk.DISABLED)
        self.btn_open_feishu_pc.configure(state=tk.DISABLED)
        self._append_log("=" * 84)
        self._append_log("预检命令: " + " ".join(preflight_cmd))
        self._append_log("启动命令: " + " ".join(cmd))

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)

        def _worker() -> None:
            try:
                rc = self._run_streaming_cmd(preflight_cmd, env, "全平台登录态预检")
                if rc != 0:
                    self.queue.put(("line", "[GUI] 全平台登录态预检失败，主流程已终止"))
                    self.queue.put(("preflight_failed", rc))
                    return
                self.process = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                assert self.process.stdout is not None
                for raw_line in self.process.stdout:
                    line = raw_line.rstrip("\n")
                    self.queue.put(("line", line))
                    progress_match = PROGRESS_RE.search(line)
                    if progress_match:
                        pct = int(progress_match.group(1))
                        msg = progress_match.group(2).strip()
                        self.queue.put(("progress", pct))
                        self.queue.put(("status", msg))
                    pc_url_match = FEISHU_PC_URL_RE.search(line)
                    if pc_url_match:
                        self.queue.put(("feishu_pc", pc_url_match.group(1).strip()))
                    else:
                        main_url_match = FEISHU_MAIN_URL_RE.search(line)
                        if main_url_match:
                            self.queue.put(("feishu_main", main_url_match.group(1).strip()))
                rc = self.process.wait()
                self.queue.put(("done", rc))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("line", f"[GUI ERROR] {exc}"))
                self.queue.put(("done", 1))

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()

    def stop_run(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
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

            if kind == "line":
                text = str(value)
                self._append_log(text)
                pc_url_match = FEISHU_PC_URL_RE.search(text)
                if pc_url_match:
                    self.feishu_url_pc.set(pc_url_match.group(1).strip())
                    self.btn_open_feishu_pc.configure(state=tk.NORMAL)
                else:
                    main_url_match = FEISHU_MAIN_URL_RE.search(text)
                    if main_url_match:
                        self.feishu_url_main.set(main_url_match.group(1).strip())
                        self.btn_open_feishu_main.configure(state=tk.NORMAL)
            elif kind == "progress":
                self._set_progress(int(value))
            elif kind == "status":
                self.status_text.set(str(value))
            elif kind == "feishu_main":
                self.feishu_url_main.set(str(value))
                self.btn_open_feishu_main.configure(state=tk.NORMAL)
            elif kind == "feishu_pc":
                self.feishu_url_pc.set(str(value))
                self.btn_open_feishu_pc.configure(state=tk.NORMAL)
            elif kind == "done":
                rc = int(value)
                self.process = None
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                if rc == 0:
                    self._set_progress(100, "任务完成")
                    self._append_log("[GUI] 任务完成")
                else:
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
                    self._append_log(f"[GUI] 任务失败，退出码={rc}")
                    messagebox.showerror("运行失败", "任务执行失败，请查看日志。")
            elif kind == "preflight_failed":
                rc = int(value)
                self.process = None
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                self.status_text.set("登录态预检失败")
                self._append_log(f"[GUI] 登录态预检失败，退出码={rc}")
                messagebox.showerror("预检失败", self._extract_preflight_failure_reason())
            elif kind == "aux_done":
                rc, label = value  # type: ignore[misc]
                self.aux_running = False
                if int(rc) == 0:
                    self._append_log(f"[GUI] {label}完成")
                    messagebox.showinfo("完成", f"{label}成功。")
                else:
                    self._append_log(f"[GUI] {label}失败，退出码={rc}")
                    messagebox.showerror("失败", f"{label}失败，请查看日志。")
                self._refresh_schedule_status()
            elif kind == "recover_done":
                ok, reason = value  # type: ignore[misc]
                self.aux_running = False
                if ok:
                    self._append_log("[GUI] 自动登录修复完成，开始重试任务")
                    self.status_text.set("自动修复成功，任务重试中")
                    self.start_run(from_auto_retry=True)
                else:
                    self.status_text.set("自动修复失败")
                    self._append_log(f"[GUI] 自动修复失败: {reason}")
                    messagebox.showerror("自动修复失败", str(reason))
                self._refresh_schedule_status()

        self.root.after(120, self._drain_queue)

    def _is_busy(self) -> bool:
        return self.process is not None or self.aux_running

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

        def _worker() -> None:
            rc = 1
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    self.queue.put(("line", line))
                rc = proc.wait()
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("line", f"[GUI ERROR] {exc}"))
                rc = 1
            self.queue.put(("aux_done", (rc, label)))

        threading.Thread(target=_worker, daemon=True).start()

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
        if self.process is None:
            return True
        should_stop = messagebox.askyesno(
            "当前任务仍在执行",
            f"{label}前需要先停止当前主流程，否则会继续占用登录态与文件。\n是否先停止当前任务，再继续上传 HAR？",
        )
        if not should_stop:
            return False
        proc = self.process
        self._append_log(f"[GUI] 为执行“{label}”，先停止当前任务")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("停止失败", f"无法停止当前任务：{exc}")
            return False
        self.process = None
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_text.set("主流程已停止，可更新登录态")
        self._append_log("[GUI] 当前任务已停止，开始更新登录态")
        return True

    def _run_streaming_cmd(self, cmd: list[str], env: Dict[str, str], label: str) -> int:
        self.queue.put(("line", "=" * 84))
        self.queue.put(("line", f"[GUI] {label}"))
        self.queue.put(("line", "执行命令: " + " ".join(cmd)))
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            self.queue.put(("line", raw_line.rstrip("\n")))
        return int(proc.wait())

    def _looks_like_auth_failure(self) -> bool:
        haystack = "\n".join(self.log_history[-220:]).lower()
        patterns = [
            "870登录态不可用",
            "870登录态预检失败",
            "登录态不可用",
            "请先登录",
            "session_cookie",
            "扩展登录态预检失败",
            "pc网页端登录态预检失败",
            "pc网页端接口失败 status=-100",
            "pc网页端接口失败 status=-101",
            "fenxi token 预检失败",
            "e_token",
            "返回登录页",
        ]
        return any(p.lower() in haystack for p in patterns)

    def _extract_preflight_failure_reason(self) -> str:
        patterns = [
            "870登录态不可用",
            "分析后台登录态预检失败",
            "505后台登录态预检失败",
            "PC后台登录态预检失败",
            "PC会员登录态预检失败",
            "扩展登录态预检失败",
            "fenxi token 预检失败",
        ]
        for line in reversed(self.log_history[-120:]):
            text = str(line).strip()
            if not text:
                continue
            for pattern in patterns:
                if pattern in text:
                    return text
        return "全平台登录态预检未通过，请查看日志中的具体失败平台。"

    def start_auth_recovery_and_retry(self) -> None:
        if self._is_busy():
            messagebox.showinfo("提示", "当前有任务在执行，请稍后再试。")
            return
        env = os.environ.copy()
        env.update(self._load_scheduler_env())
        self.aux_running = True
        self.status_text.set("执行登录修复中")
        extra_auth_file = self._extra_auth_path()
        preflight_cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--check-extra-auth",
        )

        def _worker() -> None:
            try:
                self.queue.put(("line", "=" * 84))
                self.queue.put(("line", "[GUI] 自动登录修复"))
                result = recover_auth(
                    RecoverySettings(
                        extra_auth_file=extra_auth_file,
                        output=extra_auth_file,
                        pc_login_url="http://yadmin.4399.com/",
                        fenxi_url="https://fenxi.4399dev.com/analysis/",
                        timeout_seconds=300,
                        browser_channel="",
                        phone="",
                        sms_code="",
                        ask_sms=True,
                        auto_fill=True,
                        skip_pc=False,
                        skip_fenxi=False,
                    )
                )
                self.queue.put(("line", json.dumps(result, ensure_ascii=False)))
                rc = self._run_streaming_cmd(preflight_cmd, env, "登录态预检")
                if rc != 0:
                    self.queue.put(("recover_done", (False, f"登录态预检失败，退出码={rc}")))
                    return
                self.queue.put(("recover_done", (True, "")))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("line", f"[GUI ERROR] {exc}"))
                self.queue.put(("recover_done", (False, str(exc))))

        threading.Thread(target=_worker, daemon=True).start()

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
        self.btn_open_feishu_main.configure(state=tk.DISABLED)
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--push-report-file",
            str(report_path),
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
        self.btn_open_feishu_pc.configure(state=tk.DISABLED)
        cmd = self._build_cli_command(
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
            "--push-pc-report-file",
            str(report_path),
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
            self.schedule_status.set("定时任务：仅 macOS 支持此区域")
            return
        result = subprocess.run(["launchctl", "list", self.LAUNCHD_LABEL], capture_output=True, text=True)
        if result.returncode == 0:
            self.schedule_status.set(f"定时任务：已加载（{self.LAUNCHD_LABEL}）")
            return
        if exists:
            self.schedule_status.set("定时任务：已配置但未加载（可点安装/更新）")
        else:
            self.schedule_status.set("定时任务：未安装")

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


def main() -> None:
    root = tk.Tk()
    ReportLauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
