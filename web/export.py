"""Taking the grid away: the pane on screen, as a .csv or an .xlsx workbook.

Everything the grids show is already a DuckDB result set, so an export is not a new
question -- it is the question the pane just asked, run once more without a LIMIT of a
screenful. That is the whole design, and it is what makes the file match the pane:

  * the plays grid runs on AG Grid's infinite row model, so the browser holds a dozen
    blocks of a selection that can be 1.5M rows. A client-side "export what you can
    see" would therefore export a scroll position. This re-runs the query instead, with
    the sidebar's WHERE clause AND the grid's own header filters AND its sort order --
    the last `getRowsRequest` is kept in a Store for exactly that, because it is
    literally what the grid asked for rather than a guess reconstructed from props.
  * the rolled-up grids (Kickers, Teams) are client-side, so `virtualRowData` IS the
    displayed rows, after their header filters and in their sort order, and it is used
    as given. It can be empty for a beat after the grid mounts, so the caller passes a
    server-side fallback that recomputes the same rollup.

Columns come from the same column definitions the grid was built with, so the file has
the pane's columns, in the pane's order, under the pane's headers -- including whatever
the "Add columns" control has switched on. Values do NOT: a grid renders a boolean as
a tick and a rate through a d3 format, and a spreadsheet wants the number. Formatting
is a display concern and it is dropped here on purpose.

THE ROW CAP is real and is stated in the filename rather than hidden. 1.5M rows of
thirty columns is a multi-hundred-megabyte CSV assembled in the web worker's memory,
and it is over Excel's own 1,048,576-row sheet limit besides. `PBP_WEB_EXPORT_MAX`
raises or lowers it; a truncated file says `-first50000` in its name and carries the
full selection size on its About sheet, so nobody quotes a capped count as a total.
"""
from __future__ import annotations

import datetime as dt
import os
import re

import dash_mantine_components as dmc
import pandas as pd
from dash import Output, ctx, dcc
from dash.exceptions import PreventUpdate

from . import data, league, lens

# How many rows a single download may carry. Not a limit of the warehouse -- the pane
# will happily select ten times this -- but of what is reasonable to build in memory
# and hand to a browser. Excel's sheet limit is the hard ceiling above it.
MAX_ROWS = int(os.environ.get("PBP_WEB_EXPORT_MAX") or 100_000)
XLSX_MAX_ROWS = 1_048_575  # 1,048,576 minus the header row

_SHEET = "Data"
_ABOUT = "About"

# The report palette, restated rather than imported. web/deploy/push.sh ships `web/`
# and nothing else, so an import of reports/workbook.py would be a NameError on the
# box and fine on the laptop -- the worst shape a dependency can have. Four hex codes
# are a cheaper price than that, and they are here so a workbook off this app and one
# off reports/ read as the same product.
_ACCENT, _INK, _MUTED, _RULE = "#3b7dd8", "#1f2933", "#6b7785", "#c9d2dc"


# --------------------------------------------------------------------------- the control
def controls(prefix: str, note: str | None = None):
    """The two download buttons for one grid.

    `prefix` names the grid, not the page: the ids are `{prefix}-csv` and
    `{prefix}-xlsx`, and they are rendered BESIDE the grid they export rather than in
    the page chrome. That is deliberate. The explorer swaps one grid for another as the
    grain changes, and a button that outlives its grid would be a callback State
    pointing at a component that is no longer in the tree. Rendering them together
    means the pair and the grid it reads are always the same age.

    The `dcc.Download` sink is NOT here -- one per page is enough, and three of them
    would be three components competing to be written by one click.
    """
    return dmc.Group(gap=6, align="center", wrap="nowrap", children=[
        dmc.Text("Download", className="st-cap"),
        dmc.Tooltip(
            label=note or _default_note(), multiline=True, w=300, withArrow=True,
            openDelay=200, position="top-end", zIndex=1500,
            children=dmc.Group(gap=4, wrap="nowrap", children=[
                dmc.Button("CSV", id=f"{prefix}-csv", variant="default",
                           size="compact-xs"),
                dmc.Button("Excel", id=f"{prefix}-xlsx", variant="default",
                           size="compact-xs"),
            ])),
    ])


