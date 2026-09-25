"""Parse special-teams detail out of college football play text.

Grounded in skeleton analysis of 833,544 plays / 2016-2021.
Three feed dialects are present:

  ESPN         "Chris Callahan 33 yd FG GOOD" / "Dom Dzioban punt for 36 yds"
  NCAA         "A. MacGinnis field goal attempt from 44 GOOD, clock 12:28, PENALTY ..."
               -- appears mainly on penalty-flagged plays
  GAMEBOOK     "(07:53) #33 C.Brown punt 48 yards to the USU37 #7 K.Davis return 1 yard
               to the USU38 (#84 N.Elksnis)"
               -- the NCAA-official rendering ESPN began concatenating in from 2021,
               dominant by 2025. Added 2026-08-30.

parse_field_goal did not strip the gamebook clock until 2026-08-31, although parse_punt and
parse_kickoff had since the dialect was added. 1,012 field goals -- a third of the 2025
attempts -- therefore carried the clock and jersey number inside kicker_name
("(09:34) #98 I.Hankins"), matched no athlete id, and read as 973 phantom one-kick kickers.

The gamebook dialect differs from the other two in four ways that each broke a regex:
a leading "(MM:SS)" clock, "#NN" jersey prefixes on every name, "Last,First" or "F.Last"
name rendering, and outcome clauses written without the connecting words the older
patterns required -- "return 9 yards" not "returns for 9 yds", "fair catch by X at UNT06"
not "fair catch by X at the UNT06". Before this was handled, 49% of 2025 punts and 25% of
2025 kickoffs came out with every outcome flag false, which is indistinguishable from a
kick that genuinely had none of those outcomes.

UNREADABLE OUTCOMES ARE NULL, NOT FALSE (2026-08-31). The five "how did it end" flags --
returned, touchback, fair_catch, downed, out_of_bounds -- carry None when the play text
states no outcome at all, which is 20,950 of the 211,659 punts and kickoffs (9.9%) and
worst in 2023-2025. Those five flags do not apply to field goals or conversions, so
`returned IS NULL` only means "outcome not stated" WITHIN punts and kickoffs; scope the
test. Before this they were stored false, indistinguishable from a kick that had no touchback
and no return, so every rate built on them was deflated by however many rows were
unreadable. `returned IS NULL` is now the single-column test for "outcome not stated", and
`avg(touchback::int)` drops those rows from numerator and denominator by itself.
kick_blocked and onside deliberately stay false: both are knowable from what the text DOES
say. See _mark_outcome_unknown.

VALUES THE FEED STATES IMPOSSIBLY are dropped, not published (2026-08-31). A field goal
shorter than 17 yards (10 of end zone plus a ~7-yard snap) and a kickoff longer than the
field are physically impossible; 7 rows state them. Distance becomes None and the row is
marked ambiguous. play_text is retained, so nothing is destroyed.

RETURN-YARDAGE RULES, each chosen against a plausible wrong reading (validated 2026-08-30):
  1. Touchbacks. A touchback is not a 25-yard (kickoff) / 20-yard (punt) return -- that is
     resulting field position. return_yds is None and the touchback flag carries it, so
     return averages are not inflated by 15,824 phantom returns.
  2. "no gain" returns. The trailing yard line is not the return distance:
     "returns for no gain to the IowSt 19" is 0, not 19. (~2,400 plays)
  3. Return losses. "a loss of 34 yards" is stored as -34, not +34. (~1,500 plays)

Every parser returns `parse_confidence`:
    exact     -- matched a known format, key fields recovered
    partial   -- matched, but a key field is genuinely absent from the text
    ambiguous -- structure recognised, values contradictory (feed self-conflict)
    none      -- no pattern matched; caller should log and keep raw text
"""
import re

