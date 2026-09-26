# Decision log

Why the project is built the way it is. One entry per decision that had real alternatives,
newest at the bottom. The format for each is context, what we chose, what we didn't choose and why,
and what it means for later work.

Standing design rules (dry run by default, LLMs only at the edges, rules as pure functions) live in
`CLAUDE.md`. This file records the specific calls made while building. What to re-check and how often:
`docs/maintenance.md`.

---

## D1. Phase 0 auth: capture the session from a browser login, not from scripted credentials

*Phase 0, 2026-09*

**Context.** FPL login goes through `account.premierleague.com` (a redirect/SSO flow, possibly with bot
checks). We need to know which auth the FPL API accepts before designing the cloud job.

**Decision.** Open a browser, let a human log in, and record whatever auth the site's own API calls use
(the `x-api-authorization` / `authorization` headers and cookies).

**Alternatives.**
- *Script the login form with email and password.* It breaks whenever the login page changes, may trip
  bot protection, and means storing the password. Not worth it before we know what the API wants.
- *Copy the token from DevTools by hand.* No automation needed, but it's a manual step every time the
  token expires, and it doesn't tell us which headers vs cookies matter.

**Consequences.** Phase 0 answers "what auth, and how long does it last?" The cloud job (Phase 4) still
needs a way to refresh the session with no browser. That is still an open question (see below).

## D2. Use an isolated Playwright Chromium, not the user's own Chrome profile

*Phase 0, 2026-09*

**Context.** Logging in through your normal Chrome would be convenient, since you might already be logged in.

**Decision.** Launch a clean Playwright Chromium with its own empty context.

**Alternatives.**
- *`launch_persistent_context` on the real Chrome profile.* Since Chrome 136, Chrome refuses to be
  controlled by automation tools when it's using the default profile folder (an anti-cookie-theft
  measure). Chrome must also be fully closed, because a profile can only be open in one Chrome
  process at a time. And it would give a throwaway script access to every site you're logged
  into (Gmail, banking, etc.).
- *Chrome extension / remote debugging into the running browser.* Same exposure, more moving parts.

**Consequences.** The captured session has only the FPL cookies and headers, which makes it clear what
auth is actually needed, and it matches the cloud job, which has no personal browser either. Having to log
in every run is fixed by D4.

## D3. Listen on the browser context and wait with sleep + polling (fix for `TargetClosedError`)

*Phase 0, 2026-09*

**Context.** The first version attached the request listener to one `page` and waited with
`page.wait_for_timeout`. The login flow opens or replaces tabs, which killed that page handle and
crashed the script with `TargetClosedError`.

**Decision.** `ctx.on("request", ...)` on the whole browser context, and a `time.sleep` loop that polls
`/api/me/` through `ctx.request`.

**Alternatives.**
- *Follow the popup with `page.expect_popup()`.* This only works if we know the exact flow ahead of time,
  and it breaks when the flow changes.
- *`ctx.wait_for_event(...)`.* We'd have to know which event marks "logged in". Polling `/api/me/` asks the
  API directly whether we're logged in.

**Consequences.** Sync Playwright only runs event handlers while some Playwright call is in progress.
The poll's `ctx.request.get` is that call, so captured headers can lag by up to one poll interval (3s).
That's fine for a login done by hand.

Closing the browser now gives a clear exit, and the closed-browser check has two signals:
- The browser's `disconnected` event.
- No open tabs for **two** polls in a row. One empty poll could just be the login flow swapping tabs.

Other Playwright errors (network failures, etc.) print their own message instead of being reported as
"browser closed".

## D4. Reuse the saved session with a browserless HTTP client before opening a browser

*Phase 0, 2026-09*

**Context.** A full browser login every run is slow, and it's not how the agent will run in the cloud.

**Decision.** If `.secrets/fpl_state.json` exists, load it into `pw.request.new_context(storage_state=...)`,
a plain HTTP client with no browser, and add the saved headers from `.secrets/fpl_auth.json`. If `/api/me/`
still shows a logged-in entry, skip the browser. Otherwise fall back to D1/D2. `--fresh` forces a new login.

**Alternatives.**
- *Launch a browser with the saved state.* This works too, but it opens a window for nothing and hides
  whether the session works with plain HTTP.
- *Use `requests` with the cookies copied over by hand.* This is closer to the final agent, but it means
  converting the cookie format ourselves. We can do that later once we know which cookies matter.

**Consequences.**
- This path is basically the cloud job's auth path, so each successful run confirms that plain HTTP with
  stored credentials works.
- The reuse path deliberately **doesn't re-save** the state files, so their modification time stays equal
  to the login time. "Saved Xh ago" on a failed reuse is then a direct measurement of how long a session lasts.

## D5. Refresh the access token over plain HTTP, and save the new tokens before using them

*Phase 0, 2026-09-24*

**Context.** Access tokens last 1 hour, but the saved state contains an OIDC refresh token. The cloud job
has no human and ideally no browser.

**Decision.** `spikes/phase0_refresh.py` POSTs `grant_type=refresh_token` plus `client_id` (a public SPA
client with no secret) to the token endpoint, which it finds via OIDC discovery (`{iss}/.well-known/openid-configuration`)
rather than hardcoding it. It uses `requests`, not Playwright. It writes the new tokens to disk **before**
testing them, with an atomic temp-file-plus-rename write and 0600 permissions.

**Alternatives.**
- *Keep a headless browser and let the site refresh its own token.* This works, but the cloud image would
  need Chromium (hundreds of MB), start more slowly and break more easily. One HTTP POST is enough.
- *Use the tokens first, then save them.* If the server rotates refresh tokens (it does, see Findings) and the
  script crashes between receiving and saving, the only valid token is lost and a human has to log in again.
- *Test whether the old refresh token still works after rotation.* We deliberately didn't. Many OIDC servers
  treat reuse of a rotated token as theft and revoke the whole session.

