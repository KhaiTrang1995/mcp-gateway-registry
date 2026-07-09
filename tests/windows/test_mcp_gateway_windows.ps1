# Unit tests for scripts/windows/McpGatewayWindows.ps1 (shipped helpers).
# Drive the real library functions — no reimplementation of the production logic.
#
# Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests/windows/test_mcp_gateway_windows.ps1
#
# Exit 0 on success; non-zero on failure.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$LibPath = Join-Path $RepoRoot "scripts\windows\McpGatewayWindows.ps1"
if (-not (Test-Path -LiteralPath $LibPath)) {
    Write-Error "Library not found: $LibPath"
    exit 1
}
. $LibPath

$failures = New-Object System.Collections.Generic.List[string]
$passed = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) {
        $script:passed++
        Write-Host "PASS: $Message"
    }
    else {
        $script:failures.Add($Message) | Out-Null
        Write-Host "FAIL: $Message"
    }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    if ($Expected -eq $Actual) {
        $script:passed++
        Write-Host "PASS: $Message"
    }
    else {
        $msg = "$Message (expected='$Expected' actual='$Actual')"
        $script:failures.Add($msg) | Out-Null
        Write-Host "FAIL: $msg"
    }
}

# --- Get-McpGatewayComposeArgs ---
$args = Get-McpGatewayComposeArgs
Assert-Equal 4 $args.Count "compose args length is 4"
Assert-Equal "-f" $args[0] "first flag is -f"
Assert-Equal "docker-compose.prebuilt.yml" $args[1] "prebuilt compose file"
Assert-equal "-f" $args[2] "second flag is -f"
Assert-equal "docker-compose.windows.yml" $args[3] "windows overlay file (not override)"
Assert-True ($args -notcontains "docker-compose.override.yml") "does not use auto-loaded override"

# --- Set-McpGatewayHomeEnv ---
$prevHome = $env:HOME
$prevProfile = $env:USERPROFILE
try {
    # Synthetic profile path (not a user-profile directory) so the tree never looks like a real home path
    $env:USERPROFILE = "C:\mcp-gw-test-home"
    $result = Set-McpGatewayHomeEnv
    Assert-equal "C:\mcp-gw-test-home" $result "HOME set from USERPROFILE"
    Assert-equal "C:\mcp-gw-test-home" $env:HOME "env:HOME updated"
}
finally {
    $env:HOME = $prevHome
    $env:USERPROFILE = $prevProfile
}

