#!/bin/bash
#
# Copyright 2026 Sour Labs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Report whether Vinegar is still running, on two channels with different
# blind spots.
#
# The heartbeat to healthchecks.io is the one that matters. A watchdog on this
# machine cannot tell you this machine is off, so the signal has to be the
# silence: healthchecks.io alerts when the pings stop, which covers a reboot
# parked at the FileVault screen, a power cut, and a dead network alike.
#
# The ntfy push is the fast path, for the case the watchdog is still alive to
# observe: Vinegar crashed but the Mac did not. That one arrives in seconds
# instead of after the grace window.

set -u

HOME_DIR="${VINEGAR_HOME:-$HOME/.vinegar}"
CONF="$HOME_DIR/watchdog.env"
PIDFILE="$HOME_DIR/vinegar.pid"

HEALTHCHECK_URL=""
NTFY_TOPIC=""
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1"
}

# Liveness is the pid file, not the log file. Vinegar logs per event rather
# than per poll, so a healthy daemon watching quiet repos writes nothing for
# hours and log freshness would report it dead.
#
# The pid has to be checked against what is actually running under it, not just
# for existence. Vinegar never removes the file, so it outlives every exit and
# from then until the next start it names a process that is gone. Pids get
# reused, so that number can come back as somebody else's process. `kill -0`
# answers "is this number taken", which is then a yes, and the watchdog reports
# a dead Vinegar as healthy and sends nothing. That is the one failure a
# watchdog cannot have, and it is not hypothetical: a reboot handed pid 780 to
# a system extension while the daemon was down. Matching the command line
# answers "is this number our daemon" instead, which is the actual question.
is_alive() {
    [ -r "$PIDFILE" ] || return 1
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    case "$pid" in
        '' | *[!0-9]*) return 1 ;;
    esac
    ps -p "$pid" -o command= 2>/dev/null | grep -q 'vinegar\.py'
}

# A single failed check is not news, and alerting on one is how a watchdog
# teaches you to ignore it. Two cases produce a down reading that fixes itself
# within seconds: at login, launchd starts this agent and the daemon at once,
# and this one can win the race to run before the daemon has taken its lock;
# and after a crash, KeepAlive brings the daemon back, but no sooner than the
# plist's ThrottleInterval of 30 seconds. So confirm over a window longer than
# both before saying anything.
alive=0
attempt=1
while [ "$attempt" -le 4 ]; do
    if is_alive; then
        alive=1
        break
    fi
    [ "$attempt" -lt 4 ] && sleep 15
    attempt=$((attempt + 1))
done

# Nothing is logged on a healthy pass. This runs every five minutes forever,
# and a line each time is 6MB a year saying nothing happened, which is the
# only thing here that grows enough to need rotating. The heartbeat is already
# recorded at healthchecks.io, so a local copy of it is duplication. Only the
# things worth reading later get written: a confirmed outage, or a send that
# failed, which means the alerting itself is broken.
if [ "$alive" = "1" ]; then
    if [ -n "$HEALTHCHECK_URL" ]; then
        curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECK_URL" \
            || log "heartbeat failed to send"
    fi
    exit 0
fi

log "vinegar is NOT running, confirmed over 45s"

# Tell healthchecks.io explicitly rather than waiting for the grace window.
# A crash that the watchdog can see should not take 15 minutes to report.
if [ -n "$HEALTHCHECK_URL" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECK_URL/fail" \
        || log "failure ping did not send"
fi

if [ -n "$NTFY_TOPIC" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null \
        -H "Title: Vinegar is down" \
        -H "Priority: high" \
        -H "Tags: rotating_light" \
        -d "No Vinegar process on $(hostname -s). Pull requests are not being reviewed. Check $HOME_DIR/logs/vinegar.err.log" \
        "https://ntfy.sh/$NTFY_TOPIC" \
        || log "ntfy push did not send"
fi
