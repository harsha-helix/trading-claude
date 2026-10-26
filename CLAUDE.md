# trading-claude: paper trading on NSE (runbook for Claude)

Paper trading only. No real orders, no broker connection. Claude proposes trades
and the owner approves them on the dashboard. Spec: README.md. Settings: config.json.

## Daily run (Mon–Fri, after about 6 PM IST)
1. `pip install -r requirements.txt`
2. Sync approvals: read the dashboard's `approvals` collection with ArtifactData (dashboard URL
   is in `state/dashboard_url.txt`). For each doc, run
   `python -m papertrade approve <id> [--reject] --by owner --at <doc.at>`.
   Decisions timestamped after 09:15 IST on the fill day are ignored by the engine, which is intended.
3. `python -m papertrade update-data` (fundamentals are refreshed weekly per symbol)
4. `python -m papertrade run`. This fills approved orders at the next open, applies stops and
   targets, marks to market, and writes new proposals to `state/proposals.json`.
5. **News check:** for each new pending proposal, web-search the company for results dates,
   corporate actions, regulatory or legal news, and promoter pledges or selling. Record the result with
   `python -m papertrade note <id> "<1–2 lines>"`. If something is a clear red flag (results in the
   next 2 sessions, fraud or regulatory action, a pending large block deal), write `VETO:` at the
   start of the note and do not recommend it.
6. `python -m papertrade report`, then republish the dashboard with `reports/dashboard.json`.
7. Commit `state/`, `reports/` and `data/` (excluding large caches; see .gitignore) to `main`,
   then push.

## Weekly review (Saturday)
Write `reports/weekly/YYYY-MM-DD.md` covering performance against the NIFTY 500, win rate,
expectancy in R, drawdown, charges and estimated tax, which setups worked, rule
violations, the go-live checklist, and at most one or two proposed strategy changes. Strategy changes are
proposals for the owner. Never change `config.json` or `strategy.PARAMS` without their OK.

## Rules
- Never edit past entries in the ledger or `state/*.json` by hand. Corrections go in as new events.
- Never use data after the as-of date when generating proposals (no lookahead).
- Proposals from the Oct 1–26 backfill were auto-approved by Claude (labelled
  `claude (backfill)`) and had no news check, because news can't be checked without hindsight.
