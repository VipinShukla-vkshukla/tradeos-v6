@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM  TradeOS — one command to start the trading day.
REM
REM    tradeos            full sequence: preflight, Kite, dashboard, monitor
REM    tradeos check      verify readiness only, start nothing
REM    tradeos status     what is live, what is paper, what is off
REM    tradeos ip         this machine's public IP for the Kite allowlist
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
REM  Double-click this file, or run it from anywhere. It resolves its own
REM  location so the working directory does not matter — a launcher that only
REM  works from one folder is a launcher you have to remember how to use.
REM ═══════════════════════════════════════════════════════════════════════════

setlocal
cd /d "%~dp0backend"

if /i "%~1"=="check"    goto CHECK
if /i "%~1"=="status"   goto STATUS
if /i "%~1"=="ip"       goto IP
if /i "%~1"=="stop"     goto STOP
if /i "%~1"=="evening"  goto EVENING
if /i "%~1"=="intraday" goto INTRADAY
if /i "%~1"=="swing"    goto SWING
goto START

:START
python start_day.py
goto END

:CHECK
python start_day.py --check
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
