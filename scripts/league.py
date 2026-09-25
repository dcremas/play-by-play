"""Which league a pipeline run is about, and everything that differs because of it.

This project was single-league for its whole life and hardcoded `college-football` into
four fetchers. The NFL needs the same pipeline pointed at a different ESPN league slug, and
the differences between the two turn out to be small, finite and worth naming in one place
rather than branching at each call site:

    the ESPN slug        `college-football` vs `nfl`
    where raw data lands data/espn vs data/espn_nfl -- the two corpora stay independently
                         rebuildable, which is the one thing a shared directory would cost
    the week sweep       college is 17 regular + 5 postseason every season; the NFL went to
                         an 18th regular week in 2021, so WEEKS is a function of the season
    the division filter  `groups=80` restricts college to FBS. There is no NFL equivalent
                         and sending it returns nothing, so it is a per-league string
    the conference tree  college walks groups 80/81 (FBS/FCS); the NFL has AFC and NFC and
                         eight divisions under them, which is a different shape of walk

WHAT IS NOT HERE, because it was measured rather than assumed (see README):

    athlete ids are ONE id space across both leagues. 720 of 720 ids sampled from NFL
    participants that also appear in the college dimension returned the identical name from
    the NFL endpoint. Patrick Mahomes is 3139477 in both feeds and is the same man, so
    dim_athlete stays single-keyed and his college and NFL careers land on one row.

    venue ids are likewise ONE id space -- 27 of 31 sampled NFL venues were already in
    dim_venue under the same name, because a bowl game at AT&T Stadium is that building.

    team ids and conference ids are NOT. NFL team ids run 1-34 and collide with college ids
    outright (2 is Auburn and the Falcons' id is 1); conference 8 is the SEC and also the
    AFC. Those three dimensions are keyed (league, id) for that reason.
"""
import os

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CFB = "cfb"
NFL = "nfl"


def _cfb_weeks(season):
    return [(2, w) for w in range(1, 18)] + [(3, w) for w in range(1, 6)]


def _nfl_weeks(season):
    """17 regular-season weeks through 2020, 18 from 2021; postseason is 1-5 throughout.

    Postseason week 4 is the Pro Bowl in most seasons and week 5 the Super Bowl, except
    where the Pro Bowl was cancelled or unranked -- 2014 has no week 4 at all. Sweeping the
    full 1-5 range and keeping whatever comes back completed is correct for every season;
    the Pro Bowl itself is dropped by name at build time, the way college drops all-star
    bowls.
    """
    last = 17 if season <= 2020 else 18
    return [(2, w) for w in range(1, last + 1)] + [(3, w) for w in range(1, 6)]


SPEC = {
    CFB: {
        "slug": "college-football",
        "dir": os.path.join(HOME, "data", "espn"),
        "seasons": list(range(2014, 2027)),
        "weeks": _cfb_weeks,
        "groups": "&groups=80",
        "divisions": {"80": "FBS", "81": "FCS"},
        "exclude_teams": set(),
        "label": "D-I FBS college football",
    },
    NFL: {
        "slug": "nfl",
        "dir": os.path.join(HOME, "data", "espn_nfl"),
        "seasons": list(range(2014, 2027)),
        "weeks": _nfl_weeks,
        "groups": "",
        # The NFL's two conferences, walked directly rather than through a division group.
        # Measured 2026-09-11: AFC=8, NFC=7, stable across 2014-2025.
        "divisions": None,
        # The Pro Bowl. ESPN files it as postseason week 4 and gives the two all-star squads
        # their own team ids, 31 (AFC) and 32 (NFC), which appear in 8 games each and nowhere
        # else -- 34 distinct team ids across the corpus against 32 franchises. It is an
        # exhibition between rosters that do not exist, so it is dropped at fetch time and
        # never reaches a fact table. Excluding by TEAM ID rather than by "postseason week 4"
        # keeps the rule true if ESPN ever renumbers the week.
        "exclude_teams": {"31", "32"},
        "label": "NFL",
    },
}

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/{slug}"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/{slug}"


def spec(league):
    if league not in SPEC:
        raise SystemExit(f"unknown league {league!r}; expected one of {sorted(SPEC)}")
    return SPEC[league]


def site_base(league):
    return SITE.format(slug=spec(league)["slug"])


def core_base(league):
    return CORE.format(slug=spec(league)["slug"])


def data_dir(league):
    return spec(league)["dir"]


def seasons(league):
    return list(spec(league)["seasons"])


def weeks(league, season):
    return spec(league)["weeks"](season)


def from_argv(argv, default=CFB):
    """`--league nfl`, or the LEAGUE environment variable, or college.

    Defaulting to college is deliberate: every existing invocation of these scripts, every
    cron line and every line in the README predates the flag and must keep meaning what it
    meant.
    """
    if "--league" in argv:
        return argv[argv.index("--league") + 1]
    return os.environ.get("LEAGUE", default)
