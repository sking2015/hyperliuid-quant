@echo off
setlocal
cd /d "%~dp0" || exit /b 1

where docker >nul 2>&1 || (
    echo Docker CLI not found. Install Docker Desktop, then reopen this script.
    goto failed
)

docker compose version >nul 2>&1 || (
    echo Docker Compose v2 is required. Update Docker Desktop.
    goto failed
)

docker info >nul 2>&1
if not errorlevel 1 goto engine_ready

echo Starting Docker Desktop. Complete any first-run prompts if shown.
docker desktop start --detach >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
        start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    ) else (
        echo Could not start Docker Desktop automatically. Open it manually and run this script again.
        goto failed
    )
)
for /l %%I in (1,1,60) do (
    docker info >nul 2>&1
    if not errorlevel 1 goto engine_ready
    powershell.exe -NoProfile -Command "Start-Sleep -Seconds 2"
)
echo Docker did not become ready. Check Docker Desktop and WSL 2.
goto failed

:engine_ready
for /f %%I in ('docker info --format "{{.OSType}}" 2^>nul') do (
    if /i not "%%I"=="linux" (
        echo Switch Docker Desktop to Linux containers and run this script again.
        goto failed
    )
)
echo Building and starting HL Quant Lab...
docker compose up --build --detach --wait --wait-timeout 120
if errorlevel 1 goto service_failed

for /l %%I in (1,1,30) do (
    docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://frontend/api/v1/health', timeout=2).close()" >nul 2>&1
    if not errorlevel 1 goto ready
    powershell.exe -NoProfile -Command "Start-Sleep -Seconds 1"
)
echo The frontend could not reach the API health endpoint.
goto service_failed

:ready
echo Frontend: http://127.0.0.1:5173
echo API docs: http://127.0.0.1:8000/docs
if /i not "%~1"=="--no-browser" start "" "http://127.0.0.1:5173"
exit /b 0

:service_failed
docker compose ps -a
echo For details, run: docker compose logs --tail 80
:failed
if /i not "%~1"=="--no-browser" pause
exit /b 1