def clicked(n_csv, n_xlsx) -> str:
    """Which of the pair was actually pressed -- or nothing happens.

    `prevent_initial_call=True` is NOT enough on these buttons, and this function
    exists for that one reason. It suppresses a callback on the INITIAL page load only,
    and these buttons are never on the initial page: `controls` renders them into the
    grid's chrome from another callback, so they ARRIVE by callback on the explorer's
    first paint and again on every grain switch. Dash fires the callbacks of components
    it adds to the layout dynamically, with `n_clicks=None` -- and a handler that reads
    only `ctx.triggered_id` cannot tell that apart from a press. It downloaded a file
    every time the grid was drawn.

    So the trigger names the FORMAT and that button's own count says whether it was
    PRESSED, and both have to agree. A freshly mounted button counts None, which is the
    state this guards.
    """
    fmt = str(ctx.triggered_id or "").rsplit("-", 1)[-1]
    if not {"csv": n_csv, "xlsx": n_xlsx}.get(fmt):
        raise PreventUpdate
    return fmt

def busy(prefix: str) -> list[tuple]:
    """The `running=` spec that puts a spinner in both buttons while the file builds.

    Not cosmetic. A 100,000-row workbook takes the better part of twenty seconds to
    assemble, and a button that looks identical the whole time reads as one that did
    not register the click -- so the next thing a reader does is click it again, and
    now two of those are queued. The spinner is the difference between "working" and
    "broken", and it is the only feedback there is: a download has no page to land on.
    """
    return [(Output(f"{prefix}-csv", "loading"), True, False),
            (Output(f"{prefix}-xlsx", "loading"), True, False)]



def _default_note() -> str:
    return (f"The rows this pane is showing — every sidebar filter, every column-header "
            f"filter and the sort you put it in — with the columns on screen, including "
            f"any switched on from Add columns. Excel is a .xlsx workbook with an About "
            f"sheet recording the selection. Capped at {MAX_ROWS:,} rows; a capped file "
            f"says so in its name.")


# --------------------------------------------------------------------------- the frame
def shape(rows, coldefs) -> pd.DataFrame:
    """Result set + the grid's column definitions -> the frame to write.

    The pane's columns, in the pane's order, under the pane's headers. A column the
    grid defines but the view does not carry is dropped rather than written empty --
    that pairing does not arise today, and a silent empty column would be a worse way
    to find out that it had.

    VALUES are not formatted -- 0.43706896551724135 goes into the cell, not "43.7%" --
    because a rate rounded to a string is a rate nobody can average afterwards. What
    the grid's formatter contributes instead is a DISPLAY format, carried on
    `df.attrs` for the workbook writer to apply as a cell format. So Excel shows what
    the pane showed and holds what the warehouse holds, and CSV, which has no formats
    at all, gets the full number -- which is the right answer for a file whose whole
    purpose is to be read by something else.
    """
    df = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows or [])
    if not len(df.columns):
        # An empty selection reaching here as a bare list has no columns to match
        # against, so the declared ones are taken as given. The point is that a click
        # on Download always produces the file it promises: a header row and no data is
        # a legible answer to "nothing matched", and silence is not.
        kept = [c for c in coldefs if c.get("field")]
        return pd.DataFrame(columns=_unique([c.get("headerName") or c["field"]
                                             for c in kept]))
    kept = [c for c in coldefs if c.get("field") and c["field"] in df.columns]
    if not kept:
        return pd.DataFrame()
    out = df[[c["field"] for c in kept]].copy()
    out.columns = _unique([c.get("headerName") or c["field"] for c in kept])
    out.attrs["number_formats"] = {
        header: fmt for header, c in zip(out.columns, kept)
        if (fmt := _xl_format(c)) is not None
    }
    return _spreadsheet_safe(out)


