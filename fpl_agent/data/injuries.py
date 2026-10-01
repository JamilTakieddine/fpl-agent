"""How long a flag lasts: a player's availability in a LATER gameweek (the transfer horizon).

FPL's flag (status + chance_of_playing_next_round) is about the NEXT round only. Applied to all
five horizon weeks it would make the planner sell every player with a knock (D32). From the
second horizon week on, the flag is read with the news text FPL writes in a few fixed forms:
- "... - Expected back 10 Oct" / "Suspended until 25 Oct": out until that date, then fit;
- doubtful (50 or 75%) with no date: a knock, over after the next gameweek;
- injured / suspended with no date ("Unknown return date"): out for the whole horizon, the
  cautious reading;
- unavailable or gone ("u", "n": loans, transfers abroad): out for good.
The next gameweek always uses the flag as it is.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from fpl_agent.data.models import Player

_RETURN = re.compile(r"(?:Expected back|until) (\d{1,2}) ([A-Z][a-z]{2})\b")
DOUBTFUL = "d"
GONE = ("u", "n")


def return_date(news: str, today: date) -> date | None:
    """The date in "Expected back 10 Oct" / "until 25 Oct", in the year that puts it no more than
    a few weeks in the past (the season crosses New Year). None if the news has no date."""
    m = _RETURN.search(news)
    if m is None:
        return None
    for year in (today.year, today.year + 1):
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%d %b %Y").date()
        except ValueError:
            return None
        if (d - today).days >= -60:
            return d
    return None


def as_of(player: Player, deadline: datetime, first_deadline: datetime, today: date) -> Player:
    """The player as the lineup model should see him for the gameweek with `deadline`:
    unchanged for the first (next) gameweek, otherwise with the flag cleared once it has run out."""
    flagged = player.status != "a" or player.chance_of_playing_next_round is not None
    if deadline <= first_deadline or not flagged or player.status in GONE:
        return player
    back = return_date(player.news, today)
    if back is not None:
        recovered = deadline.date() >= back
    elif player.status == DOUBTFUL:
        recovered = True  # a knock with no date: over after the next gameweek
    else:
        recovered = False  # injured or suspended, no date: out for the horizon
    if not recovered:
        return player
    return player.model_copy(
        update={"status": "a", "chance_of_playing_next_round": None, "news": ""}
    )
