# The gameweek email (D46): plain-English subject and body from a GameweekReport (decisions,
# chances, reasons, opponent, look-ahead; test runs labelled; never payloads or IDs), the test
# run's copy of the team after transfers, and failure emails keeping the technical log; no network.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fpl_agent.agent import RunResult, apply_moves
from fpl_agent.data.models import MyTeam
from fpl_agent.optimize.rules import Lineup, SquadPlayer
from fpl_agent.optimize.transfers import Move
from fpl_agent.report import Captaincy, ChipVerdict, GameweekReport, Transfer, body, subject
from fpl_agent.run import email_body, email_subject
from fpl_agent.submit import SubmitResult
from tests.conftest import load_fixture

NEW_YORK = ZoneInfo("America/New_York")

# GW8 as the agent saw it on 20 Oct 2026.
GW8 = GameweekReport(
    event=8,
    deadline=datetime(2026, 10, 23, 17, 30, tzinfo=UTC),
    live=True,
    opponent="Rival FC",
    opponent_captains=(("João Pedro", 0.87),),
    opponent_chip_chance=0.06,
    opponent_forecast=49.4,
    transfers=(
        Transfer("F.Kadıoğlu", "Thomas", 44, 40),
        Transfer("Calvert-Lewin", "Gonzalo", 60, 61),
        Transfer("Maguire", "Muharemović", 48, 50),
    ),
    transfer_chip=None,
    team_chip=None,
    free_transfers=3,
    hit=0,
    bank_after=2,
    gain_horizon=6.9,
    gain_now=2.1,
    roll_best=None,
    horizon=5,
    chips=(
        ChipVerdict("3xc", False, 5.9, 7.2),
        ChipVerdict("bboost", False, 9.0, 15.5),
        ChipVerdict("wildcard", False),
        ChipVerdict("freehit", False),
    ),
    starters=(
        ("GKP", "Raya"),
        ("DEF", "Calafiori"),
        ("DEF", "Mukiele"),
        ("DEF", "Thomas"),
        ("MID", "Mbeumo"),
        ("MID", "B.Fernandes"),
        ("MID", "Ødegaard"),
        ("MID", "E.Le Fée"),
        ("FWD", "Haaland"),
        ("FWD", "Gonzalo"),
        ("FWD", "Barry"),
    ),
    bench=("Kinsky", "Gomez", "Muharemović", "O'Shea"),
    captaincy=Captaincy("Calafiori", "Haaland", 5.93, 0.92, 5.71),
    forecast=58.2,
    p_win=0.65,
    p_win_before=0.605,
    free_next=1,
)


def test_the_subject_says_what_happened_at_a_glance() -> None:
    assert subject(GW8) == "FPL GW8: 3 transfers · Calafiori (C) · 65% to win vs Rival FC"
    assert subject(replace(GW8, live=False)).startswith("TEST, nothing sent to FPL · FPL GW8: ")
    wildcard = replace(GW8, transfer_chip="wildcard", transfers=GW8.transfers * 2)
    assert "FPL GW8: Wildcard (6 changes) · Calafiori (C)" in subject(wildcard)
    triple = replace(GW8, team_chip="3xc", transfers=())
    assert subject(triple).startswith("FPL GW8: no transfers · Calafiori (C) · Triple Captain · ")
    assert subject(replace(GW8, update="captain Haaland (was Calafiori)")).startswith(
        "FPL GW8 update: "
    )