LOOSE = r"[^,;()]{1,45}?"
# One capitalised name token, tolerant of the forms the three feeds actually emit:
# "Williams", "M.Toney", "O'Neal", "Báez", "Jr.".
_NTOK = r"[A-Z\u00C0-\u00DE][^\s,;()\d]*"
# A person. Either rendering ("Dee Williams" or "Robinson,Javon"), optionally behind the
# gamebook jersey number ("#10 M.Toney").
PNAME = (r"(?:#\d{1,2}\s+)?(?:" + _NTOK + r",\s?" + _NTOK
         + r"|(?:" + _NTOK + r"\s+){0,3}" + _NTOK + r")")

_CLOCK     = re.compile(r"^\(\d{1,2}:\d{2}\)\s*")
_NO_PLAY   = re.compile(r"\bNO PLAY\b|nullified by penalty|score nullified", re.I)
_BLOCKED_BY = re.compile(r"blocked by\s+(?P<blocker>" + LOOSE + r")(?=\s*(?:,|$|\s+return|\s+blocked))", re.I)
# Deliberately case-SENSITIVE on the verb: "Return" with a capital R only ever appears in
# the scoring-summary form ("98 Yd Kickoff Return"), where the number is return yardage and
# is handled by the trap patterns below. The returner is optional because the ESPN dialect
# sometimes omits it (", returns for no gain to the CLT 10").
_RETURN    = re.compile(r"(?:(?P<returner>" + PNAME + r")\s+)?"
                        r"return(?:s|ed)?\s+(?:for\s+|of\s+)?"
                        r"(?P<y>no gain|(?:a\s+)?loss of \d+\s*yards?"
                        r"|-?\d+\s*(?:yds?|yards?))")
# "at the SMU 37" (ESPN/NCAA) and "at UNT06" (gamebook); the name may carry a comma.
_FAIRCATCH = re.compile(r"fair catch by\s+(?P<returner>[^;()]{1,45}?)\s+at\s+(?:the\s+)?\w", re.I)
_FC_FLAG   = re.compile(r"\bfair catch\b", re.I)
_TD        = re.compile(r"\bfor a TD\b|\bTouchdown\b", re.I)
_MUFFED    = re.compile(r"\bmuffed\b", re.I)


def _clean(n):
    if n is None:
        return None
    n = re.sub(r"^#\d+\s*", "", n)              # jersey number prefix: "#0 E.Mooney"
    n = re.sub(r"\s+N/A\b\.?\s*$", "", n)        # the feed's null, glued to a real name
    n = re.sub(r"\s+", " ", n).strip(" .,-")
    if not n or n.lower() in {"null", "n/a", "team", "none"}:
        return None
    return n


def _fix_kicker(n):
    """Some rows prepend an unrelated clause to the kicker
    ("PENALTY UT sideline interference 15 yards to the UT20. Mitchell Becker kickoff...").
    Only trim when the capture is clearly not a name -- "Galitz,Drew" must survive intact."""
    if not n:
        return None
    if "PENALTY" in n.upper() or len(n.split()) > 4:
        m = re.search(r"([A-Z][A-Za-z.'\u2019\-]*(?:\s+[A-Z][A-Za-z.'\u2019\-]*){0,2})\s*$", n)
        if m:
            return _clean(m.group(1))
    return n


def _yards(txt):
    """'no gain' -> 0, 'a loss of 3 yards' -> -3, '12 yds' -> 12."""
    if txt is None:
        return None
    t = txt.lower()
    if "no gain" in t:
        return 0
    m = re.search(r"loss of (\d+)", t)
    if m:
        return -int(m.group(1))
    m = re.search(r"(-?\d+)", t)          # the NCAA dialect writes "returned -3 yards"
    return int(m.group(1)) if m else None


# The five flags that answer "how did this kick end". They are NULLed together when the
# text answers that question not at all -- see _mark_outcome_unknown.
_END_FLAGS = ("touchback", "fair_catch", "downed", "out_of_bounds", "returned")


