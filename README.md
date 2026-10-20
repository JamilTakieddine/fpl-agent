# fpl-agent

An autonomous Fantasy Premier League manager. Before every gameweek deadline it simulates the week
10,000 times, then sets your lineup, captain, bench order, transfers and chips to maximize the chance
of beating this week's head-to-head opponent. It emails you what it did and why. You don't have to do
anything.

**Track record:**
- **Live as of GW7 (2026/27):** won 81–74 in my head-to-head league, running entirely on its own.
- **Replaying the whole 2025/26 season with the agent in charge:** **2,157 points**, against 1,536
  for never transferring and 2,025 for one-week transfer logic (`docs/planner_backtest.md`).

## How it works

1. **Betting odds become expected goals.** Each fixture's match-result and total-goals prices
   (Kalshi prediction markets, with fallbacks) are turned into expected goals for both teams.
2. **10,000 simulated gameweeks.** For every player, each simulation draws:
   - **minutes:** no minutes, a cameo or a full match, from predicted lineups and injury flags;
   - **attacking returns:** goals and assists, given the simulated scoreline;
   - **defensive returns:** clean sheets, saves and defensive contributions;
   - **bonus.**

   Each simulation is then scored with FPL's own scoring rules. Validated against two seasons
   (`docs/validation.md`).
3. **A model of your opponent.** Their picks are hidden until the deadline, so they're modelled
   from last week's squad:
   - a probability spread over their likely captain, from their past picks and who's in form;
   - a chance they play a chip, from how often managers really do (FPL's own counts).
4. **Lineup and captain.** It picks the lineup that maximizes P(beat the opponent by 3+), allowing
   at most 1 point less in expected points than the highest-scoring lineup.
5. **Transfers and chips, 5 weeks ahead.**
   - **Planning:** an exact optimizer (HiGHS mixed-integer programming) plans transfers and chips
     over the next 5 weeks.
   - **Checking:** every transfer and chip is then re-checked on the full simulation before it's
     made. A chip is only played when it beats what keeping it is worth.
   - **Limits:** free transfers only (unless you allow -4 hits). The first set of chips is
     scheduled before the GW19 cutoff, when unused chips are lost.
6. **It runs itself.** A Google Cloud job runs at three times for every deadline:
   - **24 hours before:** checks the login still works;
   - **60 minutes before:** makes the transfers, saves the lineup and emails the summary;
   - **15 minutes before:** re-checks with the latest team news.

   A watchdog checks every 30 minutes that the runs will fire. Alerts email you if anything fails.

Every design decision, the alternatives considered and why they lost, is in
[`docs/decisions.md`](docs/decisions.md) (44 of them, so far).

## Run it for your own team

You need Python 3.12 and an FPL team. A head-to-head league is optional: without one, it plays
to beat FPL's average score.

```bash
git clone https://github.com/JamilTakieddine/fpl-agent.git && cd fpl-agent
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,login,cloud]"
playwright install chromium
cp .env.example .env
```

1. **Your IDs.** In `.env`, set:
   - `FPL_ENTRY_ID`, the number in your team's points URL
     (`fantasy.premierleague.com/entry/<this>/event/…`);
   - optionally, `FPL_H2H_LEAGUE_ID`, the number in your head-to-head league's URL.
2. **Log in once.** `python spikes/phase0_auth.py` opens a browser for you to sign in to FPL.
   The session is saved to `.secrets/` (git-ignored, never committed).
3. **Dry run.** `python -m fpl_agent.optimize` prints the recommended lineup, captain, transfers and
   chips against this week's opponent. **Nothing is sent to FPL.**
4. **Go live**, either way:
   - **On your machine, before each deadline:**
     - `python -m fpl_agent.optimize --live` saves the lineup;
     - add `--transfers` to also make the planned transfers.
   - **Fully automatic, on Google Cloud:** follow [`deploy/README.md`](deploy/README.md). It needs
     a Google Cloud project with billing, and a Gmail app password for the emails.
     `./deploy/deploy.sh` sets everything up. Once your login is uploaded, it proves the scheduled
     runs work end to end.

**Safety defaults:**
- **Dry run unless you ask:** anything that writes to FPL is a dry run unless you pass `--live`
  (or set `FPL_LIVE=1` in the cloud).
- **Checked first:** every write is logged exactly, and checked against FPL's rules and the deadline
  before it's sent. It's never retried automatically, and it's read back to confirm it landed.
- **Free transfers only:** -4 hits are never taken unless you set `FPL_ALLOW_HITS=1`.

**Cost on Google Cloud:** about $0.10 a month (a fourth Cloud Scheduler job). Everything else
fits the free tiers. Set a budget alert anyway (Billing → Budgets & alerts in the Google Cloud
console).

## More commands

```bash
python -m fpl_agent.data          # deadlines, the next 6 gameweeks, your opponent, odds, predicted minutes
python -m fpl_agent.model         # simulate the next gameweek: your squad's expected points
pytest                            # 417 tests, no network
python -m fpl_agent.validation --report           # back-test the simulation on two seasons
python -m fpl_agent.validation.planner_backtest   # replay 2025/26 with the planner (~1 hour)
python -m fpl_agent.validation.optimizer          # back-test the lineup picks vs a league's real ones
python scripts/fetch_history.py   # download last season's archives for back-testing
python scripts/fit_opponent.py    # refit the opponent model on your league's history
```

What to re-check during and between seasons (FPL changes its rules and API):
[`docs/maintenance.md`](docs/maintenance.md).

## Disclaimer

An unofficial, personal project, not affiliated with the Premier League or Fantasy Premier League.
It uses FPL's undocumented web API with your own login, the way the FPL website does. Automating
your account may not be what FPL intends, so use it at your own risk. Odds data comes from Kalshi's
public market API.

MIT licensed: see [LICENSE](LICENSE).
