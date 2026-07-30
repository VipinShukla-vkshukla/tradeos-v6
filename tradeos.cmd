@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM  TradeOS — one command to start the trading day.
REM
REM    tradeos            ASK which framework, then run it
REM    tradeos both       swing + intraday, no prompt
REM    tradeos check      verify readiness only, start nothing
REM    tradeos health     run EVERY check and report what is broken
REM    tradeos learn      weekly review — measures, proposes, changes nothing
REM    tradeos learn show just read the open proposals
REM    tradeos status     what is live, what is paper, what is off
REM    tradeos ip         this machine's public IP for the Kite allowlist
REM    tradeos server     validate the daemon server (run this ON the server)
REM    tradeos vcn        live logs from the Oracle daemon
REM    tradeos vcn fix    pull latest code there and restart it
REM    tradeos vcn stop   stop the Oracle daemon
REM    tradeos vcn status is it running, and on which commit
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
if /i "%~1"=="learn"    goto LEARN
if /i "%~1"=="status"   goto STATUS
if /i "%~1"=="ip"       goto IP
if /i "%~1"=="stop"     goto STOP
if /i "%~1"=="evening"  goto EVENING
if /i "%~1"=="intraday" goto INTRADAY
if /i "%~1"=="swing"    goto SWING
if /i "%~1"=="both"     goto BOTH
if /i "%~1"=="server"   goto SERVER
if /i "%~1"=="vcn"      goto VCN
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
echo    L   Weekly learning review   what the evidence says to change
echo    7   Oracle daemon — logs     what the server is doing right now
echo    8   Oracle daemon — update   git pull + restart on the server
echo    9   Oracle daemon — status   running? on which commit?
echo    0   Oracle daemon — stop     hand the book to this laptop
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
if /i "%PICK%"=="L" goto LEARN
if "%PICK%"=="7" set "VCNACT=logs"
if "%PICK%"=="8" set "VCNACT=fixc"
if "%PICK%"=="9" set "VCNACT=status"
if "%PICK%"=="0" set "VCNACT=stopc"
if defined VCNACT goto VCN
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

:VCN
REM The daemon that actually holds the lease usually runs on Oracle, so "what is
REM the system doing" is often a question about a machine this laptop cannot see.
REM Reads SERVER_IP and SSH_KEY from the environment when set, so the key path is
REM not baked into a file that lives in git.
if "%TRADEOS_SSH_KEY%"=="" (set "KEY=%USERPROFILE%\Downloads\ssh-key-2026-07-28.key") else (set "KEY=%TRADEOS_SSH_KEY%")
if "%TRADEOS_SERVER_IP%"=="" (set "SRV=140.245.218.229") else (set "SRV=%TRADEOS_SERVER_IP%")
REM Dispatch AFTER KEY and SRV are resolved above — the menu paths jump here
REM rather than to the action labels directly, because a label reached without
REM those two produces an ssh call with an empty key and an empty host.
if /i "%~2"=="fix"        goto VCNFIX
if /i "%~2"=="stop"       goto VCNSTOP
if /i "%~2"=="status"     goto VCNSTAT
if /i "%VCNACT%"=="fixc"   goto VCNFIXC
if /i "%VCNACT%"=="stopc"  goto VCNSTOPC
if /i "%VCNACT%"=="status" goto VCNSTAT
echo Streaming the Oracle daemon log — Ctrl+C to stop.
echo   server %SRV%
echo.
ssh -i "%KEY%" ubuntu@%SRV% "journalctl -u tradeos-intraday -n 60 -f --no-pager"
goto END

:VCNFIXC
REM Reached from the MENU, where a mistyped digit should not restart a daemon
REM that is mid-session and working. The typed form (tradeos vcn fix) skips this
REM — someone who typed the words meant them.
echo.
echo   This pulls the latest code on %SRV% and RESTARTS the daemon.
echo   Any position it is mid-way through acting on will be re-evaluated
echo   from scratch on restart. Broker-side GTT stops are unaffected.
echo.
set "OK="
set /p "OK=Type y to proceed: "
if /i not "%OK%"=="y" goto VCNCANCEL
goto VCNFIX

:VCNSTOPC
echo.
echo   This STOPS the Oracle daemon. Your laptop's monitor takes over the
echo   book within about two minutes, once the lease expires.
echo   If no laptop monitor is running, nothing will manage exits — open
echo   positions keep only their resting GTT stops.
echo.
set "OK="
set /p "OK=Type y to proceed: "
if /i not "%OK%"=="y" goto VCNCANCEL
goto VCNSTOP

:VCNCANCEL
echo   Cancelled. Nothing was changed on the server.
goto END

:VCNSTAT
echo Oracle daemon status on %SRV%...
ssh -i "%KEY%" ubuntu@%SRV% "systemctl is-active tradeos-intraday; echo '--- commit ---'; cd ~/tradeos-v6 && git log --oneline -1; echo '--- next timer ---'; systemctl list-timers tradeos-intraday.timer --no-pager | head -3"
goto END

:VCNSTOP
echo Stopping the Oracle daemon on %SRV%...
ssh -i "%KEY%" ubuntu@%SRV% "sudo systemctl stop tradeos-intraday && systemctl is-active tradeos-intraday; echo 'stopped — the timer will start it again at 09:00 on the next weekday'"
goto END

:VCNFIX
echo Pulling latest code on %SRV% and restarting the daemon...
ssh -i "%KEY%" ubuntu@%SRV% "cd ~/tradeos-v6 && git pull --ff-only && sudo systemctl restart tradeos-intraday && sleep 8 && systemctl is-active tradeos-intraday && journalctl -u tradeos-intraday -n 25 --no-pager"
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

:LEARN
if /i "%~2"=="show" (python -m tools.weekly_review --show) else (python -m tools.weekly_review)
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
