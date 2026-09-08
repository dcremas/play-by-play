"""House style and grid mechanics shared by every report in this package.

The shape almost every special teams report wants is the same: some categorical rows
(distance bands, field position bands, outcome types) down the left, seasons across the
top, and two or three measures under each season. `write_grid` writes exactly that, and
takes a callback so the caller decides whether a cell holds a number computed in Python
or an Excel formula that recalculates when the reader changes something.

Formulas can carry a cached value. Excel recalculates on open and ignores it, but every
other viewer -- Preview, Google Sheets' import, a quick pandas read -- shows the cached
number instead of a blank, so a formula-driven sheet is not dead on arrival.
"""
from __future__ import annotations

import datetime as dt
from typing import NamedTuple

from xlsxwriter.utility import xl_col_to_name

# Pulled off app.py so the workbook and the console read as one product.
ACCENT = "#3b7dd8"
INK = "#1f2933"
MUTED = "#6b7785"
RULE = "#c9d2dc"
ZEBRA = "#f4f7fb"
GOOD, MID, BAD = "#b7e1cd", "#fce8b2", "#f4c7c3"

MEASURES = ("Att", "Made", "Pct")


class F(NamedTuple):
    """An Excel formula plus the value it evaluates to for the default selection."""
    formula: str
    value: float | str | None = None


class Style:
    """Every format the reports use, built once per workbook."""

    def __init__(self, wb):
        base = {"font_name": "Calibri", "font_size": 10, "font_color": INK}
        mk = lambda **kw: wb.add_format({**base, **kw})

        self.title = mk(font_size=15, bold=True)
        self.subtitle = mk(font_size=9.5, font_color=MUTED, italic=True)
        self.note = mk(font_size=10, text_wrap=True, valign="top")
        self.note_head = mk(font_size=11, bold=True)

        self.period = mk(bold=True, font_color="#ffffff", bg_color=ACCENT,
                         align="center", valign="vcenter", border=1, border_color=ACCENT)
        self.period_all = mk(bold=True, font_color="#ffffff", bg_color=INK,
                             align="center", valign="vcenter", border=1, border_color=INK)
        self.measure = mk(bold=True, font_size=9, align="center", bg_color=ZEBRA,
                          bottom=1, bottom_color=RULE, font_color=MUTED)

        self.corner = mk(bold=True, font_size=9, bg_color=ZEBRA, bottom=1,
                         bottom_color=RULE, font_color=MUTED)
        self.band = mk(bold=True, indent=1)
        self.band_alt = mk(bold=True, indent=1, bg_color=ZEBRA)

        # Attempts blank out at zero: no attempts in a band is nothing to report, and a
        # season a team spent in FCS should not read as a season it kicked nothing. Makes
        # keep their zero -- 0 for 3 from 50 is a real and different statement.
        self.num_att = mk(num_format='#,##0;-#,##0;""', align="right")
        self.num_att_alt = mk(num_format='#,##0;-#,##0;""', align="right", bg_color=ZEBRA)
        self.num = mk(num_format="#,##0", align="right")
        self.num_alt = mk(num_format="#,##0", align="right", bg_color=ZEBRA)
        self.pct = mk(num_format="0.0%", align="right")
        self.pct_alt = mk(num_format="0.0%", align="right", bg_color=ZEBRA)

        self.total_band = mk(bold=True, indent=1, top=2, top_color=INK)
        self.total_num = mk(num_format="#,##0", align="right", bold=True, top=2, top_color=INK)
        self.total_num_att = mk(num_format='#,##0;-#,##0;""', align="right", bold=True,
                                top=2, top_color=INK)
        self.total_pct = mk(num_format="0.0%", align="right", bold=True, top=2, top_color=INK)

        self.picker_label = mk(bold=True, font_color=MUTED, align="right")
        self.picker = mk(bold=True, font_size=12, bg_color="#fff8e1", border=1,
                         border_color="#e8a33d", align="center")

    def body(self, measure: str, *, zebra: bool, total: bool):
        if total:
            return {"Pct": self.total_pct, "Att": self.total_num_att}.get(
                measure, self.total_num)
        if measure == "Pct":
            return self.pct_alt if zebra else self.pct
        if measure == "Att":
            return self.num_att_alt if zebra else self.num_att
        return self.num_alt if zebra else self.num


