@echo off
setlocal

set "PYTHON_EXE=%DRAINTRACER_PYTHON%"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

"%PYTHON_EXE%" "%~dp0process_tree.py" %*
exit /b %ERRORLEVEL%
