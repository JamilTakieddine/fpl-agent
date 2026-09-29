"""FPL scoring thresholds that the API doesn't expose.

game_config.scoring gives the POINT values (read at run time, rule 8). The thresholds below are
not anywhere in the API, so they are constants here, each VERIFIED against how FPL actually
awarded points in GW1-5 2026/27 (the `explain` breakdown in /event/{gw}/live/; D21):

- DEFCON count: CBIT (clearances, blocks, interceptions, tackles) for DEF; CBIRT (+ recoveries)
  for MID/FWD. Matched FPL's defensive_contribution stat in 100% of player-matches.
- DEFCON thresholds: DEF 10, MID/FWD 12. DEF awarded from 10, never at 9; MID from 12, never at
  11; FWD consistent (no forward reached 9-15 in the sample). At most one award per match.
- Saves: 1 point per 3 (100/100 goalkeeper matches).
- Goals conceded: -1 per 2, GK and DEF only (633/633).
- Clean sheet: 0 conceded WHILE ON THE PITCH and 60+ minutes (633/633).
- Appearance: 60+ minutes is long_play, 1-59 short_play (point values from game_config).

Re-verify at the start of each season (docs/maintenance.md).
"""

from __future__ import annotations

from fpl_agent.data.models import PositionCode

DEFCON_THRESHOLD: dict[PositionCode, int | None] = {"GKP": None, "DEF": 10, "MID": 12, "FWD": 12}
SAVES_PER_POINT = 3
GOALS_CONCEDED_PER_POINT = 2
CLEAN_SHEET_MIN_MINUTES = 60
LONG_PLAY_MINUTES = 60
