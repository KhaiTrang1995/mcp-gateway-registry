# Windows Setup Guide: MCP Gateway & Registry

This guide covers running the MCP Gateway & Registry on **native Windows** with Docker Desktop. For macOS, see the [macOS Setup Guide](macos-setup-guide.md). For a general Linux-oriented flow, see the [Quick Start Guide](quickstart.md).

> **SECURITY WARNING**
>
> Local defaults generate random passwords into `.env` for development only.
> Never use development credentials in production. Prefer secrets managers
> (for example AWS Secrets Manager) for real deployments.

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [One-command start](#2-one-command-start)
3. [What the start script does](#3-what-the-start-script-does)
4. [Stop, logs, and reset](#4-stop-logs-and-reset)
5. [Manual compose (advanced)](#5-manual-compose-advanced)
6. [WSL2 fallback](#6-wsl2-fallback)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Prerequisites

### System

- Windows 10 or Windows 11
- At least 8 GB RAM (16 GB recommended)
- About 10 GB free disk for images and volumes

### Software

- **Docker Desktop** with the **WSL2 backend** enabled  
  Install: https://www.docker.com/products/docker-desktop/
- **Git for Windows** (for clone)
- **PowerShell** 5.1+ (built-in) or PowerShell 7+

Verify Docker before continuing:

```powershell
docker version
docker compose version
```

Docker Desktop must be running (whale icon in the system tray).

### Line endings (contributors)

This repository uses `.gitattributes` so `*.sh` stays LF and `*.ps1` uses CRLF.
If you edit shell scripts on Windows, avoid converting the whole tree to CRLF;
that breaks Linux containers and CI.

---

## 2. One-command start

```powershell
git clone https://github.com/agentic-community/mcp-gateway-registry.git
cd mcp-gateway-registry

# Optional: review defaults first
# Copy-Item .env.example .env
# notepad .env

.\start.ps1
```

Show help without starting containers:

```powershell
.\start.ps1 -Help
```

Prepare `.env` and `%USERPROFILE%\mcp-gateway` only (no `compose up`):

```powershell
.\start.ps1 -SkipDockerStart
```

When startup finishes, open:

- Registry UI: http://localhost
- Keycloak Admin: http://localhost:8080

Credentials are printed at the end of the script (from `.env`). Typical local users are `admin` and `testuser`.

---

## 3. What the start script does

`start.ps1` is the Windows equivalent of the prebuilt path of `build_and_run.sh`. It:

1. Sets `HOME` to `%USERPROFILE%` so compose mounts `${HOME}/mcp-gateway/...` resolve correctly on Windows.
2. Creates `.env` from `.env.example` if missing and replaces weak default secrets.
3. Creates the host data layout under `%USERPROFILE%\mcp-gateway` (servers, agents, models, scopes, ssl, logs).
4. Starts the stack with **two explicit compose files**:
   - `docker-compose.prebuilt.yml`
   - `docker-compose.windows.yml` (named volume for container log paths that would otherwise bind `/var/log/containers/ai-registry` on Linux)
5. Runs MongoDB init, waits for Keycloak, then runs the existing bash Keycloak/bootstrap scripts **inside** a Linux container (those scripts are not reimplemented in PowerShell).
6. Restarts `auth-server` and `registry` so client secrets take effect.

The Windows overlay is **opt-in**. It is never named `docker-compose.override.yml`, so Linux and macOS developers do not load it by accident.

Helpers live in `scripts/windows/McpGatewayWindows.ps1` and are covered by `tests/windows/test_mcp_gateway_windows.ps1`.

---

## 4. Stop, logs, and reset

Always pass the same compose files:

```powershell
$cf = @("-f", "docker-compose.prebuilt.yml", "-f", "docker-compose.windows.yml")

# Status
docker compose @cf ps

# Logs
docker compose @cf logs -f

# Stop containers (keeps named volumes and data)
docker compose @cf down
```

### Safe default vs data wipe

- `.\start.ps1` stops any previous project containers with `down --remove-orphans` only. **Named volumes are kept.**
- To wipe volumes and start clean (destructive):

```powershell
.\start.ps1 -ResetData
```

Or manually:

```powershell
docker compose -f docker-compose.prebuilt.yml -f docker-compose.windows.yml down --volumes
```

---

## 5. Manual compose (advanced)

```powershell
$env:HOME = $env:USERPROFILE
$env:DOCKERHUB_ORG = "mcpgateway"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

docker compose -f docker-compose.prebuilt.yml -f docker-compose.windows.yml pull
docker compose -f docker-compose.prebuilt.yml -f docker-compose.windows.yml up -d
docker compose -f docker-compose.prebuilt.yml -f docker-compose.windows.yml up mongodb-init
```

You still need Keycloak realm/client bootstrap (see [installation.md](installation.md) or let `start.ps1` do it).

---

## 6. WSL2 fallback

If you prefer the bash entrypoint unchanged:

1. Install Ubuntu (or another distro) from the Microsoft Store and enable WSL2.
2. Install Docker Desktop and enable WSL integration for that distro.
3. Inside WSL:

```bash
cd /mnt/c/path/to/mcp-gateway-registry   # or clone into the Linux filesystem
cp .env.example .env
export DOCKERHUB_ORG=mcpgateway
./build_and_run.sh --prebuilt
```

Use Linux paths under `$HOME/mcp-gateway` inside WSL. You do not need `docker-compose.windows.yml` in this mode.

---

## 7. Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `Docker is not running` | Start Docker Desktop; wait until it reports running; re-open the terminal. |
| Port 80 already in use | Stop IIS/other web servers, or change published ports in compose (advanced). |
| Empty/wrong host mounts | Confirm `$env:HOME` equals `$env:USERPROFILE` before `docker compose`. |
| Keycloak timeout | `docker compose -f docker-compose.prebuilt.yml -f docker-compose.windows.yml logs keycloak` |
| Permission errors on volumes | Prefer the Windows overlay (named volume for logs). Avoid binding Linux-only host paths. |
| CRLF breaks `*.sh` in containers | Rely on `.gitattributes`; run `git add --renormalize .` only if you intentionally fix endings. |

### Run helper unit tests

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_mcp_gateway_windows.ps1
```

---

## Related documentation

- [Quick Start Guide](quickstart.md)
- [Installation Guide](installation.md)
- [macOS Setup Guide](macos-setup-guide.md)
- [Authentication](auth.md)
