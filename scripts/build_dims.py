"""Build the dimension tables: team-season conference, team, venue, athlete.

The conference dimension is a SLOWLY CHANGING one on purpose. Between 2014 and 2026 the
Pac-12 went 12 teams -> 2 -> 8, the Big Ten 14 -> 18, the Big 12 10 -> 16; 83 of 275 teams
changed conference at least once. Storing conference as a team attribute rather than a
team-SEASON attribute is the single most likely way this project quietly produces wrong
grouped answers.

Venue and team come free from data already on disk. Conference needs ~250 API calls.
Athlete is derived from fetched participants plus the names the parser already extracted.

    python build_dims.py conf     -> data/out/dim_team_season.csv, dim_conference.csv
    python build_dims.py venue    -> data/out/dim_venue.csv, fact_game.csv
    python build_dims.py athlete  -> data/out/dim_athlete.csv, fact_play_athlete.csv

`conf` and `venue` always write the whole window; the tables are small (3.4k team-seasons,
201 venues) and sql/load_dims.sql replaces them wholesale, so there is nothing to scope.

`athlete` builds ONE dimension over BOTH fact tables (PLAN.md §10d). The union is 62,879
athletes: 33,891 from special teams, 58,800 from scrimmage, 29,812 in both. A receiver who
also returns kicks has to be a single row or every cross-phase question double-counts him.

Names come from data/espn/athletes.json.gz, fetched by scripts/fetch_athletes.py, not from
play text. Voting names out of the text only ever named 25.9% of the special-teams athletes
and would have done worse on scrimmage. The voted name survives in `text_name` beside its
confidence, because it is derived from a different source than the fetched one and a
disagreement between them points at a bad athlete-to-play link.

The scrimmage side is rolled up in DuckDB rather than Python dicts -- it joins 3.1M bridge
rows to a 382 MB fact, and the ST path walks the participants files directly only because it
also needs the parsed names off each play.

`athlete` is different from `conf` and `venue`, and the difference matters for the in-season
update. dim_athlete is a CAREER aggregate -- first_season, last_season, the play counts,
primary_role and primary_team_id are all taken across every season at once -- so it must be
derived from the whole corpus even when only one season is being loaded:

    python build_dims.py athlete --plays data/out/st_plays.csv data/out/st_plays_2026.csv \
                                 --only-season 2026

`--plays` accepts several extracts and de-duplicates on play_uid, so the frozen full CSV and
a single-season one can be read together without re-running build_table over twelve seasons.
Those paths belong to `--league` alone. On a `--leagues cfb nfl` rebuild, give each corpus
its own with the repeatable per-league form, or the one not named by `--league` falls back
to its frozen extract and contributes play ids ESPN has since renumbered away:

    python build_dims.py athlete --league nfl --leagues cfb nfl \
        --plays-for nfl data/out/st_plays_nfl.csv data/out/st_plays_nfl_2026.csv \
        --plays-for cfb data/out/st_plays.csv     data/out/st_plays_2026.csv

`--scrim-fact` and `--scrim-bridge` follow the same rule, with `--scrim-fact-for` and
`--scrim-bridge-for` as their per-league forms.
`--only-season` narrows the BRIDGE and WIDE outputs to that season while the dimension stays
global. Scoping the dimension instead would give every returning kicker first_season=2026.

A scoped run writes play_athlete_<season>.csv and play_athlete_wide_<season>.csv rather than
overwriting the global pair. sql/load_athletes_2_apply.sql TRUNCATEs pbp.play_athlete and
reloads it from whatever play_athlete.csv holds, so leaving one season's rows under the
global name would arm that script to destroy the other twelve.
"""
import sys, os, json, gzip, csv, re, time, glob, collections, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import league as lg

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAGUE = lg.CFB
CORE = lg.core_base(LEAGUE)
ESPN, OUT = lg.data_dir(LEAGUE), f"{HOME}/data/out"
SEASONS = list(range(2014, 2027))
DIVISIONS = {"80": "FBS", "81": "FCS"}
SFX = ""            # '' for college, '_nfl' for the NFL; keeps the two extracts apart


TEAM_ID = re.compile(r"/teams/(\d+)")
NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def use(name):
    """Point the module at one league: its API, its raw directory, its output suffix."""
    global LEAGUE, CORE, ESPN, SFX
    LEAGUE, CORE, ESPN = name, lg.core_base(name), lg.data_dir(name)
    SFX = "" if name == lg.CFB else f"_{name}"


def name_key(n):
    """(first initial, surname) -- the identity a name variant votes for.

    ESPN writes the same player two ways and changed which it prefers: 2025 play text is
    40% "T.Ahmetbasic" and 60% "Tarik Ahmetbasic", 2026 is 98% the abbreviated form. Voting
    on the raw string makes a returning player split his own vote between two spellings of
    his own name, drop under the confidence floor, and end up with no known_name at all --
    1,281 athletes active into 2025/26 were holding a full-form name for the 2026 text to
    dilute. Collapsing to initial + surname first makes the two forms one candidate.

    Keyed per athlete_id, so two different players sharing an initial and a surname never
    collide -- their names are counted in separate Counters to begin with.
    """
    toks = [t for t in n.replace(".", ". ").split() if t]
    if not toks:
        return ()
    tail = [t for t in toks if t.strip(".").lower() not in NAME_SUFFIX] or toks
    return (tail[0].strip(".")[:1].lower(), tail[-1].strip(".").lower())


