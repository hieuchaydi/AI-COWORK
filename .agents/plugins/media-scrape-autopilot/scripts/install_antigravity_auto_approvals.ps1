param(
  [switch]$Wildcard = $true
)

$ErrorActionPreference = "Stop"

$configRoot = Join-Path $env:USERPROFILE ".gemini\config"
$globalPath = Join-Path $configRoot "config.json"
$projectsDir = Join-Path $configRoot "projects"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Read-JsonObject($Path) {
  if (Test-Path -LiteralPath $Path) {
    $raw = Get-Content -LiteralPath $Path -Raw
    if ($raw.Trim()) {
      return $raw | ConvertFrom-Json
    }
  }
  return [pscustomobject]@{}
}

function Ensure-ObjectProperty($Object, [string]$Name) {
  if (-not ($Object.PSObject.Properties.Name -contains $Name) -or $null -eq $Object.$Name) {
    $Object | Add-Member -Force -NotePropertyName $Name -NotePropertyValue ([pscustomobject]@{})
  }
}

function Ensure-ArrayProperty($Object, [string]$Name) {
  if (-not ($Object.PSObject.Properties.Name -contains $Name) -or $null -eq $Object.$Name) {
    $Object | Add-Member -Force -NotePropertyName $Name -NotePropertyValue @()
  }
}

function Merge-Allow($Holder, [string[]]$Rules) {
  Ensure-ArrayProperty $Holder "allow"
  $current = @($Holder.allow)
  foreach ($rule in $Rules) {
    if ($current -notcontains $rule) {
      $current += $rule
    }
  }
  $Holder.allow = $current
}

function Write-JsonObject($Object, [string]$Path) {
  $parent = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  if (Test-Path -LiteralPath $Path) {
    Copy-Item -LiteralPath $Path -Destination "$Path.bak-$stamp"
  }
  $Object | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $Path -Encoding UTF8
}

$rules = if ($Wildcard) {
  @("command(*)", "read_file(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "mcp(*)")
} else {
  @(
    "command(git)",
    "command(python)",
    "command(pytest)",
    "command(.venv\\Scripts\\python.exe)",
    "command(npm)",
    "command(node)",
    "read_url(*)",
    "execute_url(*)"
  )
}

$global = Read-JsonObject $globalPath
Ensure-ObjectProperty $global "userSettings"
$global.userSettings | Add-Member -Force -NotePropertyName toolPermission -NotePropertyValue "always-proceed"
$global.userSettings | Add-Member -Force -NotePropertyName artifactReviewPolicy -NotePropertyValue "always-proceed"
Ensure-ObjectProperty $global.userSettings "globalPermissionGrants"
Merge-Allow $global.userSettings.globalPermissionGrants $rules
Write-JsonObject $global $globalPath

if (Test-Path -LiteralPath $projectsDir) {
  Get-ChildItem -LiteralPath $projectsDir -Filter "*.json" | ForEach-Object {
    $project = Read-JsonObject $_.FullName
    Ensure-ObjectProperty $project "settings"
    $project.settings | Add-Member -Force -NotePropertyName toolPermission -NotePropertyValue "always-proceed"
    $project.settings | Add-Member -Force -NotePropertyName artifactReviewPolicy -NotePropertyValue "always-proceed"
    $project.settings | Add-Member -Force -NotePropertyName allowNonWorkspaceAccess -NotePropertyValue "allow"
    $project.settings | Add-Member -Force -NotePropertyName internetAccess -NotePropertyValue "allow"

    Ensure-ObjectProperty $project "permissionGrants"
    Ensure-ObjectProperty $project.permissionGrants "permissionGrants"
    Merge-Allow $project.permissionGrants.permissionGrants $rules
    Write-JsonObject $project $_.FullName
  }
}

Write-Output "Antigravity auto approvals installed. Restart Antigravity to reload live sessions."
