#!/bin/bash
# Run `manage.py sync_events` every hour on the Elastic Beanstalk instance.
#
# Elastic Beanstalk runs this script after every deployment and every
# configuration change (see the postdeploy hooks). It installs a systemd
# timer, the scheduler built into Amazon Linux 2023, which has no cron.
#
# - The timer fires at the top of every hour. Persistent=true runs a missed
#   sync as soon as the instance is back up.
# - The job runs as the web app's user, with the same environment variables
#   (RDS_*, DJANGO_SECRET_KEY, ...) that Elastic Beanstalk gives the site.
# - systemd never starts a run while the previous one is still going.
# - Output is appended to /var/log/team5_sync.log, which `eb logs` includes.
#
# The sync exits non-zero and leaves the database untouched when the feed
# is down, so a failed run only shows up in the log and in `systemctl status`.
# If installing the timer fails, the deployment still succeeds.
#
# The paths below can be overridden to try the script outside AWS.
set -uo pipefail

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
LOGTASKS_DIR="${LOGTASKS_DIR:-/opt/elasticbeanstalk/config/private/logtasks}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
APP_DIR="${APP_DIR:-/var/app/current}"
ENV_FILE="${ENV_FILE:-/opt/elasticbeanstalk/deployment/env}"
APP_USER="${APP_USER:-webapp}"
LOG_FILE="${LOG_FILE:-/var/log/team5_sync.log}"
UNIT="team5-sync"

log() {
    echo "[sync-timer] $*"
}

write_units() {
    cat > "$SYSTEMD_DIR/$UNIT.service" <<UNIT_FILE
[Unit]
Description=Sync NYC Parks events from the city feed
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/bin/bash -c 'source /var/app/venv/*/bin/activate && exec python3 manage.py sync_events'
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
UNIT_FILE

    cat > "$SYSTEMD_DIR/$UNIT.timer" <<UNIT_FILE
[Unit]
Description=Run the NYC Parks events sync every hour

[Timer]
OnCalendar=*-*-* *:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT_FILE
}

# The log is written by the app user, and `eb logs` should include it.
prepare_log() {
    touch "$LOG_FILE" && chown "$APP_USER" "$LOG_FILE" 2>/dev/null
    for task in tail bundle; do
        mkdir -p "$LOGTASKS_DIR/$task" &&
            echo "$LOG_FILE" > "$LOGTASKS_DIR/$task/$UNIT.conf"
    done
}

if ! write_units; then
    log "Could not write the systemd units; the hourly sync is not scheduled."
    exit 0
fi
prepare_log || log "Could not set up $LOG_FILE; runs will log to the journal."

if "$SYSTEMCTL" daemon-reload && "$SYSTEMCTL" enable --now "$UNIT.timer"; then
    log "Hourly sync scheduled ($UNIT.timer)."
else
    log "Could not enable $UNIT.timer; the hourly sync is not scheduled."
fi
exit 0
