@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM  TradeOS — one command to start the trading day.
REM
REM    tradeos            ASK which framework, then run it
REM    tradeos both       swing + intraday, no prompt
REM    tradeos check      verify readiness only, start nothing
REM    tradeos health     run EVERY check and report what is broken
REM    tradeos status     what is live, what is paper, what is off
REM    tradeos ip         this machine's public IP for the Kite allowlist
REM    tradeos server     validate the daemon server (run this ON the server)
REM    tradeos stop       set the kill switch — everything stops trading
REM    tradeos evening    run the swing pipeline by hand
REM
REM    tradeos intraday paper|live|off
REM    tradeos swing    paper|live|off
REM
REM  Mode changes are separate from the daily launch on purpose: you launch
REM  every morning but promote a framework to live once, and folding a rare,
REM  consequential decision into a daily routine is how it gets made by accident.
REM
REM  DOUBLE-CLICK asks which framework to run today, then does everything for
REM  that choice in one go. Typing a subcommand skips the prompt, so scripts and
REM  habits both keep working.
REM
REM  Double-click this file, or run it from anywhere. It resolves its own
REM  location so the working directory does not matter — a launcher that only
REM  works from one folder is a launcher you have to remember how to use.
REM ═══════════════════════════════════════════════════════════════════════════

setlocal
cd /d "%~dp0backend"

if /i "%~1"=="check"    goto CHECK
if /i "%~1"=="health"   goto HEALTH
if /i "%~1"=="status"   goto STATUS
if /i "%~1"=="ip"       goto IP
if /i "%~1"=="stop"     goto STOP
if /i "%~1"=="evening"  goto EVENING
if /i "%~1"=="intraday" goto INTRADAY
if /i "%~1"=="swing"    goto SWING
if /i "%~1"=="both"     goto BOTH
if /i "%~1"=="server"   goto SERVER
if "%~1"==""            goto MENU
goto START

:MENU
echo.
echo  ===========================================================
echo   TradeOS — what are you running today?
echo  ===========================================================
echo.
echo    1   Both frameworks          swing + intraday  (default)
echo    2   Swing only               intraday stands down
echo    3   Intraday only            swing automation stands down
echo.
echo    4   Check readiness          start nothing
echo    5   Show current status
echo    6   Full health sweep        every check, find what is broken
echo.
echo   Your choice turns the OTHER framework off in the database, so
echo   the Oracle server daemon obeys it too — it reads the same rows.
echo.
echo   The live monitor starts either way: it is the price feed and exit
echo   manager for BOTH books, so swing positions keep real-time stops.
echo   Paper/live is NOT changed here; use "tradeos swing live".
echo.
set "PICK="
set /p "PICK=Choice [1]: "
if not defined PICK set "PICK=1"
if "%PICK%"=="1" goto BOTH
if "%PICK%"=="2" goto ONLYSWING
if "%PICK%"=="3" goto ONLYINTRA
if "%PICK%"=="4" goto CHECK
if "%PICK%"=="5" goto STATUS
if "%PICK%"=="6" goto HEALTH
echo.
echo   "%PICK%" is not one of the choices. Nothing was started.
goto END

:BOTH
python start_day.py --only both
goto END

:ONLYSWING
python start_day.py --only swing
goto END

:ONLYINTRA
python start_day.py --only intraday
goto END

:SERVER
echo Validating the intraday daemon server (run this ON the server)...
python "%~dp0deploy\validate_server.py"
goto END

:START
python start_day.py
goto END

:CHECK
python start_day.py --check
goto END

:HEALTH
python -m tools.health
goto END

:STATUS
python control_panel.py
goto END

:IP
python control_panel.py --ip
goto END

:INTRADAY
python control_panel.py --intraday %~2
goto END

:SWING
python control_panel.py --swing %~2
goto END

:STOP
echo Setting the master kill switch...
python -c "import sys; sys.path.insert(0,'.'); from config import get_supabase; get_supabase().table('system_config').update({'value':'true'}).eq('key','master_kill_switch').execute(); print('  Kill switch ON. Nothing will trade until you clear it.')"
goto END

:EVENING
python run_pipeline.py
goto END

:END
echo.
pause
endlocal
