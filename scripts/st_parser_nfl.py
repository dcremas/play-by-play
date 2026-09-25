"""Parse NFL kick play text. The NFL counterpart to st_parser_cfb.py, and the one component
of this pipeline that did NOT port between the two leagues.

WHY THIS FILE EXISTS. Running st_parser_cfb.py over 566 NFL kicks scored 14.1% `exact` against
98.51% on college -- kickoffs 0.4%, punts 29.7%, field goals 11.4%. Not a tuning problem:
NFL play text is the official NFL gamebook rendering, a dialect unrelated to any of the
three st_parser_cfb.py knows, and unrelated to the NCAA gamebook dialect added on 2026-08-30
despite the shared name.

    M.Bosher kicks 65 yards from ATL 35 to end zone, Touchback.
    (10:31) M.Koenen punts 44 yards to ATL 17, Center-A.DePaola. D.Hester to TB 35 for 48 yards
    (6:07) S.Hauschka 35 yard field goal is GOOD, Center-C.Gresham, Holder-J.Ryan.
    (:06) M.Bryant 59 yard field goal is No Good, Wide Left, Center-J.Harris, Holder-M.Bosher.

WHAT MAKES IT EASIER THAN COLLEGE, and it is worth saying because the headline number above
reads worse than the job turned out to be:

  * The gamebook is MACHINE-GENERATED from official scoring, so it is far more regular than
    any college dialect -- no `#NN` jersey prefixes, no `Last,First` rendering, and
    `Touchback`, `fair catch by`, `downed by`, `out of bounds`, `MUFFS catch` and
    `No Good, Wide Left` are literal and stable strings.
  * There is NO ERA DRIFT. Measured over all 3,297 games, the gamebook form holds at
    95-97% of kicks across the whole 2014-2025 window. The college side is the opposite:
    its gamebook dialect arrived in 2021 and moved every year after.

THE SECOND FORM. The residual is ESPN's own scoring-summary rendering, which appears on
made field goals and nothing else -- `Graham Gano 31 Yd Field Goal`, 34.7% of all field
goal rows, plus a handful of `48 yd FG GOOD` and `50 Yard Field Goal Missed`. It carries a
FULL name where the gamebook carries an initial, and it carries no holder, no snapper and
no miss reason. Both forms are parsed; `parse_confidence` does not distinguish them,
because both state the kick's outcome exactly.

RICHER THAN COLLEGE, and new columns rather than ported ones: every gamebook kick names the
LONG SNAPPER (`Center-C.Gresham`) and every place kick names the HOLDER (`Holder-J.Ryan`).
The college feed gives neither. They land in snapper_name and holder_name, which are NULL
for every college row by construction.

The output contract is st_parser_cfb.py's, key for key, so build_table.py calls either one
through PARSER_BY_LEAGUE without knowing which league it is on.
"""
import re

import st_parser_cfb

# --------------------------------------------------------------------------- names
#
# Two renderings, and they never mix within one clause. The gamebook writes an initial and
# a surname ('M.Bosher', 'Ta.Johnson', 'P.O'Donnell', 'A.Levine Sr.'); the scoring summary
# writes the name out ('Graham Gano', "Ka'imi Fairbairn", 'Odell Beckham Jr.').
_INITIAL = r"[A-Z][a-zA-Z]{0,2}\.[A-Z][A-Za-z'’\-]+(?:\.[A-Z][A-Za-z'’\-]+)?"
_SUFFIX = r"(?:\s+(?:Jr|Sr|II|III|IV|V)\.?)?"
_GAMEBOOK_NAME = _INITIAL + _SUFFIX
# The leading '(?:\.[A-Z]\.?)*' is for 'A.J. Brown' and 'T.J. Carrie', where the first token
# is itself a pair of initials. Without it the name fails to match and a kickoff-return
# touchdown loses its returner.
_FULL_NAME = (r"[A-Z][A-Za-z'’\-]*(?:\.[A-Z]\.?)*(?:\s+[A-Z][A-Za-z'’\.\-]+){1,2}" + _SUFFIX)
_NAME = rf"(?:{_GAMEBOOK_NAME}|{_FULL_NAME})"

