# Documentation index

> **Last updated:** 2026-05-03.
> If you are new to this codebase or returning after a context loss,
> read the four files in **Recovery (read in order)** below first.

## Recovery (read in order)

1. [`handoff/current_state.md`](handoff/current_state.md) — what is
   true today: branches, deployed shas, modules, environments.
2. [`handoff/next_steps.md`](handoff/next_steps.md) — what should
   ship next, what's blocked.
3. [`handoff/known_bugs.md`](handoff/known_bugs.md) — the open
   issue list.
4. [`handoff/staging_urls.md`](handoff/staging_urls.md) — what to
   click on staging to verify anything.

## Runbooks (procedures you may need to run)

- [`runbooks/staging_setup.md`](runbooks/staging_setup.md) — the
  staging environment, top-to-bottom.
- [`runbooks/production_deploy.md`](runbooks/production_deploy.md) —
  how to deploy to production.
- [`runbooks/pre_deploy_checklist.md`](runbooks/pre_deploy_checklist.md)
- [`runbooks/post_deploy_verification.md`](runbooks/post_deploy_verification.md)
- [`runbooks/rollback.md`](runbooks/rollback.md) — the recovery story.
- [`runbooks/backup_restore.md`](runbooks/backup_restore.md) — DB,
  uploads, source.
- [`runbooks/domain_dns_ssl.md`](runbooks/domain_dns_ssl.md) —
  domain + cert hygiene.

## Architecture (explanations)

- [`architecture/system_overview.md`](architecture/system_overview.md)
- [`architecture/module_map.md`](architecture/module_map.md)
- [`architecture/data_model.md`](architecture/data_model.md)
- [`architecture/integrations.md`](architecture/integrations.md)

## Decisions (ADRs)

- [`decisions/README.md`](decisions/README.md) — format + index.
- 6 initial ADRs for the foundation choices.

## Roadmap

- [`roadmap/master_product_roadmap.md`](roadmap/master_product_roadmap.md)

## Prompt library

- [`prompts/pai_build_prompts.md`](prompts/pai_build_prompts.md) —
  reusable build / sprint patterns.
- [`prompts/repeatable_operator_prompts.md`](prompts/repeatable_operator_prompts.md) —
  reusable backup / deploy / rollback / verification prompts.

## Background design docs (pre-existing, kept in place)

These pre-existing docs cover deeper material referenced from the
new structure above:

- [`accounts_business_date_night_audit_plan.md`](accounts_business_date_night_audit_plan.md)
- [`admin_dashboard_plan.md`](admin_dashboard_plan.md)
- [`channel_manager_architecture.md`](channel_manager_architecture.md)
- [`channel_manager_build_phases.md`](channel_manager_build_phases.md)
- [`channel_manager_risk_checklist.md`](channel_manager_risk_checklist.md)
- [`guest_folio_accounting_pos_roadmap.md`](guest_folio_accounting_pos_roadmap.md)
- [`multi_property_access_model.md`](multi_property_access_model.md)
- [`multi_property_foundation_plan.md`](multi_property_foundation_plan.md)
- [`multi_property_migration_strategy.md`](multi_property_migration_strategy.md)

## How to keep this index honest

- New runbook → add a link under "Runbooks" + a one-line description.
- New ADR → add a link in `decisions/README.md`.
- Sprint that changes the deploy story → update both
  `current_state.md` AND `production_deploy.md`.
- Anything that becomes wrong over time → fix it instead of leaving
  it.