def _mark_outcome_unknown(out):
    """Distinguish 'no outcome' from 'outcome not stated'.

    The flags carried no NULL state: a kick whose ending the text never mentions was
    stored as false on every one of them, which is indistinguishable from a kick that
    genuinely had no touchback, no fair catch and no return. Every rate built on those
    columns was therefore understated by however many rows are unreadable -- 20,950 of
    them, worst in 2023-2025.

    Only the five "how did it end" flags become NULL. `kick_blocked` and `onside` stay
    false, because those two ARE knowable from what the text does say: a punt recorded
    as travelling 41 yards was not blocked, and both feeds name an onside kick
    explicitly, so an unqualified "kickoff" is a normal one.

    After this, `returned IS NULL` is the single-column test for "outcome unreadable".
    """
    if any(out.get(f) for f in _END_FLAGS) or out.get("kick_blocked"):
        return False
    for f in _END_FLAGS:
        out[f] = None
    out["return_yds"] = None
    return True


def _return_clause(t, out):
    # Some rows carry the clause twice, once anonymously and once with the returner
    # ("... , return for no gain to the MSST 35 , Antonio Harmon return for no gain ...").
    # Prefer whichever occurrence actually names somebody.
    r = None
    for m in _RETURN.finditer(t):
        if r is None:
            r = m
        if m.group("returner"):
            r = m
            break
    if r:
        out["return_yds"] = _yards(r.group("y"))
        name = _clean(r.group("returner"))
        # On a blocked kick the blocker is named immediately before the returner, and a
        # capitalised-run capture swallows both ("Pharoah McKever Dexter Wright").
        blk = out.get("blocker_name")
        if name and blk and name.lower().startswith(blk.lower()):
            # On a blocked kick the blocker is named immediately before the returner, and a
            # capitalised-run capture swallows both ("Pharoah McKever Dexter Wright").
            # When the residual is empty the feed named only one person, and on a blocked
            # kick that person usually did both jobs ("Wyatt Ray  Wyatt Ray return for no
            # gain"), so keep the name rather than dropping the row's only identity.
            name = _clean(name[len(blk):]) or name
        out["returner_name"] = out.get("returner_name") or name
        # ESPN writes "returns for 44 yds for a TD"; the gamebook writes the yardage and
        # then "TOUCHDOWN". A return clause plus either marker is a return touchdown.
        out["returned_for_td"] = bool(_TD.search(t))
    return bool(r)


# --------------------------------------------------------------------------- field goals

# Ordered. The blocked/missed-return forms must be tested FIRST -- their leading number is
# RETURN yardage, not kick distance, and the generic pattern would silently misread it.
_FG_RET_TRAP = re.compile(r"(?P<ret>\d{1,3})\s*Yds?\s+Return\s+of\s+(?P<what>Blocked|Missed)\s+"
                          r"(?:Field\s+Goal|FG)", re.I)
# "nullified by penalty" is written with no separating space before the result
# ("... from 40 yards nullified by penaltyGOOD"), which used to defeat the \s+ before
# <res> and drop the row to parse_confidence 'none'.
_FG_NCAA = re.compile(r"^(?P<kicker>.+?)\s+field goal attempt from\s+(?P<dist>\d{1,2})"
                      r"(?:\s+yards?)?\s*(?:nullified by penalty)?\s*"
                      r"(?P<res>NO GOOD|GOOD|MISSED|BLOCKED|RETURNED)", re.I)
_FG_ESPN = re.compile(r"^(?P<kicker>.+?)\s+(?P<dist>\d{1,2})\s*(?:yd|yds|yard)\s+"
                      r"(?:FG|Field\s+Goal)\s+(?P<res>GOOD|MISSED|BLOCKED|RETURNED)", re.I)
_FG_LONG = re.compile(r"^(?P<kicker>.+?)\s+(?P<dist>\d{1,2})\s*(?:yd|yds|yard)s?\s+"
                      r"Field\s+Goal\s*(?P<res>Missed|Good|Blocked)?\s*$", re.I)
_MISS_REASON = re.compile(r"\b(wide right|wide left|short|hit upright|hit the upright|blocked)\b", re.I)

# Plausibility floors/ceilings for values the feed sometimes states impossibly.
_FG_MIN_YDS = 17      # 10 yd end zone + ~7 yd snap; the NCAA record short is 17
_KO_MAX_YDS = 100     # a kickoff cannot travel further than the field is long


