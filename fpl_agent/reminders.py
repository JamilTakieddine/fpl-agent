"""Reminder emails for the few times the agent needs you (D40). Sent by the check run 24 hours
before the gameweek's deadline, so the dates follow FPL's real schedule even if fixtures move.

The subject starts with ACTION_PREFIX so these stand out from the weekly summaries. Each one says
what to do, why, roughly how long it takes, and how to start.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_PREFIX = "⚠️ ACTION NEEDED - FPL agent: "
HOW_TO_START = (
    "How to start: open Claude Code in the fpl-agent folder and say what this email is about "
    '(for example "start the GW10-11 review"). Everything needed is in docs/decisions.md and '
    "docs/maintenance.md."
)


@dataclass(frozen=True)
class Reminder:
    gameweek: int  # the check run before this gameweek's deadline sends it
    subject: str
    body: str


def _r(gameweek: int, title: str, why: str, steps: list[str], time_needed: str) -> Reminder:
    body = "\n".join(
        [
            why,
            "",
            "What to do:",
            *(f"  {i}. {s}" for i, s in enumerate(steps, 1)),
            "",
            f"Time needed: {time_needed}.",
            "",
            HOW_TO_START,
            "",
            "Until then the agent keeps managing your team as usual; nothing breaks if this waits "
            "a few days.",
        ]
    )
    return Reminder(gameweek, ACTION_PREFIX + title, body)


REMINDERS: dict[int, Reminder] = {
    r.gameweek: r
    for r in (
        _r(
            10,
            "GW10-11 review during the international break (after GW10, before GW11)",
            "GW6-10 are the first five gameweeks the agent ran on its own, with the same injury "
            "information managers have. That's the fair test the GW2-5 back-test couldn't be "
            "(D35).",
            [
                "Rerun the optimizer back-test on GW6-10 (Q9): is the agent beating the league's "
                "managers, and does the 'humble' switching rule help?",
                "Decide whether to allow -4 hits (D37, FPL_ALLOW_HITS).",
                "Refit the opponent captain model and the AVERAGE scale (Q6, "
                "scripts/fit_opponent.py).",
                "Re-check the injury rules (D32 flag durations, D33 doubtful absences) and the "
                "minutes model's 10% surprise non-start rate (maintenance schedule).",
            ],
            "about 1-2 hours with Claude",
        ),
        _r(
            14,
            "check the chip plan before the GW15-19 run-in",
            "From GW15 the planner must use every first-set chip (Wildcard, Free Hit, Bench "
            "Boost, Triple Captain) by the GW19 deadline, when unused ones are lost.",
            [
                "Read the last few summary emails' 'keep it / play it' chip lines.",
                "Sanity-check the chips' keep-values (Q10) and adjust them if the agent has held "
                "chips through weeks where they'd clearly have paid.",
                "Look at which weeks the plan wants each remaining chip in, from GW15 to GW19.",
            ],
            "about 30-60 minutes",
        ),
        _r(
            19,
            "confirm every first-set chip is used (GW19 is the last chance)",
            "Unused first-set chips are lost at this gameweek's deadline. The planner should "
            "already have scheduled them.",
            [
                "Check tomorrow's summary email: every first-set chip should be played by GW19.",
                "If one isn't, play it yourself on the FPL site before the deadline, or ask "
                "Claude why the plan kept it.",
            ],
            "5-10 minutes",
        ),
        _r(
            20,
            "second half of the season: new chips, opponents' chips",
            "A second set of all four chips starts at GW20, and the opponent model's chip chances "
            "were set with the first half in mind (Q6).",
            [
                "Confirm the summary email shows the new chips available.",
                "Revisit the opponents' chip chances for the second half (Q6), when double "
                "gameweeks cluster late.",
                "Review the chip keep-values for the new set (Q10).",
            ],
            "about 30 minutes",
        ),
        _r(
            38,
            "season over: summer maintenance",
            "The season ends after this gameweek. FPL changes rules and the API between seasons.",
            [
                "Follow the 'start of each season' rows of docs/maintenance.md: archives, scoring "
                "rules, test fixtures, Kalshi team names, auth behaviour.",
                "Re-run the validation and back-tests on the finished season.",
                "Set up the new season's team on the FPL site; the agent resumes from GW1.",
            ],
            "an afternoon, any time in the summer",
        ),
    )
}


def reminder_for(gameweek: int) -> Reminder | None:
    return REMINDERS.get(gameweek)