**Consequences.**
- The cloud job needs only `requests` and one stored secret, the latest refresh token. Every run must write
  the rotated token back to Secret Manager, and that write must succeed before the job does anything else.
- Only one process may refresh at a time. Two concurrent runs would each rotate the token, and one of
  them would end up holding a dead token.
- Refreshing rewrites `.secrets/fpl_state.json`, so its modification time no longer means "login time"
  (the D4 measurement). That no longer matters, because the refresh token's `exp` gives the lifetime directly.

---

## D6. Production-shaped tooling: pyproject.toml, ruff, mypy --strict, pre-commit

*Phase 0, 2026-09-24*

**Context.** The parent `CLAUDE.md` sets a production-shaped Python baseline for every project in the series
(`pyproject.toml`, ruff, mypy, pytest, pre-commit). This project's `CLAUDE.md` said `requirements.txt`,
which was a leftover rather than a deliberate exception. The agent runs unattended, writes to a real
account, and lives in a public repo, so it is the kind of project the baseline exists for.

**Decision.**
- **`pyproject.toml` is the single source of truth.** Runtime deps are only what the code imports
  (`playwright`, `requests`). Tooling goes in a `[dev]` extra so it stays out of the cloud image. `python-dotenv`
  was dropped until code actually reads `.env` (Phase 1). `packages = []` for now, because there's no package
  yet and setuptools auto-discovery can fail on folders like `spikes/`.
- **mypy `strict = true` from the start.** Adding strictness later means fixing a backlog of errors. Turning
  it on for the Phase 0 spikes surfaced 14 errors, all of them bare `dict`s and missing annotations; they were
  fixed with `Json` and `Auth` aliases.
- **ruff runs as the official pinned pre-commit hook**, so anyone who clones the repo gets the same version.
- **mypy runs as a local hook from `.venv/`.** The official mirrors-mypy hook runs in an isolated environment
  without playwright and types-requests installed, so it would quietly treat their objects as `Any` and check
  much less. The cost is that the hook needs `.venv/` to exist, which the README setup creates.
- **A `no-jwt` pygrep hook** refuses any commit containing a JWT-shaped string (`eyJ...eyJ...`). The repo is
  public and `.secrets/` holds a live 180-day refresh token. The hook was tested against a fake token and
  blocks it.

**Alternatives.**
- *Keep `requirements.txt`.* It works, but it has no place for tool config, no separation of dev and runtime
  deps, and it breaks the series convention.
- *uv / Poetry.* Faster installs and lockfiles, but they're another tool to learn and the baseline says venv.
  Revisit if dependency drift becomes a problem.
- *gitleaks for secret scanning.* Broader coverage, but it needs a Go binary. The pygrep hook covers the one
  secret format this project actually handles.

**Consequences.** Every commit is formatted, lint-clean and passes mypy strict. Tests are not run in the hook
(too slow); run pytest manually / in CI. When `fpl_agent/` is created, switch `[tool.setuptools]` to package
discovery and add `fpl_agent tests` to the mypy hook's entry.

---

## D7. Hosting: public GitHub repo, pushed over HTTPS with `gh` credentials

*Phase 0, 2026-09-25*

**Context.** The project is meant to be reusable by other FPL players (goal 3), and the repo needed a remote.

**Decision.** A public repo at `github.com/JamilTakieddine/fpl-agent`, created with `gh repo create`. Git uses
HTTPS with the `gh` login, which is stored in the macOS keychain (`gh auth setup-git`).

**Alternatives.**
- *SSH keys.* Also standard, but it means generating keys and uploading the public key, and `gh` is needed
  anyway for PRs and CI.
- *A private repo until the project is polished.* Safer against leaks, but the leak protection comes from
  `.gitignore`, the pre-commit `no-jwt` hook, and scanning history before the first push, not from keeping the
  repo private. Going public from the start keeps those habits honest.

**Consequences.** Everything committed is public right away. Before any push, check `git status` and make sure
the hooks passed. If a secret ever lands in a commit, rotate it first (for FPL, log in again to replace the
refresh token), then rewrite the history. Deleting the file in a later commit isn't enough.

---

## D8. Phase 1 data layer: pydantic at the boundary, swappable token store, protocols for HTTP

*Phase 1 step 1, 2026-09-25*

**Context.** Everything after Phase 1 should be pure functions over trusted inputs. The FPL API is
undocumented and changes between seasons, is flaky around deadlines, and needs a token that rotates.

**Decisions.**
- **Every response is validated with pydantic on arrival** (`fpl_agent/data/models.py`). Unknown fields are
  ignored (`extra="ignore"`) and models are frozen.
- **Money stays as integer tenths** (`now_cost: 60` = 6.0m), exactly as the API sends it. No floats.
- **`TokenStore` interface plus `TokenManager`** (`fpl_agent/auth.py`). The token is refreshed when it has
  less than 5 minutes left, and the new tokens are **saved before they're returned**. The HTTP session and
  the clock are passed in, so tests can fake expiry and revocation. A revoked token raises `AuthError`
  telling you to log in again.
- **The package owns its tokens in `.secrets/fpl_tokens.json`**, imported from the Phase 0 login state. It
  re-imports whenever the login state is newer, so `phase0_auth.py --fresh` is the re-login path. The old
  `phase0_refresh.py` now refuses to run, because its copy of the refresh token goes stale after the first
  rotation and replaying a rotated token can revoke the whole session.
- **GET-only retries** in the session (urllib3 `Retry`: 3 attempts, 1/2/4s backoff, on 429 and 5xx, honoring
  `Retry-After`). `bootstrap-static` is cached per client. There's a project `User-Agent`.
- **Code depends on small `Protocol`s** (`HttpSession`, `HttpResponse`, `AuthProvider`), not on
  `requests.Session`.