def parse_field_goal(text):
    out = {"fg_distance_yds": None, "fg_made": None, "kick_blocked": False,
           "kicker_name": None, "blocker_name": None, "miss_reason": None,
           "return_yds": None, "returner_name": None, "returned_for_td": False,
           "negated_by_penalty": False, "parse_confidence": "none"}
    if not text:
        return out
    # Gamebook rows open with "(09:34) ". parse_punt and parse_kickoff have stripped this
    # since the dialect was added; parse_field_goal did not, so 1,012 field goals carried
    # the clock and jersey number inside kicker_name ("(09:34) #98 I.Hankins") and linked
    # to no athlete id -- 33% of the 2025 attempts.
    t = _CLOCK.sub("", text.strip())
    out["negated_by_penalty"] = bool(_NO_PLAY.search(t))

    m = _FG_RET_TRAP.search(t)
    if m:
        # Defense returned a blocked/missed FG. The kicker is not named in this form and
        # the only number present is the return, so distance is genuinely unrecoverable.
        out.update(kick_blocked=m.group("what").lower() == "blocked", fg_made=False,
                   return_yds=int(m.group("ret")), returned_for_td=bool(_TD.search(t)),
                   parse_confidence="partial")
        return out

    m = _FG_NCAA.match(t) or _FG_ESPN.match(t) or _FG_LONG.match(t)
    if not m:
        return out
    res = (m.group("res") or "GOOD").upper()
    out["fg_distance_yds"] = int(m.group("dist"))
    out["kicker_name"] = _clean(m.group("kicker"))
    out["kick_blocked"] = res == "BLOCKED"
    # A kick wiped out by penalty has no outcome -- recording it as made would corrupt
    # every FG percentage downstream.
    out["fg_made"] = None if out["negated_by_penalty"] else (res == "GOOD")
    if res == "NO GOOD" and not out["negated_by_penalty"]:
        out["fg_made"] = False
    if res in ("MISSED", "BLOCKED", "RETURNED", "NO GOOD"):
        r = _MISS_REASON.search(t)
        out["miss_reason"] = r.group(1).lower() if r else None
    b = _BLOCKED_BY.search(t)
    if b:
        out["blocker_name"] = _clean(b.group("blocker"))
    _return_clause(t, out)
    out["parse_confidence"] = ("ambiguous" if out["negated_by_penalty"]
                               else ("exact" if out["kicker_name"] else "partial"))
    # The shortest possible field goal is 17 yards: 10 of end zone plus a ~7-yard snap.
    # Four rows state 0 or 14 ("D. Dzioban 0 yd FG GOOD"). The feed is wrong, not the
    # parse, so drop the value and mark the row contradictory rather than publish it.
    if out["fg_distance_yds"] is not None and out["fg_distance_yds"] < _FG_MIN_YDS:
        out["fg_distance_yds"] = None
        out["parse_confidence"] = "ambiguous"
    return out


# --------------------------------------------------------------------------- punts

# "Adoree' Jackson 55 Yd Punt Return (Matt Cummins Kick)". All 82 of these carry a
# conversion parenthetical and none say "TD", because this IS the scoring-summary form --
# the play is a return touchdown by definition.
_PUNT_RET_TRAP = re.compile(r"^(?P<returner>.+?)\s+(?P<ret>\d{1,3})\s*Yds?\s+Punt\s+Return", re.I)
_PUNT_ESPN = re.compile(r"^(?P<punter>.*?)\s*punt for\s+(?P<gross>\d{1,3})\s*yds?", re.I)
_PUNT_NCAA = re.compile(r"^(?P<punter>.*?)\s*punt\s+(?P<gross>\d{1,3})\s+yards?\s+"
                        r"(?:from the \S+\s+)?to the", re.I)
_PUNT_BARE = re.compile(r"^(?P<punter>.*?)\s*punt\b", re.I)