def test_the_body_is_plain_english_in_your_time_zone() -> None:
    text = body(GW8, NEW_YORK)
    assert text.startswith("GW8 · deadline Fri 23 Oct, 1:30 pm (New York time)")
    for section in ("DECISIONS (done)", "YOUR CHANCES", "WHY", "YOUR OPPONENT: Rival FC"):
        assert section in text
    assert "Transfers (3 of 3 free, no points hit):" in text
    assert "OUT F.Kadıoğlu     ->  IN Thomas" in text
    assert "Bank after: £0.2m" in text
    assert "Lineup (3-4-3): Raya | Calafiori, Mukiele, Thomas |" in text
    assert "Bench: Kinsky (GK) · 1. Gomez · 2. Muharemović · 3. O'Shea" in text
    assert "Chance to win: 65% (60% with the team as it was)" in text
    assert "Transfers: +6.9 points expected over the next 5 weeks (+2.1 this week)." in text
    assert (
        "Captain Calafiori: forecast 5.9 when he plays (92% chance he does); if he doesn't, "
        "the armband passes to Haaland (5.7)." in text
    )
    assert "Bench Boost: kept, +9.0 points now vs 15.5 saved for a better week." in text
    assert "Wildcard: kept for a better week." in text
    assert "Likely captain: João Pedro (87%)" in text and "Chance they play a chip: 6%" in text
    assert "LOOKING AHEAD: 1 free transfer next week." in text
    for jargon in ("{", "payload", "element", "xPts", "P(win by", "DRY RUN"):
        assert jargon not in text  # no JSON, IDs or developer terms


def test_the_other_cases_read_right() -> None:
    test = body(replace(GW8, live=False), ZoneInfo("UTC"))
    assert "THIS IS A TEST RUN: nothing was sent to FPL." in test
    assert "DECISIONS (what a live run would do; nothing was sent)" in test
    assert "(UTC time)" in test
    rolled = body(replace(GW8, transfers=(), roll_best=(3.1, 4.5), free_next=4), NEW_YORK)
    assert "Transfers: none this week" in rolled
    assert "the best option added +3.1 points over 5 weeks, short of the 4.5 needed" in rolled
    sure = replace(GW8, captaincy=Captaincy("Haaland", "Raya", 8.1, 1.0, 4.2))
    assert "Captain Haaland: forecast 8.1 points." in body(sure, NEW_YORK)
    played = replace(GW8, team_chip="3xc", chips=(ChipVerdict("3xc", True, 8.1, 7.5),))
    assert "Chips: Triple Captain played" in body(played, NEW_YORK)
    assert "Triple Captain: PLAYED, +8.1 points now vs 7.5 to keep it." in body(played, NEW_YORK)
    average = replace(GW8, opponent="FPL's average score", opponent_captains=())
    assert "playing to beat FPL's average score" in body(average, NEW_YORK)


def test_a_test_run_applies_transfers_to_a_copy_of_the_team() -> None:
    team = MyTeam.model_validate(load_fixture("my_team"))
    sold = next(p for p in team.picks if p.element == 391)  # a defender, selling for 5.6m
    players = {173: SquadPlayer(173, "DEF", 7, 40)}
    after = apply_moves(team, [Move(sell=391, buy=173)], players, {173: 2})
    new = next(p for p in after.picks if p.element == 173)
    assert new.position == sold.position and new.element_type == 2
    assert new.selling_price == new.purchase_price == 40
    assert after.transfers.bank == team.transfers.bank + sold.selling_price - 40
    assert after.transfers.made == team.transfers.made + 1
    assert any(p.element == 391 for p in team.picks)  # the real team object is untouched


def outcome(report: GameweekReport | None, *, problems: list[str] | None = None) -> RunResult:
    saved = SubmitResult(sent=True, payload={}, problems=problems or [], verified=True)
    lineup = Lineup((1,), (2,), 1, 1)
    return RunResult(8, GW8.deadline, lineup, True, 0.61, saved, None, report=report)


def test_run_emails_use_the_report_and_failures_keep_the_details() -> None:
    assert email_subject("save", outcome(GW8), live=True) == subject(GW8)
    failed = outcome(GW8, problems=["deadline passed"])
    assert email_subject("save", failed, live=True) == (
        "FPL GW8: lineup SAVE FAILED (P(win by 3+) 61%)"
    )
    test_failed = email_subject("save", failed, live=False)
    assert test_failed is not None and test_failed.startswith("TEST, nothing sent to FPL · ")
    log = 'LIVE payload for POST /my-team/: {"chip": null}'
    calm = email_body(outcome(GW8), log, NEW_YORK, failed=False)
    assert "TECHNICAL DETAILS" not in calm and "payload" not in calm
    broken = email_body(failed, log, NEW_YORK, failed=True)
    assert (
        broken.startswith("GW8 · deadline") and "TECHNICAL DETAILS (for troubleshooting)" in broken
    )
    assert log in broken
