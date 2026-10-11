# Deploying the agent to Google Cloud

The agent runs as a Cloud Run job, three times per gameweek (D36):

| Run | When | What |
|---|---|---|
| `check` | 24 h before the deadline | Refreshes the FPL login (proving it works), records flag/odds snapshots. Emails only if something needs you. |
| `save` | 60 min before | Picks and **saves** the lineup, emails the summary. |
| `final` | 15 min before | Re-runs with the latest news; saves and emails only if the pick changed or something failed. |

After every run the job points the three Cloud Scheduler jobs at the next deadline (read from FPL).

## One-time setup
1. A Google Cloud project with billing, `gcloud auth login`, and
   `gcloud auth application-default login` (lets the Python code on your Mac use Google Cloud),
   then `gcloud auth application-default set-quota-project <project>`. Add a budget alert.
2. A **Gmail app password** for the account that sends the emails
   (<https://myaccount.google.com/apppasswords>; needs 2-Step Verification). Copy it, then run
   `pbpaste > .secrets/gmail_app_password && chmod 600 .secrets/gmail_app_password`, so it is
   never typed anywhere a transcript could keep it. The deploy script uploads it to Secret Manager
   and deletes the file. Without the file, it asks for it with hidden input.
3. In `.env`: `FPL_GCP_PROJECT`, `FPL_EMAIL` (plus the usual `FPL_ENTRY_ID`, `FPL_H2H_LEAGUE_ID`).
4. `./deploy/deploy.sh`. It's safe to re-run, and re-running it is also how you ship code changes.
5. Move the FPL login to the cloud. Add `FPL_TOKEN_STORE=secret-manager` and `FPL_BUCKET` to
   `.env`, then run `python -m fpl_agent.cloud.upload_token`. From now on **every** command,
   local ones included, uses that single token. The local token file is retired, because two
   copies would end in a revoked login.
6. Prove the real path and set the real schedule: `./deploy/verify.sh`. Cloud Scheduler starts a
   `check` run with its own service account, and the script fails loudly unless that run succeeds.
   (`deploy.sh` does this itself once a login is uploaded.) Don't test with
   `gcloud run jobs execute`: it runs as you, the project owner, and proves nothing about the
   scheduler's permissions. That gap is how GW6 was missed (D41).

## Alerts (D41)
`deploy.sh` also sets up two Cloud Monitoring alerts. They email `FPL_EMAIL` with the subject
"⚠️ ACTION NEEDED - FPL agent: …" if a scheduled trigger can't start the job, or if a run fails.
They watch the logs, so they work even when the job never starts and can't email you itself.
`./deploy/alerts.sh` re-creates anything missing.

## When the email says "log in again"
```bash
python spikes/phase0_auth.py --fresh       # browser login on your Mac
python -m fpl_agent.cloud.upload_token     # becomes the cloud's only token
```

## Costs
A few runs a week of about a minute each. This normally fits the free tiers of Cloud Run,
Scheduler (3 jobs), Secret Manager and Cloud Storage in us-east1. A budget alert guards against
surprises.