# --- Get/Set-McpGatewayEnvVar against real temp dotenv ---
$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("mcp-gw-win-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpDir | Out-Null
try {
    $envPath = Join-Path $tmpDir ".env"
    @(
        "# KEYCLOAK_ADMIN_PASSWORD=changeme"
        "SECRET_KEY="
        "OTHER=keep"
    ) | Set-Content -LiteralPath $envPath -Encoding utf8

    $before = Get-McpGatewayEnvVar -Path $envPath -Key "KEYCLOAK_ADMIN_PASSWORD"
    Assert-equal "changeme" $before "reads commented KEY=value"

    Set-McpGatewayEnvVar -Path $envPath -Key "KEYCLOAK_ADMIN_PASSWORD" -Value "secure-pass-1"
    $after = Get-McpGatewayEnvVar -Path $envPath -Key "KEYCLOAK_ADMIN_PASSWORD"
    Assert-equal "secure-pass-1" $after "updates commented key to active KEY=value"

    Set-McpGatewayEnvVar -Path $envPath -Key "SECRET_KEY" -Value "abc123"
    Assert-equal "abc123" (Get-McpGatewayEnvVar -Path $envPath -Key "SECRET_KEY") "updates empty SECRET_KEY"

    Set-McpGatewayEnvVar -Path $envPath -Key "NEW_KEY" -Value "newval"
    Assert-equal "newval" (Get-McpGatewayEnvVar -Path $envPath -Key "NEW_KEY") "appends missing key"

    $content = Get-Content -LiteralPath $envPath -Raw
    Assert-True ($content -match "OTHER=keep") "preserves unrelated keys"
}
finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

# --- New-McpGatewayRandomHex ---
$hex = New-McpGatewayRandomHex -ByteCount 16
Assert-equal 32 $hex.Length "hex length is 2*bytes"
Assert-True ($hex -match '^[0-9a-f]+$') "hex is lowercase hex only"
$hex2 = New-McpGatewayRandomHex -ByteCount 16
Assert-True ($hex -ne $hex2) "two hex draws are not identical (probabilistic)"

# --- Expand-McpGatewayEnvPlaceholders ---
$prevFoo = [System.Environment]::GetEnvironmentVariable("MCP_TEST_FOO", "Process")
try {
    [System.Environment]::SetEnvironmentVariable("MCP_TEST_FOO", "bar-value", "Process")
    $expanded = Expand-McpGatewayEnvPlaceholders -Content 'url=${MCP_TEST_FOO}/x and $MCP_TEST_FOO'
    Assert-equal "url=bar-value/x and bar-value" $expanded "expands both placeholder forms"
    $untouched = Expand-McpGatewayEnvPlaceholders -Content 'keep ${MISSING_XYZ} and $defs'
    Assert-True ($untouched -match '\$\{MISSING_XYZ\}') "unknown braced vars left intact"
}
finally {
    [System.Environment]::SetEnvironmentVariable("MCP_TEST_FOO", $prevFoo, "Process")
}

# --- Initialize-McpGatewayHomeLayout ---
$layoutRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mcp-gw-home-" + [guid]::NewGuid().ToString("N"))
try {
    $mcpHome = Initialize-McpGatewayHomeLayout -HomeRoot $layoutRoot
    Assert-True (Test-Path -LiteralPath (Join-Path $mcpHome "servers")) "creates servers dir"
    Assert-True (Test-Path -LiteralPath (Join-Path $mcpHome "auth_server")) "creates auth_server dir"
    Assert-True (Test-Path -LiteralPath (Join-Path $mcpHome "federation.json")) "creates federation.json"
    Assert-True (Test-Path -LiteralPath (Join-Path $mcpHome "ssl\certs")) "creates ssl/certs"
}
finally {
    Remove-Item -LiteralPath $layoutRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Export-McpGatewayServerJsonFiles: real seed path, BOM-free, json.load-compatible ---
# Mirrors registry Python: open(path, encoding="utf-8") + json.load — not utf-8-sig.
$seedTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("mcp-gw-seed-" + [guid]::NewGuid().ToString("N"))
$seedSrc = Join-Path $seedTmp "src"
$seedDst = Join-Path $seedTmp "dst"
New-Item -ItemType Directory -Path $seedSrc | Out-Null
New-Item -ItemType Directory -Path $seedDst | Out-Null
try {
    $sampleName = "sample-server.json"
    $samplePath = Join-Path $seedSrc $sampleName
    # Source without BOM; include a placeholder the export path must expand
    $prevSeed = [System.Environment]::GetEnvironmentVariable("MCP_SEED_TEST_URL", "Process")
    [System.Environment]::SetEnvironmentVariable("MCP_SEED_TEST_URL", "http://seed.example/mcp", "Process")
    $sampleJson = '{"server_name":"seed-demo","path":"${MCP_SEED_TEST_URL}","num":1}'
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($samplePath, $sampleJson, $utf8NoBom)
    # Noise file that must be skipped
    [System.IO.File]::WriteAllText((Join-Path $seedSrc "server_state.json"), '{"state":true}', $utf8NoBom)

    $written = @(Export-McpGatewayServerJsonFiles -SourceDir $seedSrc -DestDir $seedDst)
    Assert-True ($written.Count -ge 1) "export wrote at least one server json"
    $outPath = Join-Path $seedDst $sampleName
    Assert-True (Test-Path -LiteralPath $outPath) "exported sample-server.json exists"
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $seedDst "server_state.json"))) "skips server_state.json"

    $bytes = [System.IO.File]::ReadAllBytes($outPath)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    Assert-True (-not $hasBom) "exported JSON first bytes are not UTF-8 BOM (EF BB BF)"

    $text = [System.IO.File]::ReadAllText($outPath, $utf8NoBom)
    Assert-True ($text -match 'http://seed\.example/mcp') "export expands env placeholders in JSON"

    # Honest cross-check with Python json.load(encoding=utf-8) when python is available
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if ($py) {
        $pyCheck = @"
import json, sys
p = sys.argv[1]
with open(p, encoding='utf-8') as f:
    data = json.load(f)
assert data.get('server_name') == 'seed-demo', data
assert data.get('path') == 'http://seed.example/mcp', data
print('python_json_load_ok')
"@
        $pyScript = Join-Path $seedTmp "check_json.py"
        [System.IO.File]::WriteAllText($pyScript, $pyCheck, $utf8NoBom)
        $pyOut = & $py.Source $pyScript $outPath 2>&1
        $pyCode = $LASTEXITCODE
        Assert-equal 0 $pyCode "python json.load(encoding=utf-8) succeeds on exported file"
        Assert-True ("$pyOut" -match "python_json_load_ok") "python check printed success"
    }
    else {
        # Fallback: PowerShell ConvertFrom-Json still fails on BOM; without BOM it must parse
        $null = $text | ConvertFrom-Json
        Assert-True $true "ConvertFrom-Json accepts BOM-free export (python not on PATH)"
    }

    # Contrast (Windows PowerShell 5.1): Set-Content -Encoding utf8 emits BOM — the bug class we fixed.
    # PowerShell 7+ often writes UTF-8 without BOM; only assert the control on 5.x.
    if ($PSVersionTable.PSVersion.Major -le 5) {
        $bomPath = Join-Path $seedDst "bom-contrast.json"
        Set-Content -LiteralPath $bomPath -Value '{"x":1}' -Encoding utf8
        $bomBytes = [System.IO.File]::ReadAllBytes($bomPath)
        $setContentHasBom = ($bomBytes.Length -ge 3 -and $bomBytes[0] -eq 0xEF -and $bomBytes[1] -eq 0xBB -and $bomBytes[2] -eq 0xBF)
        Assert-True $setContentHasBom "control: PS 5.1 Set-Content -Encoding utf8 emits BOM"
    }
}
finally {
    if ($null -ne $prevSeed) {
        [System.Environment]::SetEnvironmentVariable("MCP_SEED_TEST_URL", $prevSeed, "Process")
    }
    else {
        [System.Environment]::SetEnvironmentVariable("MCP_SEED_TEST_URL", $null, "Process")
    }
    Remove-Item -LiteralPath $seedTmp -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Structural: start.ps1 references overlay and safe defaults ---
$startPath = Join-Path $RepoRoot "start.ps1"
$startText = Get-Content -LiteralPath $startPath -Raw
Assert-True ($startText -match "docker-compose\.windows\.yml") "start.ps1 references windows overlay"
Assert-True ($startText -notmatch '(?i)-f\s+["'']?docker-compose\.override\.yml') "start.ps1 does not pass override.yml to compose"
Assert-True ($startText -match "ResetData") "start.ps1 exposes explicit ResetData"
Assert-True ($startText -match "down --remove-orphans") "safe path uses down without requiring volumes wipe"
Assert-True ($startText -match 'if \(\$ResetData\)') "ResetData gates destructive branch"
# Plain-text policy: no emoji / party-popper from the prototype script
Assert-True ($startText -notmatch "SUCCESS!") "start.ps1 success banner is plain SUCCESS"
Assert-True ($startText -notmatch "Autostart") "start.ps1 uses professional title (not Autostart prototype)"
# Security: success banner must not interpolate secret env values into the console
Assert-True ($startText -notmatch 'admin\s*/\s*\$finalAdmin') "start.ps1 does not echo admin password value"
Assert-True ($startText -match 'passwords are in \.env') "start.ps1 points operators at .env for secrets"
Assert-True ($startText -notmatch '(?i)Users\\[A-Za-z0-9._-]+') "start.ps1 has no user-profile path literals"

$overlayPath = Join-Path $RepoRoot "docker-compose.windows.yml"
Assert-True (Test-Path -LiteralPath $overlayPath) "docker-compose.windows.yml exists"
$overlayText = Get-Content -LiteralPath $overlayPath -Raw
Assert-True ($overlayText -match "ai_registry_logs") "overlay defines named log volume"

$gitattributes = Join-Path $RepoRoot ".gitattributes"
Assert-True (Test-Path -LiteralPath $gitattributes) ".gitattributes exists"
$ga = Get-Content -LiteralPath $gitattributes -Raw
Assert-True ($ga -match '\*\.sh\s+text\s+eol=lf') ".gitattributes forces LF for .sh"
Assert-True ($ga -match '\*\.ps1\s+text\s+eol=crlf') ".gitattributes forces CRLF for .ps1"

Write-Host ""
Write-Host "Passed: $passed  Failed: $($failures.Count)"
if ($failures.Count -gt 0) {
    Write-Host "Failures:"
    $failures | ForEach-Object { Write-Host " - $_" }
    exit 1
}
exit 0
