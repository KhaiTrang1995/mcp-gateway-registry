# Shared helpers for Windows-native MCP Gateway local deploy.
# Dot-sourced by start.ps1 and by tests/windows/test_mcp_gateway_windows.ps1.
# Keep pure (no Docker side effects) so unit tests stay cheap and deterministic.

Set-StrictMode -Version Latest

function Get-McpGatewayRepoRoot {
    <#
    .SYNOPSIS
        Resolve the repository root from this script's location.
    #>
    param(
        [string]$StartPath = $PSScriptRoot
    )
    # scripts/windows -> repo root is two levels up
    $candidate = (Resolve-Path (Join-Path $StartPath "../..")).Path
    return $candidate
}


function Set-McpGatewayHomeEnv {
    <#
    .SYNOPSIS
        Ensure HOME is set for docker compose ${HOME}/mcp-gateway binds on Windows.
    .DESCRIPTION
        Docker Compose interpolates ${HOME}. On Windows USERPROFILE is the
        correct home directory; HOME may be unset or point elsewhere.
    #>
    param(
        [string]$UserProfile = $env:USERPROFILE
    )
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        throw "USERPROFILE is empty; cannot set HOME for compose mounts."
    }
    $env:HOME = $UserProfile
    return $env:HOME
}


function Get-McpGatewayComposeArgs {
    <#
    .SYNOPSIS
        Return the docker compose -f arguments for Windows prebuilt stack.
    #>
    param(
        [string]$PrebuiltFile = "docker-compose.prebuilt.yml",
        [string]$WindowsOverlay = "docker-compose.windows.yml"
    )
    return @("-f", $PrebuiltFile, "-f", $WindowsOverlay)
}


function Get-McpGatewayEnvVar {
    <#
    .SYNOPSIS
        Read a KEY=value entry from a dotenv file (supports optional leading #).
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $escaped = [regex]::Escape($Key)
    $pattern = "^\s*#?\s*$escaped=(.*)$"
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match $pattern) {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}


function Set-McpGatewayEnvVar {
    <#
    .SYNOPSIS
        Set or update KEY=value in a dotenv file (uncomments if previously commented).
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Key,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Env file not found: $Path"
    }
    $escaped = [regex]::Escape($Key)
    $pattern = "^\s*#?\s*$escaped=.*"
    $found = $false
    $newContent = foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match $pattern) {
            $found = $true
            "$Key=$Value"
        }
        else {
            $line
        }
    }
    if (-not $found) {
        $newContent = @($newContent) + "$Key=$Value"
    }
    Write-McpGatewayUtf8NoBom -Path $Path -Lines ([string[]]@($newContent))
}


function Write-McpGatewayUtf8NoBom {
    <#
    .SYNOPSIS
        Write text as UTF-8 without a BOM (PowerShell 5.1 Set-Content -Encoding utf8 emits BOM).
    .DESCRIPTION
        Registry Python code uses open()+json.load with encoding=utf-8 (not utf-8-sig).
        A leading EF BB BF causes JSONDecodeError. Always use this for .env and seed JSON.
    #>
    [CmdletBinding(DefaultParameterSetName = "Content")]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true, ParameterSetName = "Content")]
        [string]$Content,
        [Parameter(Mandatory = $true, ParameterSetName = "Lines")]
        [string[]]$Lines
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    if ($PSCmdlet.ParameterSetName -eq "Lines") {
        [System.IO.File]::WriteAllLines($Path, $Lines, $utf8NoBom)
    }
    else {
        [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
    }
}


function Export-McpGatewayServerJsonFiles {
    <#
    .SYNOPSIS
        Copy registry/servers/*.json to a destination with env expansion, BOM-free UTF-8.
    .DESCRIPTION
        This is the real seed-server write path used by start.ps1 on Windows.
        Skips server_state.json. Overwrites existing destination files (same as bash envsubst copy).
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,
        [Parameter(Mandatory = $true)]
        [string]$DestDir
    )
    if (-not (Test-Path -LiteralPath $SourceDir)) {
        return @()
    }
    if (-not (Test-Path -LiteralPath $DestDir)) {
        New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
    }
    $written = New-Object System.Collections.Generic.List[string]
    Get-ChildItem -Path $SourceDir -Filter "*.json" | ForEach-Object {
        if ($_.Name -eq "server_state.json") {
            return
        }
        $raw = Get-Content -LiteralPath $_.FullName -Raw
        if ($null -eq $raw) {
            $raw = ""
        }
        $expanded = Expand-McpGatewayEnvPlaceholders -Content $raw
        $dest = Join-Path $DestDir $_.Name
        Write-McpGatewayUtf8NoBom -Path $dest -Content $expanded
        $written.Add($dest) | Out-Null
    }
    return @($written)
}


function New-McpGatewayRandomHex {
    <#
    .SYNOPSIS
        Cryptographically random lowercase hex string of length 2*ByteCount.
    #>
    param(
        [ValidateRange(1, 128)]
        [int]$ByteCount = 32
    )
    $array = New-Object byte[] $ByteCount
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($array)
    return ([System.BitConverter]::ToString($array) -replace "-", "").ToLowerInvariant()
}


function New-McpGatewayRandomPassword {
    <#
    .SYNOPSIS
        Random password for local Keycloak/admin defaults (not for production crypto tokens).
    #>
    param(
        [ValidateRange(8, 64)]
        [int]$Length = 16
    )
    $chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#%"
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] $Length
    $rng.GetBytes($bytes)
    $pass = New-Object char[] $Length
    for ($i = 0; $i -lt $Length; $i++) {
        $pass[$i] = $chars[$bytes[$i] % $chars.Length]
    }
    return -join $pass
}


function Test-McpGatewayDockerReady {
    <#
    .SYNOPSIS
        Return $true if docker can list containers (daemon reachable).
    #>
    try {
        $null = & docker ps -q 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}


function Expand-McpGatewayEnvPlaceholders {
    <#
    .SYNOPSIS
        Substitute ${VAR} and $VAR (uppercase names) using process environment.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$Content
    )
    $result = [regex]::Replace($Content, '\$\{([A-Z0-9_]+)\}', {
            param($m)
            $name = $m.Groups[1].Value
            $val = [System.Environment]::GetEnvironmentVariable($name, "Process")
            if ($null -ne $val) { $val } else { $m.Value }
        })
    $result = [regex]::Replace($result, '\$([A-Z0-9_]+)', {
            param($m)
            $name = $m.Groups[1].Value
            $val = [System.Environment]::GetEnvironmentVariable($name, "Process")
            if ($null -ne $val) { $val } else { $m.Value }
        })
    return $result
}


function Initialize-McpGatewayHomeLayout {
    <#
    .SYNOPSIS
        Create ${HOME}/mcp-gateway directory tree used by compose volume mounts.
    #>
    param(
        [Parameter(Mandatory = $true)]
        [string]$HomeRoot
    )
    $mcpHome = Join-Path $HomeRoot "mcp-gateway"
    $subdirs = @(
        "servers",
        "agents",
        "models",
        "auth_server",
        "logs",
        (Join-Path "ssl" "certs"),
        (Join-Path "ssl" "private"),
        "security_scans"
    )
    foreach ($rel in $subdirs) {
        $path = Join-Path $mcpHome $rel
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
        }
    }
    $fed = Join-Path $mcpHome "federation.json"
    if (-not (Test-Path -LiteralPath $fed)) {
        New-Item -ItemType File -Force -Path $fed | Out-Null
    }
    return $mcpHome
}
