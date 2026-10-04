"""Move the FPL login to Secret Manager: python -m fpl_agent.cloud.upload_token   (D36)

Run once at deploy, and again after any fresh browser login (spikes/phase0_auth.py --fresh):
1. reads the local token (importing a newer browser login if there is one);
2. saves it as the secret's only enabled version and clears a `needs-relogin` flag;
3. retires the local token file (renamed .moved-to-secret-manager): from now on there must be
   ONE copy of the refresh token, or sending a stale one would revoke the login (D12).
Needs FPL_TOKEN_STORE=secret-manager, FPL_GCP_PROJECT and FPL_BUCKET (in .env), and holds the
run lock so it can't overlap a cloud run.
"""

from __future__ import annotations

import sys

from fpl_agent.auth import FileTokenStore
from fpl_agent.cloud.tokens import GoogleSecretVersions, SecretManagerTokenStore
from fpl_agent.config import load_settings
from fpl_agent.stores import TOKEN_SECRET, cloud_config, token_guard


def main() -> int:
    settings = load_settings()
    cloud = cloud_config()
    if cloud is None:
        print("Set FPL_TOKEN_STORE=secret-manager, FPL_GCP_PROJECT and FPL_BUCKET first (.env).")
        return 1
    retired = settings.token_file.with_suffix(".moved-to-secret-manager")
    state = settings.phase0_state_file
    # Already uploaded once: only a FRESH browser login may replace the cloud's token. The old
    # browser state holds a long-replaced refresh token; uploading it would revoke the login (D12).
    uploaded = retired.exists() and not settings.token_file.exists()
    if uploaded and (not state.exists() or state.stat().st_mtime <= retired.stat().st_mtime):
        print("No fresh login since the last upload. Run first:")
        print("  python spikes/phase0_auth.py --fresh")
        return 1
    local = FileTokenStore(settings.token_file, import_from=state)
    with token_guard():
        tokens = local.load()
        store = SecretManagerTokenStore(GoogleSecretVersions(cloud.project, TOKEN_SECRET))
        store.save(tokens)
        store.clear_relogin()
    if settings.token_file.exists():
        settings.token_file.replace(retired)
    print(f"Uploaded the FPL login to Secret Manager ({TOKEN_SECRET}); local copy retired.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