def fullness(n):
    """Rank name variants by how much they tell you, to pick one to display per key."""
    toks = [t for t in n.replace(".", ". ").split() if t]
    spelled_out = bool(toks) and len(toks[0].strip(".")) > 1
    return (spelled_out, len(n))


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=45) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def stage_conf_nfl():
    """AFC/NFC and their eight divisions, per season.

    A different SHAPE of walk from the college one, not just different ids. College asks
    for the children of a division group (FBS=80, FCS=81) and gets conferences; the NFL has
    no division group at all -- the two conferences ARE the top-level groups, and their
    children are the eight divisions. Measured 2026-09-11 and stable across 2014-2025:
    AFC=8, NFC=7, four divisions of four teams each.

    Realignment risk here is close to nil -- only the three relocations (Rams 2016,
    Chargers 2017, Raiders 2020), none of which changed a division -- but the table stays
    team-SEASON grained anyway, because that is the grain the college side needs and one
    table with two grains would be worse than one redundant key.
    """
    rows, confs = [], {}

    def per_season(season):
        out = []
        try:
            top = get(f"{CORE}/seasons/{season}/types/2/groups?limit=60")
        except Exception as e:
            print(f"  {season}: ERR {e}", flush=True)
            return out
        for it in top.get("items") or []:
            try:
                conf = get(it["$ref"])
            except Exception:
                continue
            cid, cname = str(conf.get("id")), conf.get("name")
            confs[cid] = (cname, conf.get("shortName") or conf.get("abbreviation")
                          or ("AFC" if cname and cname.startswith("American") else
                              "NFC" if cname and cname.startswith("National") else None))
            kids = (conf.get("children") or {}).get("$ref")
            if not kids:
                continue
            try:
                kd = get(kids)
            except Exception:
                continue
            for c in kd.get("items") or []:
                try:
                    div = get(c["$ref"])
                except Exception:
                    continue
                ref = (div.get("teams") or {}).get("$ref")
                if not ref:
                    continue
                try:
                    tl = get(ref + "&limit=100")
                except Exception:
                    continue
                for t in tl.get("items") or []:
                    m = TEAM_ID.search(t.get("$ref", ""))
                    if m:
                        # `division` stays NULL: it means FBS | FCS and has no NFL meaning.
                        out.append((m.group(1), season, cid, cname, None, div.get("name")))
        return out

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(per_season, SEASONS):
            rows.extend(res)
    best = {}
    for tid, season, cid, cname, dv, dname in rows:
        best[(tid, season)] = (cid, cname, dv, dname)
    _write_conf(best, confs)


