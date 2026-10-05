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

## D20. Attacking events: scorers weighted by xG among players on the pitch, assists by position-adjusted xA

*Phase 2 step 3, 2026-09-28*

**Context.** Real GW1–5 data:
- Own goals are **5.0%** of team goals (7 of 141).
- **97%** of other goals get an FPL assist (130 of 134). FPL counts rebounds from saves, penalties won and so on.
- FPL assists exceed xA by about 41% (130 vs 92.3), and the gap is concentrated in forwards: 15.4% of FPL assists
  against 7.0% of xA.
- Goal shares are close to xG shares (MID 57.5% of goals vs 50.6% of xG; FWD 26.9% vs 31.0%).

**Decisions** (`fpl_agent/model/attack.py`). For each simulated team goal:
- **The minute** is uniform on [0, 90).
- **Own goal** with the season's own-goal share; nobody is credited.
- **The scorer** is chosen among that team's players **on the pitch at that minute** (from D18), weighted by xG
  per 90. Penalty xG is included, so penalty takers are covered.
- **An assist** follows with the season's assist rate: a teammate on the pitch other than the scorer, weighted
  by **xA per 90 × a per-position factor** (FPL assists ÷ xA). This is the user's option (c). Currently FWD
  ×3.08, MID ×1.30, DEF ×1.22.

**Also:**
- Per-90 rates are **shrunk toward the position mean by 270 minutes**, so one chance in a cameo isn't treated
  as elite.
- **Only the *relative* factors matter** for who assists, because the assist rate fixes how many assists
  happen. Forwards come out about ×2.4 relative to midfielders.
- **Everything is vectorized over simulations**; the loop runs over goal number per team. GW6 takes about
  0.9s for 10,000 simulations.

**Alternatives.**
- *Raw xA.* Underrates forwards' FPL assists by more than half.
- *Actual FPL assists per 90.* Too noisy per player over 5 gameweeks.
- *Position-adjusting goals too.* The goals-vs-xG gaps look like finishing noise, and xG is the better
  predictor.
- *Realistic goal timing* (goals lean slightly late). Easy to refine later.
- *Simulating the −2 own-goal penalty.* About 0.02 points per player per season.

**Known limitations.**
- The goalkeeper factor (×3.03) rests on one assist, but keepers' xA is close to 0.
- Assist rate and own-goal share are season-wide constants: they don't vary by team or style.

---

## D21. Defensive events: exact conceded-while-on-the-pitch, player-level DEFCON with light shrinkage, saves by opponent attack

*Phase 2 step 4, 2026-09-29*

**Context.** `game_config.scoring` gives point values, but **no thresholds** anywhere in the API. Verified against
how FPL actually awarded points in GW1–5 (the `explain` breakdown):
- The DEFCON count is CBIT (DEF) / CBIRT (MID, FWD): matched in 100% of player-matches.
- Thresholds are **10 / 12**: defenders awarded from 10, never at 9; midfielders from 12, never at 11.
- Saves: **1 point per 3** (100/100). Goals conceded: **−1 per 2** (633/633).
- Clean sheet = **0 conceded while on the pitch and 60+ minutes** (633/633).

Hit rates at 90 minutes: DEF 31%, MID 20%, FWD 2%. Goalkeepers average 2.9 saves per full match, weakly
correlated with goals conceded (+0.10).

**Decisions** (`fpl_agent/model/defence.py`, thresholds in `fpl_agent/model/scoring_rules.py`):
- **Step 3 now keeps every goal's minute**, own goals included. A player's goals conceded are the opponent goals
  inside his time on the pitch, so a defender subbed off at 70 keeps his clean sheet if the goal comes at 80.
- **DEFCON:** a Poisson count at the player's own rate × minutes / 90, awarded once at the threshold. Not
  scaled by the opponent yet (unmeasured).
- **Saves:** Poisson at the keeper's rate × (opponent expected goals ÷ league average) × minutes / 90, drawn
  independently of goals conceded.

**Finding: shrinkage hurts DEFCON.** With 270-minute shrinkage, the simulated hit rates were 26/15/0% against
the real 31/20/2%. Tested **out of sample** (rates from GW1–4 predicting GW5 threshold hits, 118 appearances):

| Shrinkage | Log-likelihood |
|---|---|
| none | −0.442 |
| 45 min | −0.450 |
| 90 min | −0.458 |
| 270 min | −0.483 |

Less is strictly better. Defensive-action rate is a **stable trait of a player's role**, and the position
average mixes very different roles (center-backs and full-backs). DEFCON therefore uses **45 minutes**: nearly
as good as none, and still a guard against cameo-only samples. Simulated hit rates are now **29/18/1%**.
(In-sample, unshrunk Poisson reproduces 30.9/19.1/1.3% against the actual 30.7/19.9/1.9%, so the Poisson
shape itself is fine.)

**Alternatives.**
- *Full-match result for clean sheets.* Wrong for players subbed off before or brought on after a goal.
- *Position-level DEFCON rates.* They erase exactly the differences between players that DEFCON rewards.
- *A shots-on-target model for saves.* Not justified by a +0.10 correlation.
- *Hardcoding thresholds unverified.* The API doesn't expose them, so each one was checked against real
  scoring.

**Consequences.**
- The attacking model's 270-minute shrinkage and the saves shrinkage haven't been tested out of sample. The
  same test is now on the validation list.
- Re-verify the thresholds each season.

---

## D22. Points: discipline events, a pure points function, one seeded gameweek entry point

*Phase 2 step 5, 2026-09-29*

**Context.** Remaining scoring actions, per 90 minutes this season:
- **Yellow cards:** DEF 0.17, MID 0.19, FWD 0.23 (−1 each), so about −0.2 points a match.
- **Red cards:** 0.005–0.010 (−3).
- **Penalty saves:** GK 0.020 (+5), so about +0.1 a match for a keeper.
- **Penalty misses:** FWD 0.019 (−2).
- **Own goals:** about 0.01 (−2).
- The `mng_*` keys are all 0 this season.

**Decisions.**
- **`fpl_agent/model/discipline.py`:** yellow and red cards are drawn per match as yes/no from the player's own
  rate × minutes. **A red clears the yellow**, because a second-yellow dismissal scores −3 only. Goalkeeper
  penalty saves are a Poisson count. Rates are shrunk by 270 minutes; that's untested out of sample and on the
  validation list. **Skipped:** penalty misses (about −0.04 a match) and own goals (about −0.02).
- **`fpl_agent/model/points.py` is pure arithmetic, with no randomness:**
  - appearance (1 for 1–59 minutes, 2 for 60+), goals, assists, clean sheet;
  - goals conceded (per 2, rounded down), saves (per 3, rounded down), DEFCON award, cards, penalty saves;
  - **every value comes from `game_config.scoring`.** Only the thresholds come from `scoring_rules.py`.

  Double gameweeks sum both matches. The randomness stays in the simulators, so points can be tested exactly
  (a defender with two goals and a clean sheet scores exactly 2 + 12 + 4).
- **The output (`PointsSamples`):**
  - the **players × simulations points matrix** (27 MB at 10,000 simulations), which Phase 3 needs in full;
  - **`played`** (any minutes in the gameweek), which bench auto-subs (rule 2) trigger on;
  - each player's **mean per component**, to explain a projection. Keeping every component per simulation
    would be about 400 MB.
- **`fpl_agent/model/gameweek.py`, `simulate_gameweek()`:** steps 1–5 in a fixed order from one seed. It takes
  already-fetched data, not an API client, so it's offline-testable and cheap to call repeatedly. A full
  gameweek takes about 2.6s.
- **`python -m fpl_agent.model`:** a read-only live check of your squad's expected points and haul chances.
  Recording snapshots and odds stays with `python -m fpl_agent.data`.

**Sanity check against FPL's `ep_next`:**
- Correlation **0.78**; means 1.72 (ours, no bonus yet) against 1.84.
- The biggest gaps are FPL's form artifacts. `ep_next` had defenders coming off a big week at 9–11 points;
  we have them at 3–4 for one match.
- `ep_next` isn't the truth. Step 7 back-tests against actual points.

**Alternatives.**
- *Randomness inside the points function.* It can't be tested exactly.
- *Storing the full per-component matrices.* About 15× the memory.
- *Including penalty misses and own goals.* Negligible.
- *An API client inside the simulation.* It couldn't run offline or repeatedly without refetching.

---

## D23. Bonus: BPS fitted from real data, empirical-Bayes base BPS, FPL tie rules

*Phase 2 step 6, 2026-09-29*

**Context.** Bonus is 3/2/1 for the top three BPS per match. BPS weights aren't in the API, and BPS also
rewards actions we don't simulate (passes, key passes, recoveries). Real GW1–5: 72% of matches handed out 6
bonus points, 26% handed out 7 and 2% handed out 9 (ties).

**Decisions** (`fpl_agent/model/bonus.py`):
- **Simulated BPS** = event BPS + base BPS × minutes / 90 + noise, rounded to whole numbers.
- **Event-BPS weights are fitted by least squares** on real per-match BPS from the window's live data, refitted
  every run (1,538 player-matches, R² 0.88). Goal FWD/MID/GK-DEF 24.3/20.9/13.9, assist 11.2, clean sheet 12.8,
  save 2.9, conceded −3.8, 60+ minutes 9.2, and so on. They're fitted rather than taken from FPL's published
  table because the job is predicting BPS *from the events we simulate*: "60+ minutes" also absorbs regulars'
  passing BPS. The GW1–5 fit is the fallback when there's too little data.
