@echo off
rem FILMCRAFT.bat - start FilmCraft with a fresh control token, register the
rem FilmCraft MCP bridge with Claude using that same token, and leave a prompt
rem open that shares the token.
setlocal

set "FC_DIR=C:\Program Files\ArtCraft\FilmCraft"
set "FC_EXE=filmcraft.exe"
set "FC_CLI=filmcraft-cli.exe"
set "FILMCRAFT_CONTROL_PORT=9876"

rem ---- Options (1 = on, 0 = off) ----
rem Register the MCP server with Claude Code (user scope, token refreshed each run)
set "REG_CLAUDE_CODE=0"
rem Start Claude Code in this window after FilmCraft is up
set "LAUNCH_CLAUDE_CODE=0"
rem Write the server into Claude Desktop's config (needs a full Desktop restart after)
set "REG_CLAUDE_DESKTOP=1"

if not exist "%FC_DIR%\%FC_EXE%" (
    echo Cannot find "%FC_DIR%\%FC_EXE%"
    echo Edit FC_DIR / FC_EXE at the top of this file.
    pause
    exit /b 1
)

REM Activate Anaconda base environment
set "CONDA_ACTIVATE=%USERPROFILE%\anaconda3\Scripts\activate.bat"
if not exist "%CONDA_ACTIVATE%" (
    echo Cannot find "%CONDA_ACTIVATE%"
    echo Edit CONDA_ACTIVATE at the top of this file.
    pause
    exit /b 1
)
call "%CONDA_ACTIVATE%"

rem 64 random hex characters, new every time
set "FILMCRAFT_CONTROL_TOKEN="
for /f %%i in ('python -c "import secrets; print(secrets.token_hex(32))"') do set "FILMCRAFT_CONTROL_TOKEN=%%i"
if not defined FILMCRAFT_CONTROL_TOKEN (
    echo Could not make a token. Is python available after the Anaconda activation?
    pause
    exit /b 1
)

echo Starting FilmCraft on port %FILMCRAFT_CONTROL_PORT% ...
start "" /d "%FC_DIR%" "%FC_DIR%\%FC_EXE%" --control %FILMCRAFT_CONTROL_PORT%

rem ---- Register the MCP bridge with Claude ----
set "HAVE_CLI=1"
if not exist "%FC_DIR%\%FC_CLI%" (
    set "HAVE_CLI=0"
    echo.
    echo Cannot find "%FC_DIR%\%FC_CLI%" - skipping MCP registration.
    echo Build or copy filmcraft-cli.exe there, or edit FC_CLI at the top of this file.
)

if "%HAVE_CLI%"=="1" if "%REG_CLAUDE_CODE%"=="1" (
    where claude >nul 2>&1
    if errorlevel 1 (
        echo.
        echo Claude Code was not found on PATH - skipping Claude Code registration.
    ) else (
        echo.
        echo Registering FilmCraft MCP with Claude Code ...
        call claude mcp remove filmcraft --scope user >nul 2>&1
        call claude mcp add --scope user --env FILMCRAFT_CONTROL_TOKEN=%FILMCRAFT_CONTROL_TOKEN% filmcraft -- "%FC_DIR%\%FC_CLI%" mcp --bridge 127.0.0.1:%FILMCRAFT_CONTROL_PORT%
    )
)

if "%HAVE_CLI%"=="1" if "%REG_CLAUDE_DESKTOP%"=="1" (
    echo.
    echo Updating Claude Desktop config ...
    python "%~dp0fc_desktop_cfg.py" "%FC_DIR%\%FC_CLI%" %FILMCRAFT_CONTROL_PORT% %FILMCRAFT_CONTROL_TOKEN%
    echo Fully quit and reopen Claude Desktop to pick up the new token.
)

echo.
echo Token: %FILMCRAFT_CONTROL_TOKEN%
echo This window has the token set and Anaconda (base) active. Run FilmCraft scripts from here, e.g.
echo     python filmcraft_slideshow_gui.py
echo.

if "%HAVE_CLI%"=="1" if "%LAUNCH_CLAUDE_CODE%"=="1" (
    where claude >nul 2>&1
    if not errorlevel 1 (
        echo Starting Claude Code - type /mcp to check that filmcraft is connected.
        call claude
    )
)

cmd /k