# 'ATL 35', 'end zone', '50', 'ATL -2' (the gamebook writes a negative yard line for a ball
# fielded behind the goal line).
_SPOT = r"(?:end\s+zone|(?:[A-Z]{2,3}\s+)?-?\d{1,3})"


def _clean(n):
    if not n:
        return None
    n = re.sub(r"\s+", " ", n).strip(" .,;:-")
    return n or None


def _yards(txt):
    """'no gain' -> 0, '48 yards' -> 48, '-3 yards' -> -3."""
    if txt is None:
        return None
    t = txt.lower()
    if "no gain" in t:
        return 0
    m = re.search(r"(-?\d+)", t)
    return int(m.group(1)) if m else None


# The same five flags st_parser_cfb.py NULLs together, for the same reason: a kick whose ending
# the text never states must not be stored as false on every one of them, because that is
# indistinguishable from a kick that genuinely had no touchback and no return. After this,
# `returned IS NULL` is the single-column test for 'outcome unreadable'.
_END_FLAGS = ("touchback", "fair_catch", "downed", "out_of_bounds", "returned")


def _mark_outcome_unknown(out):
    if any(out.get(f) for f in _END_FLAGS) or out.get("kick_blocked"):
        return False
    for f in _END_FLAGS:
        out[f] = None
    out["return_yds"] = None
    return True


# Snapper and holder. Present on essentially every gamebook kick and on no college row.
_CENTER = re.compile(rf"\bCenter-(?P<n>{_GAMEBOOK_NAME})")
_HOLDER = re.compile(rf"\bHolder-(?P<n>{_GAMEBOOK_NAME})")

# 'TOUCHDOWN' is literal and upper-case in the gamebook. 'TOUCHDOWN NULLIFIED by Penalty'
# is a touchdown that did not count, so the negative lookahead matters.
_TD = re.compile(r"\bTOUCHDOWN\b(?!\s+NULLIFIED)")
_NULLIFIED = re.compile(r"NULLIFIED\s+by\s+Penalty", re.I)
# An accepted penalty that wipes the down. 'PENALTY ... - No Play' is the gamebook's marker;
# a penalty WITHOUT 'No Play' was enforced on the return and the kick itself still stands.
_NO_PLAY = re.compile(r"-\s*No\s+Play", re.I)
_MUFFS = re.compile(r"\bMUFFS\b", re.I)

# The return. '(didn't try to advance)' is the gamebook saying the ball was fielded and
# deliberately not run, which is NOT a return -- it is handled before this fires.
_RETURN = re.compile(
    rf"(?P<returner>{_GAMEBOOK_NAME})\s+(?:(?:pushed\s+)?ob\s+at|to)\s+{_SPOT}\s+"
    rf"for\s+(?P<y>no\s+gain|-?\d+\s+yards?)")
# 'X for 100 yards, TOUCHDOWN.' -- a return straight to the house names no intermediate spot.
_RETURN_TD = re.compile(
    rf"(?P<returner>{_GAMEBOOK_NAME})\s+for\s+(?P<y>-?\d+\s+yards?),\s*TOUCHDOWN")
_NO_ADVANCE = re.compile(r"\(didn't\s+try\s+to\s+advance\)", re.I)


def _return_clause(t, out):
    """Fill return_yds / returner_name / returned_for_td. Returns True if a return was read."""
    m = _RETURN_TD.search(t)
    if m:
        out["return_yds"] = _yards(m.group("y"))
        out["returner_name"] = out.get("returner_name") or _clean(m.group("returner"))
        out["returned_for_td"] = True
        return True
    m = _RETURN.search(t)
    if not m:
        return False
    out["return_yds"] = _yards(m.group("y"))
    out["returner_name"] = out.get("returner_name") or _clean(m.group("returner"))
    out["returned_for_td"] = bool(_TD.search(t))
    return True


