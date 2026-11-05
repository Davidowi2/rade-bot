# 09 — Operations

## Runtime
- Single long-running process with a scheduler loop.
- Auto-restart via OS service / process manager (Windows: Task Scheduler or
  NSSM; or run in terminal during dev).
- State persisted to DB so restarts are safe.

## Logging
- Structured (JSON) logs to ./logs/, rotated daily.
- Every decision logged: inputs, features, probe result, risk gate, order.

## Monitoring
- Daily PnL + drawdown report (console + file).
- Heartbeat file updated each loop; external watcher can alert on staleness.
- Kill switch via flag file (`./KILL`).

## Security
- Secrets in .env only; never in code or logs.
- The file `Heres the bybit api key ixzEZtiSq4T.txt` in the parent Documents
  folder holds live credentials and sits in a synced (OneDrive) folder.
  Action before Phase 5: move to .env, add to .gitignore (done), rotate keys
  if the file was ever shared/synced to a shared drive. Bybit keys are unused
  in v1 (crypto deferred), but still rotate if leaked.

## Backup
- DB backup daily to a separate folder.
- Never back up .env.

## Open
- Alerting channel (Telegram bot to lead? email?).
