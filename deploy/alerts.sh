#!/usr/bin/env bash
# Email alerts that don't depend on the job (D41). The job emails its own failures, but if it
# never starts (GW6: Cloud Scheduler's call was refused) it can't, and silence looks like a
# quiet week. Cloud Monitoring watches the logs instead and emails FPL_EMAIL when:
#   - a scheduled trigger fails (Cloud Scheduler logs an ERROR for the attempt), or
#   - a run of the job fails (crash, timeout, error exit: Cloud Run logs an ERROR system event).
# Safe to re-run: finds the email channel and the two policies by name, creates what's missing,
# and re-applies the alert strategy to existing ones (so a change here reaches them).
# Uses the Monitoring REST API (gcloud only has alpha/beta commands for alert policies).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

PROJECT="${FPL_GCP_PROJECT:?set FPL_GCP_PROJECT}"
: "${FPL_EMAIL:?set FPL_EMAIL}"
API="https://monitoring.googleapis.com/v3/projects/${PROJECT}"
mon() {
  curl -sfS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "x-goog-user-project: ${PROJECT}" -H "Content-Type: application/json" "$@"
}

gcloud services enable monitoring.googleapis.com --project "$PROJECT"

CHANNEL="$(mon "$API/notificationChannels" | EMAIL="$FPL_EMAIL" python3 -c '
import json, os, sys
chans = json.load(sys.stdin).get("notificationChannels", [])
print(next((c["name"] for c in chans if c.get("labels", {}).get("email_address") == os.environ["EMAIL"]), ""))')"
if [[ -z "$CHANNEL" ]]; then
  CHANNEL="$(EMAIL="$FPL_EMAIL" python3 -c '
import json, os
print(json.dumps({"type": "email", "displayName": "FPL agent owner",
                  "labels": {"email_address": os.environ["EMAIL"]}}))' |
    mon -X POST "$API/notificationChannels" -d @- | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
  echo "created the email channel"
fi

# At most one email per 5 minutes per policy; later failures still email while an alert is open
# (verified, D42). Alerts close 30 minutes (the minimum) after opening, so the open-alert list
# shows current problems rather than old ones.
STRATEGY='{"notificationRateLimit": {"period": "300s"}, "autoClose": "1800s"}'

EXISTING="$(mon "$API/alertPolicies" | python3 -c '
import json, sys
print("\n".join(p["displayName"] + "\t" + p["name"] for p in json.load(sys.stdin).get("alertPolicies", [])))')"

ensure_alert() {  # name, log filter, what the email tells you to do
  local existing
  existing="$(awk -F'\t' -v n="$1" '$1 == n {print $2}' <<<"$EXISTING")"
  if [[ -n "$existing" ]]; then
    mon -X PATCH "https://monitoring.googleapis.com/v3/${existing}?updateMask=alertStrategy" \
      -d "{\"alertStrategy\": ${STRATEGY}}" >/dev/null
    echo "alert exists (strategy re-applied): $1"
    return
  fi
  NAME="$1" FILTER="$2" DOC="$3" CHANNEL="$CHANNEL" STRATEGY="$STRATEGY" python3 -c '
import json, os
e = os.environ
print(json.dumps({
    "displayName": e["NAME"],
    "combiner": "OR",
    "conditions": [{"displayName": e["NAME"], "conditionMatchedLog": {"filter": e["FILTER"]}}],
    "alertStrategy": json.loads(e["STRATEGY"]),
    "notificationChannels": [e["CHANNEL"]],
    "severity": "CRITICAL",
    "documentation": {"mimeType": "text/markdown",
                      "subject": "⚠️ ACTION NEEDED - FPL agent: " + e["NAME"],
                      "content": e["DOC"]},
}))' | mon -X POST "$API/alertPolicies" -d @- >/dev/null
  echo "created alert: $1 (new log alerts take a few minutes to become active)"
}

ensure_alert "a scheduled run could not start" \
  'resource.type="cloud_scheduler_job" AND resource.labels.job_id=~"^fpl-agent-" AND severity>=ERROR' \
  "Cloud Scheduler tried to start the fpl-agent job and failed, so the agent did NOT run: nothing was saved to FPL and no summary email will come. If a deadline is near, set your team on the FPL site yourself. Then open Claude Code in fpl-agent and paste this email (see D41)."
ensure_alert "a run failed" \
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"fpl-agent\" AND logName=\"projects/${PROJECT}/logs/cloudaudit.googleapis.com%2Fsystem_event\" AND severity>=ERROR" \
  "A run of the fpl-agent job failed (crashed, timed out or exited with an error). Its own summary email may not have been sent. If a deadline is near, check your team on the FPL site. Then open Claude Code in fpl-agent and paste this email (see D41)."
