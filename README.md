# 云游戏日报自动化工具 V1.5

AutoDataReport 是面向日常运营的数据日报工具：从 870、Fenxi、505 和 PC Web 后台采集数据，计算指标、生成主日报与 PC 日报，并按配置发布到飞书和企业微信。

V1.5 继续采用 Python 3.10+、Tkinter 和 Windows 本地 EXE。默认交互是“昨天、完整数据、自动发送、登录失效自动修复”，普通使用者不需要命令行，也不需要安装数据库或部署服务器。

## 普通同事如何使用

### 第一次使用

1. 解压完整的 Windows 分发目录，不要只复制单个 EXE。
2. 双击 `autodatareport-gui.exe`。
3. 打开右上角“设置与修复”，选择“首次设置（推荐）”。
4. 按程序提示完成各后台登录。
5. 首次设置只检查登录和连接，不会自动生成或发送日报。

### 每天生成日报

1. 打开 GUI，默认日期已经是昨天。
2. 点击“生成并发送昨天日报”。
3. 完成后可直接打开主日报、PC 日报或输出文件夹。

程序默认自动验证登录态。认证失效时会引导修复；技术日志平时折叠，失败时自动展开。运行中的正式日报、首次设置和自动修复都可以停止，关闭窗口时也会清理对应的子进程树。

打包版的个人配置、认证文件、输出和运行状态存放在：

```text
%LOCALAPPDATA%\AutoDataReport\
```

升级程序时不要删除这个目录。分发包本身不应包含任何个人 Cookie、Token、Webhook、HAR、认证日志或历史输出。

## V1.5 的主要变化

- 将执行过程组织为认证预检、数据采集、指标计算、报告渲染、外部发布五个阶段。
- 870、Fenxi/505 和 PC 数据按预算并发采集；单次运行复用 HTTP 客户端和连接池。
- 870 预检成功的首个查询响应可在正式采集时复用，减少重复请求。
- GUI 使用统一任务控制器管理正式日报、首次设置和认证修复，支持停止和窗口关闭清理。
- 飞书主日报、飞书 PC 日报和各企微目标分别记录发布状态，避免一个目标失败覆盖其他目标的成功记录。
- 发布中断且无法确认远端结果时标记为 `uncertain`，普通运行不会自动重发，需要用户确认。
- 配置、认证、缓存和发布状态的关键写入改用同目录临时文件加 `os.replace()`。
- `output/run_metrics/` 记录阶段耗时、请求数、重试、客户端创建、预检响应复用和缓存命中等信息。
- 图表缓存包含渲染版本和应用版本；V1.5 会使旧版渲染缓存自动失效。
- Windows CI 检查锁定依赖、Ruff、测试、Python 编译和依赖审计。

## 输出内容

- `output/YYYYMMDD_report.txt`：主日报。
- `output/YYYYMMDD_pc_report.txt`：PC 云游戏日报。
- `output/charts/*.png`：870 图表和 505 付费表图片。
- `output/run_metrics/*.json`：单次运行指标。
- `output/publish_state/YYYYMMDD.json`：按日期保存的发布状态。
- `output/auth_repair_logs/`：认证修复日志。
- `output/scheduler_logs/`：定时任务日志。

运行期实际根目录以 GUI 顶部显示的路径为准。打包版通常位于 `%LOCALAPPDATA%\AutoDataReport`，源码运行默认使用项目目录。

## 发布状态与安全重试

V1.5 发布状态 schema 支持：

| 状态 | 含义 | 下次运行行为 |
|---|---|---|
| `pending` | 尚未调用远端 | 可以发送 |
| `publishing` | 已开始远端调用 | 下次启动按待确认处理 |
| `failed` | 已确认没有成功 | 可以安全重试 |
| `uncertain` | 远端可能已经成功 | 阻止自动重发，等待人工确认 |
| `completed` | 已确认成功 | 相同日期和内容自动跳过 |

不要为了消除错误提示直接删除 `publish_state`。如果 GUI 提示发送结果待确认，应先检查飞书或企微，再选择“已发送，标记完成”或“确认未发送，重新发送”。

