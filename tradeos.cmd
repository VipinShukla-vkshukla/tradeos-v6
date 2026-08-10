@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM  TradeOS — one command to start the trading day.
REM
REM    tradeos            ASK which framework, then run it
REM    tradeos both       swing + intraday, no prompt
REM    tradeos check      verify readiness only, start nothing
REM    tradeos health     run EVERY check and report what is broken
REM    tradeos simulate   what BOTH books would do right now — writes nothing
REM    tradeos verify     offline logic checks — no database, ~2s. Run after editing.
REM    tradeos regression verify + allocator replay + exit ladder replay, in one — run
REM                       this after every code change, before benchmark
REM    tradeos regression 15   same, over the last 15 days instead of the default 10
REM    tradeos benchmark  snapshot / compare the live book's actual state — see
REM                       "APPLE TO APPLE COMPARISON" below
REM    tradeos benchmark snapshot "label"   record today's state under a label
REM    tradeos benchmark compare  diff the two most recent snapshots (or pass two paths)
REM    tradeos backfill   score any past intraday session the daemon never resolved
REM    tradeos backup     dump the system of record off-platform, then verify it
REM    tradeos rollback   what Phase 4 has switched on, and what it was before
REM    tradeos rollback off   every Phase 4 switch back to its pre-Phase-4 value
REM    tradeos alloc      is the allocator ahead of greedy, and is there evidence yet
REM    tradeos alloc today   today's allocation ledger, ordered by edge
REM    tradeos expectancy    net R per trade, by product and clip size
REM    tradeos expectancy reconcile FILE.xlsx   check it against a Zerodha P&L export
REM    tradeos quote-parity   does the live feed agree with the historical one yet
REM    tradeos quote-parity arm   start logging both, for one session
REM    tradeos learn      weekly review — measures, proposes, changes nothing
REM    tradeos learn show just read the open proposals (same as "tradeos proposals")
REM    tradeos discover   look for engines that do not exist yet
REM    tradeos settings   which Control Room switches this week's evidence supports
REM    tradeos proposals  read what is waiting for a decision (same as "tradeos learn show")
REM    tradeos status     what is live, what is paper, what is off
REM    tradeos ip         this machine's public IP for the Kite allowlist
REM    tradeos server     validate the daemon server (run this ON the server)
REM    tradeos vcn        live logs from the Oracle daemon
REM    tradeos vcn fix    pull latest code there and restart it
REM    tradeos vcn env    push THIS laptop's .env there, backing the old one up
REM    tradeos vcn stop   stop the Oracle daemon
REM    tradeos vcn status is it running, and on which commit
REM    tradeos vcn savelog [YYYY-MM-DD]   pull that day's Oracle log into backend\logs
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
REM  APPLE TO APPLE COMPARISON — how to trust a code change before it trades
REM  -------------------------------------------------------------------------
REM  Two different questions, two different tools. Run both, in this order,
REM  every time a change touches trading logic:
REM
REM    1. tradeos regression            BEFORE the change (confirm the branch
REM                                      you're starting from is clean)
REM    2. tradeos benchmark snapshot "before <what you're about to change>"
REM    3.   ... make the code change ...
REM    4. tradeos regression            AFTER the change (did it break any of
REM                                      the 220+ offline checks or the two
REM                                      walk-forward replays?)
REM    5. tradeos benchmark snapshot "after <what you changed>"
REM    6. tradeos benchmark compare     diffs the two most recent snapshots —
REM                                      no filenames to remember or paste
REM
REM  `regression` is the LOGIC half: does the code still do what 220+ pinned
REM  checks say it should, and does a walk-forward replay over real historical
REM  detections still come out the way it did. It needs no live trading to have
REM  happened since the last run.
REM
REM  `benchmark` is the STATE half: what does the REAL book actually look like
REM  right now — open positions, closed-trade win rate and R, per-engine
REM  standings — read straight from closed_positions, the same ledger the
REM  dashboard reads. It answers "did anything change", not "why" — a
REM  benchmark diff alone cannot tell a real behaviour change from two ordinary
REM  sessions of new trades between snapshots, which is exactly why step 1/4
REM  comes first: regression isolates the POLICY's effect on fixed historical
REM  data, benchmark only ever shows you today's live, moving book.
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
if /i "%~1"=="simulate" goto SIMULATE
if /i "%~1"=="verify"   goto VERIFY
if /i "%~1"=="regression" goto REGRESSION
if /i "%~1"=="benchmark" goto BENCHMARK
if /i "%~1"=="backup"   goto BACKUP
if /i "%~1"=="rollback" goto ROLLBACK
if /i "%~1"=="alloc"    goto ALLOC
if /i "%~1"=="expectancy" goto EXPECTANCY
if /i "%~1"=="quote-parity" goto QUOTEPARITY
if /i "%~1"=="learn"    goto LEARN
if /i "%~1"=="discover" goto DISCOVER
if /i "%~1"=="settings" goto SETTINGS
if /i "%~1"=="backfill" goto BACKFILL
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
REM The grouped menu prints 45 lines plus the prompt, and a default Windows
REM console shows 25 — so the first two groups scroll off before they can be
REM read, which is exactly how the LEARN options came to look absent when they
REM were present all along. Sized to 52 rather than 45: adding the Verify entry
REM took the menu to exactly 50, one line short once the "Choice [1]:" prompt
REM is counted, and a menu whose header has just scrolled away is how this was
REM missed the first time. Adding Regression and Benchmark took it to 52 —
REM bump this again if another line is ever added, by the same margin.
REM Redirected because a console that cannot be resized (a terminal tab, ssh)
REM should not print an error.
mode con: cols=100 lines=52 >nul 2>&1
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
echo     S   Simulate               what BOTH books would do right now — writes nothing
echo     V   Verify                 offline logic checks, no database (~2s)
echo     T   Regression check       verify + both walk-forward replays, in one
echo     M   Benchmark              snapshot / compare the live book — apple to apple
echo.
echo   LEARN                        measures and proposes, changes nothing
echo     L   Weekly review          what the evidence says to change
echo     D   Discover engines       look for edges nothing covers
echo     P   Open proposals         read what is waiting for a decision
echo.
echo   PHASE 4                      allocator, storage, evidence
echo     A   Allocator report       ahead of greedy yet? disagreements so far
echo     B   Backup now             dump the system of record off-platform
echo     R   Rollback status        what Phase 4 has switched on, and its default
echo     Q   Quote parity           does the live feed agree with the historical one
echo.
echo   ORACLE SERVER
echo     8   Logs                   what the server is doing right now
echo     9   Update                 git pull + restart there
echo     0   Push .env              copy THIS laptop's .env there (backs up first)
echo     N   Status                 running? on which commit?
echo     X   Stop                   hand the book to this laptop
echo     G   Save log               pull today's Oracle log into backend\logs
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
if /i "%PICK%"=="S" goto SIMULATE
if /i "%PICK%"=="V" goto VERIFY
if /i "%PICK%"=="T" goto REGRESSION
if /i "%PICK%"=="M" goto BENCHMARK
if /i "%PICK%"=="L" goto LEARN
if /i "%PICK%"=="D" goto DISCOVER
if /i "%PICK%"=="P" goto PROPOSALS
if /i "%PICK%"=="A" goto ALLOC
if /i "%PICK%"=="B" goto BACKUP
if /i "%PICK%"=="R" goto ROLLBACK
if /i "%PICK%"=="Q" goto QUOTEPARITY
if /i "%PICK%"=="E" goto EVENING
if /i "%PICK%"=="K" goto STOP
if "%PICK%"=="8" set "VCNACT=logs"
if "%PICK%"=="9" set "VCNACT=fixc"
if "%PICK%"=="0" set "VCNACT=envc"
if /i "%PICK%"=="N" set "VCNACT=status"
if /i "%PICK%"=="X" set "VCNACT=stopc"
if /i "%PICK%"=="G" set "VCNACT=savelog"
REM The four server options SET an action rather than jumping, so they all enter
REM through :VCN — the only place the ssh key and host are resolved. Without this
REM line they set the variable and fall through to "not one of the choices",
REM which is what 8/9/N/X did after the layout was restored.
if defined VCNACT goto VCN
echo.
echo   "%PICK%" is not one of the choices. Nothing was started.
goto END

:BOTH
REM Two windows, clearly titled, so ACTIVE/STANDBY is something you can watch
REM happen instead of reconstructing afterwards from two separate logs on two
REM machines — that reconstruction is what the 2026-08-06 lease handoff cost.
if "%TRADEOS_SSH_KEY%"=="" (set "KEY=%USERPROFILE%\Downloads\ssh-key-2026-07-28.key") else (set "KEY=%TRADEOS_SSH_KEY%")
if "%TRADEOS_SERVER_IP%"=="" (set "SRV=140.245.218.229") else (set "SRV=%TRADEOS_SERVER_IP%")
start "SERVER - TradeOS (Oracle %SRV%)" cmd /k ssh -i "%KEY%" ubuntu@%SRV% "journalctl -u tradeos-intraday -n 60 -f --no-pager"
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
if /i "%~2"=="env"        goto VCNENV
if /i "%~2"=="savelog"    goto VCNSAVELOG
if /i "%VCNACT%"=="fixc"   goto VCNFIXC
if /i "%VCNACT%"=="stopc"  goto VCNSTOPC
if /i "%VCNACT%"=="envc"   goto VCNENVC
if /i "%VCNACT%"=="status" goto VCNSTAT
if /i "%VCNACT%"=="savelog" goto VCNSAVELOG
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

:VCNSAVELOG
REM Pulls that day's already-written daemon log — backend/logs/tradeos_YYYY-MM-DD.log
REM on the server, the exact file config.py's loguru sink writes all session and
REM finalises the moment the daemon self-exits at 15:40 — down to THIS laptop's
REM backend\logs. Read-only, so no confirmation gate, same as Status above.
REM Defaults to today; `tradeos vcn savelog YYYY-MM-DD` backfills a specific day
REM — the server keeps every day's file, this laptop only has what was fetched.
set "WANT=%~3"
if "%WANT%"=="" for /f %%D in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "WANT=%%D"
if not exist "%~dp0backend\logs" mkdir "%~dp0backend\logs"
set "LOCALLOG=%~dp0backend\logs\tradeos_%WANT%.log"
echo Pulling %WANT%'s log from %SRV%...
scp -i "%KEY%" ubuntu@%SRV%:~/tradeos-v6/backend/logs/tradeos_%WANT%.log "%LOCALLOG%"
if errorlevel 1 (
  echo   Could not fetch tradeos_%WANT%.log from %SRV% — check the date, or whether
  echo   the daemon ran that day.
  goto END
)
echo   Saved to "%LOCALLOG%"
goto END

:VCNSTOP
echo Stopping the Oracle daemon on %SRV%...
ssh -i "%KEY%" ubuntu@%SRV% "sudo systemctl stop tradeos-intraday && systemctl is-active tradeos-intraday; echo 'stopped — the timer will start it again at 09:00 on the next weekday'"
goto END

:VCNFIX
echo Pulling latest code on %SRV% and restarting the daemon...
ssh -i "%KEY%" ubuntu@%SRV% "cd ~/tradeos-v6 && git pull --ff-only && sudo systemctl restart tradeos-intraday && sleep 8 && systemctl is-active tradeos-intraday && journalctl -u tradeos-intraday -n 25 --no-pager"
goto END

:VCNENVC
REM Confirmed form, reached from the MENU. Option 9 pulls CODE; .env is
REM gitignored (it holds the Kite secret and the Supabase service key), so no
REM git pull will ever carry it — which is how the laptop came to size against
REM Rs 30,000 while the server used Rs 20,000 for the same live book.
echo.
echo   This OVERWRITES %SRV%'s backend\.env with this laptop's copy.
echo   It contains API keys and tokens — you are pushing secrets to that host.
echo   The current server .env is backed up first, and the daemon is NOT
echo   restarted: .env is read once at import, so run option 9 afterwards.
echo.
set "OK="
set /p "OK=Type y to proceed: "
if /i not "%OK%"=="y" goto VCNCANCEL
goto VCNENV

:VCNENV
REM %~dp0 is the folder THIS script lives in, with a trailing backslash. The
REM path must not be relative to the current directory: running `scp backend\.env`
REM from anywhere other than the repo root fails with "No such file or directory",
REM which is exactly what happened from C:\Users\vkshu\Downloads.
set "LOCALENV=%~dp0backend\.env"
if not exist "%LOCALENV%" (
  echo   Local .env not found at "%LOCALENV%" — nothing was sent.
  goto END
)
REM Stamp the backup on the SERVER's clock, not this one. cmd.exe leaves $(...)
REM alone and the remote bash expands it, so the date is the server's own.
echo Backing up the server's current .env...
ssh -i "%KEY%" ubuntu@%SRV% "cd ~/tradeos-v6/backend && cp .env .env.bak.$(date +%%F-%%H%%M%%S) && ls -1t .env.bak.* | head -3"
if errorlevel 1 (
  echo   Backup FAILED — .env was NOT replaced. Nothing on the server changed.
  goto END
)
echo Copying this laptop's .env to %SRV%...
scp -i "%KEY%" "%LOCALENV%" ubuntu@%SRV%:~/tradeos-v6/backend/.env
if errorlevel 1 (
  echo   Copy FAILED. The server still has its original .env — restore with:
  echo     ssh -i "%KEY%" ubuntu@%SRV% "cd ~/tradeos-v6/backend && cp .env.bak.LATEST .env"
  goto END
)
REM Verify by KEY NAMES and capital only. Never print the file: this runs in a
REM console that gets screenshotted and pasted into chats.
echo.
echo Verifying (key names only, no secrets printed)...
ssh -i "%KEY%" ubuntu@%SRV% "cd ~/tradeos-v6/backend && echo -n '  keys on server: ' && grep -cE '^[A-Z_]+=' .env && grep -E '^TOTAL_CAPITAL=' .env"
echo.
echo   Done. The running daemon still holds the OLD values — .env is read once
echo   at import. Run option 9 to restart it there.
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

:VERIFY
REM  Offline. Needs no database, no broker session and no network — every check
REM  is pure arithmetic over in-memory objects. That is what makes it cheap
REM  enough to run after every edit, which is the point: `health` tells you
REM  whether TODAY is safe, `verify` tells you whether a CHANGE is safe.
python -m tools.verify
goto END

:REGRESSION
REM  The LOGIC half of "did this change break anything" — see the APPLE TO
REM  APPLE COMPARISON block at the top of this file for the full workflow.
REM  Chains tools.verify, tools.allocator_replay and tools.exit_ladder_replay
REM  in one run; the two replay steps are SKIPPED (not failed) without
REM  database credentials, same distinction tools.health already draws.
REM  `tradeos regression 15` runs the replay window over 15 days instead of
REM  the default 10.
if not "%~2"=="" (python -m tools.regression_check --days %~2) else (python -m tools.regression_check)
goto END

:BENCHMARK
REM  The STATE half — what the real book actually looks like right now,
REM  ground-truth from closed_positions, the same ledger the dashboard reads.
REM  Not a backtest; see tools/benchmark.py's own docstring for the
REM  distinction from `regression` above.
if /i "%~2"=="snapshot" goto BENCH_SNAPSHOT
if /i "%~2"=="compare"  goto BENCH_COMPARE
goto BENCH_HELP

:BENCH_SNAPSHOT
REM  %~3 is the label, quoted so a multi-word one survives — "tradeos
REM  benchmark snapshot before the VWR fix" would otherwise pass only
REM  "before" and silently drop the rest.
python -m tools.benchmark snapshot --label "%~3"
goto END

:BENCH_COMPARE
REM  Bare "tradeos benchmark compare" auto-picks the two most recent
REM  snapshots — passing two literal empty strings through instead (what
REM  cmd.exe would do with unset %~3/%~4) would break tools.benchmark's own
REM  nargs="?" default, so the no-args case is dispatched separately here.
if "%~3"=="" (python -m tools.benchmark compare) else (python -m tools.benchmark compare "%~3" "%~4")
goto END

:BENCH_HELP
echo.
echo   "tradeos benchmark" needs to know which of two things you want:
echo.
echo     tradeos benchmark snapshot "label text"     record today's state
echo     tradeos benchmark compare                   diff the two most recent snapshots
echo     tradeos benchmark compare fileA.json fileB.json   diff two specific ones
echo.
echo   See the APPLE TO APPLE COMPARISON block at the top of this file (open
echo   tradeos.cmd in a text editor) for when to run each step.
echo.
goto END

:SIMULATE
REM Read-only. Runs the same engines, gates and sizing both books use live —
REM writes nothing to system_config or open_positions. Paired with `health` as
REM the two commands to run before trusting a session; now direction-aware, so
REM a SHORT row appears here (marked LONG/SHORT) if one would fire.
python -m tools.simulate
goto END

:BACKUP
python -m tools.backup --keep 8
goto END

:ALLOC
REM  Scorecard by default. The number that matters is scored DISAGREEMENTS with
REM  the greedy path — sessions where the two agree carry no information at all.
if /i "%~2"=="today" (python -m tools.allocator_report --today) else (python -m tools.allocator_report)
goto END

:ROLLBACK
REM  Status by default. Turning switches off is the thing you must ask for by
REM  name, because it is the one that changes behaviour.
if /i "%~2"=="off" (python -m tools.rollback --all-off) else (python -m tools.rollback --status)
goto END

:EXPECTANCY
REM  The ledger by default. "reconcile FILE" checks it against what the broker
REM  actually charged — Zerodha Console > Reports > P&L > Tradewise > download.
if /i "%~2"=="reconcile" (python -m tools.expectancy_ledger --reconcile "%~3") else (python -m tools.expectancy_ledger)
goto END

:QUOTEPARITY
REM  Report by default: does the live feed agree with the historical one over
REM  the last session it was armed for. "arm" turns logging on for one session;
REM  "disarm" turns it back off once you have read the report.
if /i "%~2"=="arm" (python -m tools.quote_parity --arm) else if /i "%~2"=="disarm" (python -m tools.quote_parity --disarm) else (python -m tools.quote_parity)
goto END

:LEARN
if /i "%~2"=="show" goto LEARN_SHOW
python -m tools.weekly_review
goto END

:LEARN_SHOW
python -m tools.weekly_review --show
goto END

:DISCOVER
python -m tools.discover_engines --days 21
goto END

:SETTINGS
REM Reads the same evidence as `learn` and answers the question it does not:
REM which switch to move, to what, and whether the sample actually supports it.
if /i "%~2"=="propose" (python -m tools.control_room --propose) else (python -m tools.control_room)
goto END

:BACKFILL
REM Score any past session the daemon never resolved. Idempotent and safe.
python -m intraday.outcomes --backfill
goto END

:PROPOSALS
REM  Same read-only view as "tradeos learn show" — kept as its own command
REM  name because the menu treats "read what's waiting" and "run the weekly
REM  review" as different actions even though today they call the same
REM  underlying tool. DEDUPED 10-Aug-2026: this used to repeat the exact
REM  `python -m tools.weekly_review --show` line LEARN_SHOW already has —
REM  one command now, two names into it.
goto LEARN_SHOW

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
REM TRADEOS_UNATTENDED is set only by the scheduled savelog pull (Task
REM Scheduler has no console to press a key on) — an interactive run, typed
REM or via the menu, always still pauses so the output is not lost.
if not defined TRADEOS_UNATTENDED pause
endlocal
