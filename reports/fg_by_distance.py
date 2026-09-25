"""Field goal make rate by distance band and season, as a formatted workbook.

Three visible sheets:

  Totals     every FBS field goal in the corpus, distance bands down, seasons across.
  By Team    the same grid, driven by a dropdown in B4. The numbers are SUMIFS over a
             hidden `_data` sheet, so changing the team recalculates in place -- one
             sheet rather than 137 near-identical ones, and no macros, so it survives
             email, SharePoint and Google Sheets' importer.
  Notes      what was counted, what was excluded, and why.

Scope decisions, all of them restated on the Notes sheet:

  * Field goals only. PATs are place kicks too, but they are 55k attempts at one distance
    and would drown the under-20 band.
  * `fg_made IS NULL` is excluded -- those are kicks wiped out by penalty, so they are
    neither a make nor a miss.
  * FBS means the KICKING team was FBS in that season (`kicking_ncaa_division`), which is a
    per-season fact, not a per-team one, so a team that moved up mid-window is blank
    before it arrived rather than zero.
  * Attempts whose distance did not parse land in an `Unknown` row rather than being
    dropped, so `All distances` still reconciles to the team's real attempt count.

    .venv/bin/python -m reports.fg_by_distance
    .venv/bin/python -m reports.fg_by_distance --team "Ohio State Buckeyes"
    .venv/bin/python -m reports.fg_by_distance --league nfl --team "Baltimore Ravens"

The snapshot has held two leagues since 2026-09-11, so `--league` is a real scope and not a
label: without it the totals would blend 31,460 college field goals with 12,967 NFL ones.
The FBS/FCS exclusions above are college-only, because the NFL has no second division.
"""
from __future__ import annotations

import argparse
import os

import duckdb
import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell

from .workbook import F, MEASURES, Style, abs_range, heading, stamp, write_grid, write_notes

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HOME, "data", "out", "pbp.duckdb")
OUT = os.path.join(HOME, "data", "out", "fg_by_distance.xlsx")

# Keyed by lower bound so the rows sort numerically; 999 is the unparseable bucket.
BANDS = [(10, "Under 20"), (20, "20-29"), (30, "30-39"), (40, "40-49"),
         (50, "50-59"), (60, "60 and over"), (999, "Unknown")]

# A field goal shorter than 10 yards is not physically possible -- the posts sit 10 yards
# deep and the snap is 7 more -- so treat it as a parse failure, not a very short kick.
BAND_EXPR = """
CASE WHEN fg_distance_yds IS NULL OR fg_distance_yds < 10 THEN 999
     WHEN fg_distance_yds < 20 THEN 10
     ELSE least((fg_distance_yds // 10) * 10, 60) END
"""

# `{league}` is not optional. The snapshot has held two leagues since 2026-09-11, and
# although `kicking_ncaa_division = 'FBS'` happens to exclude the NFL here (that column is NULL
# for every NFL row), relying on that would be relying on an accident -- and EXCLUDED_SQL
# below has no such filter, so its counts really would have blended the two corpora.
FACTS_SQL = f"""
SELECT kicking_team AS team, season, {BAND_EXPR} AS band,
       count(*) AS att, sum(fg_made::int) AS made
FROM play
WHERE play_kind = 'field_goal' AND fg_made IS NOT NULL
  AND league = '{{league}}' {{div}}
{{extra}}
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

EXCLUDED_SQL = f"""
SELECT count(*) FILTER (WHERE fg_made IS NULL)                                AS negated,
       count(*) FILTER (WHERE fg_made IS NOT NULL {{div}}
                          AND ({BAND_EXPR}) = 999)                            AS unparsed,
       count(*) FILTER (WHERE fg_made IS NOT NULL AND {{non_top}})            AS non_fbs
