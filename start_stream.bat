@echo off
REM =========================================================================
REM Launch the DVR -> YouTube Stream relay.
REM Usage:
REM   start_stream.bat            Runs the multi-channel engine in console
REM   start_stream.bat --gui      Opens the desktop GUI control window
REM   start_stream.bat --probe    Tests all camera streams and exits
REM =========================================================================
cd /d "%~dp0"

if "%~1"=="--gui" (
    shift
    start "" pythonw dvr_stream_app.pyw %*
) else (
    python stream_engine.py %*
)