def _write_conf(best, confs):
    with open(f"{OUT}/dim_team_season{SFX}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["league", "team_id", "season", "conference_id", "conference_name",
                    "ncaa_division", "nfl_division"])
        for (tid, season), (cid, cname, dv, dname) in sorted(best.items(),
                                                             key=lambda x: (x[0][1], int(x[0][0]))):
            w.writerow([LEAGUE, tid, season, cid, cname, dv, dname])
    with open(f"{OUT}/dim_conference{SFX}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["league", "conference_id", "conference_name", "short_name"])
        for cid, (n, sn) in sorted(confs.items(), key=lambda x: int(x[0])):
            w.writerow([LEAGUE, cid, n, sn])
    print(f"dim_team_season: {len(best):,} team-seasons; dim_conference: {len(confs)} conferences")
    per = collections.Counter(s for (t, s) in best)
    print("  teams per season:", {s: per[s] for s in SEASONS if per[s]})


def stage_conf():
    os.makedirs(OUT, exist_ok=True)
    if LEAGUE == lg.NFL:
        return stage_conf_nfl()
    rows, confs = [], {}

    def per_season(args):
        season, gid, div = args
        out = []
        try:
            kids = get(f"{CORE}/seasons/{season}/types/2/groups/{gid}/children?limit=60")
        except Exception as e:
            print(f"  {season} {div}: ERR {e}", flush=True)
            return out
        for it in kids.get("items") or []:
            try:
                g = get(it["$ref"])
            except Exception:
                continue
            cid, cname = str(g.get("id")), g.get("name")
            confs[cid] = (cname, g.get("shortName") or g.get("abbreviation"))
            ref = (g.get("teams") or {}).get("$ref")
            if not ref:
                continue
            try:
                tl = get(ref + "&limit=100")
            except Exception:
                continue
            for t in tl.get("items") or []:
                m = TEAM_ID.search(t.get("$ref", ""))
                if m:
                    out.append((m.group(1), season, cid, cname, div))
        return out

    jobs = [(s, g, d) for s in SEASONS for g, d in DIVISIONS.items()]
    with ThreadPoolExecutor(max_workers=5) as ex:
        for res in ex.map(per_season, jobs):
            rows.extend(res)

    # a team can only be in one conference per season; FBS wins if both divisions claim it
    best = {}
    for tid, season, cid, cname, div in rows:
        k = (tid, season)
        if k not in best or (div == "FBS" and best[k][2] != "FBS"):
            # nfl_division is NULL for college: this project has never tracked the
            # intra-conference divisions (SEC East and the like), and inventing them here
            # would put two different things in one column across the two leagues.
            best[k] = (cid, cname, div, None)
    _write_conf(best, confs)
    fbs = collections.Counter(s for (t, s), v in best.items() if v[2] == "FBS")
    print("  FBS teams per season:", {s: fbs[s] for s in SEASONS})


def stage_venue():
    venues, games = {}, []
    # Roof comes from a different payload than the rest of the venue. `summary.gameInfo.venue`
    # has `grass` but no `indoor`; only the scoreboard states it, and fetch_espn.stage_games
    # keeps it as `venue_indoor` per game. So it is accumulated per venue here and joined on
    # venue_id below, rather than read off the summary with everything else.
    #
    # Venue-grain is safe but not free: measured over all 10,470 games, 201 venues each report
    # a single consistent value and NOT ONE reports both -- so there is no per-game roof state
    # to lose. The guard below stays anyway, because if ESPN ever does disagree with itself the
    # right outcome is a visible complaint, not a silent first-wins.
    indoor_by_vid, indoor_conflict = {}, {}
    for season in SEASONS:
        gp = f"{ESPN}/games_{season}.json"
        if not os.path.exists(gp):
            continue
        for g in json.load(open(gp)).values():
            vid, vi = g.get("venue_id"), g.get("venue_indoor")
            if not vid or vi is None:
                continue
            vi = bool(vi)
            if vid in indoor_by_vid and indoor_by_vid[vid] != vi:
                indoor_conflict.setdefault(vid, set()).update({indoor_by_vid[vid], vi})
            indoor_by_vid[vid] = vi
    for season in SEASONS:
        gp = f"{ESPN}/games_{season}.json"
        if not os.path.exists(gp):
            continue
        gl = json.load(open(gp))
        for gid, g in gl.items():
            sp = f"{ESPN}/summaries/{gid}.json.gz"
            v = {}
            if os.path.exists(sp):
                try:
                    with gzip.open(sp, "rt") as f:
                        gi = json.load(f).get("gameInfo") or {}
                    v = gi.get("venue") or {}
                    att = gi.get("attendance")
                except Exception:
                    att = None
            else:
                att = None
            vid = v.get("id")
            if vid and vid not in venues:
                a = v.get("address") or {}
                venues[vid] = [vid, v.get("fullName"), a.get("city"), a.get("state"),
                               a.get("zipCode"), a.get("country") or "USA",
                               "grass" if v.get("grass") else "turf",
                               indoor_by_vid.get(vid)]
            home = next((t["id"] for t in g["teams"] if t["home_away"] == "home"), None)
            away = next((t["id"] for t in g["teams"] if t["home_away"] == "away"), None)
            games.append([gid, season, g["week"],
                          "postseason" if g["season_type"] == 3 else "regular",
                          g.get("date"), home, away, vid, att,
                          g.get("neutral_site"), g.get("conference_competition")])
    teams = {}
    for season in SEASONS:
        gp = f"{ESPN}/games_{season}.json"
        if not os.path.exists(gp):
            continue
        for g in json.load(open(gp)).values():
            for t in g["teams"]:
                if t.get("id"):
                    teams[str(t["id"])] = t.get("name")   # later seasons overwrite: current name
    with open(f"{OUT}/dim_team{SFX}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["league", "team_id", "display_name"])
        for tid, nm in sorted(teams.items(), key=lambda x: int(x[0])):
            w.writerow([LEAGUE, tid, nm])
    print(f"dim_team: {len(teams):,} teams")

    # dim_venue carries NO league column. ESPN venue ids are one id space across both
    # feeds -- 27 of 31 sampled NFL venues were already in the college table under the same
    # name -- so the two leagues MERGE into one venue dimension rather than each owning a
    # copy. sql/load_dims.sql upserts this file instead of replacing the table.
    with open(f"{OUT}/dim_venue{SFX}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["venue_id", "venue_name", "city", "state", "zip", "country", "surface",
                    "indoor"])
        for v in sorted(venues.values(), key=lambda r: int(r[0])):
            w.writerow(v)
    with open(f"{OUT}/fact_game{SFX}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["league", "game_id", "season", "week", "season_type", "kickoff_utc",
                    "home_team_id", "away_team_id", "venue_id", "attendance", "neutral_site",
                    "conference_game"])
        w.writerows([LEAGUE] + g for g in games)
    print(f"dim_venue: {len(venues):,} venues; fact_game: {len(games):,} games")
    surf = collections.Counter(v[6] for v in venues.values())
    intl = sum(1 for v in venues.values() if v[5] and v[5] != "USA")
    print(f"  surfaces: {dict(surf)}; non-US venues: {intl}")
    roof = collections.Counter(v[7] for v in venues.values())
    print(f"  roof: indoor={roof[True]}, outdoor={roof[False]}, unknown={roof[None]}")
    if indoor_conflict:
        print(f"  !! {len(indoor_conflict)} venues report BOTH indoor and outdoor across "
              f"games; last value wins and the roof is not trustworthy for them: "
              f"{sorted(indoor_conflict)}")


