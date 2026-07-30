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
REM    tradeos discover   look for engines that do not exist yet
REM    tradeos proposals  read what is waiting for a decision
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
if /i "%~1"=="discover" goto DISCOVER
if /i "%~1"=="proposals" goto PROPOSALS
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
REM The grouped menu prints 36 lines and a default Windows console shows 25, so
REM the first two groups scroll off before they can be read — which is exactly
REM how the LEARN options came to look absent when they were present all along.
REM Resizing costs one line and keeps the layout. Redirected because a console
REM that cannot be resized (a terminal tab, ssh) should not print an error.
mode con: cols=100 lines=45 >nul 2>&1
cls
echo.
echo  ===========================================================
echo   TradeOS — what are you running today?
echo  ===========================================================
echo.
echo   RUN TODAY
echo     1   Both frameworks        swing + intraday  (default)
echo     2   Swing only             intraday stands down
echo     3   Intraday only          swing automation stands down
echo.
echo   INSPECT
echo     4   Check readiness        start nothing
echo     5   Status                 live/paper, and where the monitor is
echo     6   Health sweep           every check, find what is broken
echo     7   IP                     this machine's, vs the Kite allowlist
echo.
echo   LEARN                        measures and proposes, changes nothing
echo     L   Weekly review          what the evidence says to change
echo     D   Discover engines       look for edges nothing covers
echo     P   Open proposals         read what is waiting for a decision
echo.
echo   ORACLE SERVER
echo     8   Logs                   what the server is doing right now
echo     9   Update                 git pull + restart there
echo     N   Status                 running? on which commit?
echo     X   Stop                   hand the book to this laptop
echo.
echo   OTHER
echo     E   Evening pipeline       run the swing pipeline by hand
echo     K   KILL SWITCH            stop all trading, both frameworks
echo.
echo   Your choice of 1/2/3 turns the OTHER framework off in the database,
echo   so the Oracle daemon obeys it too. The live monitor starts either
echo   way — it is the price feed and exit manager for BOTH books.
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
if "%PICK%"=="7" goto IP
if /i "%PICK%"=="L" goto LEARN
if /i "%PICK%"=="D" goto DISCOVER
if /i "%PICK%"=="P" goto PROPOSALS
if /i "%PICK%"=="E" goto EVENING
if /i "%PICK%"=="K" goto STOP
if "%PICK%"=="8" set "VCNACT=logs"
if "%PICK%"=="9" set "VCNACT=fixc"
if /i "%PICK%"=="N" set "VCNACT=status"
if /i "%PICK%"=="X" set "VCNACT=stopc"
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

:DISCOVER
python -m tools.discover_engines --days 21
goto END

:PROPOSALS
python -m tools.weekly_review --show
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