# The grids format numbers with d3 in the browser. Excel has its own format language
# and no d3, so the handful of patterns the column builders actually use are mapped
# across by hand. An unmapped pattern -- or a formatter that is not a d3 call at all,
# which is what the boolean tick is -- comes back None and the column is written
# unformatted rather than approximately.
_D3_CALL = re.compile(r"""d3\.format\(\s*['"]([^'"]+)['"]\s*\)""")
_D3_TO_XL = {",": "#,##0", ".0%": "0%", ".1%": "0.0%", ".2%": "0.00%",
             ".1f": "0.0", ".2f": "0.00", "+d": "+0;-0"}


def _xl_format(coldef: dict) -> str | None:
    fn = coldef.get("valueFormatter")
    js = fn.get("function") if isinstance(fn, dict) else fn
    if not isinstance(js, str):
        return None
    m = _D3_CALL.search(js)
    return _D3_TO_XL.get(m.group(1)) if m else None


def _unique(names: list[str]) -> list[str]:
    """Two columns may legitimately share a header; a DataFrame's may not."""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


def _spreadsheet_safe(df: pd.DataFrame) -> pd.DataFrame:
    """The two things xlsxwriter refuses, fixed before it sees them.

    A tz-aware timestamp raises outright ("Excel does not support datetimes with
    timezones"), and pandas' missing-value flavours -- NaT, pd.NA -- are not all
    recognised as blank. Dates are left as dates: a spreadsheet wants a date cell it
    can sort and subtract, not the ISO string the grid displays.
    """
    for col in df.columns:
        s = df[col]
        if isinstance(s.dtype, pd.DatetimeTZDtype):
            df[col] = s.dt.tz_localize(None)
    return df


# --------------------------------------------------------------------------- delivery
def cap(fmt: str) -> int:
    return min(MAX_ROWS, XLSX_MAX_ROWS) if fmt == "xlsx" else MAX_ROWS


def deliver(fmt: str, df: pd.DataFrame, stem: str, about: list[tuple[str, str]],
            total: int | None = None):
    """The frame, as the file Dash hands the browser.

    `total` is the size of the whole selection when it is larger than the frame. It
    reaches the reader twice -- in the filename and on the About sheet -- because a
    truncated export that looks complete is the one way this feature could put a wrong
    number in front of someone.
    """
    n = len(df)
    truncated = total is not None and total > n
    name = f"{stem}-first{n}" if truncated else stem
    name = f"{name}-{_now():%Y%m%d-%H%M}"
    rows = list(about)
    rows.append(("Rows in this file", f"{n:,}"))
    if total is not None:
        rows.append(("Rows in the selection", f"{total:,}"))
    if truncated:
        rows.append(("Truncated",
                     (f"yes — capped at {cap(fmt):,} rows. Narrow the selection, "
                      "or raise PBP_WEB_EXPORT_MAX.")))
    if fmt == "csv":
        return dcc.send_data_frame(df.to_csv, f"{name}.csv", index=False)
    return dcc.send_bytes(lambda buf: _workbook(buf, df, rows), f"{name}.xlsx")


def _workbook(buf, df: pd.DataFrame, about: list[tuple[str, str]]) -> None:
    """A two-sheet .xlsx: the rows, and what selection they are.

    `engine="xlsxwriter"` is resolved by pandas at call time, which is why nothing in
    this module imports xlsxwriter at the top. A box without it serves CSV perfectly
    and fails only on the Excel button -- a degraded feature rather than a dead app.
    """
    with pd.ExcelWriter(buf, engine="xlsxwriter", date_format="yyyy-mm-dd",
                        datetime_format="yyyy-mm-dd hh:mm") as xw:
        df.to_excel(xw, sheet_name=_SHEET, index=False)
        book, ws = xw.book, xw.sheets[_SHEET]
        head = book.add_format({"font_name": "Calibri", "font_size": 10, "bold": True,
                                "font_color": "#ffffff", "bg_color": _ACCENT,
                                "border": 1, "border_color": _ACCENT, "valign": "vcenter",
                                "text_wrap": False})
        # A column format rather than a cell format: pandas writes the values with no
        # format of their own, so set_column's is what they pick up, and one format
        # object per column beats one per cell on a 100k-row sheet.
        fmts = df.attrs.get("number_formats") or {}
        cell = {f: book.add_format({"font_name": "Calibri", "font_size": 10,
                                    "num_format": f}) for f in set(fmts.values())}
        for i, name in enumerate(df.columns):
            ws.write(0, i, str(name), head)
            ws.set_column(i, i, _width(df, name, name in fmts),
                          cell.get(fmts.get(name)))
        ws.freeze_panes(1, 0)
        if len(df):
            # Header filters on the sheet, because the reader has just come from a grid
            # that had them and will reach for them again.
            ws.autofilter(0, 0, len(df), max(len(df.columns) - 1, 0))
        _about_sheet(book, about)


