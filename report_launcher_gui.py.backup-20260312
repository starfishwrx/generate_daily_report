from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import tkinter as tk
from tkinter import messagebox, ttk

import yaml


PROGRESS_RE = re.compile(r"\[PROGRESS\]\s*(\d{1,3})\|(.+)")
FEISHU_URL_RE = re.compile(r"Feishu doc published:\s*(https?://\S+)")


class ReportLauncherApp:
    LAUNCHD_LABEL = "com.starfish.autodatareport.daily"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("云游戏日报控制台")
        self.root.geometry("1100x760")
        self.root.minsize(980, 700)

        self.project_root = Path(__file__).resolve().parent
        self.config_path = self.project_root / "config.yaml"
        self.script_path = self.project_root / "generate_daily_report.py"
        self.python_bin = self._resolve_python_bin()

        self.process: Optional[subprocess.Popen[str]] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.queue: "queue.Queue[tuple[str, str | int]]" = queue.Queue()

        self.date_mode = tk.StringVar(value="today")
        self.date_value = tk.StringVar(value=date.today().isoformat())
        self.with_extra = tk.BooleanVar(value=True)
        self.verify_feishu = tk.BooleanVar(value=False)
        self.disable_feishu = tk.BooleanVar(value=False)

        self.status_text = tk.StringVar(value="待命")
        self.progress_value = tk.IntVar(value=0)
        self.progress_pct_text = tk.StringVar(value="0%")
        self.feishu_url = tk.StringVar(value="")

        self.schedule_hour = tk.StringVar(value="09")
        self.schedule_minute = tk.StringVar(value="10")
        self.schedule_status = tk.StringVar(value="定时任务：未检测")

        self._configure_style()
        self._build_ui()
        self._refresh_schedule_status()
        self.root.after(120, self._drain_queue)

    def _resolve_python_bin(self) -> str:
        venv_bin = self.project_root / ".venv" / "bin" / "python"
        if venv_bin.exists():
            return str(venv_bin)
        return "python3"

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

        self.btn_open_feishu = ttk.Button(
            bar, text="打开最新飞书文档", style="Ghost.TButton", command=self.open_latest_feishu, state=tk.DISABLED
        )
        self.btn_open_feishu.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Button(bar, text="打开日志目录", style="Ghost.TButton", command=self.open_log_dir).pack(side=tk.LEFT, padx=(10, 0))

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
        cmd = [
            self.python_bin,
            str(self.script_path),
            "--config",
            str(self.config_path),
            "--date",
            self.date_value.get().strip(),
            "--no-runtime-gui",
        ]
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

    def start_run(self) -> None:
        if self.process is not None:
            return
        run_date = self.date_value.get().strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_date):
            messagebox.showerror("日期格式错误", "请输入 YYYY-MM-DD 格式日期。")
            return
        if not self.config_path.exists():
            messagebox.showerror("配置缺失", f"未找到配置文件：{self.config_path}")
            return

        cmd = self._build_command()
        env = os.environ.copy()
        env.update(self._load_scheduler_env())

        self._set_progress(0, "任务启动中")
        self.feishu_url.set("")
        self.btn_open_feishu.configure(state=tk.DISABLED)
        self._append_log("=" * 84)
        self._append_log("启动命令: " + " ".join(cmd))

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)

        def _worker() -> None:
            try:
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
                    url_match = FEISHU_URL_RE.search(line)
                    if url_match:
                        self.queue.put(("feishu", url_match.group(1).strip()))
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
                self._append_log(str(value))
            elif kind == "progress":
                self._set_progress(int(value))
            elif kind == "status":
                self.status_text.set(str(value))
            elif kind == "feishu":
                self.feishu_url.set(str(value))
                self.btn_open_feishu.configure(state=tk.NORMAL)
            elif kind == "done":
                rc = int(value)
                self.process = None
                self.btn_start.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED)
                if rc == 0:
                    self._set_progress(100, "任务完成")
                    self._append_log("[GUI] 任务完成")
                else:
                    self.status_text.set("任务失败")
                    self._append_log(f"[GUI] 任务失败，退出码={rc}")
                    messagebox.showerror("运行失败", "任务执行失败，请查看日志。")

        self.root.after(120, self._drain_queue)

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

    def open_latest_feishu(self) -> None:
        url = self.feishu_url.get().strip()
        if not url:
            messagebox.showinfo("提示", "当前没有可打开的飞书链接。")
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