def _snap_crew(t, out):
    m = _CENTER.search(t)
    out["snapper_name"] = _clean(m.group("n")) if m else None
    m = _HOLDER.search(t)
    out["holder_name"] = _clean(m.group("n")) if m else None


# --------------------------------------------------------------------------- kickoffs
#
# 'M.Crosby kicks 65 yards from GB 35 to end zone, Touchback.'
# 'C.Parkey kicks onside 3 yards from CLV 35 to CLV 38. RECOVERED by CLV-R.Higgins.'
#
# `onside` sits between the verb and the distance, so it cannot be read as a trailing flag.
# 'to landing zone to end zone' is the 2024 dynamic-kickoff rendering: the ball reached the
# landing zone and then the end zone. Both spots are stated, and the second is the one that
# says how the kick ended, so the first is skipped rather than captured.
_KO = re.compile(
    rf"(?P<kicker>{_GAMEBOOK_NAME})\s+kicks\s+(?P<onside>onside\s+)?(?P<yds>-?\d+)\s+yards?\s+"
    rf"from\s+{_SPOT}\s+to\s+(?:landing\s+zone\s+to\s+)?(?P<to>{_SPOT})")
# The scoring-summary rendering of a kickoff return touchdown. Its leading number is RETURN
# yardage, never kick distance -- the same trap st_parser_cfb.py guards, for the same reason.
_KO_RET_TRAP = re.compile(
    rf"(?P<returner>{_FULL_NAME})\s+(?P<ret>\d{{1,3}})\s*(?:Yds?|Yrds?)\s+"
    rf"(?:Kickoff|KO)\s+Return", re.I)
_RECOVERED = re.compile(rf"RECOVERED\s+by\s+(?:[A-Z]{{2,3}}-)?(?P<n>{_GAMEBOOK_NAME})", re.I)


def parse_nfl_kickoff(text):
    return _with_fallback("kickoff", text, _parse_nfl_kickoff(text))


def _parse_nfl_kickoff(text):
    out = {"kicker_name": None, "returner_name": None, "snapper_name": None,
           "holder_name": None, "kickoff_yds": None, "return_yds": None, "returned": False,
           "touchback": False, "onside": False, "fair_catch": False, "downed": False,
           "out_of_bounds": False, "returned_for_td": False, "recovered_by": None,
           "negated_by_penalty": False, "parse_confidence": "none"}
    t = text or ""
    if not t:
        return out
    # NO _snap_crew here, deliberately. A kickoff is not snapped, so any 'Center-' or
    # 'Holder-' in a kickoff's text belongs to the extra point that followed a return
    # touchdown and happens to share the row. Scanning for it credited 65 kickoffs with a
    # holder, which is not a thing that exists.
    out["negated_by_penalty"] = bool(_NULLIFIED.search(t)) or bool(_NO_PLAY.search(t))

    trap = _KO_RET_TRAP.search(t)
    m = _KO.search(t)
    if not m:
        if trap:
            # A return touchdown in scoring-summary form: the returner and the return are
            # stated, the kick itself is not.
            out["returner_name"] = _clean(trap.group("returner"))
            out["return_yds"] = int(trap.group("ret"))
            out["returned"] = True
            out["returned_for_td"] = True
            out["parse_confidence"] = "partial"
            return out
        _mark_outcome_unknown(out)
        return out

    out["kicker_name"] = _clean(m.group("kicker"))
    out["kickoff_yds"] = int(m.group("yds"))
    out["onside"] = bool(m.group("onside"))
    tail = t[m.end():]

    out["touchback"] = bool(re.search(r"\bTouchback\b", t))
    out["out_of_bounds"] = bool(re.search(r"out\s+of\s+bounds", tail, re.I))
    out["fair_catch"] = bool(re.search(r"fair\s+catch", tail, re.I))
    out["downed"] = bool(re.search(r"\bdowned\s+by\b", tail, re.I))
    rec = _RECOVERED.search(tail)
    if rec:
        out["recovered_by"] = _clean(rec.group("n"))

    # Order matters. "(didn't try to advance) to DET 46 for no gain" is the gamebook saying
    # the ball was fielded and knelt on; it matches the return pattern and is not a return.
    if _NO_ADVANCE.search(tail):
        out["returned"] = False
        out["return_yds"] = 0
    elif _return_clause(tail, out):
        out["returned"] = True
    elif out["touchback"] or out["out_of_bounds"] or out["fair_catch"] or out["downed"]:
        out["return_yds"] = 0

    if out["returned"] and out["touchback"]:
        # 'to end zone, Touchback' followed by a return clause is the feed contradicting
        # itself -- usually a touchback after a muffed catch in the end zone.
        out["parse_confidence"] = "ambiguous"
        return out
    _mark_outcome_unknown(out)
    out["parse_confidence"] = ("ambiguous" if out["negated_by_penalty"]
                               else "exact" if out["kicker_name"] else "partial")
    if _MUFFS.search(t) and not out["returned"]:
        # A muff is a real outcome with no column of its own, and what follows it decides
        # possession. Flag the row rather than record a silent false on every flag.
        out["parse_confidence"] = "partial"
    return out