def load_identity(leagues=None):
    """Authoritative names, positions and jerseys from scripts/fetch_athletes.py.

    Reads EVERY league's store, because the dimension spans them. The two stores are keyed
    by the same athlete id space, so a player who appears in both is one entry; where both
    name him, the later league in `leagues` wins. That ordering matters only for the fields
    that legitimately change between college and the pros -- jersey, team, listed weight --
    and the pro record is the more current of the two.

    Absent is not fatal -- the dimension falls back to the voted text name, which is what it
    used before this existed. It just names 25.9% of athletes instead of ~100%.
    """
    out = {}
    for lname in (leagues or [LEAGUE]):
        path = f"{lg.data_dir(lname)}/athletes.json.gz"
        if not os.path.exists(path):
            print(f"  WARNING: no {os.path.relpath(path, HOME)} -- run "
                  f"scripts/fetch_athletes.py --league {lname}.", flush=True)
            continue
        with gzip.open(path, "rt") as f:
            d = json.load(f)
        print(f"  identity store [{lname}]: {len(d):,} athletes", flush=True)
        out.update(d)
    if not out:
        print("  Falling back to text-voted names only.", flush=True)
    return out


def _dedup_view(con, name, paths):
    """One view over several extracts, later files winning on play_uid.

    Same rule as --plays above: the in-season run has a frozen global CSV plus a
    single-season one, and the season file is the fresher truth for the rows it holds.
    """
    parts = " UNION ALL BY NAME ".join(
        f"SELECT *, {i} AS _ord FROM read_csv_auto('{p}', sample_size=-1)"
        for i, p in enumerate(paths))
    # Superseding is per SEASON, not per play_uid, for the reason spelled out in
    # stage_athlete: ESPN renumbers a corrected game's plays, so the frozen extract's copy
    # of the live season carries uids the fresh one does not, and winning-on-uid leaves
    # those behind. Whichever file is latest for a season owns that season outright.
    con.execute(f"""CREATE VIEW {name} AS
                    WITH u AS ({parts}),
                         owner AS (SELECT season, max(_ord) AS top FROM u GROUP BY season)
                    SELECT u.* EXCLUDE (_ord) FROM u JOIN owner o USING (season)
                    WHERE u._ord = o.top
                    QUALIFY row_number() OVER (PARTITION BY play_uid ORDER BY _ord DESC) = 1""")


def scrimmage_rollup(fact_csvs, bridge_csvs):
    """Per-athlete role, team, season and play counts over the scrimmage fact.

    DuckDB rather than Python dicts: this joins 3.1M bridge rows to a 1.5M-row, 382 MB fact,
    and holding a play -> (season, team) map for all of it in a dict costs more memory than
    the rest of this script put together. The ST path above stays as it was -- it walks the
    participants files directly because it also needs the parsed names off each play, which
    have no equivalent here.

    Team attribution follows the role. A tackler belongs to the DEFENCE; counting him for the
    offense -- which is what a single `offense_team_id` join would do -- would put every
    defender on the wrong roster, and primary_team_id is derived from exactly this count.
    """
    import duckdb
    con = duckdb.connect()
    _dedup_view(con, "f", fact_csvs)
    con.execute("CREATE VIEW b_all AS " + " UNION ALL BY NAME ".join(
        f"SELECT play_uid, role, athlete_id, ordinal, {i} AS _ord "
        f"FROM read_csv_auto('{p}', sample_size=-1)" for i, p in enumerate(bridge_csvs)))
    # The bridge has no play_uid uniqueness -- many rows per play -- so dedupe it on the
    # full key instead of letting _dedup_view collapse it to one row per play.
    #
    # It also carries no season, so it cannot supersede by season on its own; it takes the
    # season off the fact. Without that, a game ESPN corrected inside the refresh window
    # contributes BOTH participant sets -- the frozen extract's and the fresh one's -- and
    # the union credits the play twice over. Joining f also drops rows for plays the fresh
    # fact no longer has, which is what makes the count below right.
    con.execute("""CREATE VIEW b AS
                   WITH j AS (SELECT b_all.*, f.season FROM b_all
                              JOIN f ON f.play_uid = b_all.play_uid),
                        owner AS (SELECT season, max(_ord) AS top FROM j GROUP BY season)
                   SELECT DISTINCT j.play_uid, j.role, j.athlete_id, j.ordinal
                   FROM j JOIN owner o USING (season) WHERE j._ord = o.top""")
    rows = con.execute("""
        SELECT b.athlete_id, b.role, f.season,
               CASE WHEN b.role IN ('tackler','assistedBy','sackedBy','passDefender',
                                    'forcedBy','recoverer')
                    THEN f.defense_team_id ELSE f.offense_team_id END AS team_id,
               count(*) AS n
        FROM b JOIN f ON f.play_uid = b.play_uid
        GROUP BY 1,2,3,4
    """).fetchall()
    # Separately, because an athlete can hold two roles on one play (rusher and scorer) and
    # summing a per-group DISTINCT would count that play twice.
    playcount = con.execute("""
        SELECT athlete_id, count(DISTINCT play_uid) FROM b GROUP BY 1
    """).fetchall()
    con.close()

    roles = collections.defaultdict(collections.Counter)
    teams = collections.defaultdict(collections.Counter)
    seasons = collections.defaultdict(set)
    plays = collections.Counter()
    for aid, role, season, team_id, n in rows:
        a = str(aid)
        roles[a][role] += n
        if team_id is not None:
            teams[a][str(int(team_id))] += n
        if season is not None:
            seasons[a].add(int(season))
    for aid, npl in playcount:
        plays[str(aid)] = npl
    print(f"  scrimmage rollup: {len(roles):,} athletes over {sum(plays.values()):,} "
          f"athlete-plays", flush=True)
    return roles, teams, seasons, plays


