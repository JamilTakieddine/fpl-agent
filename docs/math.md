# The maths behind the agent

The agent uses no language model. Every decision comes from probability, simulation and
optimisation, in plain, tested code. This page lists each mathematical idea it uses: what it
is in plain English, the formula as the code implements it, where it lives, and why it was chosen.
The "D" numbers point to [`decisions.md`](decisions.md), where the alternatives that lost are
written up.

**The pipeline in one line:** market prices → probabilities (1) → expected goals (2–5) →
10,000 simulated gameweeks (6–11) → a model of the opponent (12–14) → the best lineup (15–16) →
the best plan of transfers and chips (17–19), all checked by statistics (20–21).

---

## From market prices to expected goals

### 1. Implied probability: removing the overround
- **What:** a market's prices for home, draw and away don't add up to exactly 100%. The excess (or
  shortfall) is the *overround*. Rescaling the prices to sum to 1 gives fair probabilities.
- **Formula:** with the bid/ask midpoints $m_i$, $\;p_i = m_i \,/\, \sum_j m_j$.
- **Where:** `fpl_agent/data/odds.py` (D15).
- **Why:** the agent needs honest win, draw and loss chances, not prices that include a margin.

### 2. The Poisson distribution
- **What:** the probability of seeing $k$ rare, independent events when $\lambda$ are expected
  on average.
- **Formula:** $P(k) = \dfrac{\lambda^k e^{-\lambda}}{k!}$.
- **Where:**
  - each team's goals, $\text{goals} \sim \text{Poisson}(\lambda)$ (`data/odds.py`,
    `model/scoreline.py`);
  - goalkeeper saves and penalty saves (`model/defence.py`, `model/discipline.py`).
- **Why:** goals are rare, roughly independent events in a fixed time. Poisson is the standard
  model for that, and it has a single parameter.

### 3. Bisection: finding λ from the prices
- **What:** a root-finding method. Repeatedly halve an interval until the value that solves an
  equation is pinned down.
- **Formula:**
  - **Total goals:** find $T = \lambda_{home} + \lambda_{away}$ with
    $P(\text{goals} > X.5 \mid T)$, from a Poisson with mean $T$, equal to the market's "over $X.5$" price.
  - **The split:** split $T$ so the model's $P(home) - P(away)$ matches the market's.
- **Where:** `data/odds.py` (D15). When there's no usable total-goals market, $T$ comes from the
  draw price.
- **Why:** there's no formula that turns prices back into expected goals, but each probability
  rises steadily with $\lambda$, so bisection always converges. No extra dependency is needed.

### 4. The Dixon–Coles adjustment
- **What:** a correction to two independent Poissons, which underrate low-scoring draws.
- **Formula:** multiply the probabilities of the four lowest scores by:
  - $\tau(0,0) = 1 - \lambda_h\lambda_a\rho$;
  - $\tau(0,1) = 1 + \lambda_h\rho$;
  - $\tau(1,0) = 1 + \lambda_a\rho$;
  - $\tau(1,1) = 1 - \rho$.

  All other scores are unchanged. $\rho$ is set so the model's draw probability matches the
  market's.
- **Where:** `data/odds.py`, `model/scoreline.py` (D27).
- **Why:** real football has more 0–0 and 1–1 results than the plain model predicts, and the
  market's draw price says by how much.

### 5. A multiplicative ratings model (the fallback)
- **What:** when a match has no usable market, expected goals come from team strengths.
- **Formula:**
  $`\lambda_{home} = \text{home avg} \times \text{attack}_{home} \times \text{weakness}_{away}`$,
  from expected-goals (xG) data, with each rating shrunk towards 1 (see 9).
- **Where:** `model/scoreline.py` (D16).
- **Why:** it's simple and stable. Measured against the market, xG ratings beat ratings built from
  actual goals: mean absolute error 0.39 against 0.47 goals, on the 8 teams available at the time.

## Simulating the gameweek

### 6. Monte Carlo simulation
- **What:** estimate a whole probability distribution by random sampling, many times over. The
  **law of large numbers** says the averages settle on the true values.
- **Where:** `fpl_agent/model/` plays out **10,000** gameweeks. Each one draws scorelines,
  minutes, goals, assists, clean sheets, defensive actions, cards and bonus, and scores them
  with FPL's own rules.
- **Why:** a team's points come from many interacting random events. Simulation gives the full
  distribution, so the agent can ask "what's my chance of beating this opponent?", not just
  "what's my average?".

### 7. Conditional and categorical sampling
- **What:** draw an outcome from weighted options, given what has already happened.
- **Where:**
  - **Minutes:** each player is available or not, then starts, comes on or doesn't play
    (`minutes.py`, D18).
  - **Goals:** each simulated goal goes to a player **on the pitch at that minute**, weighted by
    expected goals per 90 (`attack.py`, D20).
  - **Assists:** each assist goes to another player on the pitch, weighted by expected assists.
