# 云游戏日报自动化工具（V1.5）

自动抓取多个后台数据统计，并制作可视化表格，渲染合并成一整份完整的数据日报

## 项目功能
- 拉取后台多条线路数据，输出总览日报与 PC 日报。
- 自动计算并发峰值、排队峰值、排队时段，并生成趋势图。
- 拉取 后台2 扩展指标：新增、活跃、会员付费率、会员充值、会员数。
- 拉取 后台3付费数据并输出两张固定样式图片：页游对比表、手游双列榜单表。
- 将以上内容整合到同一份日报文本里。

## 项目结构
- `generate_daily_report.py`: 主入口（单命令跑完整日报）。
- `extra_metrics_service.py`: fenxi/505 数据抓取与解析。
- `extra_metrics_render.py`: 扩展文案渲染 + 505 图片渲染。
- `extra_auth.py`: 从 HAR 构建并读取扩展认证信息。
- `network_hosts.py`: hosts 映射重写。
- `templates/`: 日报模板。

## 快速开始
建议 Python 3.10+。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

复制本地配置文件：

```bash
cp config.example.yaml config.yaml
cp hosts_870.example.yaml hosts_870.yaml
cp hosts_505.example.yaml hosts_505.yaml
cp extra_auth.example.json extra_auth.json
```

## 核心配置说明
`config.yaml` 重点字段：
- `base_url`: 870 接口地址。
- `session_cookie`: 870 登录态（`PHPSESSID=...`）。
- `network.hosts_yaml_path`: 870 hosts 文件路径。
- `targets`: 870 线路配置（总/页游/主机/手游/原神等）。
- `extra_metrics.enabled`: 是否启用 fenxi+505 扩展数据。
- `extra_metrics.fenxi_base`: fenxi 基地址。
- `extra_metrics.manage_base`: 505 基地址。
- `extra_metrics.hosts_yaml_path`: 505 hosts 文件路径。
- `feishu_doc.enabled`: 是否推送到飞书文档（不填时默认开启）。
- `feishu_doc.app_id` / `feishu_doc.app_secret`: 飞书自建应用凭证（建议用环境变量提供）。
- `feishu_doc.folder_token`: 文档创建目录（可选）。
- `feishu_doc.image_width` / `feishu_doc.narrow_image_width`: 飞书图片展示宽度（默认 `960/760`）。
- `feishu_doc.prevent_upscale`: 是否禁止小图放大（默认 `true`，推荐保持开启，避免变形）。

## 运行方式
### GUI 启动器（推荐）
你可以直接用桌面按钮运行，不需要命令行。

macOS：
```bash
chmod +x scripts/start_gui.command
./scripts/start_gui.command
```

Windows：
- 双击 `scripts/start_gui.bat`

启动器功能：
- 默认选择昨天，点击一次 `生成并发送昨天日报` 即可执行完整流程。
- 也可切换今天或自定义日期；`本次仅生成，不发送` 会关闭本次所有外部发布。
- 主界面直接展示任务状态、执行摘要和结果入口，成功后可打开主日报、PC 日报或输出文件夹。
- 技术日志默认折叠，任务失败时自动展开；运行中可按 `Esc` 停止。
- 高级运行选项、强制重推与登录修复统一放在右上角菜单，减少日常操作干扰。

可选配置：
- `config.yaml` 可增加 `login_url_870`，用于覆盖默认登录跳转地址。

### 命令行
生成完整日报（870 + 扩展 + 图表）：

```bash
./.venv/bin/python generate_daily_report.py --date 2026-02-20 --with-extra-metrics
```

说明：`--with-extra-metrics` 现在会先做 fenxi/505 登录态预检，预检失败会直接中止，避免输出缺失扩展数据的“伪完整”日报。

生成完整日报（默认会推送飞书文档）：

```bash
FEISHU_APP_ID="cli_xxx" \
FEISHU_APP_SECRET="xxx" \
./.venv/bin/python generate_daily_report.py --date 2026-02-20 --with-extra-metrics
```

可选参数：
- `--no-push-feishu-doc`: 本次运行禁用飞书推送。
- `--feishu-folder-token`: 指定飞书目录 token。
- `--feishu-doc-title`: 指定文档标题（不传则按 `title_prefix_YYYYMMDD` 自动生成）。
- `--feishu-doc-url-prefix`: 自定义结果链接前缀（默认 `https://www.feishu.cn/docx/`）。
- `--verify-feishu-content`: 推送后调用 `docs/v1/content` 拉回 markdown 做内容校验（需权限 `docs:document.content:read`）。

仅推送已有报告文件（快速验证，不重跑数据）：

```bash
FEISHU_APP_ID="cli_xxx" \
FEISHU_APP_SECRET="xxx" \
./.venv/bin/python generate_daily_report.py --push-report-file ./output/2026220_report.txt --date 2026-02-20
```

每日登录态建议流程（手机验证码登录后）：

```bash
# 1) 用最新 HAR 刷新扩展认证
./.venv/bin/python generate_daily_report.py \
  --build-extra-auth \
  --fenxi-har "/path/to/fenxi.har" \
  --manage-har "/path/to/manage.har"

# 2) 只做扩展登录态预检（不跑870）
./.venv/bin/python generate_daily_report.py --check-extra-auth --date 2026-02-20

# 3) 预检通过后再跑正式日报
./.venv/bin/python generate_daily_report.py --date 2026-02-20 --with-extra-metrics
```

也可以先一条命令刷新并预检：

```bash
./.venv/bin/python generate_daily_report.py \
  --build-extra-auth \
  --check-extra-auth \
  --date 2026-02-20
```

`--extra-auth-max-age-hours` 可设置认证文件老化阈值（默认 24 小时）：