def parse_punt(text):
    out = {"punt_gross_yds": None, "punt_net_yds": None, "return_yds": None,
           "returner_name": None, "touchback": False, "fair_catch": False, "downed": False,
           "out_of_bounds": False, "kick_blocked": False, "returned_for_td": False,
           "kicker_name": None, "blocker_name": None, "negated_by_penalty": False,
           "returned": False, "parse_confidence": "none"}
    if not text:
        _mark_outcome_unknown(out)
        return out
    t = _CLOCK.sub("", text.strip())        # gamebook rows open with "(07:53) "
    out["negated_by_penalty"] = bool(_NO_PLAY.search(t))

    m = _PUNT_RET_TRAP.match(t)
    if m:                       # "Adoree' Jackson 55 Yd Punt Return (...)": no punt distance here
        out.update(return_yds=int(m.group("ret")), returned=True, returned_for_td=True,
                   returner_name=_clean(m.group("returner")), parse_confidence="partial")
        return out

    if re.search(r"\bblocked\b", t, re.I):
        out["kick_blocked"] = True
        b = _BLOCKED_BY.search(t)
        if b:
            out["blocker_name"] = _clean(b.group("blocker"))

    m = _PUNT_ESPN.match(t) or _PUNT_NCAA.match(t)
    if m:
        out["punt_gross_yds"] = int(m.group("gross"))
        out["kicker_name"] = _fix_kicker(_clean(m.group("punter")))
    else:
        m = _PUNT_BARE.match(t)
        if not m:
            # No pattern matched at all: the strongest case of an unstated outcome.
            _mark_outcome_unknown(out)
            return out
        out["kicker_name"] = _fix_kicker(_clean(m.group("punter")))
        if out["kick_blocked"]:
            out["punt_gross_yds"] = 0        # a blocked punt travels no distance

    out["touchback"] = bool(re.search(r"\btouchback\b", t, re.I))
    out["downed"] = bool(re.search(r"\bdowned\b", t, re.I))
    out["out_of_bounds"] = bool(re.search(r"out.of.bounds", t, re.I))

    # The flag comes from the phrase, the name from the fuller pattern. Keeping them
    # together meant a fair catch whose "at <spot>" clause was missing set no flag at all.
    out["fair_catch"] = bool(_FC_FLAG.search(t))
    f = _FAIRCATCH.search(t)
    if f:
        out["returner_name"] = _clean(f.group("returner"))

    had_return = _return_clause(t, out)
    out["returned"] = had_return
    if not had_return and (out["touchback"] or out["fair_catch"] or out["downed"]
                           or out["out_of_bounds"] or out["kick_blocked"]):
        out["return_yds"] = 0                # ball was never advanced
    if out["out_of_bounds"] and had_return:
        # Feed self-conflict: the ball is already dead out of bounds, yet a return is also
        # listed (usually ending on the same yard line). Suppress the return, flag the row.
        out["return_yds"] = None
        out["parse_confidence"] = "ambiguous"

    _mark_outcome_unknown(out)
    if out["punt_gross_yds"] is not None and out["return_yds"] is not None:
        out["punt_net_yds"] = out["punt_gross_yds"] - out["return_yds"]
    if out["parse_confidence"] != "ambiguous":
        # NCAA-dialect rows carrying an accepted penalty have unreliable outcome flags.
        penalised = bool(re.search(r"PENALTY", t, re.I)) and bool(_PUNT_NCAA.match(t))
        out["parse_confidence"] = ("ambiguous" if (out["negated_by_penalty"] or penalised)
                                   else ("exact" if out["kicker_name"] else "partial"))
    # A muff is a real outcome with no column of its own, and the text that follows it
    # ("recovered by TECH ...") decides possession. Flag the row rather than record a
    # silent false on every outcome.
    if _MUFFED.search(t) and not had_return:
        out["parse_confidence"] = "partial"
    return out


# --------------------------------------------------------------------------- kickoffs