def stage_athlete(plays_csvs=None, only_season=None, scrim_fact=None, scrim_bridge=None,
                  leagues=None):
    """Link plays to ESPN athlete ids, and derive an athlete dimension from the data itself.

    ESPN tags the kick as role 'kicker' (kickoffs, field goals) or 'punter' (punts), plus
    'returner' and 'tackler'. There is NO blocker role, so blocker identity stays a parsed
    name string only. Names are not in the participants payload -- they are taken from the
    text the parser already extracted, keyed on the authoritative athlete id.
    """
    # ESPN names the kick 'kicker' (kickoffs, FGs) or 'punter' (punts). A conversion is
    # NOT either of those -- it is 'patScorer' for a kick and 'patPasser' for a two-point
    # pass. Linking conversions on KICK_ROLES alone found nothing, which is half of why
    # all 58,535 PAT and two-point rows had a NULL kicker_athlete_id.
    KICK_ROLES = {"kicker", "punter"}
    CONV_ROLES = {"patScorer", "patPasser"}
    # primary_role should describe the player, not the row that happened to link him: a
    # placekicker takes far more PATs than field goals, so counting patScorer literally
    # would relabel most kickers 'patScorer'. The bridge keeps the raw role.
    ROLE_CANON = {"patScorer": "kicker", "patPasser": "passer"}

    # ONE dimension over BOTH leagues, which is the same principle that already makes it one
    # dimension over both FACTS: ESPN athlete ids are a single id space across the college
    # and NFL feeds -- 720 of 720 sampled overlapping ids returned the identical name from
    # the NFL endpoint -- so Patrick Mahomes is one row whose college and pro plays both
    # count. Keying by league would split every drafted player in the window into two people
    # and make the most interesting cross-corpus question unaskable.
    leagues = leagues or [LEAGUE]
    plays = {}
    play_league = {}
    for lname in leagues:
        # Explicit --plays paths belong to the league this run was POINTED AT, not to every
        # league in --leagues. The in-season caller passes the NFL's full extract plus its
        # current season and also asks for a both-league dimension; applying those NFL paths
        # to the college side too read the NFL corpus twice and dropped all 60,100
        # college-only athletes out of the dimension.
        paths = (plays_csvs.get(lname) if isinstance(plays_csvs, dict)
                 else (plays_csvs if lname == LEAGUE else None))
        sfx = "" if lname == lg.CFB else f"_{lname}"
        for path in paths or [f"{OUT}/st_plays{sfx}.csv"]:
            if not os.path.exists(path):
                print(f"  {os.path.basename(path)}: absent, skipped", flush=True)
                continue
            n0 = len(plays)
            fresh = {}
            for r in csv.DictReader(open(path)):
                # 'defensive_conversion' is in this list because those plays have
                # participants like any other -- somebody returned the blocked kick.
                # Omitting it silently left 75 plays with no athlete link at all.
                if r["play_kind"] in ("punt", "kickoff", "field_goal", "pat", "two_point",
                                      "defensive_conversion"):
                    fresh[r["play_uid"]] = (r["season"], r["kicking_team_id"],
                                            r["receiving_team_id"], r["kicker_name"],
                                            r["returner_name"])
            # A later extract SUPERSEDES an earlier one for every season it covers, rather
            # than merely overwriting the uids the two share. ESPN renumbers the plays in a
            # game it corrects, so the frozen full CSV's copy of the live season holds ids
            # that no longer exist; a plain union keeps them, and the bridge then names
            # plays the fact table does not have. load_athletes_league.sql refuses that load
            # -- correctly -- which is how this was found, on 2026-09-22, with 9 orphans
            # across two week-1 games.
            drop = [u for u, v in plays.items()
                    if play_league[u] == lname and v[0] in {f[0] for f in fresh.values()}]
            for u in drop:
                del plays[u], play_league[u]
            plays.update(fresh)
            for u in fresh:
                play_league[u] = lname
            print(f"  {os.path.basename(path)}: {len(plays) - n0:,} new play_uids"
                  + (f", {len(drop):,} superseded" if drop else ""), flush=True)

    # build_table emits a conversion as a second row off the scoring play, with ':pat'
    # appended to the play_uid. The participants feed knows only the ESPN play, so one
    # participants entry can feed two fact rows -- a kickoff-return touchdown produces
    # both a 'kickoff' row and a 'pat' row from the same play.
    by_base = collections.defaultdict(list)
    for uid in plays:
        by_base[uid.removesuffix(":pat")].append(uid)
    kickable = sum(1 for u in plays if not u.endswith(":pat"))
    print(f"  plays to link: {len(plays):,} ({kickable:,} kicks, "
          f"{len(plays) - kickable:,} conversions)", flush=True)

    bridge = []
    wide = {}
    names = collections.defaultdict(collections.Counter)
    teams = collections.defaultdict(collections.Counter)
    roles = collections.defaultdict(collections.Counter)
    seasons = collections.defaultdict(set)
    st_plays = collections.defaultdict(set)     # distinct ST play_uids per athlete
    st_by_league = collections.defaultdict(lambda: collections.defaultdict(set))
    part_files = [(lname, p) for lname in leagues
                  for p in glob.glob(f"{lg.data_dir(lname)}/participants/*.json.gz")]
    print(f"  participants files: {len(part_files):,} across {len(leagues)} league(s)",
          flush=True)
    for lname, path in part_files:
        gid = os.path.basename(path).split(".")[0]
        try:
            d = json.load(gzip.open(path, "rt"))
        except Exception:
            continue
        for pid, parts in d.items():
            # Two stored shapes. Files written before 2026-08-31 are keyed by the core-API
            # play `id`, which is the game id followed by a sequence number; files written
            # since are keyed by the bare sequenceNumber, because the two endpoints stopped
            # agreeing on `id` from week 9 of 2025. A key beginning with the game id is the
            # old form; anything else is already the sequence number.
            seq = pid[len(gid):] if pid.startswith(gid) else pid
            base = f"espn:{gid}:{seq}"
            targets = by_base.get(base)
            if not targets:
                continue
            # Roles and seasons describe the athlete, so count them once per ESPN play even
            # when it feeds both a kick row and a conversion row.
            for role, aid in parts:
                roles[aid][ROLE_CANON.get(role, role)] += 1
            for role, aid in parts:
                st_plays[aid].add(base)
                st_by_league[lname][aid].add(base)
            for uid in targets:
                season, kteam, rteam, kname, rname = plays[uid]
                is_conv = uid.endswith(":pat")
                want = CONV_ROLES if is_conv else KICK_ROLES
                w = wide.setdefault(uid, {})
                for i, (role, aid) in enumerate(parts):
                    bridge.append([uid, role, aid, i])
                    seasons[aid].add(int(season))
                    if role in want:
                        w.setdefault("kicker_athlete_id", aid)
                        if kname:
                            names[aid][kname] += 1
                        if kteam:
                            teams[aid][kteam] += 1
                    elif role == "returner" and not is_conv:
                        w.setdefault("returner_athlete_id", aid)
                        if rname:
                            names[aid][rname] += 1
                        if rteam:
                            teams[aid][rteam] += 1
                    elif role == "tackler" and not is_conv:
                        w.setdefault("tackler_athlete_id", aid)
                        if kteam:
                            teams[aid][kteam] += 1

    # The dimension below is built from every counter above, i.e. from the whole corpus.
    # Only the bridge and the wide row-per-play file get narrowed, because those are what
    # sql/load_athletes_3_season.sql deletes and re-inserts for one season.
    keep = (lambda uid: True) if only_season is None else (
        lambda uid: int(plays[uid][0]) == only_season)
    bridge_out = [r for r in bridge if keep(r[0])]
    wide_out = {u: d for u, d in wide.items() if keep(u)}
    if only_season is not None:
        print(f"  scoped to {only_season}: {len(bridge_out):,} of {len(bridge):,} bridge rows, "
              f"{len(wide_out):,} of {len(wide):,} plays", flush=True)
    # The DIMENSION spans every league; the BRIDGE does not. Each league's bridge is loaded
    # and deleted independently, so a file holding both leagues' rows under one league's
    # name would arm the loader to delete the other league's athletes. Split on the play's
    # own league rather than on which league this run was pointed at.
    season_sfx = "" if only_season is None else f"_{only_season}"
    for lname in leagues:
        lsfx = ("" if lname == lg.CFB else f"_{lname}") + season_sfx
        lb = [r for r in bridge_out if play_league.get(r[0].removesuffix(":pat")) == lname
              or play_league.get(r[0]) == lname]
        lw = {u: d for u, d in wide_out.items()
              if play_league.get(u.removesuffix(":pat")) == lname
              or play_league.get(u) == lname}
        with open(f"{OUT}/play_athlete{lsfx}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["play_uid", "role", "athlete_id", "ordinal"])
            w.writerows(lb)
        with open(f"{OUT}/play_athlete_wide{lsfx}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["play_uid", "kicker_athlete_id", "returner_athlete_id",
                        "tackler_athlete_id"])
            for uid, d in lw.items():
                w.writerow([uid, d.get("kicker_athlete_id"), d.get("returner_athlete_id"),
                            d.get("tackler_athlete_id")])
        print(f"  play_athlete{lsfx}.csv: {len(lb):,} bridge rows over {len(lw):,} plays",
              flush=True)
    # ---- merge the scrimmage side in -------------------------------------------------
    # Both facts feed ONE dimension. A receiver who also returns kicks has to be one row or
    # every cross-phase question double-counts him -- that is the whole reason this project
    # keys on ESPN athlete ids instead of name strings (PLAN.md §10d).
    ident = load_identity(leagues)
    s_roles, s_teams, s_seasons, s_plays = ({}, {}, {}, collections.Counter())
    scrim_by_league = {}
    for lname in leagues:
        lsfx = "" if lname == lg.CFB else f"_{lname}"
        # Same rule as --plays above: explicit paths are this run's league only.
        lf = (scrim_fact.get(lname) if isinstance(scrim_fact, dict)
              else (scrim_fact if lname == LEAGUE else None))
        lb = (scrim_bridge.get(lname) if isinstance(scrim_bridge, dict)
              else (scrim_bridge if lname == LEAGUE else None))
        facts = [p for p in (lf or [f"{OUT}/scrimmage_plays{lsfx}.csv"]) if os.path.exists(p)]
        bridges = [p for p in (lb or [f"{OUT}/scrimmage_athlete{lsfx}.csv"]) if os.path.exists(p)]
        if not (facts and bridges):
            continue
        r_, t_, ss_, p_ = scrimmage_rollup(facts, bridges)
        scrim_by_league[lname] = p_
        for aid, c in r_.items():
            s_roles.setdefault(aid, collections.Counter()).update(c)
        for aid, c in t_.items():
            s_teams.setdefault(aid, collections.Counter()).update(c)
        for aid, ss in ss_.items():
            s_seasons.setdefault(aid, set()).update(ss)
        s_plays.update(p_)
    if s_roles:
        for aid, c in s_roles.items():
            # Same canonicalisation the ST side applies. A placekicker is tagged patScorer on
            # every touchdown his team scores, and those rows live on SCRIMMAGE plays, so
            # merging the raw role would hand most placekickers primary_role='patScorer' --
            # precisely the mislabelling ROLE_CANON exists to prevent.
            for role, n in c.items():
                roles[aid][ROLE_CANON.get(role, role)] += n
        for aid, c in s_teams.items():
            teams[aid].update(c)
        for aid, ss in s_seasons.items():
            seasons[aid] |= ss

    everyone = names.keys() | roles.keys() | ident.keys() | s_plays.keys()

    with open(f"{OUT}/dim_athlete.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["athlete_id", "known_name", "full_name", "position", "jersey",
                    "text_name", "text_name_confidence", "primary_role",
                    "primary_team_id", "first_season", "last_season",
                    "st_plays", "scrimmage_plays", "leagues", "nfl_plays",
                    "date_of_birth", "debut_year", "height_in", "weight_lb"])
        weak = 0
        for aid in sorted(everyone, key=int):
            # Names come from play text, and a player tagged in a kick role on a play whose
            # text names someone else inherits the wrong name. That is harmless for a kicker
            # with hundreds of plays and dominant for one with two, so require the modal name
            # to be seen twice AND hold a clear majority; otherwise leave it NULL.
            #
            # The vote is over name_key(), not over the raw string, so "T.Ahmetbasic" and
            # "Tarik Ahmetbasic" back the same candidate instead of splitting it; the winning
            # key is then displayed in its most informative observed spelling. Confidence is
            # therefore the share of plays that agree on the PERSON, which is the thing the
            # guard was always trying to measure.
            nm, conf = None, None
            if names[aid]:
                keyed = collections.Counter()
                for variant, c in names[aid].items():
                    keyed[name_key(variant)] += c
                key, n = keyed.most_common(1)[0]
                tot = sum(names[aid].values())
                conf = round(n / tot, 3)
                if n >= 2 and conf >= 0.6:
                    nm = max((v for v in names[aid] if name_key(v) == key),
                             key=lambda v: (fullness(v), names[aid][v], v))
                else:
                    weak += 1
            # most_common breaks a tie on INSERTION order, and these two counters are fed
            # by the DuckDB rollup, whose row order is not stable across runs. That made the
            # dimension irreproducible: 939 athletes -- an offensive tackle with one
            # 'penalized' and one 'tackler', say -- flipped primary_role or primary_team_id
            # between two builds off identical input. Count first, then the key itself, so a
            # tie resolves the same way every time.
            pick = lambda c: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] if c else None
            tm = pick(teams[aid])
            pr = pick(roles[aid])
            ss = sorted(seasons[aid]) or [None]
            # known_name is ESPN's, with the voted text name as fallback where the fetch has
            # no record. The voted name and its confidence are kept in their own columns
            # rather than discarded: they are the only INDEPENDENT signal that an athlete id
            # is attached to the right play, and a fetched name would paper straight over a
            # bad link.
            idn = ident.get(str(aid)) or {}
            # Which corpora this person appears in. A drafted player reads 'cfb+nfl', and
            # that single row spanning both is the whole reason the dimension is not keyed
            # by league.
            seen_in = [ln for ln in leagues
                       if len(st_by_league[ln].get(aid, ()))
                       or scrim_by_league.get(ln, {}).get(str(aid), 0)]
            nfl_n = (len(st_by_league[lg.NFL].get(aid, ()))
                     + scrim_by_league.get(lg.NFL, {}).get(str(aid), 0))
            w.writerow([aid, idn.get("displayName") or nm, idn.get("fullName"),
                        idn.get("position"), idn.get("jersey"), nm, conf, pr, tm,
                        ss[0], ss[-1], len(st_plays[aid]), s_plays.get(str(aid), 0),
                        "+".join(seen_in) or None, nfl_n,
                        idn.get("dateOfBirth"), idn.get("debutYear"),
                        idn.get("height"), idn.get("weight")])
    linked = sum(1 for d in wide.values() if d.get("kicker_athlete_id"))
    kick_linked = sum(1 for u, d in wide.items()
                      if d.get("kicker_athlete_id") and not u.endswith(":pat"))
    conv_linked = linked - kick_linked
    print(f"play_athlete: {len(bridge):,} rows over {len(wide):,} plays "
          f"({100*len(wide)/max(len(plays),1):.1f}% of plays linked)")
    print(f"  with a kicker id: {linked:,} ({100*linked/max(len(plays),1):.1f}%)"
          f"  -- {kick_linked:,} kicks, {conv_linked:,} conversions")
    named = sum(1 for a in everyone if (ident.get(str(a)) or {}).get("displayName"))
    print(f"dim_athlete: {len(everyone):,} athletes -- {named:,} named from the identity "
          f"store ({100*named/max(len(everyone),1):.1f}%), "
          f"{weak:,} text-name candidates rejected as low-confidence")
    both = sum(1 for a in everyone if len(st_plays[a]) and s_plays.get(str(a), 0))
    print(f"  {both:,} appear in BOTH facts -- one row each, which is the point")


