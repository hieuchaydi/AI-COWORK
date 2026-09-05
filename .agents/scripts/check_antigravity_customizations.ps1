<#
.SYNOPSIS
  Validates Antigravity workspace customizations are loadable.
.DESCRIPTION
  Checks:
  - Root AGENTS.md exists
  - .agents/plugins.json and .agents/skills.json are valid JSON
  - Required plugin directories/files exist
  - agy.exe is available at %USERPROFILE%\.gemini\bin\agy.exe
  - agy plugin validate passes for auto-execute and media-scrape-autopilot
  - agy plugin list includes both plugin names
  - agy --add-dir <workspace> agent includes code_builder
  Prints a concise PASS/FAIL report and exits nonzero on failure.
#>
[CmdletBinding()]
param(
  [string]$WorkspaceRoot,
  [string]$AgyPath
)

$ErrorActionPreference = "Continue"

if (-not $WorkspaceRoot) {
  if ($PSScriptRoot) {
    $WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
  } else {
    $WorkspaceRoot = (Get-Location).Path
  }
} else {
  $WorkspaceRoot = (Resolve-Path $WorkspaceRoot).Path
}

if (-not $AgyPath) {
  $AgyPath = Join-Path $env:USERPROFILE ".gemini\bin\agy.exe"
}

$results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Record-Check {
  param(
    [string]$Name,
    [bool]$Passed,
    [string]$Details = ""
  )
  $status = if ($Passed) { "PASS" } else { "FAIL" }
  $results.Add([PSCustomObject]@{
    Check   = $Name
    Passed  = $Passed
    Status  = $status
    Details = $Details
  })
}

# 1. Root AGENTS.md exists
$rootAgentsPath = Join-Path $WorkspaceRoot "AGENTS.md"
$hasRootAgents = Test-Path -LiteralPath $rootAgentsPath
Record-Check -Name "Root AGENTS.md exists" -Passed $hasRootAgents -Details $rootAgentsPath

# 2. .agents/plugins.json and .agents/skills.json are valid JSON
$pluginsJsonPath = Join-Path $WorkspaceRoot ".agents\plugins.json"
$pluginsJsonValid = $false
if (Test-Path -LiteralPath $pluginsJsonPath) {
  try {
    $null = Get-Content -LiteralPath $pluginsJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $pluginsJsonValid = $true
  } catch {
    $pluginsJsonValid = $false
  }
}
Record-Check -Name ".agents/plugins.json is valid JSON" -Passed $pluginsJsonValid -Details $pluginsJsonPath

$skillsJsonPath = Join-Path $WorkspaceRoot ".agents\skills.json"
$skillsJsonValid = $false
if (Test-Path -LiteralPath $skillsJsonPath) {
  try {
    $null = Get-Content -LiteralPath $skillsJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    $skillsJsonValid = $true
  } catch {
    $skillsJsonValid = $false
  }
}
Record-Check -Name ".agents/skills.json is valid JSON" -Passed $skillsJsonValid -Details $skillsJsonPath

# 3. Required plugin directories and files exist
$requiredPaths = @(
  ".agents\plugins\auto-execute",
  ".agents\plugins\auto-execute\plugin.json",
  ".agents\plugins\auto-execute\hooks.json",
  ".agents\plugins\auto-execute\allow.py",
  ".agents\plugins\auto-execute\rules\AGENTS.md",
  ".agents\plugins\auto-execute\skills\review\SKILL.md",
  ".agents\plugins\media-scrape-autopilot",
  ".agents\plugins\media-scrape-autopilot\plugin.json",
  ".agents\plugins\media-scrape-autopilot\hooks.json",
  ".agents\plugins\media-scrape-autopilot\allow.py",
  ".agents\plugins\media-scrape-autopilot\rules\AGENTS.md",
  ".agents\plugins\media-scrape-autopilot\rules\media-scrape-autopilot.md",
  ".agents\plugins\media-scrape-autopilot\skills\media-scrape-bundle\SKILL.md",
  ".agents\agents\code_builder\agent.md",
  ".agents\rules\AGENTS.md"
)