- **Tests use trimmed recordings of real responses** (`tests/fixtures/`, made by `scripts/record_fixtures.py`),
  never the live API. `python -m fpl_agent.data` is a separate read-only live check.

**Alternatives.**
- *Plain dataclasses or TypedDicts.* No runtime checks, so a renamed field would surface as a `KeyError`
  deep in the optimizer, or as a silently wrong value.
- *`extra="forbid"`.* It would crash whenever FPL adds a field, which is likely the week a new feature launches.
- *Float prices.* `0.1 + 0.2 != 0.3`, and the sell-on rule rounds down, so small float errors would cause
  off-by-0.1m mistakes.
- *A hand-written retry loop, or retrying POSTs too.* More code, and an auto-retried lineup save could be
  applied twice.
- *Mocking `requests` with `responses` or `requests-mock`.* Another dependency, and the tests would be tied
  to `requests` internals. With protocols, a 20-line fake is enough.
- *Committing the full `bootstrap-static`.* It's 1.9 MB, over the 500 KB large-file hook, and mostly unused
  fields. The trimmed version keeps the real shape at 157 KB.

**Consequences.**
- New endpoints follow the same pattern: add a model, a client method, a trimmed test fixture, and tests.
- Re-record the fixtures at the start of each season to catch schema changes early.
- The Phase 4 Secret Manager store only needs to implement `load` and `save`.

## D9. Odds source: Kalshi

*Superseded in part by D15: correct-score markets proved unusable; match-result markets are used.*

*Phase 1, 2026-09-25 (your choice; to be built in step 5)*

**Context.** Phase 2 simulates scorelines from each team's expected goals. FPL provides no odds.

**Decision.** Use Kalshi's public market data API (`api.elections.kalshi.com/trade-api/v2`). A probe found
Premier League series for match result (`KXEPLGAME`), **correct score** (`KXEPLSCORE`), first half, first team
to score, and team points. Correct-score prices are close to exactly what a scoreline model needs: each team's
expected goals can be fitted straight from them.

**Alternatives.** Bookmaker odds aggregators (e.g. The Odds API): wide coverage, but a limited free tier and
margins that need removing. Scraping bookmakers: fragile and against their terms. FPL's own `strength_*`
ratings: free, but coarse and rarely updated.

**Consequences / open.** How much trading these markets see is unknown. Thinly traded prices give noisy
probabilities, so step 5 must measure spreads and volume per match and define a fallback (for example,
match-result prices only, or FPL strength ratings) when a market is too thin. Market data needs no login.

---

## D10. Fixture calendar: per-team counts, recomputed every run, postponed matches kept

*Phase 1 step 2, 2026-09-25*

**Context.** Rule 7 says to track double and blank gameweeks for chip timing. Real data in late September
has none yet (all 38 gameweeks have 10 fixtures). They appear mid-season, when cup ties or TV changes
postpone a match (`event: null`) and it gets rescheduled into a gameweek where that team already plays. The
GW6 deadline is **90 minutes** before the first kickoff, not the usual 60.