- **Why:** it keeps the simulated world consistent. If the team scores 0, nobody scores. A
  substituted defender only loses his clean sheet for goals conceded while he was on.

### 8. Poisson processes and the negative binomial distribution
- **What:**
  - In a **Poisson process**, the chance of at least one event in time $t$ is
    $1 - e^{-\text{rate} \times t}$.
  - The **negative binomial** is a count distribution like Poisson, but with variance larger than
    the mean (*overdispersion*).
- **Where:**
  - **Cards:** $P(\text{card}) = 1 - e^{-\text{rate} \times \text{minutes}/90}$
    (`discipline.py`, D22).
  - **Defensive contributions:** negative binomial with variance $= k \times$ mean (`defence.py`,
    D21 and D27).
- **Why:** cards are yes/no events that scale with time on the pitch. Defensive actions vary
  more than Poisson allows, which matters because the +2 bonus only counts once a threshold is
  hit.

### 9. Shrinkage (Bayesian priors and empirical Bayes)
- **What:** blend a player's own rate with his position's average, trusting his own figures more
  the more minutes of evidence there are.
- **Formula:**
  - **Rates:**
    $\text{rate}_{90} = \dfrac{\text{events} + \text{position rate} \times m_0/90}{(\text{minutes} + m_0)/90}$,
    where $m_0$ is the prior's weight in minutes (`attack.py`, `defence.py`, `discipline.py`).
  - **Bonus base (empirical Bayes):** the weight comes from the data itself.
    $\text{keep} = \dfrac{\sigma^2_{between}}{\sigma^2_{between} + \sigma^2_{within}/n}$
    (`bonus.py`, D23).
- **Why:** early in the season, samples are tiny. Without shrinkage, one lucky 20-minute cameo
  looks like elite form.

### 10. Least-squares regression
- **What:** choose weights that minimise the squared error between predictions and real data.
- **Where:**
  - **Bonus points:** fit FPL's bonus-points (BPS) weights to the simulated events, from real
    per-match BPS, every run (`bonus.py`, D23).
  - **The "AVERAGE" opponent:** a scale fitted through the origin,
    $s = \sum t_i y_i / \sum t_i^2$ (`optimize/opponent.py`, D30).
- **Why:** to learn the hidden formulas from past data rather than assume them.

### 11. Ranking, ties and common random numbers
- **What:**
  - **Competition ranking:** rank = 1 + the number of players with a strictly higher score.
  - **Common random numbers:** compare options on the *same* random draws.
- **Where:**
  - **Bonus:** handed out by competition ranking, which reproduces FPL's tie rules exactly
    (`bonus.py`).
  - **Comparing lineups:** every lineup is scored against the same 10,000 simulated gameweeks and
    opponent draws (`scoreline.py`, `lineup.py`).
- **Why:** reusing the same draws cancels most of the noise when two lineups are compared, so
  small real differences show through.

## Modelling the opponent

### 12. Softmax with temperature
- **What:** turns scores into probabilities that sum to 1. The temperature controls how
  decisive the result is.
- **Formula:**
  $P(\text{captain} = i) = w \cdot \text{habit}_i + (1 - w) \cdot \dfrac{e^{xP_i/T}}{\sum_j e^{xP_j/T}}$.
  - $\text{habit}_i$ is their share of recent captaincies;
  - $xP$ is expected points;
  - $w = 0.85$ and $T = 1.0$.
- **Where:** `optimize/opponent.py` (D30).
- **Why:** managers mostly repeat their captain, and otherwise pick the obvious choice. This
  blends both.

### 13. Maximum likelihood estimation
- **What:** pick the parameters that make what actually happened most probable.
- **Formula:** maximise $\sum \log P(\text{actual captain})$ over a grid of $w$ and $T$.
- **Where:** `fit_captain` in `optimize/opponent.py`, fitted on the league's real captain picks
  (D30).
- **Why:** principled, and with only two parameters the whole grid can be printed and checked by
  eye.

### 14. Base rates for chips
- **What:** the chance an opponent plays a chip.
- **Formula:**
  - **In a double gameweek or a chip's last 2 weeks:** $1 / (\text{chip-worthy weeks left})$,
    kept between 30% and 80%.
  - **Otherwise:** FPL's own rate for ordinary weeks,
    $\text{mean}(\text{plays} / \text{managers still holding it})$.