def _multi(argv, flag, default):
    """Collect the several values that may follow a flag, or fall back to the default."""
    if flag not in argv:
        return default
    i = argv.index(flag) + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--"):
        out.append(argv[i]); i += 1
    return out or default


if __name__ == "__main__":
    a = sys.argv[1:]
    use(lg.from_argv(a))
    if a[0] == "athlete":
        csvs = None
        if "--plays" in a:
            i = a.index("--plays") + 1
            csvs = []
            while i < len(a) and not a[i].startswith("--"):
                csvs.append(a[i]); i += 1
        # `--plays-for <league> <paths...>`, repeatable: the per-league form of --plays.
        #
        # stage_athlete has always accepted a {league: paths} dict, and nothing could
        # produce one. A bare --plays list is attributed to --league alone, so on a
        # both-league rebuild the OTHER league silently fell back to its frozen full
        # extract with no in-season file behind it to supersede -- and a frozen extract
        # holds play ids from before ESPN last renumbered a corrected game. That is
        # exactly the orphan this file's comment above records on 2026-09-22, and it
        # recurred on 2026-09-29 from the other direction: an NFL update was blocked by
        # a college play ESPN had removed two days earlier.
        #
        # `--scrim-fact-for` and `--scrim-bridge-for` are the same thing for the scrimmage
        # pair, and were missed by that fix. On 2026-10-05 a college run fed pbp.dim_athlete
        # the NFL's frozen scrimmage extract with no 2026 file behind it: nothing refused,
        # because the scrimmage side has no orphan guard, and verify_split.py caught it as
        # 140 NFL careers whose scrimmage counts disagreed with nfl.dim_athlete.
        def per_league(flag, bare):
            per = {}
            for idx, tok in enumerate(a):
                if tok != flag:
                    continue
                lname, j, paths = a[idx + 1], idx + 2, []
                while j < len(a) and not a[j].startswith("--"):
                    paths.append(a[j]); j += 1
                per[lname] = paths
            if not per:
                return bare
            if bare:
                per.setdefault(LEAGUE, bare)
            return per
        csvs = per_league("--plays-for", csvs)
        # `--leagues cfb nfl` builds ONE dimension over both corpora, which is how it should
        # normally be run once the NFL is loaded. Without it the dimension covers only the
        # league named by --league, and a rebuild would drop every athlete from the other.
        lnames = _multi(a, "--leagues", None) or [LEAGUE]
        stage_athlete(csvs,
                      int(a[a.index("--only-season") + 1]) if "--only-season" in a else None,
                      per_league("--scrim-fact-for", _multi(a, "--scrim-fact", None)),
                      per_league("--scrim-bridge-for", _multi(a, "--scrim-bridge", None)),
                      lnames)
    else:
        {"conf": stage_conf, "venue": stage_venue}[a[0]]()