**Decisions.**
- **Counts are per team** (`Gameweek.fixture_count`, including 0 for teams that don't play). The
  gameweek-level `is_double` / `is_blank` flags are derived from them. A gameweek can be both at once.
- **Postponed fixtures are kept in `Calendar.unscheduled`**, not dropped. They're the early warning of a future
  double gameweek.
- **Recompute from `/fixtures/` every run.** Never keep the calendar between weeks, because FPL moves fixtures.
- **`next_deadline()` uses `deadline_time` and a timezone-aware clock**, not FPL's `is_next` flag, and it rejects
  naive datetimes. The live check prints both as a cross-check.
- **Plain frozen dataclasses, not pydantic.** Pydantic is for checking outside data on arrival. The calendar is
  computed from models that have already been checked, so checking again would only cost time.

**Alternatives.**
- *A single `is_dgw` flag per gameweek.* It can't answer "how many of *my* players double?", which is what Bench
  Boost and Triple Captain value depends on.
- *Deadline = first kickoff minus 60 minutes.* It's already wrong for GW6 (90 minutes). The rule forbids it.
- *Trust `is_next`.* It flips on FPL's schedule, not ours, which could briefly point at a deadline that has
  already passed.

**Consequences.**
- Tests for doubles and blanks use small made-up seasons (the real data has none), plus one test on the
  recorded GW1–8.
- The Phase 5 planner combines `fixture_count` with the squad, and uses `window(start, 6)` for its horizon.

---

## D11. Opponent inputs: public endpoints, 5 gameweeks of captaincy, AVERAGE detected by null entry

*Phase 1 step 3, 2026-09-25*

**Context.** The H2H objective needs a model of this week's opponent, but their current picks are hidden until
the deadline (the upcoming gameweek's picks endpoint returns **404**). Everything needed is public: the H2H
fixtures, entry history (chips played), and picks for finished gameweeks.

**Decisions.**
- **`fpl_agent/data/opponent.py` returns an `OpponentSnapshot`**: who they are, their last finished squad,
  captain and vice-captain for the last **5** finished gameweeks (with any chip played), and **chips left for
  the target gameweek's half**.
- **Playing the league average is detected by the other side's entry ID being null**, not by `is_bye`. The
  real data had a match against `"AVERAGE"` with `is_bye: false`.
- **`chips_remaining` works per window.** A chip counts as used only if it was played inside the same
  GW1–19 / GW20–38 window. First-half chips simply don't exist in the second half (forfeited at GW19, rule 3).
- **`entry_picks` returns `None` on 404** ("not visible yet" is expected, not an error). Gameweeks with no picks
  (a manager who joined late) are skipped. H2H matches follow pagination, capped at 20 pages.
- **Logic lives in pure functions** (`find_opponent`, `chips_remaining`, `captain_history`). `load_opponent` is
  the only one that makes network calls: about 8 per run.
- **Test data is anonymized.** Other managers' names and entry IDs, your entry ID, and the league ID are
  replaced with made-up ones by `scripts/record_fixtures.py`, because the repo is public.

**Alternatives.**
- *Full-season captaincy history.* Up to 37 requests per run for little gain, since recent choices predict
  the next one better.
- *Scraping the opponent's team on the FPL website near the deadline.* It's hidden there too until the
  deadline passes.
- *Trusting `is_bye`.* The real data showed it's wrong for the league-average case.
- *Putting the captain probability spread here.* That's modeling, which belongs to Phase 2. Phase 1 only
  provides the history.

**Consequences.**
- Phase 2 builds the captain distribution from `captain_history` plus the players' expected points.
- Phase 3 treats a `None` snapshot as "beat the league average".
- Finished-gameweek picks never change, so a disk cache could later make repeat runs free. It isn't worth it
  at 8 requests per run.

---

## D12. The login server is rate-limited by Cloudflare: fewer calls, retry 429s, refresh early

*Phase 1, 2026-09-26*

**Context.** A re-login looped endlessly: clicking "Log in" kept returning to the login page. The `--debug`
log showed the FPL page's request for `account.premierleague.com/as/.well-known/openid-configuration`
failing with a CORS error. `curl` from the same connection got **`429` from Cloudflare** whatever the User-Agent.
The connection was a commercial VPN on an M247 datacenter IP, shared with many other users. Cloudflare's 429
page has no CORS headers, so the browser reported it as a CORS error and the page couldn't finish the login.
With the VPN off, the login worked first time.

**Decisions.**
- **Cache the token endpoint** in `TokenSet.token_endpoint` after the first discovery, so each refresh is 1
  request to the login server instead of 2. Older token files without the field still load and discover once.
- **Retry the token request on 429** (3 attempts, honoring `Retry-After`, capped at 30s each). Retrying a token
  request is normally unsafe, because a processed request would have used up the rotating refresh token.
  Cloudflare's 429 is sent *before* the request reaches the login server, so the token is untouched. If it's
  still 429 after that, raise **`RateLimitedError`**, separate from `AuthError`: it means "try again soon",
  not "a human must log in". Only 400/401 mean a revoked login.
- **Plan for Phase 4: refresh early.** Access tokens last 1 hour, so the deadline run should refresh 30–60
  minutes before the deadline (plus the day-before check run), not at T-15. A temporary 429 then still leaves
  time, and an already-refreshed access token keeps working even if later calls fail.
- **`phase0_auth.py --debug`** logs login requests by exact hostname, without query strings, and never logs
  tokens. (An earlier substring match also logged tracking URLs that embedded the login URL; fixed.)

**Alternatives.**
- *Spoof a browser User-Agent.* It made no difference: the limit applies to the IP, not the client.
- *Retry any failed token request.* Unsafe: a request the server did process consumes the refresh token, and
  replaying that token after the grace period revokes the login (see Findings).
- *Hardcode the token endpoint.* Saves the first discovery too, but breaks silently if FPL moves it. Caching
  what was discovered keeps it correct.

**Consequences.**
- Cloud Run also runs from datacenter IPs, so the same Cloudflare treatment is likely. The early refresh and
  the day-before check run are what protect Goal 1, not the retries alone.
- Locally, don't run the agent through a VPN.

---

## D13. Predicted lineups, baseline: FPL flags, per-match role over 6 gameweeks, measured surprise rate

*Phase 1 step 4, 2026-09-26*

**Context.** The agent decides before confirmed lineups exist, so Phase 2's minutes model (no minutes / cameo /
start) needs per-player probabilities. FPL offers availability flags (`status`, `chance_of_playing_next_round`,
`news`) and `/event/{gw}/live/`. That endpoint returns **every player's** minutes and starts for a gameweek in
one request, with `explain` giving minutes **per match** (0 for unused players).

**Decisions** (`fpl_agent/data/lineups.py`):
- **Availability:** an explicit percentage wins (75 → 0.75). With no flag, status `a` is 1.0, `i`/`s`/`u`/`n` is 0,
  and `d` without a percentage is 0.5.
- **Role:** start and cameo rates over the team's **finished matches** in the last **6** gameweeks, counted per
  match. A blank gameweek isn't "benched", a double gameweek is two matches, and matches for a previous club
  are ignored. Cost: **5–6 requests per run**, not one per player (667).
- **Shrinkage:** a Dirichlet prior worth **one match** of season-long start rate, so thin data can't give 0% or
  100%.
- **Surprise non-starts: `SURPRISE_NON_START = 0.10`, measured, not guessed.** Of 130 currently unflagged players
  who started all their team's GW1–4 matches, 13 didn't start GW5. (21 of 144, 14.6%, before excluding players
  flagged now, who were likely known to be injured beforehand.) It's one gameweek, so about ±3 points. The
  removed share goes to "no minutes".
- **Output:** `p_start`, `p_cameo`, `p_no_minutes` per player. Turning these into minute distributions is
  Phase 2's job.

**Alternatives.**
- *`/element-summary/{id}/` per player.* Full history, but 667 requests per run.
- *Season totals from bootstrap.* Stale: a player dropped a month ago still looks nailed.
- *Raw frequencies without shrinkage.* One match gives 0% or 100%.
- *Treat an ever-present starter as 100%.* The measurement says that's wrong about 1 time in 10, and
  captain/vice-captain and bench decisions exist precisely for that risk.
- *Scrape external predicted lineups now.* Fragile and possibly against the sites' terms. Worth adding only
  once we can measure whether it beats this baseline.

**Consequences / limitations.**
- A player just back from injury looks like a non-starter, because the matches he missed count against him.
  News-text flags (LLM at the edge) or external lineups can fix this later.
- Re-measure `SURPRISE_NON_START` as the season goes on, in the Phase 2 calibration.
- An early live check on the real squad already flags a starting-XI player with an 18% chance to start (a
  rotation risk) and a 75% doubtful forward. Phase 3's lineup and bench logic acts on exactly these.

---

## D14. Flag snapshots: save everyone's availability every run, and excuse flagged-out matches

*Phase 1 step 4b, 2026-09-26*

**Context.** The lineup baseline (D13) counted a match a player missed through **injury** the same as being
**benched while fit**. FPL only exposes flags as they are *now*, so "was he injured before GW3's deadline?"
can't be answered later. Example: Sangaré's flag is 100%, meaning cleared, so he was probably flagged
recently, and his 2 "unused" matches may have been injury absences.

**Decisions** (`fpl_agent/data/snapshots.py`):
- **Every run saves one `FlagSnapshot` per gameweek**: `status`, `chance_of_playing_next_round`, `news` and
  `news_added` for all players. It's taken from the bootstrap the run already fetched, so there are **no
  extra requests**. It's stored in `data/snapshots/flags_gwNN.json` (gitignored; ~70 KB) behind a
  `SnapshotStore` interface, so Phase 4 can swap in a cloud bucket.
- **The latest snapshot before the deadline wins.** It's only saved if taken before that gameweek's deadline,
  and an older run can never overwrite a newer snapshot. The day-before check and the deadline run both write.
- **Excused matches:** a gameweek where the player was **flagged out** (availability 0) **and didn't play** is
  dropped from his history (`RoleHistory.excused`). If he played anyway, the flag was wrong and the match
  counts. Doubtful players (25–75%) aren't excused.
- **Recording is separate from predicting:** `record_snapshot()` writes, and `load_predictions()` only reads.
- **The snapshot is recorded before any logged-in call**, so a login failure can't cost a gameweek of flags.
- **Snapshots are validated with pydantic when read back** (a file on disk is outside data too).
- **Side fix found here:** the HTTP session now uses `raise_on_status=False`, so after its retries run out it
  returns the final 429/5xx instead of raising urllib3's `RetryError`. Without this, a rate-limited discovery
  request crashed with a traceback instead of raising `RateLimitedError` (D12's handling never ran). The
  unit tests hadn't caught it because the fake session doesn't imitate urllib3; the live check did.

**Alternatives.**
- *Infer injuries from minutes alone.* Impossible: 0 minutes looks the same whether injured or dropped.
- *Use `news_added` to date injuries backwards.* It only holds the time of the *latest* change, so it can't
  tell when an absence started.
- *Third-party historical datasets.* An outside dependency with unknown accuracy, and not needed once we
  record our own.
- *Commit snapshots to git.* They're generated output, not source, and would bloat the repo weekly.

**Consequences.**
- **It only helps from now on.** The first snapshot is GW6 (2026-09-26). The 6-week window is fully covered
  from about GW12. Until then, older absences still count against a player.
- With snapshots, `SURPRISE_NON_START` can be measured properly (unflagged *at the deadline*, not today's
  flag as a stand-in). See `docs/maintenance.md`.

---

## D15. Odds from Kalshi's match-result markets, turned into expected goals with a Poisson fit

*Phase 1 step 5, 2026-09-26. Updates D9.*

**Context.** D9 picked Kalshi and hoped to use correct-score markets. Real data changed that:
- **Match result (`KXEPLGAME`) is liquid.** A settled GW5 match traded 5.3M contracts. Two weeks out, Arsenal–Leeds
  already had 6.9k contracts and 2–3¢ spreads.
- **Correct score (`KXEPLSCORE`) is about 50x thinner** (100k contracts) **and priced incoherently.** "Man Utd
  wins 4-0" last traded at 16%.
- **The API moved to decimal-string fields** (`yes_bid_dollars`, `volume_fp`), and old field names return nothing.
  Settled markets show only final 0.99/0.01 prices, so back-testing needs the price-history endpoint.
- **Team names differ for 7 teams, and Kalshi isn't self-consistent** ("Nottingham" and "Nottingham Forest").
  `occurrence_datetime` isn't always kickoff.

**Decisions** (`fpl_agent/data/kalshi.py`, `fpl_agent/data/odds.py`):
- **Sources:** match result (`KXEPLGAME`) and **total goals (`KXEPLTOTAL`)**, one paginated query per series.
  Total goals is well traded (about 810k contracts per settled match, mostly on the 1.5 and 2.5 lines), but
  opens later than match result: none were open two weeks before GW6. Totals attach to a match through the
  **shared match code** in the event ticker (`KXEPLGAME-26SEP20FULMUN` ↔ `KXEPLTOTAL-26SEP20FULMUN`), with
  no second round of name matching.
- **Price** = bid/ask midpoint, normalized so home + draw + away = 1.
- **Quality gate** (starting values): all three outcomes quoted on both sides, spread ≤ **6¢**, volume ≥
  **1,000** contracts. A match that fails gets **no odds** rather than bad odds, and the reason is reported.
- **Mapping:** an **explicit alias table** (`KALSHI_TO_FPL_NAME`) plus the same two teams plus kickoff within
  **±36h**. **Home/away comes from FPL's fixture.** Unknown names are reported and skipped, never guessed.
- **Expected goals** use independent Poisson (λ_home, λ_away), solved by bisection with no scipy:
  - **The total T = λ_home + λ_away comes from the total-goals market when possible.** Under this model the
    match's total goals is Poisson(T), so each liquid "over X.5" line gives a T. They're combined as a
    volume-weighted mean.
  - **Each line has its own gate:** a two-sided quote, spread ≤ 6¢, volume ≥ 1,000, and a price between 0.03
    and 0.97, since lines near 0 or 1 carry little information and are very sensitive to noise.
  - **The fallback** (`total_source="draw"`) takes the total from the draw price, which plain Poisson biases
    low because it underrates draws.
  - **The split between the teams** always comes from P(home) − P(away).
  - **`draw_gap`** (model P(draw) − market P(draw)) measures the Poisson draw bias match by match, as evidence
    for the Phase 2 Dixon-Coles decision.
  - The outcome grid is normalized so the truncated tail is shared out evenly; a symmetry test caught the away
    side absorbing it.

**Alternatives.**
- *Correct-score markets.* Too thin and incoherent.
- *Fuzzy name matching.* "Manchester" could silently match the wrong club.
- *Trusting Kalshi's home/away order or timestamp as kickoff.* Neither is guaranteed.
- *scipy's optimizer.* A heavy dependency for a two-parameter problem that bisection solves exactly.
- *Removing the margin with the Shin or power method.* The margin here is about 0–3%, so proportional
  normalization is enough.
- *Dixon-Coles now.* Plain Poisson slightly underrates draws. With totals from the totals market, that bias
  no longer affects the total, and `draw_gap` measures what remains before deciding.
- *Weighted least squares across totals lines.* Slightly more principled, but a volume-weighted mean of
  per-line totals gives almost the same answer and is easier to check by eye.
- *A second odds provider now* (The Odds API, Betfair Exchange). A key or account, quotas, bookmaker margins
  and another schema, for coverage Kalshi may already give by deadline time. Revisit under Q3.

**Consequences.**
- First live run (GW6, two weeks out): 4 of 10 fixtures priced. 2 were below the volume gate, and 4 had no
  market yet (they open fixture by fixture). Totals of 2.55–3.11 goals match the Premier League norm.
- Phase 2 needs a fallback for fixtures without odds (see Open questions).

---

## D16. Phase 2 simulation approach; the fallback for fixtures without odds (answers Q3)

*Phase 2 step 1, 2026-09-26*

**Context.** Phase 3 maximizes H2H win probability, which depends on the *distribution* of points, including
variance and correlation (teammates share clean sheets; captaincy doubles one player's swing), not just
expected points. Some fixtures have no usable Kalshi odds at run time (Q3).

**Decisions.**
- **Monte Carlo over whole gameweeks.** Each simulation draws every fixture's scoreline, every player's
  minutes, then events and points, so correlations come out naturally. Phase 3 scores any lineup against the
  same simulations.
- **`numpy`** (new runtime dependency) for vectorized sampling: 10,000 simulations × ~380 players is far too
  slow in pure Python.
- **A seeded `numpy.random.Generator`**, with fixtures drawn in id order. Runs are reproducible, and Phase 3
  compares lineups on identical draws (common random numbers), so differences reflect the lineups, not luck.
- **Scorelines are independent Poisson** with the Kalshi expected goals, the same model Phase 1 used to fit
  the odds.
- **Fallback when there are no odds: shrunk xG team ratings** (`fpl_agent/model/scoreline.py`):
  - λ_home = home_avg × attack(home) × weakness(away), and likewise for away.
  - Attack = the team's xG per match; weakness = xG conceded per 90 by its most-used goalkeeper.
  - Both are relative to the league mean and shrunk toward average by 5 made-up matches.
  - `FixtureRates.source` records `kalshi-totals`, `kalshi-draw` or `xg-ratings`.

**Evidence.**
- **FPL's `strength_attack/defence_*` ratings are all 0 this season**, so they're unusable. Only coarse
  `strength_overall_*` values (4–5) are filled in.
- **Against Kalshi on the 4 fixtures that have both** (8 team rates), the mean absolute error was **0.47 goals**
  for ratings from actual goals and **0.39** for xG. Both are mediocre; 5 gameweeks of team data is weak next
  to a market price.

**Alternatives.**
- *Goals-based ratings.* Measurably worse, as above.
- *A flat league average.* Ignores who's playing.
- *Skipping fixtures without odds.* Leaves players unscored, which risks Goal 1.
- *Dixon-Coles or a bivariate Poisson now.* Wait for the `draw_gap` evidence (D15).
- *Pure-Python sampling.* Too slow.

**Consequences / next.**
- **Recommended next (step 1b): save Kalshi odds on every run**, like the flag snapshots, and use the **most
  recent saved market price** before the xG ratings. Markets open about two weeks early, so a slightly stale
  market price should beat a 5-gameweek rating.
- The fallback's accuracy should be re-measured once more fixtures have odds.

---

## D17. Save Kalshi odds every run; a saved market price beats the xG fallback

*Phase 2 step 1b, 2026-09-27*

**Context.** D16's xG fallback was off by about 0.39 goals against Kalshi. Kalshi opens match markets
about two weeks early, but a fixture's market can be missing or too thin at run time.

**Decisions** (`fpl_agent/data/odds_store.py`):
- **Each run merges its odds into one file per gameweek** (`data/odds/odds_gwNN.json`, gitignored). A fixture
  priced now replaces its entry; fixtures not priced now **keep their last price**. A newer price is never
  overwritten by an older run.
- **Only pre-kickoff prices are saved**, since in-play prices aren't pre-match expectations.
- **Each entry records the kickoff it was priced for.** If FPL moves the fixture by more than 36h, the saved
  price is ignored, because its market was for a different date.
- **Priority for each fixture's expected goals: live Kalshi → saved Kalshi → xG ratings.**
  `FixtureRates.source` says which (`kalshi-saved-*` for saved prices), and `odds_as_of` records the price's
  age.
- **No age cap.** Any market price for *this* fixture beat the 5-gameweek ratings in D16's comparison, and the
  age is recorded so Phase 2 calibration can test that.
- **The same pattern as the flag snapshots (D14):** a store interface (file now, cloud bucket in Phase 4) and
  validation when read back.

**Alternatives.**
- *xG fallback only.* Measurably worse.
- *Keep every historical price.* Useful for back-testing, but Kalshi's price-history endpoint already
  provides that. For fallback purposes only the latest price matters.
- *An age cap (e.g. 7 days).* It would throw away the one market price we have in favour of a weaker model.
  Revisit if calibration shows old prices are worse than the ratings.

**Consequences.**
- The first run saved the 4 priced GW6 fixtures. Coverage grows with every run as markets open.
- The day-before check run (Phase 4) doubles as an odds-saving run.

---

## D18. Minutes simulation: availability once per gameweek, position-level starter minutes

*Phase 2 step 2, 2026-09-27*

**Context.** Minutes decide appearance points (1 below 60, 2 at 60+), clean sheet eligibility (60+) and the
chance to reach the DEFCON threshold. When a player is on the pitch decides which goals he can score,
assist or concede. Real GW1–5 data (single-match gameweeks):

| Starters | Full 90 | Subbed 60–89 | Off before 60 |
|---|---|---|---|
| GK (n=100) | 100% | 0% | 0% |
| DEF (n=429) | 76% | 19% | 5% |
| MID (n=469) | 42% | 50% | 8% |
| FWD (n=102) | 52% | 41% | 7% |

Substitutes (n=438): median 16 minutes (49% up to 15, 40% 16–30, 10% 31–59). Starters who get subbed go
around minute 72 (quartiles 63–80).

**Decisions** (`fpl_agent/model/minutes.py`):
- **Availability is drawn once per player per gameweek.** An injured player misses both matches of a double.
- **Role (start / cameo / unused) is drawn per match**, from the Phase 1 probabilities divided by
  P(available).
- **A starter's minutes are resampled from real starter appearances for his position** (the user chose
  position-level over player-specific). A cameo's minutes are resampled from real substitute appearances,
  and he comes on at 90 minus those minutes.
- **The distributions are rebuilt each run from the live data the lineup model already fetches.** Double
  gameweek appearances are excluded, because FPL doesn't say which match a start belongs to. With fewer than
  30 observations it falls back to pooled outfield data, then to a coarse default approximating the table.
- **Output per player per match:** `minutes`, `on_from` (the minute he came on) and `started`, as arrays of
  shape (player-matches × simulations). Seeded, and drawn in player then fixture order. 10,000 simulations
  of all 667 players takes 0.26s.
- **Players are drawn independently**, so a simulated team won't always field exactly 11.

**Alternatives.**
- *Player-specific starter minutes, shrunk toward his position.* Better for regularly subbed players, but
  it rests on only about 5 starts each so far. Revisit in Phase 2 validation.
- *Every starter plays 90.* Wrong for half of all midfielders.
- *Drawing availability per match.* Understates the risk that a double-gameweek player blanks entirely.
- *Forcing 11 per team with a team-sheet model.* Heavier; see Q4.

**Finding: the Phase 1 start probabilities add up to only ~9.2 starters per team, not 11** (GK 0.85, DEF
3.55, MID 3.95, FWD 0.83, against actual averages of 1.00 / 4.29 / 4.69 / 1.02). About 1.0 comes from the
surprise non-start factor, which is taken from starters and given to nobody. About 0.5 comes from
unavailable players, whose starts nobody inherits. The rest comes from shrinkage. Regulars' individual
probabilities are right; **replacements are underrated**, such as a backup goalkeeper or the center-back
who comes in for an injured teammate. See Q4.

---

## D19. Top up each team's expected starters per position to what it actually fielded (answers Q4)

*Phase 2 step 2 follow-up, 2026-09-27*

**Context.** D18 found that the Phase 1 start probabilities added up to about 9.2 starters per team instead
of 11. Surprise non-starts and absences removed starts that nobody inherited, so replacements such as backup
goalkeepers were underrated.

**Decision** (`top_up_starters` in `fpl_agent/data/lineups.py`, run at the end of `predict_all`):
- **Target** per team and position = the starters that team actually fielded per match over the window. This
  keeps each team's own formation.
- **The shortfall** goes to the team's players at that position in proportion to **headroom** (P(available) −
  P(start)) × **involvement** ((starts + sub appearances + 1) / (matches + 1)). It's capped at P(available);
  whatever a capped player can't take is re-shared among the others.
- **A player who now starts more comes off the bench less**, so start + cameo never exceeds availability.
- **Surpluses are left alone.** The top-up only restores starts nobody inherited; it never removes any.
- **`LineupPrediction.topped_up`** records how much each player received.

**Result on real GW6 data:** expected starters per team are GK **1.00**, DEF **4.28**, MID **4.67**,
FWD **1.02** (total 10.97), against actual averages of 1.00 / 4.29 / 4.69 / 1.02.

**Known interactions.**
- A regular with headroom also receives a small share. Raya went from 0.90 to 0.92, making his effective
  surprise non-start about 7.7%. For goalkeepers that's probably realistic, since the 10% was pooled across
  positions. Measure the rate per position (maintenance) rather than special-casing it.
- Backups with no minutes at all split their share evenly, because the data can't tell a #2 from a #3.
  Team news could.

**Alternatives.**
- *Drop the surprise factor.* It ignores measured risk and still misses the replacements.
- *A full team-sheet model that forces exactly 11 in every simulation.* It also captures which player replaces
  which, but it's heavier. It could come later if bench and auto-sub decisions need that correlation.

---

## Findings

*Phase 0 first successful run, 2026-09-24*

- **Auth works with a header, not cookies.** The FPL API authenticates with an `x-api-authorization`
  bearer token. It's an OIDC JWT issued by `account.premierleague.com`, with scope `openid profile email`.
- **The access token lasts 1 hour** (`exp - iat`). A saved session therefore stops working for
  `/my-team/` about an hour after login, so the reuse path in D4 is only good for short gaps.
- **A refresh token exists.** The site's OIDC client stores `access_token`, `refresh_token`, `expires_at`
  and `id_token` in localStorage (`oidc.user:https://account.premierleague.com/as:<client_id>`), and
  `storage_state` already saves it.
- **Headless refresh works** (D5). The token endpoint returned 200 with new `access_token`, `refresh_token` and
  `id_token`, and `/api/me/` accepted the new access token over plain `requests` with no cookies.
- **The refresh token lasts 180 days, rotates on every use, and the window slides.** Each refresh issues a
  new refresh token whose `exp` is 180 days from *that* refresh. Since the agent refreshes at least weekly,
  the session never expires unless the server revokes it (for example after a password change) or a rotated
  token gets lost.
- **Reuse of a replaced refresh token revokes the whole login, after a grace period**
  (`spikes/phase1_reuse_detection.py`, 2026-09-25):
  - Old token replayed about 2 seconds after the refresh: **accepted** (200). That's the grace period.
  - Old token replayed **180 seconds** after the refresh: `400 invalid_grant: Refresh token does not exist`,
    **and the current refresh token was revoked too**. Recovering took one browser re-login.
  - So the grace period is between about 2 seconds and 3 minutes. Beyond it, a stale refresh token kills the
    session.
  - **Access tokens survive the revocation until they expire** (still worked with 57 minutes left). FPL's API
    checks the token itself rather than asking the login server. So a run that has already refreshed can
    always finish submitting that week's lineup.
- **The login server sits behind Cloudflare and rate-limits by IP** (2026-09-26). From a shared VPN
  datacenter IP, `account.premierleague.com` answered `429` to every request, which shows up in the browser as
  a CORS error and a login loop. On a normal connection it answered 200. See D12.
- Read-only access is confirmed: `/api/me/` and `/api/my-team/{entry}/` return picks with `selling_price`,
  the transfers info and chip status.
- **Write access is confirmed.** `POST /api/my-team/{entry}/` with the bearer header, `Origin` and `Referer`
  returned **202 Accepted** (not 200). Reading the team back matched the payload exactly, so the plain HTTP
  path can save lineups. The optimizer's output format is the `picks` list: element, position,
  is_captain and is_vice_captain, plus `chip`.
- `.secrets/` is chmod 700 and its files 600, because it holds a live refresh token.

## Open questions (with the current recommendation)

**Q1. What revokes the refresh token early (logging out on the site, a password change, other devices)?**
Recommendation: design for revocation whatever the cause, because the job will see the same `invalid_grant`
error either way. Then do one cheap test to learn which actions trigger it.
- *Refresh-token canary run*, about 24h before each deadline: refresh, save, and on `invalid_grant` email
  "log in again: `python spikes/phase0_auth.py --fresh`". Finding out 15 minutes before the deadline is too late to fix.
- *A re-login path that doesn't need the cloud*: the local login saves the new refresh token to Secret Manager.
- *One test*: log out on fantasy.premierleague.com, then run `phase0_refresh.py`. If it fails, logging out
  revokes the token, so the email should say "don't log out on the website". Cost: one browser re-login.
- *Reuse detection: **answered, yes*** (see Findings). Sending a replaced refresh token after the grace period
  revokes the whole login. The design has to guarantee that **exactly one current copy** of the refresh token
  exists and that nothing ever sends an older one:
  - Only one process refreshes at a time: a lock, or a single Cloud Run job with no parallel runs (D5).
  - Never restore a token backup. After each successful save to Secret Manager, **disable the older secret
    versions** so nothing can read a stale one.
  - `phase0_refresh.py` stays disabled (D8), and `phase1_reuse_detection.py` carries a warning.
- Still open: whether logging out on the website, changing the password, or logging in elsewhere revokes the
  agent's session. The day-before check run catches all of these anyway.

**Q2. What if saving the rotated refresh token to Secret Manager fails?**
Recommendation: save it before doing anything else and retry with backoff, but **don't let a failed save
block the gameweek submission.** The new access token in memory is valid for 1 hour, which is enough to submit.
Only *next* week's run depends on the saved token. So: retry the save during the whole run, submit the lineup
regardless, and if the save still hasn't succeeded, send an alert email saying re-login is needed before next
deadline. Reuse detection (see Findings) makes this stricter: the older token still in Secret Manager is now
**poison**, because using it would revoke the session. So a failed save must also mark the secret as needing a
re-login (for example a `needs_relogin` flag), so the next run stops and sends an alert instead of refreshing with it.
(Verified: the in-memory access token keeps working after revocation, so this week's submission is safe.) Never print or log the token as a "backup". Also, since only one refresh may happen at a time, the canary
and the deadline run must never overlap. Use a single Cloud Run job with no parallelism, and schedules far apart.

**Q3. What does Phase 2 use for a fixture with no usable odds?** *Answered in D16 and D17: live Kalshi → saved Kalshi → xG ratings.*
Options: FPL's `strength_attack/defence_home/away` ratings, a league-average scoreline, or the last known
Kalshi price. **Measure on GW6 deadline day**, about 60 minutes before the deadline (the planned refresh
time, D12):
- how many fixtures have usable match-result odds;
- how many have totals (`total_source="totals"`).

If coverage is near complete, a crude fallback is fine. If it isn't, add The Odds API (bookmaker
consensus, including totals) as a second source. Its terms and quotas haven't been checked yet.

**Q4. How should the starter shortfall be fixed (D18)?** *Answered in D19: top-up per team and position.*
Recommendation: **per team and position, top the expected starters back up to the team's recent average**
(e.g. that team's average starting defenders over the window). The missing share goes to the team's
available players *at that position*, in proportion to their headroom (P(available) − P(start)), weighted
toward players who have actually been getting minutes (starts plus substitute appearances). It's capped at
P(available). A backup goalkeeper then inherits the regular's absence, and an injured center-back's starts
go to the other defenders.
Alternatives: drop the surprise factor (ignores measured risk, and still misses the replacements), or a full
team-sheet model that forces exactly 11 in every simulation (it also captures who-replaces-whom
correlation, but it's heavier; possibly later).
