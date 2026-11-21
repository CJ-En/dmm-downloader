@echo off
rem Starts dmm-downloader: double-click it, or drag a text file with content ids onto it.
rem The first run creates a private Python environment (.venv) next to this file and installs what
rem it needs there; later runs reuse it.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv"
if exist "%VENV%\Scripts\python.exe" fc /b "%HERE%requirements.txt" "%VENV%\requirements.installed" >NUL 2>NUL && goto run
echo Preparing the Python environment (first run or after an update)...
where py >NUL 2>NUL && (py -3 -m venv "%VENV%") || (python -m venv "%VENV%")
if not exist "%VENV%\Scripts\python.exe" (
    echo Python 3.10 or newer is needed: https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
    pause
    exit /b 2
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --quiet -r "%HERE%requirements.txt" || (pause & exit /b 2)
copy /y "%HERE%requirements.txt" "%VENV%\requirements.installed" >NUL
:run
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if "%~x1"==".txt" (
    "%VENV%\Scripts\python.exe" -m dmm_downloader get -i %*
) else (
    "%VENV%\Scripts\python.exe" -m dmm_downloader %*
)