- **Where:** `chip_probabilities`, `ordinary_chip_rates` in `optimize/opponent.py` (D30, D44).
- **Why:** someone holding a chip spreads it over the good weeks left. Outside those weeks the
  chance is small but real: in GW1–7 of 2026/27, Triple Captain was played by 0.8–16% of all
  managers a week, so it isn't zero.

## Choosing the lineup

### 15. Optimising a probability, under a constraint
- **What:** pick the lineup that maximises the chance of winning, not the average score.
- **Formula:** maximise $P(\text{my points} - \text{their points} \ge 3)$ across the
  simulations, subject to $E[\text{my points}] \ge \max E - 1$.
- **Where:** `optimize/lineup.py` (D28, D31). An **upper bound** on each starting XI prunes the
  ones that can't meet the constraint, exactly, without scoring them.
- **Why:** head-to-head is won week by week. The 1-point guard stops the agent from gambling away
  overall rank.

### 16. Standard errors and the winner's curse
- **What:**
  - A **standard error** is how far an estimate could be off because of luck.
  - The **winner's curse:** the option with the highest *estimated* value is partly just lucky.
- **Formula:** lineups whose win chance is within 1 standard error of the best (of the paired
  difference) count as tied. Among those, the one with the most expected points wins.
- **Where:** `optimize/lineup.py` (D31).
- **Why:** it doesn't chase simulation noise, and it's steadier for overall rank.

## Planning transfers and chips

### 17. Mixed-integer linear programming (MILP)
- **What:** optimise a linear objective over yes/no decisions under linear rules. The **HiGHS**
  solver finds a proven best answer by *branch and bound*.
- **Formula:** for every candidate player $p$ and week $t$, binary variables say whether he's
  *in the squad*, *in the XI*, *captain*, *bought* or *sold*. The objective is:

  ```math
  \max \sum_t w_t \Big(\sum_p xP_{p,t}\,\text{XI}_{p,t} + \text{captain bonus} + \epsilon\,\text{bench}\Big) - 4\cdot\text{hits} + v\cdot\text{free transfers banked at the end}
  ```

  subject to FPL's rules:
  - a squad of 2 goalkeepers, 5 defenders, 5 midfielders and 3 forwards;
  - at most 3 per club;
  - a legal formation;
  - the bank, using selling prices;
  - free transfers rolling up to the cap;
  - at most 3 transfers a week.
- **Where:** `optimize/planner.py` (D38); chips are added in D39.
- **Why:** there are astronomically many sequences of transfers over 5 weeks, and an exact solver
  searches them all implicitly.

### 18. Discounting future weeks
- **What:** count later weeks for less, because forecasts further ahead are less certain.
- **Formula:** week weights $w = (1, 0.9, 0.8, 0.7, 0.6)$.
- **Where:** `optimize/transfers.py`, `optimize/planner.py` (D32).
- **Why:** a transfer that only pays off in five weeks' time is worth less than one that pays off
  now.

### 19. Decision thresholds ("expected value must beat its cost")
- **What:** only act when the expected gain beats what acting costs.
- **Formula:**
  - **Transfers:** a free transfer must gain 1.5 points over the horizon (nothing if it would
    otherwise be lost to the cap); a −4 hit must gain $4 + 2$ (`required_gain`, D32, D37).
  - **Chips:** play one when its exact gain $\ge$ what keeping it is worth. For Triple Captain
    and Bench Boost that's 1.25 × a typical week; Wildcard 15; Free Hit 12. On a chip's last
    possible week, any gain $\ge 0$ is enough.
- **Where:** `optimize/transfers.py`, `agent.py` (D39, D40, D43).
- **Why:** a used transfer or chip is gone. The thresholds price in its option value.

## Checking it works

### 20. Paired comparisons and standard errors
- **What:** compare two strategies week by week on the *same* weeks, and measure how much of the
  difference could be luck.
- **Formula:** the season total of the weekly differences $d$, with standard error
  $\text{sd}(d)\sqrt{n}$. That's the "±" in the back-tests.
- **Where:** `validation/planner_backtest.py`, `validation/optimizer.py` (D35, D40).
- **Why:** to avoid mistaking a lucky season for a better strategy.

### 21. Scoring rules for probability forecasts
- **What:** measures of how good a probability forecast is.
- **Formula:**
  - **Brier score:** $\frac{1}{n}\sum (p - y)^2$. Lower is better.
  - **Log loss:** $-\frac{1}{n}\sum [y \log p + (1-y)\log(1-p)]$. It punishes confident
    mistakes.
  - **Calibration:** of all the events given 30%, did about 30% happen?
  - **Spearman correlation:** how well the predicted ranking matches the real one.
- **Where:** `validation/metrics.py`, reported in [`validation.md`](validation.md) (D24–D26).
- **Why:** to prove the simulation's probabilities are honest before anything acts on them.