# --------------------------------------------------------------------------- punts
#
# '(15:00) M.Dickson punts 50 yards to WAS 36, Center-T.Ott. D.Carter to WAS 44 for 8 yards'
# 'T.Way punt is BLOCKED by P.Hendershot, Center-T.Addington, recovered by WAS-D.Gore at WAS 9.'
_PUNT = re.compile(
    rf"(?P<punter>{_GAMEBOOK_NAME})\s+punts\s+(?P<yds>-?\d+)\s+(?:yards?|Yrds?)\s+to\s+"
    rf"(?P<to>{_SPOT})", re.I)
_PUNT_BLOCKED = re.compile(
    rf"(?P<punter>{_GAMEBOOK_NAME})\s+punt\s+is\s+BLOCKED\s+by\s+(?P<blocker>{_GAMEBOOK_NAME})",
    re.I)
# 'Malcolm Brown 8 Yd Return of Blocked Punt (Greg Zuerlein Kick)' and
# 'Adoree' Jackson 55 Yd Punt Return (Matt Cummins Kick)' -- scoring-summary forms whose
# leading number is RETURN yardage.
_PUNT_RET_TRAP = re.compile(
    rf"(?P<returner>{_FULL_NAME})\s+(?P<ret>\d{{1,3}})\s*(?:Yds?|Yrds?)\s+"
    rf"(?:Punt\s+Return|Return\s+of\s+Blocked\s+Punt)", re.I)
_FAIR_CATCH_BY = re.compile(rf"fair\s+catch\s+by\s+(?P<n>{_GAMEBOOK_NAME})", re.I)
# 'Blocked Kick Recovered by Kyle Van Noy (NE) returned 29 yds for TD.' -- the scoring
# summary for a blocked kick the defence picked up. Used by both punts and field goals,
# which is why it lives between them; `classify()` has already decided which kind it is.
_BLOCKED_RECOVERED = re.compile(
    rf"Blocked\s+Kick\s+Recovered\s+by\s+(?P<n>{_FULL_NAME})\s*\((?P<tm>[A-Z]{{2,3}})\)"
    rf"(?:\s+returned\s+(?P<ret>-?\d+)\s*(?:yds?|yards?)\s+for\s+TD)?", re.I)


def parse_nfl_punt(text):
    return _with_fallback("punt", text, _parse_nfl_punt(text))