FROM play WHERE play_kind = 'field_goal' AND league = '{{league}}' {{extra}}
"""

HID = "_data"          # hidden sheet holding the team x season x band facts
C_TEAM, C_SEASON, C_BAND, C_ATT, C_MADE, C_LIST = 0, 1, 2, 3, 4, 6
PICKER = "$B$4"


def load(con, seasons: tuple[int, int] | None, league: str = "cfb"):
    extra = "" if seasons is None else f"AND season BETWEEN {seasons[0]} AND {seasons[1]}"
    # The FBS/FCS split is a college concept. The NFL has no second division, so there is
    # nothing to filter to and nothing to exclude -- see README "Known limits" §13.
    div = "AND kicking_ncaa_division = 'FBS'" if league == "cfb" else ""
    non_top = ("coalesce(kicking_ncaa_division, 'FCS') <> 'FBS'" if league == "cfb" else "FALSE")
    fmt = dict(extra=extra, league=league, div=div, non_top=non_top)
    facts = con.execute(FACTS_SQL.format(**fmt)).fetchall()
    excluded = con.execute(EXCLUDED_SQL.format(**fmt)).fetchone()
    return facts, excluded


def rollup(facts, team):
    """Sum the facts into every (season, band) combination the grid asks for.

    `None` stands for 'all', so one dict answers the body, the totals row, the totals
    column and the grand total. Returned twice: every team, and for one team.

    Neither sink is named `league`: in this file `league` means which corpus, cfb or nfl,
    and one name for both would shadow the parameter `build()` passes in.
    """
    all_teams: dict[tuple, list[int]] = {}
    mine: dict[tuple, list[int]] = {}
    for t, s, b, a, m in facts:
        for k in ((s, b), (s, None), (None, b), (None, None)):
            for sink in (all_teams,) if t != team else (all_teams, mine):
                cur = sink.setdefault(k, [0, 0])
                cur[0] += a
                cur[1] += m
    return all_teams, mine


# What to call the population on the sheets. "FBS" is a college word, and the NFL workbook
# was printing "FBS field goals by distance" over NFL kicks until 2026-09-11 -- the queries
# were scoped by league from the start, the prose was not.
TIER = {"cfb": "FBS", "nfl": "NFL"}


def build(con, out_path: str, default_team: str | None, seasons,
          league: str = "cfb") -> str:
    tier = TIER[league]
    facts, excluded = load(con, seasons, league)
    if not facts:
        raise SystemExit("no field goal rows matched -- check --seasons")

    years = sorted({r[1] for r in facts})
    teams = sorted({r[0] for r in facts})
    team = default_team or teams[0]
    if team not in teams:
        raise SystemExit(f"unknown team {team!r}; e.g. {', '.join(teams[:3])}, ...")
    all_teams, mine = rollup(facts, team)

    wb = xlsxwriter.Workbook(out_path, {"nan_inf_to_errors": True})
    wb.set_properties({"title": f"{tier} field goals by distance",
                       "comments": "Generated by reports/fg_by_distance.py"})
    style = Style(wb)
    ws_tot = wb.add_worksheet("Totals")
    ws_team = wb.add_worksheet("By Team")
    ws_notes = wb.add_worksheet("Notes")
    ws_hid = wb.add_worksheet(HID)

    write_hidden(ws_hid, facts, teams)
    span = 1 + (len(years) + 1) * len(MEASURES)
    sub = stamp(os.path.relpath(DB, HOME), os.path.getmtime(DB))

    totals_sheet(ws_tot, style, years, all_teams, span, sub, tier)
    team_sheet(ws_team, style, years, mine, span, sub, teams, team, len(facts), tier)
    notes_sheet(ws_notes, style, years, all_teams, excluded, team, sub, tier, league)

    ws_hid.hide()
    ws_tot.activate()
    wb.close()
    return out_path


def write_hidden(ws, facts, teams) -> None:
    """The long-format facts the By Team sheet sums over, plus the dropdown's team list."""
    ws.write_row(0, 0, ["team", "season", "band", "att", "made"])
    for i, (t, s, b, a, m) in enumerate(facts, start=1):
        ws.write_string(i, C_TEAM, t)
        ws.write_number(i, C_SEASON, s)
        ws.write_number(i, C_BAND, b)
        ws.write_number(i, C_ATT, a)
        ws.write_number(i, C_MADE, m)
    ws.write(0, C_LIST, "teams")
    for i, t in enumerate(teams, start=1):
        ws.write_string(i, C_LIST, t)


def totals_sheet(ws, style, years, all_teams, span, sub, tier) -> None:
    heading(ws, style, f"{tier} field goals by distance -- all teams",
            "Attempts, makes and make rate. " + sub, span)

    def cell(band, season, measure, r, c):
        att, made = all_teams.get((season, band), (0, 0))
        if measure == "Att":
            return att or None
        if measure == "Made":
            return made if att else None
        return made / att if att else None

    write_grid(ws, style, 3, years, BANDS, cell,
               all_periods_label=f"{years[0]}-{years[-1]}")
    ws.hide_gridlines(2)


