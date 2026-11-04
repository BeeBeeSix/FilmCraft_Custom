@echo off
rem FILMCRAFT.bat - start FilmCraft with a fresh control token, plus a prompt
rem and the tool launcher that share that token.
setlocal

set "FC_DIR=C:\Program Files\ArtCraft\FilmCraft"
set "FC_EXE=filmcraft.exe"
set "FILMCRAFT_CONTROL_PORT=9876"

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

echo.
echo Token: %FILMCRAFT_CONTROL_TOKEN%
echo This window has the token set and Anaconda (base) active. Run FilmCraft scripts from here, e.g.
echo     python filmcraft_slideshow_gui.py
echo.
cmd /k
