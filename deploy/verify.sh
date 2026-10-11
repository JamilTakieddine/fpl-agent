#!/usr/bin/env bash
# Prove the deployment works the way it will really run (D41): Cloud Scheduler starts a check
# run with ITS OWN service account, and that run succeeds. Testing with `gcloud run jobs execute`
# as the project owner proves nothing about the scheduler's permissions; that gap missed GW6.
#
# A check run is safe: it only reads from FPL, records this week's snapshot and odds, and points
# the three triggers at the next deadline. (In a reminder week, D40, it also re-sends that email.)
# Run by deploy.sh; run it yourself after uploading a fresh FPL login. Exits non-zero on failure.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

PROJECT="${FPL_GCP_PROJECT:?set FPL_GCP_PROJECT}"
REGION="${FPL_REGION:-us-east1}"
JOB="fpl-agent"
TRIGGER="fpl-agent-check"
fail() { printf '\nVERIFY FAILED: %s\n' "$*" >&2; exit 1; }

# The fraction keeps same-second times comparable as strings ("...:27.65Z" > "...:27.000000Z").
since="$(date -u +%Y-%m-%dT%H:%M:%S).000000Z"
gcloud scheduler jobs run "$TRIGGER" --location "$REGION" --project "$PROJECT"
echo "Triggered ${TRIGGER} through Cloud Scheduler at ${since%.*}Z; waiting for its attempt..."

attempt="" code=""
for _ in $(seq 1 24); do  # up to 2 minutes
  read -r attempt code < <(gcloud scheduler jobs describe "$TRIGGER" --location "$REGION" \
    --project "$PROJECT" --format='value(lastAttemptTime,status.code)' | tr '\t' ' ')
  [[ -n "$attempt" && "$attempt" > "$since" ]] && break
  attempt=""; sleep 5
done
[[ -n "$attempt" ]] || fail "Cloud Scheduler made no attempt within 2 minutes."
# An empty status is success; 7 is PERMISSION_DENIED (the GW6 failure).
[[ -z "$code" || "$code" == "0" ]] || fail "Cloud Scheduler's call was refused (status code ${code}). See D41."
echo "Cloud Scheduler's call was accepted."

execution=""
for _ in $(seq 1 12); do
  execution="$(gcloud run jobs executions list --job "$JOB" --region "$REGION" --project "$PROJECT" \
    --filter="metadata.creationTimestamp>=${since%.*}Z" --sort-by=~metadata.creationTimestamp \
    --limit 1 --format='value(name)')"
  [[ -n "$execution" ]] && break
  sleep 5
done
[[ -n "$execution" ]] || fail "the scheduler's call was accepted but no run of ${JOB} started."
echo "Run ${execution} started; waiting for it to finish..."

for _ in $(seq 1 60); do  # up to 10 minutes
  read -r finished ok bad < <(gcloud run jobs executions describe "$execution" --region "$REGION" \
    --project "$PROJECT" --format='value(status.completionTime,status.succeededCount,status.failedCount)' |
    awk -F'\t' '{print ($1 == "" ? "-" : $1), ($2 == "" ? 0 : $2), ($3 == "" ? 0 : $3)}')
  [[ "$finished" != "-" ]] && break
  sleep 10
done
[[ "$finished" != "-" ]] || fail "run ${execution} didn't finish within 10 minutes."
[[ "$ok" == "1" ]] || fail "run ${execution} failed (see its logs; an alert email is on its way)."

echo
echo "VERIFIED: Cloud Scheduler started ${execution} with its own account, and it succeeded."
gcloud scheduler jobs list --location "$REGION" --project "$PROJECT" --format='table(name.basename(),schedule,state)'
