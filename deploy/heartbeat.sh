#!/usr/bin/env bash
# Publish hours since the morning job last succeeded, as PbpPipeline/HoursSinceSuccess.
# The pbp-daily-stale alarm (deploy/aws-setup.sh) fires above 27 hours, so one missed morning
# is an email, whatever the reason: a failed run, a stopped timer, or a box that never woke.
#
# Always exits 0, like the box's pg-backup-heartbeat.sh: an unpublishable datapoint is
# missing data, and the alarm treats missing data as breaching.
STAMP=/opt/pbp-etl/data/state/last_success

if [[ -s "$STAMP" ]]; then
    hours=$(( ($(date -u +%s) - $(cat "$STAMP")) / 3600 ))
else
    hours=9999      # never succeeded
fi

aws cloudwatch put-metric-data --region us-east-2 \
    --namespace PbpPipeline --metric-name HoursSinceSuccess \
    --unit Count --value "$hours" \
    || echo "put-metric-data failed (hours=$hours); the alarm will see missing data"
exit 0
