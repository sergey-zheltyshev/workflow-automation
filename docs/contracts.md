# Data and behavior contracts

## Durable phases

| State | Entered when | Allowed continuation |
|---|---|---|
| running | A bounded READ batch starts | Persist full records or mark read failure |
| awaiting_semantic | Read phase completes | Review current revisions and construct a valid action queue |
| apply_running | Evidence-gated queue is non-empty | Execute through the gateway and reconcile |
| completed | Apply phase reconciles without a stopping condition | Inspect durable outcome |
| needs_reconciliation | Read/apply has an uncertain or failed outcome | Gather evidence and resolve state before retrying |

Candidate configuration includes an account binding, profile version, job policy and automation policy. A decision retains its vacancy revision, source run, reason, exact source fragments and verified candidate fact keys.

## Key invariants

- A missing full source revision blocks semantic work creation.
- A hard rejection cannot be overridden by a semantic score.
- An application decision with unresolved candidate facts is invalid.
- Unknown page capabilities do not become permission to mutate a page.
- Browser state is reconciled against durable intent and observed result.

The public scenario exercises local orchestration. Browser adapters remain a separately configured integration, with their own account, site and current UI contracts. Synthetic page fixtures test parsing contracts; they do not assert that a third-party website will never change.

## Operational separation

The default demo and test commands require no applicant account. Live adapters are deliberately outside the demonstration path. Runtime databases, browser profiles, outgoing messages and captured account material are ignored by Git.
