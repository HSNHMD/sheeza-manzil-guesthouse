# 0004 — Staging-first workflow

**Status:** Accepted (2026-Q1)

## Context

Production is a working business — every minute of downtime is real
money + reputational damage. Mistakes that touch real bookings,
real money, real WhatsApp threads with real guests are not
recoverable in the same way that bugs in code are.

Yet the team needs to ship features fast. Many features are
exploratory ("does this UX work? does this OTA pipeline make
sense?"); many features need real environments to validate.

## Decision

Every sprint lands on **staging only** by default. Production is
behind a separate, manual, gated deploy step (see
`docs/runbooks/production_deploy.md`).

Concretely:
- A separate `feature/reservation-board` branch carries all
  in-flight work. `main` is reserved for production-deployed code.
- Staging deploys are routine: `git pull`, `flask db upgrade`,
  `systemctl restart guesthouse-staging.service`. Anyone with VPS
  access can do them.
- Production deploys are not: pre-flight backup, maintenance
  window, manual migration step, smoke checks, rollback plan
  rehearsed.
- Every sprint's report ends with the literal phrase **"production
  untouched: confirmed."** That phrase corresponds to a
  `systemctl show sheeza.service -p ActiveEnterTimestamp --value`
  comparison.

## Consequences

**Easier:**
- Low-risk experimentation. Anyone can try a new feature on
  staging without endangering live data.
- Clear branding signal — staging shows the orange ribbon and a
  different brand name; you can't mistake it for production.
- The handoff story (this doc set) is honest — staging vs
  production state is always an `ssh` away from being verified.

**Harder:**
- The gap between `main` and `feature/reservation-board` grows
  the longer this pattern runs. We're at 48 commits ahead today.
  See `docs/handoff/next_steps.md` sprint 1.
- Every "production-merge spike" needs to bring forward many
  migrations at once, which is more careful work than continuous
  deployment would be.
- New developers may assume any URL they see is production unless
  the branding makes it impossible to confuse.

## Alternatives considered

- **Continuous deployment to production with feature flags.**
  Rejected for V1 — the team is small, the platform is young, and
  the cost of a bad merge to production is high. Revisit when the
  team is bigger or when GrowthBook-style flagging is in place.
- **Blue/green deployment.** Rejected as over-engineering for a
  single small VPS.