def heading(ws, style, title: str, subtitle: str, width: int) -> None:
    ws.merge_range(0, 0, 0, width - 1, title, style.title)
    ws.merge_range(1, 0, 1, width - 1, subtitle, style.subtitle)


def write_grid(ws, style, top: int, periods: list, rows: list[tuple], cell,
               *, label_header="Distance", total_label="All distances",
               all_periods_label="All") -> int:
    """Write a `rows` x `periods` x (Att, Made, Pct) grid starting at row `top`.

    `rows` is a list of (key, label). `cell(key, period, measure, r, c)` returns the
    content for one cell: a number, None for blank, or an `F` for a formula. `key` is None
    on the totals row and `period` is None in the totals column. It gets the cell's own
    (r, c) so a measure can refer to its neighbours. Returns the row after the grid.
    """
    groups = list(periods) + [None]
    ws.write(top, 0, "", style.corner)
    ws.write(top + 1, 0, label_header, style.corner)
    for gi, period in enumerate(groups):
        c0 = 1 + gi * len(MEASURES)
        fmt = style.period if period is not None else style.period_all
        ws.merge_range(top, c0, top, c0 + len(MEASURES) - 1,
                       str(period) if period is not None else all_periods_label, fmt)
        for mi, measure in enumerate(MEASURES):
            ws.write(top + 1, c0 + mi, measure, style.measure)

    first = top + 2
    for ri, (key, label) in enumerate(list(rows) + [(None, total_label)]):
        r = first + ri
        total = key is None
        zebra = ri % 2 == 1
        ws.write(r, 0, label,
                 style.total_band if total else (style.band_alt if zebra else style.band))
        for gi, period in enumerate(groups):
            for mi, measure in enumerate(MEASURES):
                c = 1 + gi * len(MEASURES) + mi
                fmt = style.body(measure, zebra=zebra, total=total)
                content = cell(key, period, measure, r, c)
                if isinstance(content, F):
                    ws.write_formula(r, c, content.formula, fmt,
                                     "" if content.value is None else content.value)
                elif content is None:
                    ws.write_blank(r, c, None, fmt)
                else:
                    ws.write(r, c, content, fmt)

    last = first + len(rows)          # the totals row
    ws.set_column(0, 0, 15)
    for gi in range(len(groups)):
        c0 = 1 + gi * len(MEASURES)
        ws.set_column(c0, c0 + 1, 7.5)
        ws.set_column(c0 + 2, c0 + 2, 8.5)
        ws.conditional_format(first, c0 + 2, last - 1, c0 + 2, {
            "type": "3_color_scale",
            "min_type": "num", "min_value": 0, "min_color": BAD,
            "mid_type": "num", "mid_value": 0.5, "mid_color": MID,
            "max_type": "num", "max_value": 1, "max_color": GOOD,
        })
    ws.freeze_panes(first, 1)
    return last + 1


def col(idx: int) -> str:
    return xl_col_to_name(idx)


def abs_range(sheet: str, c: int, r0: int, r1: int) -> str:
    """$-anchored single-column range, e.g. _data!$D$2:$D$9000."""
    letter = xl_col_to_name(c)
    return f"{sheet}!${letter}${r0 + 1}:${letter}${r1 + 1}"


def write_notes(ws, style, title: str, sections: list[tuple[str, list[str]]]) -> None:
    ws.set_column(0, 0, 110)
    ws.write(0, 0, title, style.title)
    r = 2
    for head, lines in sections:
        ws.write(r, 0, head, style.note_head)
        r += 1
        for line in lines:
            ws.write(r, 0, line, style.note)
            r += 1
        r += 1
    ws.hide_gridlines(2)


def stamp(db_path: str, mtime: float) -> str:
    built = dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    return f"Snapshot {db_path} built {built}"