`--force-publish` 会明确要求重新发布，仅适合已经确认需要重推的情况。

## 项目结构

- `generate_daily_report.py`：兼容 CLI 入口，现有脚本和参数保持可用。
- `autodatareport/application.py`：当前主应用实现和兼容业务逻辑。
- `autodatareport/orchestrator.py`：五阶段流水线编排。
- `autodatareport/pipeline.py`：严格/非严格来源任务和并发执行。
- `autodatareport/models.py`：运行上下文、错误分类、阶段与发布内部类型。
- `autodatareport/contracts.py`：数据源、计算、渲染和发布的扩展边界定义。
- `autodatareport/gui_task_controller.py`：GUI 活动任务、任务编号和停止控制。
- `autodatareport/process_runner.py`：子进程输出与 Windows 进程树清理。
- `autodatareport/publishing.py`、`publish_state.py`：发布协调、幂等状态和恢复决策。
- `autodatareport/atomic_io.py`：JSON、YAML 和文本原子写入。
- `autodatareport/events.py`：结构化事件与运行指标。
- `autodatareport/cache.py`：图表和渲染缓存。
- `extra_metrics_service.py`：Fenxi/505 数据请求与解析。
- `pc_web_metrics_service.py`：PC Web 数据请求与解析。
- `extra_auth.py`、`auth_repair.py`：HAR 认证和自动认证修复。
- `extra_metrics_render.py`：扩展文案和 505 图片渲染。
- `feishu_doc.py`、`wecom_longbot.py`：飞书文档和企业微信发布。
- `report_launcher_gui.py`：Tkinter 一键工作台。
- `templates/`：主日报和 PC 日报模板。
- `tests/`：兼容、并发、认证、GUI、发布和运行安全测试。

## 源码运行

要求 Python 3.10+，推荐使用 `uv` 安装锁定依赖。

### Windows

```powershell
uv sync --frozen --group dev
Copy-Item config.example.yaml config.yaml
Copy-Item extra_auth.example.json extra_auth.json
uv run python report_launcher_gui.py
```

### macOS/Linux

```bash
uv sync --frozen --group dev
cp config.example.yaml config.yaml
cp extra_auth.example.json extra_auth.json
uv run python report_launcher_gui.py
```

如不使用 `uv`，也可创建 Python 虚拟环境并安装 `requirements.txt`，但正式构建和 CI 以 `uv.lock` 为准。

## 核心配置

公开仓库只提供脱敏的 `config.example.yaml`。内部分发包可在构建时携带已脱敏的平台地址和 hosts 默认值，用户自己的认证信息仍保存在运行目录。

常用配置区域：

- `base_url`、`login_url_870`、`session_cookie`：870 接口、登录页和 PHPSESSID。
- `network`：870 的 direct/system/custom 代理和 hosts 映射。
- `targets`：870 各线路查询配置。
- `extra_metrics`：Fenxi/505 地址、认证文件、代理和 hosts。
- `pc_web_metrics`：PC Web 地址、严格模式和会员指标。
- `auth_repair`：自动登录修复浏览器、独立配置目录和超时。
- `feishu_doc`：飞书应用、目录、标题和图片宽度。
- `wecom_bot`：企微机器人、单聊/群聊目标和超时。
- `schedule`：定时认证准备时间、日报时间和默认日期模式。

敏感信息优先通过运行目录配置或环境变量提供，不要提交到 Git。

## 常用命令

查看版本：

```powershell
uv run python generate_daily_report.py --version
```

生成指定日期并按配置发送：

```powershell
uv run python generate_daily_report.py --date 2026-07-20 --with-extra-metrics
```

只生成和验证，不调用飞书或企微：

```powershell
uv run python generate_daily_report.py --date 2026-07-20 --with-extra-metrics --no-publish
```

检查 870、Fenxi/505 和 PC Web 登录态：

```powershell
uv run python generate_daily_report.py --date 2026-07-20 --check-extra-auth
```

只执行认证修复和预检，不生成日报：

```powershell
uv run python generate_daily_report.py --repair-auth-only --auth-repair-target all
```

手工从 HAR 构建扩展认证：

