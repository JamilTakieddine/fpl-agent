#!/usr/bin/env bash
# Deploy the FPL agent to Google Cloud (D36). Safe to re-run: creates what's missing, updates the
# job's image. Needs: gcloud logged in, a project with billing, and in the environment (or .env):
#   FPL_GCP_PROJECT, FPL_ENTRY_ID, FPL_H2H_LEAGUE_ID, FPL_EMAIL
# Optional: FPL_REGION (default us-east1), FPL_BUCKET (default <project>-fpl-agent-state).
# The Gmail app password is asked for (hidden) the first time; it goes straight to Secret Manager.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a

PROJECT="${FPL_GCP_PROJECT:?set FPL_GCP_PROJECT}"
REGION="${FPL_REGION:-us-east1}"
BUCKET="${FPL_BUCKET:-${PROJECT}-fpl-agent-state}"
JOB="fpl-agent"
RUNNER="fpl-agent-runner@${PROJECT}.iam.gserviceaccount.com"     # what the job runs as
INVOKER="fpl-agent-scheduler@${PROJECT}.iam.gserviceaccount.com" # what Scheduler triggers it as
: "${FPL_ENTRY_ID:?}" "${FPL_H2H_LEAGUE_ID:?}" "${FPL_EMAIL:?}"

say() { printf '\n== %s\n' "$*"; }
gcloud config set project "$PROJECT" >/dev/null

say "APIs"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  storage.googleapis.com

say "Bucket gs://${BUCKET} (snapshots, saved odds, the run lock)"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 ||
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" --uniform-bucket-level-access

say "Secrets"
ensure_secret() { gcloud secrets describe "$1" >/dev/null 2>&1 || gcloud secrets create "$1" --replication-policy=automatic; }
ensure_secret fpl-tokens
ensure_secret fpl-email
ensure_secret fpl-email-app-password
if [[ -z "$(gcloud secrets versions list fpl-email --filter=state:ENABLED --format='value(name)')" ]]; then
  printf '%s' "$FPL_EMAIL" | gcloud secrets versions add fpl-email --data-file=-
fi
if [[ -z "$(gcloud secrets versions list fpl-email-app-password --filter=state:ENABLED --format='value(name)')" ]]; then
  # From a private file if present (never typed anywhere a transcript could keep it), else asked.
  PW_FILE=".secrets/gmail_app_password"
  if [[ -f "$PW_FILE" ]]; then
    APP_PASSWORD="$(tr -d ' \n' < "$PW_FILE")"
  else
    read -r -s -p "Gmail app password (input hidden): " APP_PASSWORD; echo
  fi
  printf '%s' "${APP_PASSWORD// /}" | gcloud secrets versions add fpl-email-app-password --data-file=-
  unset APP_PASSWORD
  rm -f "$PW_FILE"  # it lives in Secret Manager now
fi

say "Service accounts and permissions (least privilege)"
ensure_sa() { gcloud iam service-accounts describe "$1@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 || gcloud iam service-accounts create "$1" --display-name="$2"; }
ensure_sa fpl-agent-runner "FPL agent job"
ensure_sa fpl-agent-scheduler "FPL agent scheduler trigger"
# The token secret: read, add versions, disable old ones, set the needs-relogin label.
gcloud secrets add-iam-policy-binding fpl-tokens --member="serviceAccount:${RUNNER}" --role=roles/secretmanager.admin >/dev/null
for s in fpl-email fpl-email-app-password; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:${RUNNER}" --role=roles/secretmanager.secretAccessor >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --member="serviceAccount:${RUNNER}" --role=roles/storage.objectAdmin >/dev/null
# Re-pointing its own schedules (and acting as the trigger account those schedules use).
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUNNER}" --role=roles/cloudscheduler.admin --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$INVOKER" --member="serviceAccount:${RUNNER}" --role=roles/iam.serviceAccountUser >/dev/null

# Building from source runs as the project's default compute account, which new projects
# (since 2024) no longer let read the uploaded source or push the image: grant the builder role.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/cloudbuild.builds.builder --condition=None >/dev/null

say "Job ${JOB}: build and deploy (one task, no parallel runs, live saving on)"
gcloud run jobs deploy "$JOB" --source . --region "$REGION" \
  --memory 4Gi --cpu 2 --task-timeout 15m --max-retries 0 --tasks 1 --parallelism 1 \
  --service-account "$RUNNER" \
  --set-env-vars "FPL_TOKEN_STORE=secret-manager,FPL_GCP_PROJECT=${PROJECT},FPL_BUCKET=${BUCKET},FPL_REGION=${REGION},FPL_LIVE=1,FPL_ENTRY_ID=${FPL_ENTRY_ID},FPL_H2H_LEAGUE_ID=${FPL_H2H_LEAGUE_ID}" \
  --set-secrets "FPL_EMAIL=fpl-email:latest,FPL_EMAIL_APP_PASSWORD=fpl-email-app-password:latest"
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" --member="serviceAccount:${INVOKER}" --role=roles/run.invoker >/dev/null

say "Scheduler jobs (placeholder times: the first run points them at the real deadlines)"
for mode in check save final; do
  where=(--location "$REGION")
  target=(--uri "https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
    --http-method POST --oauth-service-account-email "$INVOKER"
    --message-body "{\"overrides\":{\"containerOverrides\":[{\"args\":[\"--mode\",\"${mode}\"]}]}}")
  if gcloud scheduler jobs describe "fpl-agent-${mode}" "${where[@]}" >/dev/null 2>&1; then
    # No --schedule on update: keep the time the job last set itself, so a redeploy can't reset
    # the real schedule to the placeholder. (Updates also name the header flag differently.)
    gcloud scheduler jobs update http "fpl-agent-${mode}" "${where[@]}" "${target[@]}" \
      --update-headers Content-Type=application/json >/dev/null
  else
    gcloud scheduler jobs create http "fpl-agent-${mode}" "${where[@]}" "${target[@]}" \
      --schedule "0 0 1 1 *" --time-zone Etc/UTC --headers Content-Type=application/json >/dev/null
  fi
done

say "Done. Next (once): upload the FPL login, then run a check to set the real schedule:"
echo "  python -m fpl_agent.cloud.upload_token"
echo "  gcloud run jobs execute ${JOB} --region ${REGION} --args=--mode,check --wait"