def _width(df: pd.DataFrame, col, formatted: bool = False) -> float:
    """Wide enough for the header and a sample of the values, and no wider.

    The sample is the first 200 rows rather than the column: on 100k rows of play text
    the full scan costs more than the column is worth, and the clamp below would bind
    long before the extra rows changed the answer.

    A column carrying a number format is measured on its header alone, because the
    values are not what the reader will see: `43.706896551724135` is what the cell
    holds and `43.7` is what it shows, and sizing to the former gives a 20-character
    column for a four-character number.
    """
    if formatted:
        return min(max(len(str(col)) + 3, 9), 46)
    sample = df[col].head(200).astype("string").dropna()
    longest = max([len(str(col))] + [len(s) for s in sample]) if len(sample) else len(str(col))
    return min(max(longest + 2, 9), 46)


def _about_sheet(book, rows: list[tuple[str, str]]) -> None:
    ws = book.add_worksheet(_ABOUT)
    title = book.add_format({"font_name": "Calibri", "font_size": 14, "bold": True,
                             "font_color": _INK})
    key = book.add_format({"font_name": "Calibri", "font_size": 10, "bold": True,
                           "font_color": _MUTED, "valign": "top",
                           "bottom": 1, "bottom_color": _RULE})
    val = book.add_format({"font_name": "Calibri", "font_size": 10, "font_color": _INK,
                           "valign": "top", "text_wrap": True,
                           "bottom": 1, "bottom_color": _RULE})
    ws.set_column(0, 0, 26)
    ws.set_column(1, 1, 92)
    ws.write(0, 0, "What this file is", title)
    for i, (k, v) in enumerate(rows, start=2):
        ws.write(i, 0, k, key)
        ws.write(i, 1, "" if v is None else str(v), val)
    ws.freeze_panes(2, 0)


# --------------------------------------------------------------------------- provenance
# The sidebar facets, in the sidebar's own words. A download that says "clutch_only:
# True" is a file that needs the source to read; this is the same table the panel is
# labelled from, so the About sheet reads like the panel the reader just used.
_FACET = {
    "phases": "Phase", "seasons": "Seasons", "teams": "Team", "players": "Player",
    "outcomes": "Outcome", "opponents": "Opponent", "conferences": "Conference",
    "season_types": "Season type", "fbs_only": "FBS opponents only",
    "conf_game": "Conference game", "qtrs": "Quarter", "score_states": "Score state",
    "downs": "Down", "zones": "Field zone", "dist": "Distance",
    "clutch_only": "Clutch only", "surfaces": "Surface", "divisions": "Division",
    "neutral": "Neutral site", "roof": "Roof", "linked_only": "Linked athletes only",
}


def _idle(name: str, value, lg: str) -> bool:
    """Is this facet doing nothing?

    The two range facets are the awkward ones: full-width is idle and looks exactly
    like a deliberate selection of everything, so they are compared against the
    corpus bounds rather than against emptiness.
    """
    if value in (None, "", "any", False) or value == []:
        return True
    if name == "seasons":
        return list(value) == list(data.season_bounds())
    if name == "dist":
        return list(value) == list(data.dist_bounds(lg))
    return False


