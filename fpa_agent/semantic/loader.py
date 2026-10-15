"""
Semantic-layer admin: move the catalogue between YAML (git-reviewed), Excel
(how finance teams like to fill it) and BigQuery (what production reads).

    push   YAML dir -> BigQuery   (validated first; refuses on any error)
    pull   BigQuery -> YAML dir   (snapshot for review / diff / rollback)
    excel  workbook -> YAML dir   (one sheet per table, header row = columns)
    ddl    print the CREATE TABLE statements

Every push writes a change-log row with the catalog version hash, so any
number in an audit log can be tied back to the definitions that produced it.
"""
from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

import yaml

from fpa_agent.semantic.repository import TABLES, BigQueryRepository, YamlRepository, _build_catalog
from fpa_agent.semantic.schema import RENAMES, TABLE_SCHEMAS
from fpa_agent.semantic.validator import validate_catalog


def _to_bq_row(table: str, row: dict) -> dict:
    cols = {c for c, _, _ in TABLE_SCHEMAS[table]}
    ren = RENAMES.get(table, {})
    out = {}
    for k, v in row.items():
        k = ren.get(k, k)
        if k not in cols or v is None:
            continue
        typ = next(t for c, t, _ in TABLE_SCHEMAS[table] if c == k)
        if typ == "STRING" and isinstance(v, (dict, list)):
            if isinstance(v, list) and all(isinstance(x, str) for x in v) and k in (
                    "aliases", "values_hint", "allowed_tables", "tags", "related_metrics", "group_by"):
                v = ", ".join(v)
            else:
                v = json.dumps(v)
        out[k] = v
    return out


def push(yaml_dir: str, project: str, dataset: str, prefix: str = "sl_", dry_run: bool = False) -> dict:
    raw = YamlRepository(yaml_dir).load_raw()
    catalog = _build_catalog(raw)
    errors = [i for i in validate_catalog(catalog) if i.level == "error"]
    if errors:
        raise SystemExit("refusing to push — fix these first:\n" + "\n".join(map(str, errors)))
    rows = {t: [_to_bq_row(t, r) for r in raw.get(t, [])] for t in TABLES}
    if dry_run:
        return {"version": catalog.version, "rows": {t: len(v) for t, v in rows.items()}}
    from google.cloud import bigquery
    client = bigquery.Client(project=project)
    for t, data in rows.items():
        schema = [bigquery.SchemaField(c, typ) for c, typ, _ in TABLE_SCHEMAS[t]]
        job = client.load_table_from_json(
            data, f"{project}.{dataset}.{prefix}{t}",
            job_config=bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE"))
        job.result()
    client.insert_rows_json(f"{project}.{dataset}.{prefix}change_log", [{
        "changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "changed_by": getpass.getuser(),
        "catalog_version": catalog.version, "tables": ",".join(rows),
        "summary": json.dumps({t: len(v) for t, v in rows.items()}),
    }])
    return {"version": catalog.version, "rows": {t: len(v) for t, v in rows.items()}}


def pull(project: str, dataset: str, out_dir: str, prefix: str = "sl_") -> dict:
    raw = BigQueryRepository(project, dataset, prefix).load_raw()
    return dump_yaml(raw, out_dir)


def dump_yaml(raw: dict, out_dir: str) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for t, rows in raw.items():
        data = rows[0] if t == "client_profile" and rows else rows
        (out / f"{t}.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        counts[t] = len(rows)
    return counts


def from_excel(xlsx_path: str, out_dir: str) -> dict:
    """Workbook with one sheet per table (sheet name = table name). JSON
    columns may be typed as JSON text in the cell."""
    import pandas as pd  # optional dependency (pip install pandas openpyxl)
    sheets = pd.read_excel(xlsx_path, sheet_name=None, dtype=object)
    raw: dict[str, list[dict]] = {t: [] for t in TABLES}
    for name, df in sheets.items():
        t = name.strip().lower()
        if t not in TABLES:
            continue
        back = {bq: f for f, bq in RENAMES.get(t, {}).items()}
        df = df.where(df.notna(), None)
        raw[t] = [{back.get(k, k): v for k, v in rec.items() if v is not None} for rec in df.to_dict("records")]
    _build_catalog(raw)  # parse check (raises on malformed rows)
    return dump_yaml(raw, out_dir)