def team_sheet(ws, style, years, mine, span, sub, teams, team, n_facts, tier) -> None:
    heading(ws, style, f"{tier} field goals by distance -- one team",
            "Pick a team in B4; every number below recalculates. " + sub, span)

    ws.write(3, 0, "Team", style.picker_label)
    ws.merge_range(3, 1, 3, 4, team, style.picker)
    ws.data_validation(3, 1, 3, 1, {
        "validate": "list",
        "source": "=" + abs_range(HID, C_LIST, 1, len(teams)),
        "input_title": "FBS team",
        "input_message": "Pick a team; the grid follows.",
    })

    rng = {c: abs_range(HID, c, 1, n_facts)
           for c in (C_TEAM, C_SEASON, C_BAND, C_ATT, C_MADE)}

    def sumifs(src: int, band, season) -> str:
        parts = [rng[src], rng[C_TEAM], PICKER]
        if season is not None:
            parts += [rng[C_SEASON], str(season)]
        if band is not None:
            parts += [rng[C_BAND], str(band)]
        return "=SUMIFS(" + ",".join(parts) + ")"

    def cell(band, season, measure, r, c):
        att, made = mine.get((season, band), (0, 0))
        if measure == "Att":
            return F(sumifs(C_ATT, band, season), att)
        # Both remaining measures are guarded on the Att cell in their own group, so a
        # season a team spent in FCS reads blank while a real 0 for 3 keeps its zero.
        att_cell = xl_rowcol_to_cell(r, c - 1 if measure == "Made" else c - 2)
        if measure == "Made":
            return F(f'=IF({att_cell}=0,"",{sumifs(C_MADE, band, season)[1:]})',
                     made if att else "")
        made_cell = xl_rowcol_to_cell(r, c - 1)
        return F(f'=IF({att_cell}=0,"",{made_cell}/{att_cell})', made / att if att else "")

    write_grid(ws, style, 6, years, BANDS, cell,
               all_periods_label=f"{years[0]}-{years[-1]}")
    ws.hide_gridlines(2)


def notes_sheet(ws, style, years, all_teams, excluded, team, sub, tier, league) -> None:
    negated, unparsed, non_fbs = excluded
    grand_a, grand_m = all_teams[(None, None)]
    unk_a = all_teams.get((None, 999), [0, 0])[0]
    coverage = ", ".join(f"{y}: {all_teams[(y, None)][0]:,}" for y in years)

    write_notes(ws, style, f"Notes -- {tier} field goals by distance", [
        ("What is counted", [
            f"One row per field goal attempt by an {tier} team, {years[0]}-{years[-1]}: "
            f"{grand_a:,} attempts, {grand_m:,} made ({grand_m / grand_a:.1%}).",
            "Place kicks here means field goals only. PATs are place kicks as well, but "
            "there are about 55,000 of them at a single distance and they would drown the "
            "under-20 band. They can go on their own sheet if you want them.",
            "Bands are 10 yards wide on the kick distance -- which already includes the 10 "
            "yards of end zone and the 7-yard snap -- and are keyed by their lower bound. "
            "Under 20 and 60 and over are open-ended.",
        ]),
        ("What is excluded, and why", [
            f"{negated:,} attempts have no make or miss recorded. Those are kicks wiped out "
            "by penalty: neither a make nor a miss, so they are out of the numerator and "
            "the denominator both.",
            (f"{non_fbs:,} attempts were kicked by a non-FBS team. FBS is read per season "
             "from dim_team_season, never per team, so a programme that moved up mid-window "
             "is blank before it arrived rather than zero."
             if league == "cfb" else
             "Nothing is excluded for division: the NFL has no second division, so every "
             "attempt in the corpus is by a top-flight team."),
            f"{unparsed:,} {tier} attempts have no readable distance in the play text and sit "
            f"in the Unknown row ({unk_a:,} of them within this report's seasons). They are "
            "kept rather than dropped so All distances still reconciles to the team's real "
            "attempt count.",
        ]),
        ("How the By Team sheet works", [
            f"B4 is a dropdown over every {tier} team in the data. Each cell is a SUMIFS "
            "against the hidden _data sheet, so the whole grid recalculates the moment you "
            f"change the team. It opens on {team}.",
            "No macros, so it survives email and SharePoint, and Google Sheets imports it "
            "intact. Right-click any tab and Unhide _data to see the underlying team / "
            "season / band / attempts / makes rows, or to pivot them yourself.",
            "Pct is blank rather than 0% where there were no attempts, so an empty band "
            "does not read as a team that missed everything.",
        ]),
        ("Coverage -- worth a glance before quoting a number", [
            f"{tier} attempts by season. {coverage}.",
            "2020 is short by design: the COVID season had roughly a third fewer games. "
            "Any other season that looks light, or a single team-season that looks light "
            "next to its neighbours, is worth checking against the schedule before the "
            "number leaves the building.",
            sub + ". Rebuild it with scripts/build_snapshot.py when the warehouse changes.",
        ]),
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--team", default=None, help="team the By Team sheet opens on")
    ap.add_argument("--seasons", default=None, help="e.g. 2019-2025")
    ap.add_argument("--league", default="cfb", choices=["cfb", "nfl"],
                    help="which corpus (default college). The snapshot holds both.")
    a = ap.parse_args()

    seasons = None
    if a.seasons:
        lo, _, hi = a.seasons.partition("-")
        seasons = (int(lo), int(hi or lo))

    con = duckdb.connect(a.db, read_only=True)
    out = a.out
    if a.league != "cfb" and out == OUT:
        root, ext = os.path.splitext(out)
        out = f"{root}_{a.league}{ext}"      # never overwrite the other league's workbook
    print("wrote " + build(con, out, a.team, seasons, a.league))


if __name__ == "__main__":
    main()