```bash
./.venv/bin/python generate_daily_report.py --check-extra-auth --extra-auth-max-age-hours 24
```

## 定时任务（稳定每日推送飞书）
前提：你每天先完成一次手机验证码登录，刷新当天登录态（`session_cookie` + `extra_auth.json`）。

### macOS（launchd）
1. 准备调度环境变量：
```bash
cp .env.scheduler.example .env.scheduler
```
填入真实 `FEISHU_APP_ID/FEISHU_APP_SECRET`。

2. 给脚本执行权限：
```bash
chmod +x scripts/run_daily_report.sh scripts/install_macos_launchd.sh
```

3. 安装每天定时任务（示例：每天 09:10）：
```bash
./scripts/install_macos_launchd.sh 9 10
```

4. 手动触发一次测试：
```bash
launchctl kickstart -k gui/$(id -u)/com.starfish.autodatareport.daily
```

日志位置：
- `output/scheduler_logs/launchd_stdout.log`
- `output/scheduler_logs/launchd_stderr.log`
- `output/scheduler_logs/daily_*.log`

### Windows（任务计划程序）
1. 先手动验证一次脚本：
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_report.ps1 -DateMode today
```

2. 创建每日任务（示例：每天 09:10）：
```powershell
schtasks /Create /SC DAILY /TN "AutoDataReportDaily" /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\autodatareport\scripts\run_daily_report.ps1 -DateMode today" /ST 09:10
```

3. 立即执行测试：
```powershell
schtasks /Run /TN "AutoDataReportDaily"
```

日志位置：
- `output\scheduler_logs\daily_*.log`

脚本特性（macOS/Windows）：
- 自动做扩展登录态预检（失败直接终止，避免推送错误日报）。
- 失败自动重试（默认 3 次，间隔 300 秒）。
- 带运行日志，便于定位问题。
- macOS 脚本带并发锁，避免重复触发时重入。

## 打包分发（macOS 双版本）
本项目当前提供两种 macOS 分发包：
- `CLI`：命令行版（适合服务器/脚本调用）
- `GUI`：桌面点击版（适合运营同学）

一键构建：

```bash
chmod +x scripts/build_release_macos.sh
./scripts/build_release_macos.sh
```

构建产物目录：
- `dist/releases/<timestamp>/autodatareport-cli-macos.zip`
- `dist/releases/<timestamp>/autodatareport-gui-macos.zip`

说明：
- 打包时只包含 `config.example.yaml`，不会打包本地 `config.yaml`、`extra_auth.json` 等敏感文件。
- Windows `.exe` 需在 Windows 环境打包（保留 `build_exe.bat` 流程）。

### Windows V1.5 一键工作台与运行数据

执行 `build_exe.bat` 会按 `uv.lock` 构建 `dist/windows-release-v1.5.0/`，并生成版本、完整文件清单、文件大小、SHA-256 和敏感文件扫描结果。V1.4 发布目录会完整保留，发布目录不会复制本机的 `config.yaml`、`.env.scheduler`、`extra_auth.json` 或历史输出。

打包版把可变数据放在 `%LOCALAPPDATA%\AutoDataReport\`。首次启动会从旧 `windows-release` 复制已有配置（保留原文件），没有旧配置时则从示例创建。自动任务仍默认推送；本地验证可加 `--no-publish`，手工明确重推可加 `--force-publish`。相同日期、相同内容的成功发布会记录在 `output/publish_state/` 并在重试时跳过。

V1.5 GUI 默认选择昨天、完整数据、自动发送和登录失效自动修复，正常使用只需点击一次“生成并发送昨天日报”。正式日报、首次设置和自动修复统一受停止按钮与窗口关闭逻辑管理。若发送中断且无法确认远端结果，程序会阻止自动重发，并让用户检查后选择“标记完成”或“确认未发送后重试”。

每次执行会在 `output/run_metrics/` 写入阶段耗时、请求数、重试数和图表缓存命中数据。870、扩展指标和 PC 数据会在登录预检后并行采集；主日报与 PC 日报共享一次飞书 tenant token 并行创建，单文档图片上传并发上限为 3。

发布状态继续使用 `output/publish_state/YYYYMMDD.json`，V1.5 schema 支持 `pending`、`publishing`、`failed`、`uncertain` 和 `completed`，无需安装数据库。旧版 completed 状态会自动兼容。

仅跑 870 主报告：

```bash
./.venv/bin/python generate_daily_report.py --date 2026-02-20
```

首次接入扩展时，用 HAR 生成认证文件：

```bash
./.venv/bin/python generate_daily_report.py \
  --build-extra-auth \
  --fenxi-har "/path/to/fenxi1.har" \
  --fenxi-har "/path/to/fenxi2.har" \
  --manage-har "/path/to/manage505.har"
```

## 产物输出
- `output/YYYYMMDD_report.txt`: 总览日报（含扩展备注与图片路径）。
- `output/YYYYMMDD_pc_report.txt`: PC 云游戏日报。
- `output/charts/*.png`: 870 图表 + 505 两张付费表图。
  - `505_page_payment_table_YYYYMMDD.png`
  - `505_mobile_payment_table_YYYYMMDD.png`

## 常见问题
- 870 返回登录页：`session_cookie` 失效，重新登录后更新 `config.yaml`。
- fenxi/505 失败：检查 `extra_auth.json`、hosts 配置、是否需要重新抓 HAR。
- 扩展登录态当天可用但次日失效：先手机验证码登录，再执行 `--build-extra-auth` + `--check-extra-auth`。
- 无法弹 GUI：无图形环境是正常现象，脚本会自动跳过 GUI 输入框。

## 安全注意
- 不要提交这些本地敏感文件：`config.yaml`、`extra_auth.json`、`hosts_*.yaml`、`*.har`、`output/`。