def _parse_nfl_punt(text):
    out = {"kicker_name": None, "returner_name": None, "blocker_name": None,
           "snapper_name": None, "holder_name": None, "punt_gross_yds": None,
           "punt_net_yds": None, "return_yds": None, "returned": False, "touchback": False,
           "fair_catch": False, "downed": False, "out_of_bounds": False,
           "kick_blocked": False, "returned_for_td": False, "negated_by_penalty": False,
           "parse_confidence": "none"}
    t = text or ""
    if not t:
        return out
    _snap_crew(t, out)
    out["negated_by_penalty"] = bool(_NULLIFIED.search(t)) or bool(_NO_PLAY.search(t))

    blocked = _PUNT_BLOCKED.search(t)
    if blocked:
        out["kick_blocked"] = True
        out["kicker_name"] = _clean(blocked.group("punter"))
        out["blocker_name"] = _clean(blocked.group("blocker"))
        out["return_yds"] = 0
        _return_clause(t[blocked.end():], out)
        out["parse_confidence"] = "exact"
        return out

    br = _BLOCKED_RECOVERED.search(t)
    if br and not _PUNT.search(t):
        out["kick_blocked"] = True
        out["returner_name"] = _clean(br.group("n"))
        if br.group("ret"):
            out["return_yds"] = int(br.group("ret"))
            out["returned_for_td"] = True
        out["parse_confidence"] = "partial"      # the punt itself is never stated here
        return out

    m = _PUNT.search(t)
    if not m:
        trap = _PUNT_RET_TRAP.search(t)
        if trap:
            out["returner_name"] = _clean(trap.group("returner"))
            out["return_yds"] = int(trap.group("ret"))
            out["returned"] = True
            out["returned_for_td"] = True      # this form only ever renders a return TD
            # 'N Yd Return of Blocked Punt' is a block as well as a return.
            out["kick_blocked"] = bool(re.search(r"Blocked", trap.group(0), re.I))
            out["parse_confidence"] = "partial"
            return out
        _mark_outcome_unknown(out)
        return out

    out["kicker_name"] = _clean(m.group("punter"))
    out["punt_gross_yds"] = int(m.group("yds"))
    tail = t[m.end():]

    out["touchback"] = bool(re.search(r"\bTouchback\b", tail))
    out["out_of_bounds"] = bool(re.search(r"out\s+of\s+bounds", tail, re.I))
    out["downed"] = bool(re.search(r"\bdowned\s+by\b", tail, re.I))
    fc = _FAIR_CATCH_BY.search(tail)
    if fc:
        out["fair_catch"] = True
        out["returner_name"] = _clean(fc.group("n"))

    if _return_clause(tail, out):
        out["returned"] = True
    elif out["touchback"] or out["fair_catch"] or out["downed"] or out["out_of_bounds"]:
        out["return_yds"] = 0                  # the ball was never advanced

    if out["out_of_bounds"] and out["returned"]:
        # Dead out of bounds and returned cannot both be true. Suppress and flag.
        out["return_yds"] = None
        out["parse_confidence"] = "ambiguous"
        return out

    _mark_outcome_unknown(out)
    if out["punt_gross_yds"] is not None and out["return_yds"] is not None:
        # Same convention as st_parser_cfb.py: gross minus the return. Deliberately NOT the
        # NFL's official net, which also charges 20 yards for a touchback -- that is a
        # league accounting rule, and applying it here would make the column mean something
        # different from the identically-named college column.
        out["punt_net_yds"] = out["punt_gross_yds"] - out["return_yds"]
    out["parse_confidence"] = ("ambiguous" if out["negated_by_penalty"]
                               else "exact" if out["kicker_name"] else "partial")
    if _MUFFS.search(t) and not out["returned"]:
        out["parse_confidence"] = "partial"
    return out


# --------------------------------------------------------------------------- field goals
#
# '(14:53) D.Carlson 25 yard field goal is GOOD, Center-T.Sieg, Holder-A.Cole.'
# 'J.Myers 42 yard field goal is No Good, Wide Right, Center-C.Stoll, Holder-M.Dickson.'
# 'A.Vinatieri 53 yard field goal is BLOCKED (A.Johnson), Center-L.Rhodes, Holder-R.Sanchez.'
# 'Graham Gano 31 Yd Field Goal'            <- ESPN scoring summary, made
# 'Zane Gonzalez 50 Yard Field Goal Missed' <- ESPN scoring summary, missed
# 'Sebastian Janikowski 48 yd FG GOOD'
_FG = re.compile(
    rf"(?P<kicker>{_NAME})\s+(?P<dist>\d{{1,2}})\s+yard\s+field\s+goal\s+is\s+"
    rf"(?P<res>GOOD|No\s+Good|BLOCKED|Aborted)", re.I)
