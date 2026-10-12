@echo off
rem Exercise cmd.exe launch, a short-lived child, and a non-zero target exit.
"%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >nul
exit /b 23