def _show(name: str, value, key: str) -> str:
    if name == "phases":
        return ", ".join(lens.PHASES[key].get(p, p) for p in value)
    if name in ("seasons", "dist"):
        lo, hi = value
        return f"{lo}" if lo == hi else f"{lo}–{hi}"
    if isinstance(value, bool):
        return "yes"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def about(flt: dict | None, grain: str, *, grid_filters=None, order: str | None = None,
          where: str | None = None, ignore: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Everything needed to reproduce this file, in the order a reader wants it.

    `ignore` names facets the caller's query deliberately did NOT apply -- the profile
    pages drop the facet the page already IS -- and they are listed as ignored rather
    than omitted. Omitting them would be the more dangerous silence of the two: a
    reader who left the player picker set would otherwise have no way to tell whether
    it had bitten.

    The generated WHERE clause is included verbatim at the bottom. It is the one line
    that is exactly what ran, and the same habit as the drawer's Provenance panel: a
    number in a spreadsheet is a number away from its source unless the source travels
    with it.
    """
    flt = flt or {}
    key = lens.resolve(flt.get("lens"))
    lg = league.resolve(flt.get("league"))
    meta = data.snapshot_meta(lg)
    built = meta.get("built_at")
    rows = [
        ("Exported", _now().strftime("%Y-%m-%d %H:%M %Z")),
        ("Source", "Play-by-Play explorer"),
        ("Snapshot built", built.strftime("%Y-%m-%d %H:%M %Z") if built else "unknown"),
        ("League", f"{league.LABEL[lg]} ({lg})"),
        ("Lens", f"{lens.LABEL[key]} — a row is one {lens.NOUN[key][:-1]}"),
        ("Grain", grain),
    ]
    # Phases are read back through chip_set rather than off the dict, for the same
    # reason every query does: a chip belonging to another lens is not a phase here,
    # and printing it would name a filter that did nothing.
    shown = dict(flt, phases=data.chip_set(flt))
    active = [(label + (" (ignored on this page)" if name in ignore else ""),
               _show(name, shown.get(name), key))
              for name, label in _FACET.items()
              if not _idle(name, shown.get(name), lg)]
    rows += active or [("Filters", "none — the whole corpus for this league and lens")]
    if grid_filters:
        rows.append(("Column filters on the grid",
                     "; ".join(f"{col}: {_crit(c)}" for col, c in grid_filters.items())))
    if order:
        rows.append(("Sorted by", order))
    if where:
        rows.append(("SQL WHERE", where))
    return rows


def _now() -> dt.datetime:
    """Local wall-clock time, carrying its zone.

    `astimezone()` on a naive `now()` attaches the machine's offset rather than
    converting anything, so this is the same instant the reader's clock shows and it
    prints the zone beside it -- which matters because the About sheet puts it one row
    above the snapshot's build time, and two timestamps side by side with only one of
    them qualified invite the reader to subtract them.
    """
    return dt.datetime.now().astimezone()


def _crit(c: dict) -> str:
    """One AG Grid filter criterion, as the phrase the header showed.

    Mirrors web/data.py's _one_condition rather than sharing it: that function returns
    SQL and this one returns English, and the combined `conditions` shape is the only
    thing both have to understand.
    """
    if c.get("operator") and c.get("conditions"):
        joiner = f" {str(c['operator']).lower()} "
        return joiner.join(_crit(x) for x in c["conditions"])
    if c.get("filterType") == "set":
        return "one of " + ", ".join(str(v) for v in c.get("values") or [])
    kind = c.get("type", "")
    a, b = c.get("filter"), c.get("filterTo")
    if c.get("dateFrom") is not None:
        a, b = c.get("dateFrom"), c.get("dateTo")
    return f"{kind} {a}" + (f" and {b}" if b is not None else "")


# --------------------------------------------------------------------------- naming
def stem(flt: dict | None, grain: str) -> str:
    """`pbp-cfb-st-kicks`. League and lens are in the name because two files in a
    downloads folder are otherwise indistinguishable, and they are the two axes that
    change what a row IS rather than merely which rows there are."""
    flt = flt or {}
    key = lens.resolve(flt.get("lens"))
    return f"pbp-{league.resolve(flt.get('league'))}-{key}-{grain}"


def sink(cid: str):
    """The per-page download target. One is enough; see `controls`."""
    return dcc.Download(id=cid)