_FG_SUMMARY = re.compile(
    rf"(?P<kicker>{_FULL_NAME})\s+(?:Made\s+)?(?P<dist>\d{{1,2}})\s*"
    rf"(?:Yd|Yds|Yrd|Yrds|Yard|Yards)\s+(?:Field\s+Goal|FG)(?P<miss>\s+Missed)?", re.I)
_FG_SUMMARY_2 = re.compile(
    rf"(?P<kicker>{_NAME})\s+(?P<dist>\d{{1,2}})\s*yds?\s+FG\s+(?P<res>GOOD|MISSED|No\s+Good)",
    re.I)
_FG_BLOCKER = re.compile(rf"BLOCKED\s+\((?P<n>{_GAMEBOOK_NAME})\)", re.I)
# The miss reason is a bare clause immediately after 'No Good, ' and before the crew.
_MISS_REASON = re.compile(
    r"No\s+Good,\s+(?P<why>Wide\s+Left|Wide\s+Right|Short|Hit\s+Left\s+Upright|"
    r"Hit\s+Right\s+Upright|Hit\s+Crossbar|Hit\s+Upright)", re.I)
# 'C.Harris for 58 yards, TOUCHDOWN' after a block -- the defence returned it.
_FG_RET_TRAP = re.compile(
    rf"(?P<returner>{_FULL_NAME})\s+(?P<ret>\d{{1,3}})\s*(?:Yds?|Yrds?)\s+Return\s+of\s+"
    rf"(?:Blocked|Missed)\s+(?:Field\s+Goal|FG)", re.I)

# A field goal is 17 yards longer than the line of scrimmage and the sport's record is 66.
# Anything outside this is the parser having read the wrong number.
_FG_RANGE = (15, 75)


def parse_nfl_field_goal(text):
    return _with_fallback("field_goal", text, _parse_nfl_field_goal(text))


def _parse_nfl_field_goal(text):
    out = {"kicker_name": None, "returner_name": None, "blocker_name": None,
           "snapper_name": None, "holder_name": None, "fg_distance_yds": None,
           "fg_made": None, "kick_blocked": False, "miss_reason": None,
           "return_yds": None, "returned_for_td": False, "negated_by_penalty": False,
           "parse_confidence": "none"}
    t = text or ""
    if not t:
        return out
    _snap_crew(t, out)
    out["negated_by_penalty"] = bool(_NULLIFIED.search(t)) or bool(_NO_PLAY.search(t))

    # Tested FIRST: its leading number is RETURN yardage, and the generic pattern would
    # read it as the kick distance.
    trap = _FG_RET_TRAP.search(t)
    if trap and not _FG.search(t):
        out["returner_name"] = _clean(trap.group("returner"))
        out["return_yds"] = int(trap.group("ret"))
        out["returned_for_td"] = True
        out["fg_made"] = False
        out["kick_blocked"] = bool(re.search(r"Blocked", trap.group(0), re.I))
        out["parse_confidence"] = "partial"
        return out

    br = _BLOCKED_RECOVERED.search(t)
    if br and not _FG.search(t):
        out["kick_blocked"] = True
        out["fg_made"] = False
        out["returner_name"] = _clean(br.group("n"))
        if br.group("ret"):
            out["return_yds"] = int(br.group("ret"))
            out["returned_for_td"] = True
        out["parse_confidence"] = "partial"      # the distance is never stated here
        return out

    m = _FG.search(t)
    if m:
        res = re.sub(r"\s+", " ", m.group("res")).lower()
        out["kicker_name"] = _clean(m.group("kicker"))
        dist = int(m.group("dist"))
        out["fg_distance_yds"] = dist if _FG_RANGE[0] <= dist <= _FG_RANGE[1] else None
        out["kick_blocked"] = res == "blocked"
        if res == "good":
            out["fg_made"] = True
        elif res in ("no good", "blocked", "aborted"):
            out["fg_made"] = False
        mr = _MISS_REASON.search(t)
        if mr:
            out["miss_reason"] = re.sub(r"\s+", " ", mr.group("why")).lower()
        blk = _FG_BLOCKER.search(t)
        if blk:
            out["blocker_name"] = _clean(blk.group("n"))
        if _TD.search(t[m.end():]):
            out["returned_for_td"] = True
            _return_clause(t[m.end():], out)
        if out["negated_by_penalty"]:
            # The kick was attempted and its result stated, but the down was wiped. The
            # result is real prose and false accounting, so fg_made goes NULL rather than
            # crediting or charging a kicker for a kick that did not count.
            out["fg_made"] = None
            out["parse_confidence"] = "ambiguous"
        else:
            out["parse_confidence"] = "exact"
        return out

    for rx in (_FG_SUMMARY_2, _FG_SUMMARY):
        m = rx.search(t)
        if not m:
            continue
        out["kicker_name"] = _clean(m.group("kicker"))
        dist = int(m.group("dist"))
        out["fg_distance_yds"] = dist if _FG_RANGE[0] <= dist <= _FG_RANGE[1] else None
        if rx is _FG_SUMMARY_2:
            out["fg_made"] = m.group("res").lower() == "good"
        else:
            # The scoring-summary form renders a MADE kick with no qualifier and a missed
            # one with a trailing 'Missed'.
            out["fg_made"] = not m.group("miss")
        out["parse_confidence"] = "ambiguous" if out["negated_by_penalty"] else "exact"
        return out
    return out