```powershell
uv run python generate_daily_report.py `
  --build-extra-auth-only `
  --fenxi-har "C:\path\fenxi.har" `
  --manage-har "C:\path\manage.har"
```

输出给 GUI 使用的 JSONL 事件：

```powershell
uv run python generate_daily_report.py --date 2026-07-20 --event-stream jsonl --no-publish
```

完整参数以程序输出为准：

```powershell
uv run python generate_daily_report.py --help
```

## 定时任务

Windows 调度脚本位于 `scripts/run_daily_report.ps1`。先手工验证：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_report.ps1 -DateMode yesterday
```

再使用 Windows 任务计划程序调用同一脚本。定时任务会先准备或验证认证状态，再运行日报；日志写入 `output/scheduler_logs/`。

macOS 仍保留 `scripts/install_macos_launchd.sh` 和 `scripts/run_daily_report.sh`，但 V1.5 的主要分发目标是 Windows EXE。

## 测试与质量检查

```powershell
uv sync --frozen --group dev
uv run python -m ruff check .
uv run python -m pytest -q
uv run python -m compileall -q .
uv run python -m pip_audit
```

当前 V1.5 基线为 65 项测试通过。自动化测试不会访问真实内部平台，也不会向正式飞书或企微发送报告。

## Windows 构建

```powershell
.\build_exe.bat
```

构建产物：

```text
dist\windows-release-v1.5.0\
├─ autodatareport-gui.exe
├─ autodatareport-cli.exe
├─ _internal\
└─ release-manifest.json
```

构建脚本会：

- 从 `uv.lock` 同步固定构建依赖。
- 分别构建 CLI 和 GUI，再合并运行依赖。
- 保留旧版发布目录，不覆盖 V1.4。
- 生成完整文件清单、大小和 SHA-256。
- 扫描分发目录中的敏感文件名和已知敏感字段。

内部分发构建可通过 `AUTODATAREPORT_INTERNAL_CONFIG` 指定公司默认配置源。构建脚本只写入经过清理的平台默认值，不应复制个人认证信息。

需要让内部同事开箱即用飞书和企微时，必须显式生成内部发布版：

```powershell
$env:AUTODATAREPORT_INTERNAL_CONFIG = "C:\secure\company-config.yaml"
$env:AUTODATAREPORT_INCLUDE_PUBLISH_CONFIG = "1"
$env:AUTODATAREPORT_PUBLISH_ENV = "C:\secure\.env.scheduler"
.\build_exe.bat
```

内部发布版会携带组织共享的飞书应用密钥、企微机器人密钥和接收目标，但仍会清除 870 Cookie、Fenxi/PC 登录 Token、个人认证文件和历史输出。该目录只能通过内部可信渠道分发，不应上传到公开 GitHub Release。

## 常见问题

### 870 提示登录态不可用

通过 GUI 右上角“设置与修复”更新 870 登录态。默认已知登录后台使用 HTTPS；修复完成后程序仍会执行真实预检，Cookie 无效时不会继续生成伪完整日报。

### Fenxi、505 或 PC Web 首次配置失败

优先运行 GUI 的“首次设置（推荐）”。HAR 导入是手工回退方式，不建议非技术用户直接编辑 `extra_auth.json`。

### 程序提示发送结果待确认

说明远端可能已经成功，但本地没有拿到最终确认。先打开飞书或企微检查，不要直接强制重推。

### 停止任务后仍有浏览器窗口

正式日报、首次设置和自动修复会清理其进程树。若浏览器不是由程序启动，程序不会关闭用户原有浏览器会话。

### GitHub 推送连接超时

如果 Windows 系统代理指向本地端口，应确认代理客户端正在运行并实际监听该端口；Git 也需要配置相同的 `http.proxy`/`https.proxy`，否则可能回退到不可达的直连路径。

## 不应提交的文件

- `config.yaml`
- `extra_auth.json`
- `.env.scheduler`
- `hosts_*.yaml`
- `*.har`
- `output/`
- Cookie、PHPSESSID、Authorization、e_token、Admin-Token、Webhook 或其他 Token

发布包同样不得包含个人认证文件、认证日志和历史输出。
