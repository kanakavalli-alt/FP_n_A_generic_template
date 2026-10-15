# Onboarding a new client

Goal: from kickoff to a reconciled, deployed agent in about a week, with **no code changes**
unless the client needs a decomposition shape the registry doesn't have yet.

## Day 0 — discovery checklist (with the client's FP&A lead)

- [ ] The 5–10 questions FP&A answers every cycle (these become analyses + e2e tests).
- [ ] For each figure: the system it comes from, the table, the **exact** filters, and who signs
      the definition off (owner). Capture their existing report SQL — it becomes `reference_sql`.
- [ ] Fiscal calendar (start month, naming), close lag, default comparison, budget source.
- [ ] Dimensions they slice by and the canonical category values.
- [ ] 10–20 figures they already trust (P&L lines, board-pack numbers) for reconciliation.
- [ ] Network path to each database (VPN / private connectivity / tunnel) and a read-only DB user.

## Day 1 — connectivity

1. For each source, deploy `mcp_server/` next to the database:
   ```bash
   docker build -f mcp_server/Dockerfile --build-arg DB_DRIVER_PIP=pyodbc -t fpa-mcp-erp .
   ```
   Env: `SOURCE_ID`, `DIALECT`, `DB_DRIVER`, `DB_CONN_SECRET` (Secret Manager), `ALLOWED_TABLES`,
   `MCP_EXPECTED_AUDIENCE`, `MCP_EXPECTED_CALLER_EMAIL`.
2. `python mcp_server/server.py --test-connection` on the host, then `ping` through the tunnel.
3. Create the agent service account, store its key in Secret Manager (`AGENT_SA_KEY_SECRET`).

## Day 2 — semantic layer

1. `cp -r semantic_layer/client_template semantic_layer/<client>` and fill the eight files
   (or have finance fill the Excel workbook and run `python -m fpa_agent catalog excel`).
2. Iterate until clean:
   ```bash
   python -m fpa_agent --catalog-dir semantic_layer/<client> validate --live
   ```
   `--live` checks every referenced column exists in the source schema.
3. Create the BigQuery tables (`semantic_layer/bigquery/ddl.sql`) and push:
   ```bash
   python -m fpa_agent catalog push --dir semantic_layer/<client> --project P --dataset fpa_semantic_layer
   ```
   Push refuses on validation errors and writes `sl_change_log` with the catalogue version.

Rules of thumb
* One metric = one number finance would sign. Narrow confirmed definitions get their own row;
  put the broad colloquial word in `aliases` only if it truly means the same thing.
* `definition_status` is the gate: only `confirmed`/`partial` produce numbers. Don't promote a row
  to `confirmed` until its reconciliation test is green.
* Put ratios and per-unit figures in `formula` (derived) — never pre-divided in a measure — so
  breakdowns stay ratio-of-sums.
* Snapshot balances (headcount, census, AR) use `time_semantics: snapshot`.
* Name drivers in the client's words with `params.labels`.

## Days 3–4 — analyses and reconciliation

1. Map each recurring "why" question to a method (see ARCHITECTURE / analyses template).
2. Add a reconciliation test per trusted figure and per analysis headline.
3. Run until 100 % green:
   ```bash
   python -m fpa_agent --catalog-dir semantic_layer/<client> reconcile --auto-months 3 --out out/recon
   ```
   Hand `out/recon/reconciliation.md` to the client as evidence. A failure means a definition is
   wrong (filter, column, sign, period) — fix the row, not the code.

## Day 5 — deploy and test end to end

```bash
python deploy/deploy_agent_engine.py --project P --location us-central1 --staging-bucket gs://B --display-name "<Client> FP&A Agent"
```
Set `SEMANTIC_BACKEND=bigquery`, `BQ_PROJECT`, `BQ_DATASET`, the `*_MCP_URL` vars,
`AGENT_SA_KEY_SECRET`, `TURN_LOG_GCS_BUCKET`. Register the engine in Gemini Enterprise.
Then run the question suite against the deployed config:
```bash
python -m fpa_agent eval
```

## Day 6 — UAT, handover

* UAT testers ask their own questions; every answer is audited in `TURN_LOG_GCS_BUCKET`
  (outcome, number → evidence → SQL). Blocked answers point at catalogue gaps.
* Hand over: this repo, the catalogue, the reconciliation report, and the operating notes:
  *edit a definition → validate → reconcile → push*.

## Operating the catalogue after go-live

| Change | Steps |
|---|---|
| Fix a definition | edit row → `validate --live` → `reconcile --tags <area>` → `catalog push` |
| Add a metric | row + reconciliation test → same as above |
| New "why" question | analysis row + e2e question test |
| Source schema change | `validate --live` flags missing columns immediately |
