"""Formatted Excel reports over the special teams snapshot.

Each report is a module with a `build()` that takes a DuckDB connection and writes one
.xlsx. Shared house style and grid mechanics live in `reports.workbook`, so two reports
opened side by side look like they came from the same place.

    .venv/bin/python -m reports.fg_by_distance
"""