# Mirror of _PUNT_RET_TRAP. Without it "Deebo Samuel 97 Yd Kickoff Return (... Kick)" fell
# through to _KO_BARE, which named the RETURNER as the kicker and recorded no return at all.
_KO_RET_TRAP = re.compile(r"^(?P<returner>.+?)\s+(?P<ret>\d{1,3})\s*Yds?\s+Kickoff\s+Return", re.I)
_KO_ESPN = re.compile(r"^(?P<kicker>.*?)\s*kick(?:off)? for\s+(?P<yds>\d{1,3})\s*yds?", re.I)
_KO_NCAA = re.compile(r"^(?P<kicker>.*?)\s*kickoff\s+(?P<yds>\d{1,3})\s+yards?\s+"
                      r"(?:from the \S+\s+)?to the", re.I)
_KO_BARE = re.compile(r"^(?P<kicker>.*?)\s*kickoff\b", re.I)
_ONSIDE  = re.compile(r"^(?P<kicker>.*?)\s*on[-\s]?side kick(?:\s+recovered by\s+(?P<rec>" + LOOSE + r")\s+at the)?", re.I)


def parse_kickoff(text):
    out = {"kickoff_yds": None, "return_yds": None, "returner_name": None, "touchback": False,
           "onside": False, "fair_catch": False, "downed": False, "out_of_bounds": False,
           "returned_for_td": False, "kicker_name": None, "recovered_by": None,
           "negated_by_penalty": False, "returned": False, "parse_confidence": "none"}
    if not text:
        _mark_outcome_unknown(out)
        return out
    t = _CLOCK.sub("", text.strip())        # gamebook rows open with "(07:53) "
    out["negated_by_penalty"] = bool(_NO_PLAY.search(t))

    m = _KO_RET_TRAP.match(t)
    if m:
        out.update(return_yds=int(m.group("ret")), returned=True, returned_for_td=True,
                   returner_name=_clean(m.group("returner")), parse_confidence="partial")
        return out

    if re.search(r"on[-\s]?side kick", t, re.I):
        out["onside"] = True
        o = _ONSIDE.match(t)
        if o:
            k = _clean(o.group("kicker"))
            # "Duke recovers onside kick" names the recovering team, not the kicker
            out["kicker_name"] = None if (k and re.search(r"\brecover", k, re.I)) else k
            out["recovered_by"] = _clean(o.group("rec"))
        # An onside kick can be recovered and then advanced. Without this the yardage was
        # recorded while `returned` stayed false, which is the one state the schema treats
        # as impossible.
        out["returned"] = _return_clause(t, out)
        out["parse_confidence"] = "exact" if o else "partial"
        return out

    m = _KO_ESPN.match(t) or _KO_NCAA.match(t)
    if m:
        out["kickoff_yds"] = int(m.group("yds"))
        out["kicker_name"] = _fix_kicker(_clean(m.group("kicker")))
    else:
        m = _KO_BARE.match(t)
        if not m:
            # No pattern matched at all: the strongest case of an unstated outcome.
            _mark_outcome_unknown(out)
            return out
        out["kicker_name"] = _fix_kicker(_clean(m.group("kicker")))

    out["touchback"] = bool(re.search(r"\btouchback\b", t, re.I))
    out["downed"] = bool(re.search(r"\bdowned\b", t, re.I))
    out["out_of_bounds"] = bool(re.search(r"out.of.bounds", t, re.I))

    out["fair_catch"] = bool(_FC_FLAG.search(t))
    f = _FAIRCATCH.search(t)
    if f:
        out["returner_name"] = _clean(f.group("returner"))

    out["returned"] = _return_clause(t, out)
    if not out["returned"] and (out["touchback"] or out["fair_catch"]
                                or out["downed"] or out["out_of_bounds"]):
        out["return_yds"] = 0                # ball was never advanced
    muffed = bool(_MUFFED.search(t)) and not out["returned"]
    _mark_outcome_unknown(out)
    out["parse_confidence"] = ("ambiguous" if out["negated_by_penalty"]
                               else ("exact" if out["kicker_name"] else "partial"))
    if muffed:
        out["parse_confidence"] = "partial"
    # A kickoff cannot travel further than the field is long; three rows state 108, 125
    # and 127 yards. The feed is wrong, not the parse.
    if out["kickoff_yds"] is not None and out["kickoff_yds"] > _KO_MAX_YDS:
        out["kickoff_yds"] = None
        out["parse_confidence"] = "ambiguous"
    return out