# --------------------------------------------------------------------------- conversions
#
# Like college, the NFL folds the conversion into the TOUCHDOWN play's text rather than
# giving it a play of its own, so this is handed the touchdown's text.
#
#   'B.Walsh extra point is GOOD, Center-C.Loeffler, Holder-J.Locke.'
#   'J.Myers extra point is No Good, Wide Right, Center-C.Tinker, Holder-B.Nortman.'
#   'C.McLaughlin extra point is Blocked (I.Rodgers), Center-Z.Triner, Holder-T.Gill.'
#   'TWO-POINT CONVERSION ATTEMPT. K.Allen pass to L.Tunsil is incomplete. ATTEMPT FAILS.'
#   'TWO-POINT CONVERSION ATTEMPT. S.Carlson rushes left end. ATTEMPT SUCCEEDS.'
#   'Rob Gronkowski 2 Yd pass from Tom Brady (Ryan Succop Kick)'   <- scoring-summary form,
#                                                   identical to the college dialect's
_XP = re.compile(
    rf"(?P<kicker>{_GAMEBOOK_NAME})\s+extra\s+point\s+is\s+"
    rf"(?P<res>GOOD|No\s+Good|Blocked|Aborted|Failed)", re.I)
_TWO_PT = re.compile(
    r"TWO-POINT\s+CONVERSION\s+ATTEMPT\.\s*(?P<how>.*?)ATTEMPT\s+(?P<res>SUCCEEDS|FAILS)",
    re.I | re.S)
# The ESPN scoring-summary parenthetical, which the NFL shares with college verbatim.
_SUMMARY_KICK = re.compile(rf"\((?P<kicker>{_FULL_NAME})\s+Kick\)")
_SUMMARY_KICK_FAILED = re.compile(r"\((?:Kick|PAT)\s+failed\)", re.I)
_SUMMARY_TWO = re.compile(r"\((?P<how>Run|Pass)\s+for\s+Two-Point\s+Conversion\)", re.I)
# 'DEFENSIVE TWO-POINT ATTEMPT' is the DEFENCE scoring off a blocked kick -- a separate
# event that follows the conversion, not the conversion itself. It must not overwrite the
# pat row's `converted`, which describes the kicking team's attempt.
_DEFENSIVE_TWO = re.compile(r"DEFENSIVE\s+TWO-POINT\s+ATTEMPT", re.I)