$missingRequired = @()
foreach ($rel in $requiredPaths) {
  $target = Join-Path $WorkspaceRoot $rel
  if (-not (Test-Path -LiteralPath $target)) {
    $missingRequired += $rel
  }
}
$requiredOk = ($missingRequired.Count -eq 0)
$requiredDetails = if ($requiredOk) { "All required plugin files and directories exist" } else { "Missing: " + ($missingRequired -join ", ") }
Record-Check -Name "Required plugin directories and files exist" -Passed $requiredOk -Details $requiredDetails

# 4. agy.exe is available at %USERPROFILE%\.gemini\bin\agy.exe
$hasAgy = Test-Path -LiteralPath $AgyPath
Record-Check -Name "agy.exe is available at %USERPROFILE%\.gemini\bin\agy.exe" -Passed $hasAgy -Details $AgyPath

# 5. agy plugin validate passes for auto-execute and media-scrape-autopilot
$validateAutoExecuteOk = $false
$validateMediaOk = $false
$exitCodeAuto = -1
$exitCodeMedia = -1
if ($hasAgy) {
  $autoExecuteDir = Join-Path $WorkspaceRoot ".agents\plugins\auto-execute"
  $outAuto = & $AgyPath plugin validate $autoExecuteDir 2>&1
  $exitCodeAuto = $LASTEXITCODE
  $validateAutoExecuteOk = ($exitCodeAuto -eq 0)

  $mediaDir = Join-Path $WorkspaceRoot ".agents\plugins\media-scrape-autopilot"
  $outMedia = & $AgyPath plugin validate $mediaDir 2>&1
  $exitCodeMedia = $LASTEXITCODE
  $validateMediaOk = ($exitCodeMedia -eq 0)
}
Record-Check -Name "agy plugin validate passes for auto-execute" -Passed $validateAutoExecuteOk -Details "ExitCode: $exitCodeAuto"
Record-Check -Name "agy plugin validate passes for media-scrape-autopilot" -Passed $validateMediaOk -Details "ExitCode: $exitCodeMedia"

# 6. agy plugin list includes both plugin names
$pluginListIncludesBoth = $false
if ($hasAgy) {
  $listRaw = & $AgyPath plugin list 2>&1
  $listText = ($listRaw -join "`n")
  $hasAutoInList = $false
  $hasMediaInList = $false
  try {
    $listJson = $listText | ConvertFrom-Json -ErrorAction Stop
    $names = @($listJson.imports | ForEach-Object { $_.name })
    $hasAutoInList = $names -contains "auto-execute"
    $hasMediaInList = $names -contains "media-scrape-autopilot"
  } catch {
    $hasAutoInList = $listText -match "auto-execute"
    $hasMediaInList = $listText -match "media-scrape-autopilot"
  }
  $pluginListIncludesBoth = ($hasAutoInList -and $hasMediaInList)
}
Record-Check -Name "agy plugin list includes both plugin names" -Passed $pluginListIncludesBoth -Details "auto-execute and media-scrape-autopilot"

# 7. agy --add-dir <workspace> agent includes code_builder
$agentIncludesCodeBuilder = $false
if ($hasAgy) {
  $agentRaw = & $AgyPath --add-dir $WorkspaceRoot agent 2>&1
  $agentText = ($agentRaw -join "`n")
  if ($agentText -match "code_builder") {
    $agentIncludesCodeBuilder = $true
  }
}
Record-Check -Name "agy --add-dir <workspace> agent includes code_builder" -Passed $agentIncludesCodeBuilder -Details "code_builder"

# Print concise report
Write-Host ""
Write-Host "Antigravity Workspace Customizations Verification Report"
Write-Host "=========================================================="
$failCount = 0
foreach ($r in $results) {
  if ($r.Passed) {
    Write-Host ("[{0}] {1}" -f $r.Status, $r.Check) -ForegroundColor Green
  } else {
    $failCount++
    Write-Host ("[{0}] {1} ({2})" -f $r.Status, $r.Check, $r.Details) -ForegroundColor Red
  }
}
Write-Host "=========================================================="
$total = $results.Count
$passedCount = $total - $failCount
if ($failCount -eq 0) {
  Write-Host ("Result: PASS ({0}/{1} checks passed)" -f $passedCount, $total) -ForegroundColor Green
  exit 0
} else {
  Write-Host ("Result: FAIL ({0}/{1} checks passed, {2} failed)" -f $passedCount, $total, $failCount) -ForegroundColor Red
  exit 1
}
