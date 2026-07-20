param(
  [string]$ReportDate = "",
  [string]$DateMode = "yesterday", # today | yesterday
  [string]$ConfigPath = "",
  [int]$MaxRetries = 3,
  [int]$RetryDelaySeconds = 300,
  [switch]$NoPushFeishuDoc,
  [switch]$VerifyFeishuContent,
  [switch]$SkipAuthCheck,
  [switch]$RepairAuthOnFailure,
  [switch]$RepairAuthOnly,
  [string]$AuthRepairBrowser = "",
  [string]$AuthRepairProfile = "",
  [int]$AuthRepairTimeoutSeconds = 300,
  [string]$AuthRepairTarget = "auto"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
  $ConfigPath = Join-Path $RootDir "config.yaml"
}

$EnvFile = Join-Path $RootDir ".env.scheduler"
if (Test-Path $EnvFile) {
  Get-Content -Path $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not [string]::IsNullOrWhiteSpace($line) -and (-not $line.StartsWith("#")) -and $line.Contains("=")) {
      $parts = $line.Split("=", 2)
      $key = $parts[0].Trim()
      $value = $parts[1].Trim().Trim('"').Trim("'")
      if ($key -and [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($key, "Process"))) {
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
      }
    }
  }
}

$PythonBin = Join-Path $RootDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonBin)) {
  $PythonBin = "python"
}

$LogDir = Join-Path $RootDir "output\scheduler_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ([string]::IsNullOrWhiteSpace($ReportDate)) {
  if ($DateMode -eq "yesterday") {
    $ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd")
  } else {
    $ReportDate = (Get-Date).ToString("yyyy-MM-dd")
  }
}

$Now = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = Join-Path $LogDir "daily_$Now.log"

function Write-Log {
  param([string]$Text)
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
  Write-Output $line
  Add-Content -Path $LogFile -Value $line
}

function Invoke-PythonLogged {
  param([object[]]$CommandArgs)
  $previousErrorActionPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "Continue"
    & $PythonBin @CommandArgs 2>&1 | ForEach-Object {
      $line = "$_"
      [Console]::Out.WriteLine($line)
      Add-Content -Path $LogFile -Value $line
    }
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }
}

function Test-Truthy {
  param([string]$Value)
  if ($null -eq $Value) {
    $Value = ""
  }
  $normalized = $Value.Trim().ToLowerInvariant()
  return @("1", "true", "yes", "on") -contains $normalized
}

$RepairEnabled = $RepairAuthOnFailure -or (Test-Truthy $env:RUN_AUTH_REPAIR) -or (Test-Truthy $env:AUTH_REPAIR_ENABLED)
$RepairArgs = @()
if ($RepairEnabled) {
  $RepairArgs += "--repair-auth-on-failure"
}
$ResolvedAuthRepairBrowser = if ([string]::IsNullOrWhiteSpace($AuthRepairBrowser)) { $env:AUTH_REPAIR_BROWSER } else { $AuthRepairBrowser }
$ResolvedAuthRepairProfile = if ([string]::IsNullOrWhiteSpace($AuthRepairProfile)) { $env:AUTH_REPAIR_PROFILE } else { $AuthRepairProfile }
$ResolvedAuthRepairTarget = if ($PSBoundParameters.ContainsKey("AuthRepairTarget")) { $AuthRepairTarget } elseif (-not [string]::IsNullOrWhiteSpace($env:AUTH_REPAIR_TARGET)) { $env:AUTH_REPAIR_TARGET } else { $AuthRepairTarget }
$ResolvedAuthRepairTimeout = if ($env:AUTH_REPAIR_TIMEOUT_SECONDS) { [int]$env:AUTH_REPAIR_TIMEOUT_SECONDS } else { $AuthRepairTimeoutSeconds }
if (-not [string]::IsNullOrWhiteSpace($ResolvedAuthRepairBrowser)) {
  $RepairArgs += @("--auth-repair-browser", $ResolvedAuthRepairBrowser)
}
if (-not [string]::IsNullOrWhiteSpace($ResolvedAuthRepairProfile)) {
  $RepairArgs += @("--auth-repair-profile", $ResolvedAuthRepairProfile)
}
if (-not [string]::IsNullOrWhiteSpace($ResolvedAuthRepairTarget)) {
  $RepairArgs += @("--auth-repair-target", $ResolvedAuthRepairTarget)
}
$RepairArgs += @("--auth-repair-timeout-seconds", "$ResolvedAuthRepairTimeout")

Write-Log "start daily report, date=$ReportDate"
Write-Log "root=$RootDir"
Write-Log "config=$ConfigPath"

if ($RepairAuthOnly) {
  Write-Log "auth repair only..."
  $cmd = @(
    (Join-Path $RootDir "generate_daily_report.py"),
    "--config", $ConfigPath,
    "--date", $ReportDate,
    "--with-extra-metrics",
    "--repair-auth-only"
  ) + $RepairArgs
  $exitCode = Invoke-PythonLogged -CommandArgs $cmd
  if ($exitCode -ne 0) {
    throw "auth repair failed"
  }
  Write-Log "auth repair success"
  exit 0
}

if (-not $SkipAuthCheck) {
  Write-Log "auth precheck..."
  $authCmd = @(
    (Join-Path $RootDir "generate_daily_report.py"),
    "--config", $ConfigPath,
    "--check-extra-auth",
    "--date", $ReportDate
  ) + $RepairArgs
  $exitCode = Invoke-PythonLogged -CommandArgs $authCmd
  if ($exitCode -ne 0) {
    throw "auth precheck failed"
  }
}

$attempt = 1
while ($attempt -le $MaxRetries) {
  Write-Log "run attempt $attempt/$MaxRetries"
  $cmd = @(
    (Join-Path $RootDir "generate_daily_report.py"),
    "--config", $ConfigPath,
    "--date", $ReportDate,
    "--with-extra-metrics"
  ) + $RepairArgs
  if ($NoPushFeishuDoc) {
    $cmd += "--no-push-feishu-doc"
  }
  if ($VerifyFeishuContent) {
    $cmd += "--verify-feishu-content"
  }
  $exitCode = Invoke-PythonLogged -CommandArgs $cmd
  if ($exitCode -eq 0) {
    Write-Log "success"
    exit 0
  }
  if ($attempt -lt $MaxRetries) {
    Write-Log "failed, sleep ${RetryDelaySeconds}s then retry"
    Start-Sleep -Seconds $RetryDelaySeconds
  }
  $attempt += 1
}

throw "all retries failed"