def parse_nfl_pat(text):
    """A conversion row off a touchdown play's text, or None if there is no conversion."""
    t = text or ""
    if not t:
        return None

    # Two-point first: a text carrying both an extra-point clause and a two-point clause is
    # a blocked PAT returned by the defence, and the offence's attempt is the extra point.
    m = _XP.search(t)
    if m:
        res = re.sub(r"\s+", " ", m.group("res")).lower()
        blk = re.search(rf"extra\s+point\s+is\s+Blocked\s+\((?P<n>{_GAMEBOOK_NAME})\)", t, re.I)
        # The conversion clause names its own snapper and holder, exactly as a field goal
        # does. Read from the tail after 'extra point', so a touchdown whose text ALSO
        # describes a field goal earlier in the drive cannot lend its crew to the PAT.
        crew = {}
        _snap_crew(t[m.start():], crew)
        return {"play_kind": "pat", "converted": res == "good",
                "two_point_type": None, "kick_blocked": res == "blocked",
                "kicker_name": _clean(m.group("kicker")),
                "blocker_name": _clean(blk.group("n")) if blk else None,
                "snapper_name": crew.get("snapper_name"),
                "holder_name": crew.get("holder_name"),
                "parse_confidence": "exact"}

    m = _TWO_PT.search(t)
    if m:
        how = (m.group("how") or "").lower()
        kind = "pass" if "pass" in how else ("rush" if "rush" in how else None)
        return {"play_kind": "two_point",
                "converted": m.group("res").upper() == "SUCCEEDS",
                "two_point_type": kind, "kick_blocked": False,
                "kicker_name": None, "blocker_name": None,
                "parse_confidence": "exact"}

    m = _SUMMARY_TWO.search(t)
    if m:
        return {"play_kind": "two_point", "converted": True,
                "two_point_type": m.group("how").lower(), "kick_blocked": False,
                "kicker_name": None, "blocker_name": None, "parse_confidence": "exact"}

    m = _SUMMARY_KICK.search(t)
    if m:
        return {"play_kind": "pat", "converted": True, "two_point_type": None,
                "kick_blocked": False, "kicker_name": _clean(m.group("kicker")),
                "blocker_name": None, "parse_confidence": "exact"}

    if _SUMMARY_KICK_FAILED.search(t):
        return {"play_kind": "pat", "converted": False, "two_point_type": None,
                "kick_blocked": False, "kicker_name": None, "blocker_name": None,
                "parse_confidence": "exact"}
    return None


# --------------------------------------------------------------------------- fallback
#
# A small number of NFL rows are not in the gamebook dialect at all -- they are ESPN's OWN
# dialect, the one st_parser_cfb.py was written for:
#
#   'Steven Hauschka kickoff for 70 yds , T.J. Carrie return for 42 yds to the OAK 37'
#   'Marquette King punt for 39 yds, fair catch by Doug Baldwin at the SEA 22'
#
# 32 rows corpus-wide. Rather than re-implement a dialect this project already parses at
# 98.51%, hand anything the NFL patterns cannot read to the college parser and keep its
# answer if it did better. The two modules return the same keys, so the merge is a dict
# update; snapper_name and holder_name simply stay None, which is correct -- the ESPN
# dialect does not name them.
_FALLBACK = {"punt": st_parser_cfb.parse_punt, "kickoff": st_parser_cfb.parse_kickoff,
             "field_goal": st_parser_cfb.parse_field_goal}


def _with_fallback(kind, text, out):
    if out.get("parse_confidence") != "none":
        return out
    alt = _FALLBACK[kind](text)
    if alt.get("parse_confidence") in ("exact", "partial", "ambiguous"):
        merged = dict(out)
        for k, v in alt.items():
            if k in merged or k in ("recovered_by",):
                merged[k] = v
        # The college parser cannot know these two; keep whatever the NFL pass found.
        merged["snapper_name"] = out.get("snapper_name")
        merged["holder_name"] = out.get("holder_name")
        return merged
    return out


NFL_PARSER = {"punt": parse_nfl_punt, "kickoff": parse_nfl_kickoff,
              "field_goal": parse_nfl_field_goal}