# --------------------------------------------------------------------------- PAT / two-point

# PATs are not separate plays in this feed -- they hide in the touchdown text's
# parenthetical: "Derrick Henry 37 Yd Run (Adam Griffith Kick)". Scan every parenthetical
# and take the first that looks like a conversion; the LAST one is often a penalty clause.
_PAT_GOOD    = re.compile(r"^(?P<kicker>.+?)\s+KICK$", re.I)
_PAT_BAD     = re.compile(r"^(?P<kicker>.+?)\s+PAT\s+(?P<res>BLOCKED|MISSED|FAILED|NO GOOD)$", re.I)
_PAT_BAD_ALT = re.compile(r"^\s*(?:kick|PAT)\s+(?:failed|blocked|missed|no good)\s*$", re.I)
# From 2025 ESPN concatenates an NCAA-official rendering onto the same text field, and there
# the conversion sits OUTSIDE any parenthetical: "Carneiro,Lucas kick attempt good".
# 1,949 plays in 2025 vs 14 in 2024 -- parenthetical-only scanning misses nearly all of them.
_PAT_NCAA = re.compile(r"(?P<pre>[^;]{0,45})\bkick attempt\s+"
                       r"(?P<res>is good|good|failed|missed|blocked|no good)", re.I)
_2PT_NCAA = re.compile(r"(?P<pre>[^;]{0,45})\b(?P<how>rush|pass) attempt\s+"
                       r"(?P<res>is good|good|failed|missed|no good)", re.I)
_NAME_TAIL = re.compile(r"([A-Z][A-Za-z.'\u2019\-]*(?:,\s?[A-Za-z][A-Za-z.'\u2019\-]*)?"
                        r"(?:\s+[A-Z][A-Za-z.'\u2019\-]*){0,2})\s*$")


def parse_pat(text):
    """Return a conversion dict, or None if the touchdown text carries no conversion."""
    if not text:
        return None
    for inner in re.findall(r"\(([^)]*)\)", text):
        s = inner.strip()
        if not s:
            continue
        lo = s.lower()

        if "two" in lo and ("point" in lo or "pt" in lo):
            good = not any(w in lo for w in ("failed", "intercepted", "fumbled", "no good"))
            kind = "pass" if "pass" in lo else ("rush" if "run" in lo else None)
            return {"play_kind": "two_point", "converted": good, "two_point_type": kind,
                    "kicker_name": None, "kick_blocked": False, "parse_confidence": "exact"}

        m = _PAT_BAD.match(s)
        if m:
            return {"play_kind": "pat", "converted": False,
                    "kick_blocked": m.group("res").upper() == "BLOCKED",
                    "kicker_name": _clean(m.group("kicker")), "parse_confidence": "exact"}

        m = _PAT_GOOD.match(s)
        if m:
            return {"play_kind": "pat", "converted": True, "kick_blocked": False,
                    "kicker_name": _clean(m.group("kicker")), "parse_confidence": "exact"}

        if _PAT_BAD_ALT.match(s):
            return {"play_kind": "pat", "converted": False, "kick_blocked": "blocked" in lo,
                    "kicker_name": None, "parse_confidence": "partial"}

    m = _PAT_NCAA.search(text)
    if m:
        res = m.group("res").lower()
        nm = _NAME_TAIL.search(m.group("pre") or "")
        return {"play_kind": "pat", "converted": res in ("good", "is good"),
                "kick_blocked": res == "blocked",
                "kicker_name": _clean(nm.group(1)) if nm else None,
                "parse_confidence": "exact" if nm else "partial"}

    m = _2PT_NCAA.search(text)
    if m:
        res = m.group("res").lower()
        return {"play_kind": "two_point", "converted": res in ("good", "is good"),
                "two_point_type": m.group("how").lower().replace("rush", "rush"),
                "kicker_name": None, "kick_blocked": False, "parse_confidence": "exact"}
    return None
