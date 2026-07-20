# fenxi/PC 登录态低摩擦自动修复

## Scope

第一版只自动处理 `fenxi` 与 `pc_web` 登录态失效。`870` 与 `505` 失败不会触发自动登录态修复，只会保留明确失败原因。

## Runtime Flow

1. 08:50 执行认证预修复：`scripts/run_auth_prep.ps1`。
2. 09:00 执行昨日日报：`scripts/run_daily_report.ps1 -DateMode yesterday`。
3. 日报预检或正式任务遇到 fenxi/PC 登录态失败时，脚本会打开专用 Chrome。
4. 你在 Chrome 里完成手机验证码登录。
5. 系统捕获 `fenxi e_token`、`PC Admin-Token/Bearer/chain`，写回 `extra_auth.json`。
6. 写回后重新预检，通过后继续原日期日报生成与推送。

## Defaults

- Chrome: `C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`
- Profile: `output/auth_profiles/chrome_daily_report`
- Repair timeout: `300` seconds
- Repair targets: `fenxi`, `pc_web`
- Repair logs: `output/auth_repair_logs/auth_repair_YYYYMMDD_HHMMSS.log`
- Run state: `output/run_state/YYYYMMDD.json`

## CLI

```powershell
python .\generate_daily_report.py --check-extra-auth --repair-auth-on-failure --date 2026-02-20
python .\generate_daily_report.py --repair-auth-only --with-extra-metrics --auth-repair-target both
```

Supported repair flags:

- `--repair-auth-on-failure`
- `--repair-auth-only`
- `--auth-repair-browser chrome`
- `--auth-repair-profile output/auth_profiles/chrome_daily_report`
- `--auth-repair-timeout-seconds 300`
- `--auth-repair-target auto|fenxi|pc_web|both`

## Windows Task Scheduler

```powershell
schtasks /Create /SC DAILY /TN "AutoDataReportAuthPrep" /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\autodatareport\scripts\run_auth_prep.ps1" /ST 08:50
schtasks /Create /SC DAILY /TN "AutoDataReportDaily" /TR "powershell -ExecutionPolicy Bypass -File C:\path\to\autodatareport\scripts\run_daily_report.ps1 -DateMode yesterday" /ST 09:00
```

## Guardrails

- Agent coordinator only records failure context and runs the deterministic auth repair runbook.
- Known fenxi/PC auth failures do not depend on LLM free-form decisions.
- The repair runbook only updates `fenxi` and `pc_web` blocks in `extra_auth.json`; existing `505` auth is preserved.
- Unknown failures are left as diagnosis context instead of triggering browser login.
