# The daily job on the EC2 box

Every morning at 06:00 Central, `pbp-daily.timer` loads every game that finished since the last
run, in both leagues, and refreshes everything that reads the warehouse. Nobody has to be awake
and no laptop has to be on.

```
06:00 CT  pbp-daily.timer
            └─ pbp-daily.service  (user pbpetl, /opt/pbp-etl, MemoryMax 2500M)
                 scripts/daily_box.sh
                   new_games.py ─ nothing new? record success, stop
                   update_season.py --transform dbt
                     ESPN → CSVs → load_league.sql → pbp.*            (system of record)
                     load_raw_athletes.sql → raw.*
                     dbt build → cfb_next / nfl_next                  (tests gate the swap)
                     comment_tables.sql → swap_schema.sql → cfb / nfl (Ask Football sees it now)
                     build_snapshot.py → data/out/pbp_*.duckdb
                   verify_split.py --local
                   re-check: anything still missing fails the run
                 ExecStartPost (root): install-snapshot.sh → /var/lib/pbp-web, restart pbp-web
            on failure → pg-backup-alert@pbp-daily.service → email
hourly    pbp-daily-heartbeat → CloudWatch PbpPipeline/HoursSinceSuccess
            > 27 h or missing → alarm pbp-daily-stale → email (pg-backup-alerts)
```

**What protects the live apps.** A failing dbt test stops the run before the swap, so `cfb` / `nfl`
keep serving the previous day. A failed run never installs a snapshot. The job is capped at
2.5 GB and runs at low CPU and I/O priority; it peaks at about 1.5 GB.

**What you get by email.** A failed run, immediately. A morning with no success, within about
27 hours, whatever the cause (failed run, stopped timer, box down). A quiet day with no games
counts as a success: the alarm means the job stopped working, not that nobody played.

## Every day: nothing

## Checking on it

```bash
ssh awsvm 'systemctl list-timers pbp-daily* --no-pager'       # last and next run
ssh awsvm 'sudo journalctl -u pbp-daily -e --no-pager'        # the last run's output
ssh awsvm 'sudo -u pbpetl cat /opt/pbp-etl/data/state/last_success | xargs -I{} date -d @{}'
```

Run it now (after fixing a failure), or force a full reload of both leagues' current season
(a stat correction) by touching the one-shot flag first:

```bash
ssh awsvm 'sudo systemctl start --no-block pbp-daily'
ssh awsvm 'sudo -u pbpetl touch /opt/pbp-etl/data/state/force && sudo systemctl start --no-block pbp-daily'
ssh awsvm 'journalctl -u pbp-daily -f'       # follow it; ec2-user is in systemd-journal
```

## Deploying a code change

```bash
bash deploy/push.sh        # code + provision; the next 06:00 run uses it
```

## Developing on the laptop

The laptop no longer loads. Pull the box's data when you want it current:

```bash
bash scripts/pull_from_box.sh           # data/ files and Postgres (pbp, raw, cfb, nfl)
bash scripts/pull_from_box.sh --files   # just data/, which is all the apps read
```

`refresh.sh`, `weekly_*.sh` and `sync_ec2.py` refuse to run, because they would push the laptop's
older copy over the box. `PBP_LAPTOP_LOAD=1` overrides them for an emergency.

## The cutover (2026-11, once)

1. **Make the box current.** One last laptop-to-box sync, then compare the two row by row:
   `PBP_LAPTOP_LOAD=1 .venv/bin/python scripts/sync_ec2.py --season 2026 && .venv/bin/python scripts/verify_mirror.py`
2. **Install.** `bash deploy/push.sh --seed` copies all of `data/` (~1.6 GB, both leagues), creates
   `pbpetl`, builds both venvs, hands the warehouse schemas to the `pbpetl` role and installs the units.
3. **First run, by hand, forced** so the dbt build runs even with nothing new: `ssh awsvm 'sudo -u pbpetl mkdir -p /opt/pbp-etl/data/state && sudo -u pbpetl touch /opt/pbp-etl/data/state/force && sudo systemctl start --no-block pbp-daily'`, then read the journal.
4. **Turn it on.** `bash deploy/push.sh --enable`: ships any code changes and starts the 06:00
   timer and the hourly heartbeat.
5. **Alarm.** `bash deploy/aws-setup.sh`: the IAM policy for the `PbpPipeline` namespace, one
   heartbeat pushed and confirmed in CloudWatch, then the `pbp-daily-stale` alarm. In that order
   because the alarm treats missing data as a failure and would email at once over an empty metric.

To back out: `ssh awsvm 'sudo systemctl disable --now pbp-daily.timer pbp-daily-heartbeat.timer'`,
then `PBP_LAPTOP_LOAD=1 bash scripts/refresh.sh` loads from the laptop as before. The schema ownership
change is harmless to leave in place; `sync_ec2.py` runs as `postgres`.
