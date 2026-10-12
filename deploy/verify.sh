#!/usr/bin/env bash
# Prove the deployment works the way it will really run (D41, D42): Cloud Scheduler starts runs
# with ITS OWN service account, and they succeed. Testing with `gcloud run jobs execute` as the
# project owner proves nothing about the scheduler's permissions; that gap missed GW6.
#
# Proves two triggers:
# - fpl-agent-check: a check run only reads from FPL, records this week's snapshot and odds, and
#   points the three gameweek triggers at the next deadline. (In a reminder week, D40, it also
#   re-sends that email.)
# - fpl-agent-watchdog: reads the triggers and the bucket, never FPL's login (D42).
# Run by deploy.sh; run it yourself after uploading a fresh FPL login. Exits non-zero on failure.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

PROJECT="${FPL_GCP_PROJECT:?set FPL_GCP_PROJECT}"
REGION="${FPL_REGION:-us-east1}"
JOB="fpl-agent"
fail() { printf '\nVERIFY FAILED: %s\n' "$*" >&2; exit 1; }

prove() {  # trigger name: force it through Cloud Scheduler, require an accepted call and a good run
  local trigger="$1" since attempt code execution finished ok bad
  # The fraction keeps same-second times comparable as strings ("...:27.65Z" > "...:27.000000Z").
  since="$(date -u +%Y-%m-%dT%H:%M:%S).000000Z"
  gcloud scheduler jobs run "$trigger" --location "$REGION" --project "$PROJECT"
  echo "Triggered ${trigger} through Cloud Scheduler at ${since%.*}Z; waiting for its attempt..."

  attempt="" code=""
  for _ in $(seq 1 24); do  # up to 2 minutes
    read -r attempt code < <(gcloud scheduler jobs describe "$trigger" --location "$REGION" \
      --project "$PROJECT" --format='value(lastAttemptTime,status.code)' | tr '\t' ' ')
    [[ -n "$attempt" && "$attempt" > "$since" ]] && break
    attempt=""; sleep 5
  done
  [[ -n "$attempt" ]] || fail "${trigger}: Cloud Scheduler made no attempt within 2 minutes."
  # An empty status is success; 7 is PERMISSION_DENIED (the GW6 failure).
  [[ -z "$code" || "$code" == "0" ]] ||
    fail "${trigger}: Cloud Scheduler's call was refused (status code ${code}). See D41."
  echo "Cloud Scheduler's call was accepted."

  execution=""
  for _ in $(seq 1 12); do
    execution="$(gcloud run jobs executions list --job "$JOB" --region "$REGION" --project "$PROJECT" \
      --filter="metadata.creationTimestamp>=${since%.*}Z" --sort-by=~metadata.creationTimestamp \
      --limit 1 --format='value(name)')"
    [[ -n "$execution" ]] && break
    sleep 5
  done
  [[ -n "$execution" ]] || fail "${trigger}: the call was accepted but no run of ${JOB} started."
  echo "Run ${execution} started; waiting for it to finish..."

  for _ in $(seq 1 60); do  # up to 10 minutes
    read -r finished ok bad < <(gcloud run jobs executions describe "$execution" --region "$REGION" \
      --project "$PROJECT" --format='value(status.completionTime,status.succeededCount,status.failedCount)' |
      awk -F'\t' '{print ($1 == "" ? "-" : $1), ($2 == "" ? 0 : $2), ($3 == "" ? 0 : $3)}')
    [[ "$finished" != "-" ]] && break
    sleep 10
  done
  [[ "$finished" != "-" ]] || fail "${trigger}: run ${execution} didn't finish within 10 minutes."
  [[ "$ok" == "1" ]] || fail "${trigger}: run ${execution} failed (see its logs; an alert email is on its way)."
  echo "VERIFIED: Cloud Scheduler started ${execution} (${trigger}) with its own account, and it succeeded."
  echo
}

prove fpl-agent-check
prove fpl-agent-watchdog
gcloud scheduler jobs list --location "$REGION" --project "$PROJECT" --format='table(name.basename(),schedule,state)'
