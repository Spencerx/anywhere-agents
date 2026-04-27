# Migrations

This file documents version-to-version migration steps for `anywhere-agents` consumers. Most upgrades require no action; entries below cover the cases where a one-time refresh or config change is required.

## 2026-04-26 - v0.5.0 (auth-chain bootstrap plumbing)

`bootstrap.{sh,ps1}` now sets `AGENT_CONFIG_HOST=claude-code` by default and exports `ANYWHERE_AGENTS_UPDATE` when set in the calling environment. Pre-2026-04-26 bootstrap caches must seed-refresh once to pick up these env wirings.

Action: any consumer that bootstrapped before this date should run:

```bash
bash .agent-config/bootstrap.sh
```

(or `pwsh -File .agent-config/bootstrap.ps1` on Windows)

once to refresh the bootstrap cache. After the refresh, no further action is needed; the new env wirings flow through automatically.

The default `update_policy` flipped from `locked` to `prompt`. Existing pack-lock entries are honored; the change only affects how upstream drift is surfaced for new entries. By default, drift produces a banner notice and an interactive prompt; opt-in apply via `ANYWHERE_AGENTS_UPDATE=apply` covers non-interactive runs.