- **Base BPS** is each player's average leftover per full match, shrunk toward his position average by
  **empirical Bayes**: keep = between-player variance / (between + within-player variance / matches). It's
  computed from the data every run, not picked by hand (the DEFCON lesson). Players without a full match get
  the position average.
- **Noise** is Normal(0, within-player SD × √(minutes / 90)).
- **Competition ranking** (rank = 1 + number of players with strictly higher BPS; 1→3, 2→2, 3→1) reproduces
  FPL's tie rules exactly. Only players who played are ranked, and each match of a double gameweek is ranked
  separately.
- **Bonus is a points component**, × `scoring.bonus`.

**Checks.**
- **Bonus handed out per match:** 6.34 simulated vs 6.32 actual, so the tie frequency is right.
- **Share by position (sim / actual):** GK 9/9%, DEF 28/31%, MID 45/44%, FWD 17/17%.
- **Out of sample** (fit GW1–4, predict each player's GW5 base BPS, 191 appearances), error by share of the
  player's own average kept:

  | Keep | 0 | 0.25 | 0.5 | 0.75 | 1 |
  |---|---|---|---|---|---|
  | Error | 24.6 | **23.6** | 24.0 | 25.8 | 29.1 |

  The empirical-Bayes estimate keeps a median of **0.48** for players with 3+ full matches, which is in the
  good zone and far better than either extreme. Player-specific base BPS is a modest gain (about 4%).
- **A heavy tail in match noise** (mean within-player variance 18.5, median 10.8) makes the shrinkage
  stronger, which the out-of-sample test favors, so it's kept. An early "median keep factor 0.0" came only
  from counting the ~360 players with no full matches.

**Alternatives.**
- *FPL's published BPS table.* Unverifiable from the API, and worse at predicting from simulated events.
- *A fixed shrinkage strength.* Untested guesses already failed once (D21).
- *No base BPS (position averages only).* About 4% worse out of sample.
- *Full BPS simulation (passes and so on).* A lot of machinery for the part the base term already captures.

**Effect.** Bonus lifts the attackers most: Fernandes 4.53 → 5.20, Haaland 4.38 → 5.16, Mbeumo 4.49 → 5.11
expected points.

---

## D24. Back-testing data: last season from community archives, rebuilt without peeking

*Phase 2 step 7a, 2026-09-29*

**Context.** Validation needs real gameweeks, and five is thin.
- **The FPL API keeps only season *totals* for past seasons** (`element-summary.history_past`: minutes, xG,
  DEFCON, BPS, points…). The per-gameweek endpoints cover the current season only.
- **Kalshi's candlestick endpoint** gives hourly price history for settled markets, so this season's pre-match
  prices can be rebuilt (in 7b).

**Decisions** (`fpl_agent/validation/`, `scripts/fetch_history.py`, `scripts/check_history.py`):
- **2025/26 from two community archives**, downloaded at run time into the gitignored `data/cache/history/`:
  - **vaastav/Fantasy-Premier-League** (MIT): every player-match for all 38 gameweeks, including FPL's own
    pre-gameweek `xP`, a ready-made benchmark.
  - **football-data.co.uk:** closing odds for all 380 matches. It's offered as free downloads with no explicit
    licence stated, so it's used for **personal back-testing only**: never committed, never redistributed,
    and credited here.
- **Market-average closing odds are used for every match**, converted with the same Poisson pipeline as Kalshi
  (1X2 sets the split, over/under 2.5 sets the total). Pinnacle's closing odds are missing for 170 of the 380
  matches, and mixing sources would make matches inconsistent with each other.
- **The rebuild uses no future information** (`pointintime.build_case`). For gameweek *k*:
  - player totals are re-summed from rows *before k*;
  - fixtures from *k* on have no results;
  - live data covers only the lineup window before *k*;
  - odds are closing odds for *k*'s matches.

  Deadlines are approximated as first kickoff − 90 minutes, for the calendar only.
- **Duplicate archive rows are removed.** Ten (player, fixture) pairs appear twice, one player in 9 fixtures,
  which would double his minutes, goals and xG.

**Consistency checks** (`validation/checks.py`, run with `python scripts/check_history.py`; about 6s):
- **Scoring reproduced exactly for 29,747 / 29,747 player-matches** from their stats. Our rules, thresholds and
  point values (DEFCON included) held for 2025/26, which is far stronger evidence than D21's 5-gameweek check.
- **Goals + own goals = final score in 380 / 380 fixtures** (377 before the duplicates were removed).
- **All 380 matches have odds; zero future-information leaks** across the 37 rebuilt gameweeks.

**Limitations.**
- **No historical injury flags** exist, so everyone is "available". The back-test understates the live system
  on availability.
- **Closing odds are set slightly after FPL's deadline**, so they're a little better informed than live runs.
- **Promoted and relegated teams differ between seasons.**

**Alternatives.**
- *The current season only.* 5 gameweeks, about 1/7 of the evidence.
- *End-of-season totals as inputs.* Leaks the future.
- *Pinnacle where available, the market average elsewhere.* Inconsistent across matches.
- *Committing the archives.* A licence question for football-data, and weekly bloat.

---

## D25. Back-test runner, held-out current season, and first results

*Phase 2 step 7b, 2026-09-29*

**Decisions.**
- **The held-out season (2026/27 GW2–5)** is rebuilt from FPL's own cached live data. **Odds are Kalshi prices
  as they stood 60 minutes before each gameweek's deadline**, when the live agent reads them (D12). They're
  rebuilt from hourly candles (`scripts/fetch_kalshi_history.py`, 503 markets cached) and passed through the
  same `build_odds` pipeline and quality gate as live runs. That's *more* realistic than 2025/26's closing odds,
  which are set after the deadline.
  - Players who have since transferred can't be placed on a side, so those rows are skipped: 25 of about 3,200.
  - Scoring is reproduced for 3,191 / 3,191 player-matches, and 50 / 50 scores reconcile.
- **Seasons take a pluggable odds source** (closing odds or rebuilt Kalshi prices) and real deadlines when known.
- **Runner (`validation/backtest.py`):** for each gameweek it rebuilds inputs, predicts lineups, and simulates
  with `keep_samples=True` (2,000 simulations; about 1s per gameweek). It saves per-match, per-player-match and
  per-player-gameweek records as JSONL for 7c and 7d.
- **Benchmarks** are computed without peeking: points per appearance so far, and the average of the last three
  appearances.
- **FPL's archived `xP` is dropped as a benchmark.** It's missing in 27 of 38 gameweeks (0.00 for everyone,
  Haaland's 13-point GW10 included). Where present, it correlates **0.74–0.83** with actual points from GW2 on,
  far above what pre-gameweek forecasts achieve, so it was almost certainly recorded *after* the matches. Only
  GW1 (+0.40) looks genuine.
- **The PIT uses the randomized version for whole-number points.** A fixed midpoint put every certain 0 at 0.5,
  which made 53% of values land in the middle bin.

**First results** (`python -m fpl_agent.validation`, about 45s):

| | 2025/26 (37 GWs) | 2026/27 held out (4 GWs) |
|---|---|---|
| 1X2 Brier (coin flip 0.667) | 0.612 | 0.658 (40 matches) |
| Goals predicted / actual | 1,060 / 1,021 (+3.8%) | 116 / 111 |
| Start: predicted / actual, skill | 0.280 / 0.280, +0.52 | 0.337 / 0.338, +0.57 |
| DEFCON predicted / actual | 0.049 / **0.054** | 0.052 / 0.057 |
| Points bias | −0.006 | −0.047 |
| MAE / rank vs points-per-game (same rows) | **1.64 / 0.54** vs 2.15 / 0.39 | **1.95 / 0.44** vs 2.32 / 0.34 |
| P(6+) / P(10+) predicted vs actual | 7.1% / 1.9% vs 7.0% / 1.8% | 8.6% / 2.3% vs 8.7% / 2.2% |
| PIT histogram (honest = 0.10 per bin) | 0.09–0.11 | 0.08–0.14 (top bin 0.14) |

**Reading.**
- **The model beats both "just use form" benchmarks on accuracy and on ranking, in both seasons.**
- **Probabilities are right on average for every event** except DEFCON, which is about 10% low, as in D21.
- **The distributions are honest** (the PIT is flat).
- **To look at in 7d:** goals slightly over-predicted, DEFCON low, and the held-out top PIT bin.
- **Limitation:** no historical flags, so availability is understated relative to live runs.

**Answers Q3's open measurement.** Kalshi priced **40 / 40** GW2–5 fixtures 60 minutes before the deadline, and
**33** had a usable totals line.

---

## D26. Validation report: likely starters, decision-level checks, one source for markdown and HTML

*Phase 2 step 7c, 2026-09-29. Full numbers: `docs/validation.md`.*

**Decisions** (`fpl_agent/validation/report.py`, `python -m fpl_agent.validation --report`):
- **Report on *likely starters*** (given ≥ 50% to start *before* the gameweek) as well as everyone. That's the pool
  you actually pick from, and reserves on 0 points flatter every all-player number.
- **Decision-level checks**, the closest thing to what the agent will do. From the same pool of likely starters
  with history each gameweek:
  - the actual points of the model's top 10 vs points-per-game's top 10;
  - the actual doubled points of each one's captain pick.
- **Per position, calibration curves for every event, points reliability by predicted-points bin**, and scoreline
  calibration including the draw rate.
- **One `report.json` feeds both `docs/validation.md` (committed) and the HTML page (published, private)**, so they
  can't disagree. Both regenerate with one command after any future back-test. The HTML uses hand-drawn SVG
  charts (no library), every chart has a table view, and colors meet 3:1 contrast in both themes (the model in
  blue, the benchmarks in greys).

**Findings (2025/26):**
- **Likely starters:** error **2.31** vs 2.57 (points per game) vs 2.75 (form); ranking **0.288** vs 0.176 vs 0.152,
  about 1.6× better. The model's ranking beats points-per-game at every position.
- **Top 10:** 5.19 vs 4.10 actual points each (+27%), better in **28 of 37** gameweeks.
- **Captain:** 12.70 vs 11.95 doubled points a gameweek, **+28 points** over the season.
- **Draws are under-predicted: 23.0% vs 27.3%** (370 matches, about 2 standard errors). This is the Poisson draw bias
  D15 was waiting on, and it argues for Dixon-Coles.
- **Teams expected to score 1.6+ scored about 0.2 fewer.** Together with the draws, that's the +3.8% goals.
- **DEFCON about 10% low in both seasons; a slight optimism on starting defenders** (+0.16).
- **The PIT is flat for likely starters too** (0.09–0.11).
- **Held out (4 GWs):** better at every level; goalkeepers look off (−0.52 bias, n = 79), too few to act on.

**Alternatives.**
- *All-player numbers only.* Flattered by reserves.
- *A charting library.* Unnecessary for three chart types, and a dependency on a CDN.
- *Hand-written report prose.* Goes stale; generated text stays consistent with the data.

**Next (7d):** a Dixon-Coles or total-goals adjustment, DEFCON calibration, and the untested shrinkage strengths.
Each change is judged by re-running this back-test.

---

## D27. Tuning pass: Dixon-Coles draws, negative-binomial DEFCON, a 2-gameweek lineup window

*Phase 2 step 7d, 2026-09-29. Before/after numbers: `docs/validation.md`.*

**Method.** Diagnose first, then change. Tune on 2025/26, confirm on the held-out 2026/27 GW2–5, and adopt
only what holds up there. Prefer using information we already have over adding tuned knobs.

**1. Draws: Dixon-Coles, with ρ set per match from the market's draw price.**
- *Diagnosis (380 matches):* market draws 24.8%, our Poisson 23.0%, actual 27.4% (±2.3). Plain Poisson has only
  two dials (each team's expected goals), which the totals line and home-minus-away already use up, so the
  draw price was being thrown away. Market totals were well calibrated (over 2.5: 54.3% vs 55.0%).
- *Change:* Dixon-Coles multiplies the 0–0, 0–1, 1–0 and 1–1 cells. That leaves the mean goals, P(over 2.5)
  and home-minus-away unchanged, and moves P(draw) by −2ρλhλa·e^−(λh+λa), so ρ is solved in closed form to hit
  the market's draw exactly. All three 1X2 prices and the totals line are then reproduced. **Nothing is tuned.**
  Without a totals line, the draw price already sets the total, so ρ = 0.
- *Result:* 1X2 Brier 0.612 → 0.610 and score log-likelihood −2.865 → −2.861 on 2025/26; **0.658 → 0.653 and
  −3.076 → −3.068 held out.** Predicted draws are now at the market's 24.8%.
- *Not done:* pushing draws beyond the market's level. The remaining 24.8% vs 27.3% gap is about one standard
  error.

**2. DEFCON: negative binomial with dispersion 1.55.**
- *Diagnosis:* rates were unbiased (8.10 expected vs 8.13 actual over 4,532 full-90 outfield appearances), but
  counts varied **1.55×** more than Poisson (1.70 held out). Hits happen in the tail (10+/12+), so they came
  out about 10% short.
- *Change:* negative-binomial counts with the same mean and variance = 1.55 × mean
  (`defence.overdispersed_counts`).
- *Result:* DEFCON predicted 4.9% → 5.5% vs 5.4% actual (Brier 0.0422 → 0.0413), and **held out 5.2% → 6.0% vs
  5.7% (Brier 0.0473 → 0.0468)**.

**3. Settings swept out of sample** (`scripts/sweep_parameters.py`, the same seeds for every run):
- *Attacking shrinkage (270 minutes):* flat from 270 to 1,080 (goal log loss 0.1034 vs 0.1033). **Kept.**
- *Saves shrinkage:* error 1.534 (90) → 1.518 (270) → 1.478 (5,000), levelling off, so **5,000**. A keeper's
  saves depend on the shots his defence allows, which the opponent factor already covers.
- *Lineup window × prior:* shorter was better all the way down; the best was **window 2, prior 1** (start Brier
  0.0974 → 0.0868, points error 1.024 → 0.997, ranking 0.690 → 0.704). A prior of 0.5 over-reacts, and 2–4
  drag back toward stale roles. Roles change fast: signings, dropped players, formation switches.
- *Held-out confirmation of the chosen settings:* start Brier 0.0957 → 0.0894; points error with history
  1.954 → 1.910; ranking 0.435 → 0.465.
- *Card-rate shrinkage:* not swept, since it's worth about 0.2 points a match in total.
- **`WINDOW_GWS = 2` (the lineup model's memory) is split from `HISTORY_GWS = 6`** (live data loaded for the
  minutes and bonus fits). Before the split, one constant did both jobs, and shrinking it would have starved
  those fits.

**Combined result, 7b → 7d:**

| | 2025/26 | 2026/27 held out |
|---|---|---|
| Ranking, players with history (points-per-game 0.385 / 0.344) | 0.540 → **0.585** | 0.438 → **0.464** |
| Points error, players with history | 1.638 → **1.579** | 1.945 → **1.912** |
| Start Brier | 0.0974 → **0.0868** | 0.0955 → **0.0894** |

Bias stays near 0, P(6+) is 7.2% vs 7.0%, and the PIT is still flat.

**Also.**
- *Measurement caveat:* "likely starter" slices depend on the model's own start probabilities, so they shift
  when the lineup model changes. Compare versions on the same rows instead. For example, captaincy +72 points
  over 2025/26 in the new likely-starter pool vs +28 in the old one; top 10 +0.74 points per player, better in
  25 of 37 gameweeks.
- **The report's findings section is now data-driven** ("fixed in 7d" and "watch"). The 7c text had hardcoded
  numbers that went stale.
- `window_events` reads `WINDOW_GWS` at call time. It was a default argument, fixed when the function was defined,
  so changing the setting had no effect.

**Alternatives rejected.**
- *A global draw-inflation or goals-deflation factor fitted on 2025/26.* That tunes to one season's luck; ρ from
  market prices needs no fitting.
- *Overdispersion via an opponent-strength effect for DEFCON.* Plausible but unmeasured; a single dispersion
  number fixed the calibration in both seasons.
- *Recency weighting* (exponential decay instead of a hard window). A possible refinement; the hard window already
  captured most of the gain.
- *A 1-gameweek window.* Nearly as good on starts, but it ranks worse.

---

## D28. Phase 3 design and defaults; part 1, the rules engine, verified against FPL

*Phase 3 part 1, 2026-09-29*

**Phase 3 design** (explained and approved before building):
- **The objective is H2H win probability.** Your team and your opponent's are scored in the *same* 10,000 simulated
  gameweeks (players you both own cancel out, as in real H2H). Candidate decisions are compared on identical draws.
- **Defaults chosen by the user:**
  - **Buffer 3 points**: maximize P(my points − opponent's ≥ 3), the low end of the 3–5 idea in Q5.
  - **Points guard 1.0**: never give up more than 1.0 expected point against the highest-expected-points choice,
    to protect overall rank.
  - Both are to be revisited with evidence: Phase 3 will report what each buffer size costs in plain win
    probability.
- **Transfers and chips are recommendation-only in Phase 3.** They're reported, not executed, until the Phase 5
  multi-week planner exists. One-week-at-a-time transfers are the classic way to burn value. Chips are
  scheduled by Phase 5, **which must run well before the GW19 deadline**, when unused first-set chips are
  forfeited (target: around GW14–15).
- **Build order:**
  1. rules engine;
  2. team scoring over the simulations (vectorized, tested against the rules engine);
  3. opponent model (last gameweek's squad plus a spread over their likely captain);
  4. lineup, captain, vice-captain and bench optimizer (staged search);
  5. transfer recommendations;
  6. payload builder, dry run by default (the transfers endpoint needs a spike first);
  7. back-test of the optimizer on real gameweeks.

**Part 1, the rules engine** (`fpl_agent/optimize/rules.py`):
- **Small pure functions, scalar and readable.** They're the *reference specification*: part 2's fast vectorized
  scorer will be tested against them. They cover:
  - squad validity (counts, club limit, budget) and formation validity;
  - team-sheet validity; auto-subs;
  - captain and vice-captain multipliers (Triple Captain ×3); team points (Bench Boost counts all 15 with no
    auto-subs);
  - transfer cost and free-transfer banking; selling price;
  - chip windows and forfeiture.
- **Every limit is read from the API:** `element_types` (squad counts, formation minimums and maximums) and the
  new `GameConfig.rules` (club limit, squad size, budget, extra free transfers, sell-on fee). Nothing FPL publishes
  is hardcoded.
- **Violations are readable messages**, for example "4 players from team 7 (max 3)", so logs and the email can
  say why.

**Verified against FPL itself**, using the picks of the 11 real managers in the H2H league for GW1–5 (55
team-gameweeks, cached in the gitignored `data/cache/picks/`):
- **FPL returns the lineup *after* its auto-subs**, together with an `automatic_subs` list. Undoing those gives
  the original team sheet.
- From the original team sheets, **our auto-subs are identical to FPL's in 53 of 53 team-gameweeks** (26 had
  substitutions: 4 goalkeeper swaps and 12 with 2+ subs).
- **Our team points equal FPL's official score in 55 of 55**, including 2 vice-captain takeovers, Triple Captain,
  Bench Boost and Free Hit.
- **This settles the ordering question for rule 2:** bench players are tried in priority order, each replacing the
  first non-playing outfield starter it legally can, and legality is judged on the *final* XI (a defender may
  replace a forward if another forward remains).
- **9 anonymized cases covering every path** (no entry IDs or names) are a permanent regression test
  (`tests/fixtures/autosub_cases.json`, rebuilt by `scripts/record_fixtures.py`).

**Alternatives.**
- *Hardcoding the squad and formation limits.* They're in the API.
- *True/False validity checks.* They can't explain a rejection.
- *Writing only the fast vectorized version.* It would be hard to read and to trust; the readable reference and
  a test that the two agree is safer.
- *Assuming the auto-sub order.* Two plausible readings differ in edge cases; FPL's own data decided.

## D29. Team scoring over the simulations: vectorized, slot by slot, checked against the rules engine

*Phase 3 part 2, 2026-09-29*

**Context.** The optimizer scores many candidate lineups (and the opponent's team) on the same 10,000 simulated
gameweeks. The rules engine scores one outcome at a time in plain Python. Calling it 10,000 times per lineup
takes seconds, and the optimizer needs thousands of lineups.

**Decision** (`fpl_agent/optimize/scoring.py`):
- **`squad_sims`** pulls the squad's rows out of the gameweek simulation. A player with no fixture (blank
  gameweek) gets 0 points and "didn't play".
- **`score_lineup`** returns the team's points in every simulation (shape `(n_sims,)`), with the same steps as
  `rules.team_points`: goalkeeper swap, outfield subs in bench order with formation checks, armband (vice-captain
  if the captain didn't play; ×3 with Triple Captain), Bench Boost.
- **Python loops only over slots, never over simulations.** Four bench players × eleven slots at most, each step
  handling all 10,000 simulations at once with numpy.
- **The key simplification: a slot is filled at most once.** A sub only replaces a starter who didn't play, and a
  sub only comes on if he played, so a filled slot never reopens. Three things follow:
  - The player leaving a slot is always its original starter, so the outgoing position is a constant per slot.
  - Only two formation counts can break with any swap: the outgoing position losing one, the incoming one gaining
    one (the XI is legal before every swap). A same-position swap is always legal.
  - The total is the starters' points plus, where a sub came on, the sub's points minus the starter's. No
    "who is in each slot" matrix is needed.
- **Verification.**
  - A randomized test gives the fast scorer and the rules engine the same inputs: 25 random legal lineups ×
    400 simulations × 3 chip modes. Minutes are skewed toward misses so there are plenty of subs, including
    goalkeeper swaps. It must match in every simulation.
  - The 9 real FPL cases from D28 run through the fast scorer too.
  - On the real GW6 squad: 0 mismatches in 30,000 simulations (10,000 per chip mode), checked outside the test
    suite.
- **Speed:** 1.0 ms per lineup at 10,000 simulations on the real squad. About 1,000 lineups per second, which
  is enough for the staged search in part 4.

**How the speed was found** (measure, don't guess):
- The first version tracked who was in each slot as an (11 × 10,000) matrix: 10.8 ms per lineup.
- The first guess, the formation check, was wrong: rewriting it changed nothing.
- Profiling found Python lists being turned into arrays. Building them with numpy directly: 8.7 ms.
- Timing each operation separately showed the rest was "fancy indexing": looking up values by a per-simulation
  index (`points[occupant, cols]`). The final (11 × 10,000) lookup alone took 0.84 ms, and smaller lookups ran
  inside the loops.
- The "filled once" rewrite needs none of those lookups: 1.0 ms.

**Alternatives.**
- *Calling the rules engine per simulation.* It's exact but about 1,000× slower.
- *Numba or another compiled loop.* It would be fast, but adds a heavy dependency and a second language style,
  and numpy is already fast enough.
- *Precomputing auto-subs per "who didn't play" pattern.* There are too many patterns with 15 players; caching
  would add complexity for little gain.
- *Approximating (e.g. ignoring formation limits).* Formation-blocked subs are exactly the edge cases where bench
  order matters. Exactness keeps the bench-order decision trustworthy.

## D30. The opponent model: last week's team sheet, a fitted captain spread, chip chances, FPL's AVERAGE

*Phase 3 part 3, 2026-09-30. Design agreed with the user, then adjusted by research on the league's real data.*

**Context.** The objective (D28) is P(my points − opponent's ≥ buffer), so the opponent must be scored in the same
10,000 simulated gameweeks as my team. Their picks for the coming gameweek are hidden until the deadline.

**Research first** (the league's real GW1–5 data: 11 managers, 55 team-gameweeks, H2H results):
- **"AVERAGE" is FPL's overall average score**, across millions of managers, not the league's average. The
  league's AVERAGE opponent scored exactly `events[].average_entry_score` in 5 of 5 gameweeks. The league mean
  was off by up to 9 points. This overturned the agreed plan ("score the 10 other squads and average them"),
  which would have modelled the wrong thing.
- **Managers keep their squads.** 0.66 transfers per manager-week, and only 2 of 44 captains weren't in the
  previous week's squad.
- **Captaincy is mostly habit** (the user's instinct, confirmed). Details below.

**Decisions** (`fpl_agent/optimize/opponent.py`; data changes in `fpl_agent/data/`):
- **Their team sheet is last gameweek's**, with FPL's auto-subs swapped back (FPL returns the lineup *after*
  them, D28). A Free Hit week is skipped because that squad has reverted (`data/opponent.current_squad`).
  Unseen transfers and hits are ignored. An injured starter "doesn't play" in the simulations, so their bench
  covers him through the same auto-sub rules as mine.
- **The captain is a probability spread:** habit weight × (share of their last 5 captaincies) + the rest ×
  softmax(expected points / temperature), over their starters.
  - Fitted by maximum likelihood on the league's 44 real GW2–5 decisions, using the no-peeking back-test expected
    points: **habit weight 0.85, temperature 1.0**. The average log-likelihood is −0.58, meaning the real captain
    got about 56% probability, against −1.31 with expected points alone.
  - The fit is flat for habit weights 0.7–0.9, so the exact value matters little.
  - The most likely captain was right 86% of the time, against 64% for "top expected points". "Same as last
    week" also hits 86%, but it gives no probability to a switch.
- **Availability scales habit** (added after the first live run). Their habitual captain was flagged 75%, yet
  kept a 68% share. Now habit × chance of playing (from FPL's flags) counts, and the share lost goes to the
  expected-points part: a ruled-out player loses all of his habit share. That run became 52% for the flagged
  player and 22% for the top expected scorer. This scaling is **reasoned, not fitted**: GW2–5 had almost no doubtful
  habitual captains. The vice-captain is the second most likely captain.
- **Chips** (Triple Captain or Bench Boost only, and only if they still hold one):
  - The chance is 1 ÷ (chip-worthy gameweeks left in the chip's window, this one included), kept between
    **30% and 80%**.
  - "Chip-worthy" means a double gameweek, or one of the **last 2 gameweeks of the window**: use it or lose it.
    If no double gameweek comes before GW19, first-half chips get played in normal weeks.
  - One chip per gameweek, so the two chances together are capped at 80%. Wildcard and Free Hit aren't modelled,
    because they give a squad we can't see.
  - The user agreed 30% as the starting point and the rise towards the forfeit deadline. They approved changing
    the CLAUDE.md rule "chips only if … a DGW" accordingly.
- **Draw, don't average.** In each simulation the captain and chip are drawn. Each (captain, chip)
  combination is scored with `score_lineup`, the exact rules from D29, and each simulation takes the one it drew.
  - Averaging would shrink their spread, and win probability depends on the tails.
  - The draw is made once per run, so every candidate lineup of mine meets the same opponent (common random
    numbers).
  - Cost: one `score_lineup` call (about 1 ms) per likely captain and chip combination.
- **The AVERAGE opponent is modelled from ownership:** scale × Σ (share of FPL squads owning a player × his
  points), per simulation, rounded (FPL reports a whole number).
  - On GW2–5 the scale is **0.9**, with misses of about 3 points or less.
  - GW1 missed by 14, because only *today's* ownership was available. So ownership (`selected_by_percent`) is now
    saved in each flag snapshot, and the scale can be refit on pre-deadline ownership from GW6 on.
  - This needs no extra requests.
- **`scripts/fit_opponent.py` refits both** from cached league data, fetching only what's missing, and prints
  the fits next to the values in use. It changes nothing: updating a constant stays a reviewed decision.

**First live run** (GW6, a real opponent sharing none of my players): expected 51.1 vs 46.5 points. **P(win)
58.7%**, P(draw) 2.2%, P(win by 3+) 54.5%.

**Alternatives.**
- *League average for AVERAGE*: disproved by the data.
- *Scoring all 10 league squads*: models the wrong thing, and needed about 60 requests.
- *A single most-likely captain*: overconfident. The real captain wasn't the most likely one in 14% of cases.
- *Averaging captain points instead of drawing*: understates the opponent's big weeks.
- *Guessing their transfers*: mostly noise at 0.66 transfers a week. Part 7 measures what ignoring them costs.
- *A flat 30% chip chance*: ignores the forfeit deadline.
- *Chips only in double gameweeks*: misses first-half chips burned in normal weeks before GW19.
- *Fitting the chip chances*: there are no data yet; they're checked against reality instead (Q6).

**Consequences.**
- Part 4 compares candidate lineups against one fixed opponent vector per run.
- Three numbers are to be re-checked as evidence arrives (Q6, `docs/maintenance.md`): the captain fit (10 new
  decisions per gameweek), the AVERAGE scale (point-in-time ownership from GW6), and the chip chances (opponents'
  actual chip plays become visible after each deadline).

## D31. Lineup optimizer: exact pruning by the points guard, streaming candidates, one-SE tie rule

*Phase 3 part 4, 2026-09-30. Design explained and approved: ties within the noise go to more expected points;
buffer 3 and guard 1.0 kept (including AVERAGE weeks); chips reported as information only.*

**Context.** For a fixed squad, choose the XI, bench order, captain and vice-captain that maximize P(my points −
opponent's ≥ 3), among lineups within 1.0 expected point of the best (D28). A 2-5-5-3 squad has **550** legal XIs
(either keeper; 286 ways to pick 10 of 13 outfielders, minus 10 with two defenders and 1 with no forward).
× 6 bench orders × 110 captain pairs is about 360,000 lineups, or about 6 minutes at 1 ms each.

**Decision** (`fpl_agent/optimize/lineup.py`):
- **The scorer is split into team points and captain bonus** (`scoring.py`: `team_points_before_armband` +
  `armband_points`; `score_lineup` is their sum). The captain never changes auto-subs.
  - Each XI and bench order is scored once.
  - Every captain pair's *expected* bonus comes from one 15 × 15 matrix product: E[captain's points if he
    played] + E[vice's points if he played and the captain didn't].
- **Exact pruning by the guard.** Each XI gets a cheap **upper bound** on its expected points: the starters, plus
  every bench player's points wherever a starter of his kind (keeper or outfield) missed out, plus its best
  captain pair.
  - XIs are scored from the highest bound down, stopping once a bound falls below (best − guard). The best only
    rises, so nothing skipped could have passed the guard.
  - What survives is **every** lineup within the guard, not an approximation.
- **Streaming instead of storing.** A real squad has thousands of candidates within 1.0 point: **2,505 for GW6**,
  mostly vice-captain and bench-order variants that differ in a few simulations.
  - Storing them all at 10,000 simulations took about 200 MB, and about 1.2 GB at guard 2.0.
  - Now only each XI + bench order's team points are kept (16-bit), and lineups are rebuilt on the fly.
  - The choice is made in two passes: find the best chance per buffer, then apply the tie rule against it.
  - The optimizer adds **about 20 MB** to the run. The simulation itself peaks near 2 GB, which matters for
    Phase 4's Cloud Run sizing.
- **The one-standard-error rule against the winner's curse.** The highest estimated chance among thousands is
  partly luck.
  - A lineup counts as tied with the best if its chance is within 1 standard error of the *paired* difference.
    Both are scored on the same simulations, so only the simulations where they differ add noise.
  - Among the tied, **the most expected points wins**, as the user chose: steadier, and kinder to overall rank.
- **Tie-breaks for lineups that score exactly the same.**
  - Starting a keeper who won't play scores the same as starting his replacement, because the auto-sub brings
    him on.
  - Captaining an injured player scores the same as captaining his vice-captain, because the armband passes on.
  - Both read absurdly, so exact ties go to the starters and captain who actually play.
  - Bench orders are tried in expected-points order.
- **The guard's edge is inside it** (tolerance 1e-9). Averages over n simulations are multiples of 1/n, so
  lineups land exactly on the edge. The brute-force test caught float rounding deciding those both ways.
- **Starting order is FPL's** (GK, DEF, MID, FWD), because auto-subs scan starters in that order.
- **Dry run: `python -m fpl_agent.optimize`** (read-only). It prints:
  - the recommendation, the changes from your current lineup, and a comparison with your current lineup and
    the highest-expected-points one;
  - the lineup each buffer 1–5 would choose;
  - chip effects as information.
  - The rules engine checks the final lineup.
  - The fetch-and-simulate steps moved to `fpl_agent/model/pipeline.py`, shared with `python -m fpl_agent.model`.

**Verification.**
- **Brute force:** every XI × bench order × captain pair on random squads. The optimizer's candidates are
  exactly the lineups within the guard, with the right points in each simulation.
- **Planted cases:**
  - An underdog captains the differential. The opponent captains a player I own, so matching them can never
    win.
  - A favourite captains the shared player, which locks in the win.
  - A keeper and a star who won't play are benched and never captained.
- **The tie rule:** a +0.3% edge spread over 1,500 differing simulations is a tie, and the extra expected points
  win. A +5% edge wins outright.

**First live run (GW6).** In 2.3 s, 481 of 550 XIs were within reach. Against your opponent:
- P(win by 3+) goes from 54.6% with your current lineup to **59.6%**, expected points from 51.2 to **53.9**.
- **It starts João Pedro (2.1 expected points) over two bench players at 2.8.** He plays in only 45% of the
  simulations but averages 4.7 when he does, and when he doesn't, the auto-sub brings a bench player on. That's
  worth about 3.8: "start the doubtful player, keep a good bench", found by scoring auto-subs exactly.
- **It captains Fernandes (5.3, home to Spurs) over Haaland (5.1, away at Liverpool).** FPL's own `ep_next` says
  8.0 vs 2.0, because it follows recent form and largely ignores fixtures. The model's call is reasonable, and it
  is the kind of pick part 7's back-test must check.
- **Every buffer from 1 to 5 picks the same lineup.** This week the buffer costs nothing (Q5).

**Alternatives.**
- *Brute force*: exact but about 6 minutes.
- *Greedy steps* (best XI, then captain, then bench): fast, but it can miss XIs that are only good because of
  their bench or captain. The João Pedro case is one.
- *A heuristic shortlist* (e.g. top 20 XIs by expected points): the guard gives an *exact* cutoff for free.
- *Storing every candidate*: 200 MB to over 1 GB.
- *Always taking the highest estimated chance*: chases noise, and the lineup could flip between runs.

---

## D32. Transfer recommendations over a 5-gameweek horizon; how long a flag lasts

*Phase 3 part 5, 2026-09-30. Agreed with the user: horizon 5 gameweeks weighted 1.0 / 0.9 / 0.8 / 0.7 / 0.6; a free
transfer must gain 1.5 points (0 when rolling would lose it to the 5-transfer cap); a hit must gain 4 + 2; at most
2–3 transfers. Recommendation only (D28): nothing is sent to FPL.*

**Context.** A transfer lasts for weeks. Judging it on one gameweek buys players for one good fixture.

**Decisions** (`fpl_agent/optimize/transfers.py`, `fpl_agent/data/injuries.py`, `model/pipeline.simulate_ahead`):
- **The horizon is simulated with the same model**, one gameweek at a time, keeping only the points. Memory
  stays at the single-gameweek peak (about 2 GB), and it takes about 3 s per week.
  - Lineup predictions for later weeks use the **real recent window** (the one before the next gameweek). The
    window before a future gameweek hasn't been played yet.
  - Kalshi usually prices only the next gameweek (GW6: 8 of 10 matches; GW7–10: none). Later weeks use the
    xG-ratings fallback (D16), one reason they're weighted less.
- **How long a flag lasts** (new, reasoned rather than fitted). FPL's flag is about the *next* round only.
  Applying it to all five weeks would make the planner sell every player with a knock. From the second horizon
  week, the flag is read with FPL's news text, which comes in a few fixed forms:
  - "Expected back 10 Oct" / "Suspended until 25 Oct": out until that date.
  - Doubtful (50/75%) with no date: a knock, over after the next gameweek.
  - Injured or suspended with no date ("Unknown return date"): out for the whole horizon, the cautious reading.
  - "u" / "n" (loans, moves abroad): out for good, and never bought.
- **A squad's value in a week is its best lineup's expected points** (part 4's search with a zero guard:
  auto-subs, bench and captain included), weighted over the horizon.
- **The search runs in stages.** Scoring every option exactly costs about 2.5 s per option-week, so:
  1. **Screen.** For each player I could sell, take the 10 best replacements at his position by horizon expected
     points. Each gets a *quick* value: the best XI + captain from per-player expected points. That's exact for
     the simplified problem (brute-force tested), but blind to bench auto-subs.
  2. **Combine.** Pairs and triples come from the 20 best singles. Replacements may cost up to "bank + his
     price + the most one other sale could free", so a second sale can fund a dearer buy.
  3. **Check legality** with `rules.squad_violations`: budget from *selling* prices (rule 5), the 3-per-club
     limit (rule 6), and squad shape.
  4. **Score the 5 best exactly** in every horizon week. Choose the largest gain above its threshold; "roll" is
     the baseline at 0.
- **Tuning pass on part 4's pruning bound.** When k outfield starters miss out, the bench can add at most its
  k best scores in that simulation, rather than all of them. That's still an upper bound (brute-force tests
  unchanged), but XIs scored for GW6 fell from 481 to 156, and the search time from 2.3 s to 1.2 s.

**Verification.**
- The thresholds, including the cap case.
- The quick value against brute force over all 550 XIs.
- Planted squads:
  - a clear upgrade is taken;
  - a dearer forward is bought only when a second sale funds him;
  - the club limit blocks a fourth player;
  - a +1 upgrade is taken only when rolling would lose the transfer;
  - a second +3 upgrade doesn't justify a hit.
- The flag-duration rules, including news that isn't about injury ("on loan until January").

**First live run (GW6; 5 free transfers, £0.0m bank).** About 130 legal options screened, about 35 s in total
including the four extra simulations.
- The first run recommended João Pedro → Barry, Kadıoğlu → Mukiele, Gvardiol → Hall: +14.1 weighted points.
- **The João Pedro sale was inflated by a Phase 2 bias** (Q7). His GW5 injury absence counted as "dropped", so
  he was predicted 2.0–2.4 points a week. Fixed in D33.
- After the fix he projects 2.9 this week (75% flag) and 3.0–3.4 after. The recommendation is still João Pedro →
  Barry, Kadıoğlu → Hall, Gvardiol → Mukiele, but now **+12.7** weighted points (needs +3.0), +3.3 this week.
  This week's P(win by 3+) goes from 61.1% to 66.6%.
- Barry has 3.5 xG in 5 starts. Hall and Mukiele are nailed starters with better fixtures than Kadıoğlu and
  Gvardiol.
- Options that end in the same squad (two defenders bought for two sold, either way round) are now listed once.

**Alternatives.**
- *This week only*: the classic trap, rejected by the user.
- *Exact scoring of every option*: about 2.5 s × 5 weeks × hundreds of options.
- *Ranking by the players' own expected points*: ignores whether the new player would even start in your XI.
- *Keeping today's flag for all five weeks*: sells every player with a knock.
- *Clearing every flag after the next week*: buys players with long injuries.

---

## D33. Excusing injury absences before snapshots existed, and doubtful-then-absent matches (answers Q7)

*2026-09-30. Both changes approved by the user.*

**Context.** Q7: a regular who missed GW5 injured looked "dropped". His start rate halved, which skewed his lineup
odds and made the transfer advice sell him (João Pedro: 45% to play, about 2.2 points a week after).

**Decisions** (`fpl_agent/data/lineups.py`; this supersedes part of D14):
- **Reconstructed flags.** For a window gameweek with no snapshot, the player's flag at that deadline is
  **today's flag, if it was set before that deadline** (`news_added`, FPL's "last set or changed" time). An
  unchanged flag set earlier was already in force then. A saved snapshot always wins.
- **Doubtful counts too.** A match is excused when the player carried **any flag below 100%** (out *or*
  doubtful) at the deadline **and played 0 minutes**. D14 excused only flagged-out players. If he played, the
  match counts as usual.
- **History uses today's real flags, even for later horizon weeks.** `simulate_ahead` clears a knock for weeks
  2–5 (D32), but only for availability (`predict_all(..., available_as=...)`). Otherwise the recovered copy
  would lose the excuse for the match he missed. A first live run caught this: his later weeks stayed at
  2.1–2.4 until it was fixed.

**Effect (GW6).** João Pedro plays in 61% of simulations (was 45%). He projects 2.9 points this week (was 2.1) and
3.0–3.4 after (was 2.1–2.4). The transfer advice still sells him for Barry (who projects 4.0–5.4), but the
comparison is now fair. The back-test is unchanged: rebuilt past seasons carry no flags.

**Alternatives.**
- *Waiting for GW5 to leave the 2-gameweek window*: the decisions for GW6–7 would stay biased.
- *Reconstruction alone*: doesn't help doubtful players, and João Pedro was 75%.
- *Excusing every 0-minute match*: hides real droppings.

**Consequences.**
- A doubtful player left out for rotation is now excused too, which flatters his start rate slightly.
- To check at GW11 with the snapshot data (`docs/maintenance.md`): how often "doubtful, then 0 minutes" was
  followed by the player starting the next match.

---

## D34. Saving the lineup: DRY RUN by default, checks against a fresh read, one un-retried POST

*Phase 3 part 6, 2026-09-30.*

**Context.** Phase 0 proved the write: `POST /api/my-team/{entry}/` with `{chip, picks: [{element, position,
is_captain, is_vice_captain}]}` returned 202 Accepted, and the team read back matched. CLAUDE.md: anything that
writes to FPL defaults to DRY RUN, needs `--live` (or `FPL_LIVE=1` in the cloud), and logs the exact payload
first.

**Decisions** (`fpl_agent/submit.py`, `FplClient.save_lineup`):
- **The payload is a pure function of the lineup.** Positions 1–11 are the starters in FPL order, 12 is the bench
  keeper, 13–15 the bench in auto-sub order, plus the captain and vice flags. `chip` is always `null` until
  Phase 5.
- **DRY RUN unless told otherwise.** `python -m fpl_agent.optimize` prints the exact payload and sends nothing.
  `--live`, or `FPL_LIVE=1` for the Phase 4 job, saves the *recommended* lineup.
- **Checks against a fresh read of the team, just before sending.** Any failure means nothing is sent, and the
  reasons are printed.
  - **Before the deadline, with a 1-minute margin.** After the deadline, the same request would quietly set the
    *next* gameweek's team: a wrong-week save is worse than none.
  - **Exactly the current squad.** You may have made a transfer on the site since the recommendation was
    computed.
  - **The rules engine passes the lineup** (formation, bench keeper first, captain and vice among the starters
    and different).
  - **No chip** (D28).
- **One POST, never retried** (the session retries GETs only, D8). A failed save is reported, not repeated: a
  retried write could apply twice. After a save, **the team is read back and compared**.
- **The save comes before the transfer section** in the command, so a live save never waits behind the extra
  simulations.

**Transfers (spike, pending).** Transfers stay recommendation-only until Phase 5. The transfers endpoint has never
been exercised. Instead of a test transfer from code (a real transfer, possibly a −4), the user records the
request the website itself sends when they make a transfer they want anyway
(`spikes/phase3_transfers.md`, in their own browser). The agent's Playwright login stays untouched, because a
second login could rotate or revoke its refresh token (Q1, D12). The expected shape is unverified (Q8).

**Verification** (fake HTTP session, so nothing can reach FPL):
- the payload shape;
- `--live` / `FPL_LIVE=1` and nothing else enables a save;
- each check blocks a save (deadline, a changed squad, an illegal lineup, a chip);
- a dry run sends nothing but logs the payload;
- a live save POSTs once with the auth, `Origin` and `Referer` headers, logs before sending, and verifies the
  read-back;
- a read-back mismatch is reported;
- a failed POST raises and isn't retried.
- Live: the dry run on the real GW6 team passes every check.

**Alternatives.**
- *Saving the lineup at the end of the run*: it would wait behind the transfer simulations, closer to the
  deadline.
- *Retrying a failed save*: it could apply twice. The Phase 4 job instead alerts by email.
- *A test transfer from code*: costs a real transfer.
- *Capturing with the agent's Playwright browser*: risks the agent's login.

---

## D35. Back-testing the optimizer on the league's real lineups (GW2–5): no evidence yet that it beats managers

*Phase 3 part 7, 2026-10-01. Scope approved: lineup and captain only (transfers wait for Phase 5); AVERAGE weeks use
today's ownership; markdown report plus a published HTML page.*

**Method** (`fpl_agent/validation/optimizer.py`, `python -m fpl_agent.validation.optimizer [--reuse]`):
- **Setup.** For each of GW2–5:
  - rebuild the simulation from what was known before the deadline (the D24 point-in-time rebuild, 10,000
    simulations);
  - take each of the 11 league managers' **actual squads** (44 team-weeks) and their actual H2H opponent;
  - model the opponent as part 3 would have before the deadline.
- **Three lineups of the same squad** are scored on **real results** by the rules engine, with real minutes (so
  auto-subs happen as they did) and the manager's own chip and hits:
  - what the manager played;
  - the agent's recommendation;
  - the highest-expected-points lineup.
- **Check:** the managers' own team sheets reproduce FPL's official scores in **44 of 44** team-weeks.
- **The report also explains the gap.** It looks at the starters only one side picked, and adds a "clean"
  comparison: only team-weeks where every swapped player played the gameweek before.
- **Records keep the lineups,** in the gitignored cache. Reports never show entry IDs.

**Results** (`docs/optimizer_backtest.md`):
- **Real points:**
  - Agent vs manager: **−2.2 points per team-week (95% −4.7 to +0.4)**.
  - Highest-expected-points vs manager: −1.8.
  - The model *expected* the agent to gain +1.1.
- **H2H:** agent 19-0-25 against the managers' 21-0-23. Of the 6 results it changed, 2 were better and 4 worse.
- **Win chances:** predicted 55%, actual 43% (Brier 0.260, against 0.250 for a coin flip). Over-optimistic in this
  sample.
- **Where it loses.** It changes about 2 starters per team-week (same XI as the manager in 1 of 44).
  - Of its 87 swapped-in starters, **24% played 0 minutes** (managers' picks: 11%).
  - **18 of those were out two weeks running:** injuries FPL had flagged, which managers saw and the rebuilt
    weeks can't, since there were no flag snapshots before GW6. Part 4's "start a doubtful player, the bench
    covers him" logic is exactly what backfires when availability is wrong.
  - Even among swapped players who played, managers' picks scored more (4.5 vs 3.2 points).
  - The agent also swapped in forwards far more (24 vs 6). This season's GW2–5 has forwards overpredicted by
    +0.59 (72 cases), but 2025/26 shows no forward bias (−0.13, about 600 cases), so this is on the watch list,
    not a fix.
- **Clean comparison:** −1.9 (95% −5.8 to +2.1) over 21 team-weeks. Inconclusive.

**Reading.**
- This is **no evidence that the agent's lineups beat a careful human manager yet**. The point estimates are
  small losses, and the sample can't separate them from zero.
- The injury gap is an artifact of the back-test (live runs have flags), but it doesn't explain the whole
  difference.
- The agent's value so far is automation (never missing a deadline) and consistency. Whether it also *adds*
  points has to be shown on fair weeks.

**Humility test** (asked by the user, 2026-10-04; in the report as "A humbler agent"). Keep the manager's lineup
unless the agent's *expected* gain is at least T points:

| T (points) | Switches | Agent vs manager (95%) |
|---|---|---|
| 0 (today's agent) | 43 | −2.1 (−4.7 to +0.4) |
| 0.5 | 27 | −0.9 (−2.9 to +1.2) |
| 1.0 | 17 | −1.0 (−2.7 to +0.8) |
| 1.5 | 15 | −0.5 (−2.1 to +1.1) |
| 2.0 | 10 | −0.2 (−1.2 to +0.9) |
| 3.0 | 2 | +0.2 (−0.1 to +0.5) |

- **Most of the loss came from small-margin switches.** Predicted gains under 0.5 points covered 16 team-weeks,
  10 of which started a player out two weeks running (the injury blind spot), for a real −3.4. Live flags remove
  most of these.
- **Bigger predicted gains did no better.** 27 team-weeks, only 5 injury cases, about −1.4. Not significant, but
  no sign that the model's confident lineup changes beat an informed manager.
- **Caveat for autonomy.** Here the lineup kept is a *fresh, informed human choice*. When the agent runs alone,
  the lineup it would keep is the one saved on FPL, usually last week's, which hasn't seen this week's news. So
  this measures deferring to a manager, not deferring to last week's lineup.
- The thresholds were chosen on the same 44 team-weeks, so the curve is a guide, not a tuned value.

**Alternatives.**
- *Skipping the back-test until GW10*: this already caught the doubtful-starter risk and the forward signal.
- *Inventing past flags* (e.g. treating "0 minutes last week" as injured): that would grade the model with rules
  the live agent doesn't use.
- *Comparing only against FPL's average*: managers in this league are the real competition.

**Consequences (Q9).** The real verdict needs fair gameweeks, GW6 onward, where the agent has the same injury
flags as managers. Rerun at about GW10–11, and keep comparing the agent's weekly dry-run pick with the user's own
lineup.

---

## D36. Phase 4: a self-scheduling Cloud Run job, one token in Secret Manager, three runs per deadline

*Phase 4, 2026-10-04. Approved by the user: three runs per deadline, Gmail app password, gcloud script (no
Terraform), region us-east1 (chosen over London: free Cloud Storage tier, cheaper Cloud Run; the job talks to FPL
behind Cloudflare, Kalshi in the US, and Gmail, so the user's location doesn't matter). Live saving on: the agent
writes the lineup itself.*

**Runs** (`fpl_agent/run.py`, `fpl_agent/cloud/schedule.py`):
- **check, 24 h before the deadline.** Refreshes the FPL token, which proves the login works, and records the flag
  and odds snapshots. It emails only if something needs the user (for example "log in again"), while there's still
  time to fix it.
- **save, 60 min before.** Runs the gameweek (`fpl_agent/agent.run_gameweek`, shared with
  `python -m fpl_agent.optimize`), saves the lineup through the D34 checks, and emails the full summary.
- **final, 15 min before.** Re-runs with the latest news, saves only if the pick changed, and emails only then or
  on a failure. If it fails, the save run's lineup stands: never miss a deadline (goal 1).
- **Every run, even a failed one, re-points all three Cloud Scheduler jobs** to their next times, computed from
  FPL's `deadline_time` (CLAUDE.md). So one bad run can't break the chain.
- **Any error emails the user** before the job exits non-zero.

**The single-token rule.** The refresh token rotates on every use, and sending a replaced one revokes the login
(D12). So:
- `SecretManagerTokenStore` saves by adding a new version, then **disabling every older version**.
- A failed save sets a `needs-relogin` label, and the next run stops and emails instead of refreshing with the
  stale token (Q2). The current run still has a valid access token for an hour, so it carries on.
- After `python -m fpl_agent.cloud.upload_token`, **every command, local ones included, uses that one token**
  (`FPL_TOKEN_STORE=secret-manager`), and the local file is retired.
- A **run lock** (an object in the bucket that only one process can create at a time) means a local command and a
  cloud run can never refresh at once. A scheduled run that finds the lock held waits up to about 3 minutes.

**Other pieces.**
- **State in Cloud Storage.** Snapshots and saved odds keep the same names and JSON as the local files, behind
  the existing store protocols. `simulate_next` takes the stores as parameters.
- **Email** through Gmail SMTP with an app password. The address and password reach the job as environment
  variables straight from Secret Manager, never in code or the public repo.
- **Container.** `python:3.12-slim` plus the `cloud` extra. Playwright moved to a `login` extra, because the
  browser login runs on the user's Mac only. `.dockerignore` and `.gcloudignore` keep `.env`, `.secrets/` and
  `data/` out of the upload and the image.
- **Least-privilege accounts.** The runner gets admin on the token secret only, read access to the email secrets,
  objects in its bucket, and the right to re-point Scheduler. A separate account may only trigger the job.
- **Job settings:** 4 GiB (the simulation peaks around 2 GB, D31), 15-minute timeout, no retries (a retried run
  could save twice; the final run is the retry), no parallel runs.

**Resources, and why each one** (project `fpl-agent-jt` (`fpl-agent` was taken), region us-east1):

| Resource | What it does here | Rejected alternatives |
|---|---|---|
| **Cloud Run job** `fpl-agent` (2 vCPU, 4 GiB, 15 min, 1 task, no retries) | Runs the container on demand. It pays only while running and scales to zero. A *job* (run to completion) fits a batch task. | *Cloud Run service*: an always-on web server we'd have to call. *Compute Engine VM*: pays 24/7, and OS patching is ours. *Cloud Functions*: 9-minute and memory limits are tight for a 2 GB simulation. *GitHub Actions cron*: free, but its schedules can run late by many minutes, and it would need the FPL token as a repo secret. |
| **Cloud Scheduler** (3 jobs) | Triggers the job at exact times. It calls the Cloud Run "run" API with OAuth as the `fpl-agent-scheduler` account. | *A sleep loop on a VM*: it pays 24/7. *Fixed cron polling FPL every 15 min*: wasted runs and FPL requests. *Workflows or Pub/Sub*: more pieces for no gain. |
| **Secret Manager** (`fpl-tokens`, `fpl-email`, `fpl-email-app-password`) | Encrypted, versioned, access-controlled secrets. Versions make the "one live token" rule enforceable (disable old ones). Cloud Run injects the email secrets as environment variables. | *Environment variables baked into the job*: visible to anyone who can view the job, and unversioned. *A file in the bucket*: no versions to disable, weaker access control. |
| **Cloud Storage bucket** `fpl-agent-jt-fpl-agent-state` | The job's memory between runs: flag snapshots, saved odds, and the run lock (create-only-if-absent is atomic on the server). | *Firestore*: a database for a handful of JSON files. *Container disk*: wiped after every run. |
| **Artifact Registry + Cloud Build** | Cloud Build turns the source into an image (from the Dockerfile) and stores it in Artifact Registry. Created automatically by `gcloud run jobs deploy --source`. | *Building locally with Docker and pushing*: needs Docker Desktop on the Mac, and the image would be built for Apple silicon rather than the cloud's x86. |
| **Two service accounts** | `fpl-agent-runner` (the job's identity): admin on the token secret only, read on the email secrets, objects in its bucket, Scheduler admin to re-point its jobs. `fpl-agent-scheduler`: may only trigger the job. | *The default compute account*: broad project rights. A leaked or buggy job could touch everything. |
| **Budget alert** ($5/month; emails at 50/90/100%) | Guards against surprises. Expected cost is $0 to a few cents: free tiers cover Cloud Run, Scheduler (3 jobs), Secret Manager, and Storage in us-east1. Artifact Registry over 0.5 GB costs about $0.10/GB-month. | — |

**Deploy findings** (2026-10-04, each fixed in `deploy/deploy.sh` or the code):
- **Build permission.** New projects (since 2024) don't let the default compute account read the uploaded source.
  The script now grants it `roles/cloudbuild.builds.builder`.
- **`.gcloudignore` follows gitignore rules.** The pattern `data/` also dropped the code package
  `fpl_agent/data/`, and the first cloud run failed with `No module named fpl_agent.data`. Patterns are now
  anchored (`/data/`), and the upload list was checked with `gcloud meta list-files-for-upload` (no `.env`,
  `.secrets/` or `data/`).
- **gRPC DNS.** Google's Python clients default to gRPC, whose own DNS resolver failed on the Mac ("Could not
  contact DNS servers"). Secret Manager and Scheduler clients now use the REST transport, which works everywhere.
- **Scheduler flags.** `jobs update http` takes `--update-headers`, not `--headers`. Updates also no longer pass
  `--schedule`, so a redeploy can't reset the real schedule to the placeholder.
- **Local Google auth.** The Python libraries need Application Default Credentials (`gcloud auth
  application-default login` with a quota project), separate from the CLI login.
- **A safety gap closed.** `upload_token` refuses to run again unless a fresh browser login is newer than the last
  upload. The old browser state holds a replaced refresh token, and uploading it would revoke the login.

**Verified live.**
- The first cloud `check` logged in from the cloud and saved the GW6 snapshot and odds (9/10 matches) to the
  bucket. It set the schedule: check Fri 9 Oct 10:00, save Sat 10 Oct 09:00, final Sat 10 Oct 09:45 UTC.
- A test email arrived.
- A token refresh through Secret Manager made version 2 the only enabled one (version 1 disabled) and released
  the lock.

**Alternatives.**
- *A fixed schedule polling FPL every 15 minutes*: many wasted runs and requests.
- *One run per deadline*: a single failure misses the deadline.
- *A local file token plus a cloud copy*: two copies would revoke the login.
- *Terraform*: production-grade, but a large extra tool for about 6 resources.
- *SendGrid or the Gmail API*: another signup, or an OAuth consent flow.
- *London region*: no free storage tier, higher Cloud Run price, no benefit for this job.

**Consequences.**
- The user's to-dos: a Google Cloud project with billing (and a budget alert), a Gmail app password, and running
  the deploy script, then `upload_token`, then a first `check` run.
- After that, the agent saves the lineup on its own every gameweek. Transfers and chips stay with the user until
  Phase 5 (and the Q8 capture).

---

## D37. Phase 5a: the agent makes transfers itself (free transfers only, once per gameweek)

*2026-10-05. Approved: 5a first; free transfers only until the GW10–11 review; learn the transfers request from
FPL's own website code; optimizer-based planner later (5b).*

**Learning the request (answers Q8 without a capture).** The FPL website's public JavaScript bundle shows exactly
what "Confirm transfers" sends:
- `POST transfers/` with `{chip, entry, event, transfers: [{element_in, element_out, purchase_price,
  selling_price}]}`.
- `purchase_price` is the incoming player's `now_cost`. `selling_price` is the outgoing player's selling price
  from `/my-team/`. `event` is the upcoming gameweek. `chip` is `wildcard`/`freehit` when played, else null.
- It goes through the same helper and headers as the `my-team/` save proven in D34.
- Wildcard and Free Hit are activated the same way with an empty transfer list. Bench Boost and Triple Captain go
  through `my-team/` (5c).
- The site's error codes include `transfer_element_in_price_mismatch` / `..._out_...` (prices moved) and
  `transfer_cap_exceeded`.
- **Probe, run by the user on 2026-10-05:** an empty transfer list in exactly this shape got **200** with an empty
  body. The squad was unchanged, with 0 transfers made and 5 still free. So the endpoint, the auth and the payload
  shape are accepted, and an empty list is a no-op.

**Decisions** (`fpl_agent/submit.py`, `fpl_agent/agent.py`, `FplClient.make_transfers`):
- **Only the cloud's save run (60 min before the deadline) makes transfers.** They can't be undone, so they happen
  once, at a fixed time. The final run never transfers. Locally, it takes an explicit `--live --transfers`.
- **Order of a save run:** opponent → transfer plan (D32) → **make the transfers** → re-read the squad → pick and
  save the lineup *for the new squad*. If the transfers fail or are blocked, the run carries on and saves the best
  lineup for the current squad. The email's subject says what happened ("2 transfers made, lineup saved" or
  "TRANSFERS FAILED").
- **Checks against a fresh read of the team, just before sending.** Any failure means nothing is sent, and the
  reasons are in the email.
  - before the deadline (1-minute margin);
  - every sale still owned, every buy not owned, nobody twice;
  - **prices unchanged since planning** (purchase and selling). If they changed, it doesn't send and re-plans next
    time;
  - the resulting squad is legal (budget from selling prices, 3 per club, shape);
  - at most 3 transfers a week;
  - no chip;
  - **free transfers only**: no −4 hits until the GW10–11 review (`FPL_ALLOW_HITS=1` turns them on).
- **Free transfers left = `limit − made`,** as the FPL site computes it. Part 5 had used `limit`, which would have
  overcounted after a manual transfer earlier in the week. The planner also takes `allow_hits` and never plans a
  hit when they're off.
- **One POST, never retried, then a read-back.** Every buy must be in the squad and every sale gone, or the email
  flags it.

**Verified.**
- Tests on fakes cover: the payload shape, `limit − made`, each check (deadline, hits off, ownership, changed
  prices, budget, club limit, max 3), dry run vs live, a single POST with the website's headers, read-back,
  failure without retry, the email subject, and the planner rolling with hits off.
- Live dry run (`--transfers`, no `--live`) on the real GW6 team: the plan and payload passed every check, and
  nothing was sent.

**Alternatives.**
- *Transfers in every run*: a second run could act on a half-finished state.
- *Retrying a failed transfer*: could apply twice.
- *Sending without re-checking prices*: FPL rejects mismatches anyway, but checking first gives a clear reason in
  the email.
- *A browser capture first*: the website's own code already shows the exact request.
- *Allowing hits from day one*: the model's lineup edge isn't proven yet (D35/Q9).

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

**Q5. Should the H2H objective require winning by a buffer?** *(User idea, 2026-09-29, for Phase 3.)* *Default chosen in D28: buffer 3, points guard 1.0; Phase 3 will report what each buffer size costs.* *First evidence (D31, GW6): buffers 1–5 all chose the same lineup, so the buffer cost nothing that week. `python -m fpl_agent.optimize` prints this every run; revisit after several gameweeks.*
Aim to beat the opponent by a margin of **3–5 points**, deliberately small so the model's outputs are still
trusted. With simulated gameweeks this is one parameter: maximize P(my points − opponent's points ≥ buffer)
instead of P(my points > opponent's points). Before fixing the value, Phase 3 should show how much plain
win probability each buffer size costs.

**Q6. Do the opponent model's assumptions hold as the season goes on?** *(D30, 2026-09-30.)*
Starting values: captain habit weight 0.85 / temperature 1.0, AVERAGE scale 0.9, chip chance 30–80%, and habit
scaled by availability.
- *Captain spread*: re-run `python scripts/fit_opponent.py` every ~5 gameweeks. Each gameweek adds 10 decisions.
  Change the constants only if the fit moves clearly, not by noise (the likelihood surface is flat).
- *AVERAGE scale*: from GW6 the snapshots hold pre-deadline ownership. Refit once there are 3+ such gameweeks,
  and watch the misses: early-season ownership churn is the known weak spot.
- *Chip chances*: after each double gameweek, and in GW18–19, record which league opponents held a chip and
  whether they played it. By GW19 there's enough to say if 30%+ is too high or too low. **Revisit the whole
  scheme for the second half (GW20–38)**, where the double gameweeks cluster late and the user may want a
  different starting point.
- *Availability scaling*: check the first cases where an opponent's habitual captain was flagged. Did they switch?

**Q7. Absences before flag snapshots existed (GW1–5) count as "benched while fit".** *(Found in D32, 2026-09-30.)* *Answered in D33: both changes made.*
The lineup model excuses a missed match only if a snapshot shows the player flagged out at that deadline (D14).
Snapshots began at GW6, so an injury absence in GW4–5 looks like being dropped. With the 2-gameweek window, one
such match halves a regular's start rate: João Pedro, who started GW1–4, is predicted to play 45% this week and
about 2.2 points a week after. This drives the "sell João Pedro" recommendation and his part 4 lineup numbers. It
fades by itself once GW5 leaves the window (from GW8), but decisions before then are affected.
Two changes, for the user to decide (his case needs **both**):
- *Reconstruct past flags from the current one.* A flag whose `news_added` is before an earlier deadline, and
  is still unchanged, was in force at that deadline too. It's cheap and uses only facts FPL publishes. On its
  own it only helps players flagged *out* (D14 excuses only those). João Pedro was 75% at the GW5 deadline.
- *Also excuse a doubtful (50/75%) player's match when he played 0 minutes.* D14 deliberately doesn't, because a
  doubtful player left out can also be rotation. The back-test can't settle it (the archives have no historical
  flags). From GW6 on, snapshots let us measure how often "doubtful, then 0 minutes" was followed by the player
  going straight back into the team.
- Recommendation: do both now, and check the second with the snapshot data at GW11 (with the D13 re-measurement).

**Q8. What exactly does the transfers endpoint expect?** *(D34, 2026-09-30.)* *Answered in D37, from the FPL website's own code.* Expected, unverified:
`POST /api/transfers/` with `{"chip", "entry", "event", "transfers": [{"element_in", "element_out", "purchase_price",
"selling_price"}]}`, with Wildcard and Free Hit set via `chip`. Does the site send a check request before the
confirm?
Recommendation: capture it from the website the next time the user makes a transfer anyway
(`spikes/phase3_transfers.md`), well before Phase 5 needs it (about GW12).

**Q9. Does the agent's lineup beat a careful human manager?** *(D35, 2026-10-01.)* On GW2–5 the answer was "no
evidence": −2.2 points per team-week (95% −4.7 to +0.4), confounded by missing injury flags.
Recommendation:
- Rerun the back-test at about GW10–11 on GW6+ (flags available; about 50 more team-weeks).
- Until then, keep the agent's lineups advisory, as they already are, and log each week's dry-run pick against
  the user's actual lineup.
- Before Phase 4 makes the agent set the lineup itself, decide with that evidence. If it still trails, consider
  making the agent change fewer starters, for example only when the expected gain clears the noise.
- Watch the forward bias (+0.59 on this season's 72 cases).
- *Humility test (D35):* a minimum expected gain before switching (about 1–1.5 points) removed most of GW2–5's
  loss, but no threshold beat the managers. Decide at GW10–11, on fair weeks, whether the autonomous agent should
  only change the saved lineup when the gain clears such a threshold.
