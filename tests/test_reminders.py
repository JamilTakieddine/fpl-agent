# Gameweek reminder emails: which gameweeks send one, a subject that stands out from the weekly
# summaries, and a body that says what to do, how long it takes and how to start; no email sent.

from __future__ import annotations

from fpl_agent.reminders import ACTION_PREFIX, REMINDERS, reminder_for


def test_reminders_fall_on_the_review_gameweeks() -> None:
    assert sorted(REMINDERS) == [10, 14, 19, 20, 38]
    assert reminder_for(9) is None and reminder_for(11) is None


def test_every_reminder_stands_out_and_says_what_to_do() -> None:
    for r in REMINDERS.values():
        assert r.subject.startswith(ACTION_PREFIX)
        assert "What to do:" in r.body and "Time needed:" in r.body
        assert "How to start" in r.body
    review = reminder_for(10)
    assert review is not None and "GW10-11 review" in review.subject
    assert "Q9" in review.body and "FPL_ALLOW_HITS" in review.body
