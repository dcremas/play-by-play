#!/usr/bin/env bash
# One-time AWS wiring for the morning job, run from the laptop with its admin credentials.
#
#     bash deploy/aws-setup.sh
#
# 1. Lets the box's instance role publish one metric namespace, PbpPipeline -- the same
#    one-namespace-per-policy shape as pg-backup-metrics and cwagent-metrics.
# 2. The pbp-daily-stale alarm: HoursSinceSuccess above 27, or no datapoint at all, emails
#    through pg-backup-alerts, which is the box's general alerts topic in practice.
#
# Run it AFTER the first successful `systemctl start pbp-daily`; before that the heartbeat
# reports 9999 hours and the alarm would fire at once.
set -euo pipefail

REGION=us-east-2
ROLE=prosite-ses-role
# The account id is looked up, not written down: this repository is public.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TOPIC="arn:aws:sns:$REGION:$ACCOUNT:pg-backup-alerts"

aws iam put-role-policy --role-name "$ROLE" --policy-name pbp-pipeline-metrics \
    --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublishPbpPipelineNamespaceOnly",
    "Effect": "Allow",
    "Action": "cloudwatch:PutMetricData",
    "Resource": "*",
    "Condition": {"StringEquals": {"cloudwatch:namespace": "PbpPipeline"}}
  }]
}'
echo "policy pbp-pipeline-metrics on $ROLE"

# A datapoint must exist BEFORE the alarm does: it treats missing data as breaching, so an
# alarm created over an empty metric emails a false alert at once. IAM changes take a few
# seconds to reach the instance role, so retry the heartbeat until CloudWatch shows a value.
echo "publishing a first heartbeat from the box"
for _ in $(seq 1 12); do
    ssh awsvm 'sudo systemctl start pbp-daily-heartbeat'
    got=$(aws cloudwatch get-metric-statistics --region "$REGION" \
        --namespace PbpPipeline --metric-name HoursSinceSuccess --statistics Maximum \
        --start-time "$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --period 300 --query 'Datapoints[0].Maximum' --output text)
    [[ "$got" != None && -n "$got" ]] && { echo "heartbeat landed: $got hours since success"; break; }
    sleep 20
done
[[ "$got" != None && -n "$got" ]] || { echo "no heartbeat in CloudWatch after 4 minutes; alarm NOT created" >&2; exit 1; }

aws cloudwatch put-metric-alarm --region "$REGION" \
    --alarm-name pbp-daily-stale \
    --alarm-description "Play-by-play morning job (pbp-daily on the EC2 box) has not succeeded in over 27 hours. sudo journalctl -u pbp-daily -e" \
    --namespace PbpPipeline --metric-name HoursSinceSuccess \
    --statistic Maximum --period 3600 --evaluation-periods 1 \
    --threshold 27 --comparison-operator GreaterThanThreshold \
    --treat-missing-data breaching \
    --alarm-actions "$TOPIC" --ok-actions "$TOPIC"
echo "alarm pbp-daily-stale -> pg-backup-alerts"
