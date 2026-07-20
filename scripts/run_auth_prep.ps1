param(
  [string]$ReportDate = "",
  [string]$DateMode = "yesterday",
  [string]$ConfigPath = "",
  [int]$AuthRepairTimeoutSeconds = 300,
  [string]$AuthRepairTarget = "both"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$argsForDaily = @{
  DateMode = $DateMode
  RepairAuthOnly = $true
  AuthRepairTimeoutSeconds = [int]$AuthRepairTimeoutSeconds
  AuthRepairTarget = $AuthRepairTarget
}
if (-not [string]::IsNullOrWhiteSpace($ReportDate)) {
  $argsForDaily["ReportDate"] = $ReportDate
}
if (-not [string]::IsNullOrWhiteSpace($ConfigPath)) {
  $argsForDaily["ConfigPath"] = $ConfigPath
}

& (Join-Path $ScriptDir "run_daily_report.ps1") @argsForDaily
