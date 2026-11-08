@echo off
rem FilmCraft_Dictation_SRT.bat - start FilmCraft with a fresh control token, make sure
rem the Python packages are installed, then start the dictation tool with that same token.
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

rem Ensure whisper-ctranslate2 is available
where whisper-ctranslate2 >nul 2>&1
if errorlevel 1 (
    echo Installing whisper-ctranslate2 ...
    python -m pip install whisper-ctranslate2
    if errorlevel 1 (
        echo Failed to install whisper-ctranslate2
        pause
        exit /b 1
    )
)

rem Ensure the dictation tool's Python packages are available.
rem Each entry is  import_name:pip_name  - only missing ones are installed.
rem   sounddevice    = microphone recording
rem   lameenc        = MP3 encoding
rem   pyaudiowpatch  = recording what the speakers play (Windows loopback)
for %%P in (sounddevice:sounddevice lameenc:lameenc pyaudiowpatch:PyAudioWPatch) do (
    for /f "tokens=1,2 delims=:" %%A in ("%%P") do (
        python -c "import %%A" >nul 2>&1
        if errorlevel 1 (
            echo Installing %%B ...
            python -m pip install %%B
            if errorlevel 1 echo WARNING: could not install %%B - the related feature will be unavailable.
        )
    )
)


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
echo This window has the token set and Anaconda (base) active.
echo Starting the dictation tool ...
echo.
python "%~dp0FilmCraft_Dictation_SRT.py"
echo.
echo The dictation tool has closed. This window still has the token set. To run it again:
echo     python "%~dp0FilmCraft_Dictation_SRT.py"
echo.
cmd /k
