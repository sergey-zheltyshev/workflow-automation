# Requirements and verification

| ID | Requirement | Executable verification |
|---|---|---|
| WA-01 | Durably bind actions to the active account and current context | `test_import_is_durable_and_idempotent` |
| WA-02 | Enforce candidate isolation and evidence gating | `test_candidate_b_isolation_and_evidence_gate_blocks_apply` |
| WA-03 | Reject semantic review before a full source revision exists | `test_semantic_apply_resolves_unknown_and_builds_system_queue` |
| WA-04 | Recover batch progress without duplicating the work | `test_semantic_batch_start_next_commit_resume_finish` |
| WA-05 | Keep reader capabilities separate from mutation interfaces | `test_reader_public_methods_have_no_action_or_submit_surface` |

## Acceptance walkthrough

1. Install the dependencies specified in the README.
2. Run the demonstration with no private configuration.
3. Inspect the generated output against the documented data contract.
4. Run the automated test command.
5. Confirm malformed, conflicting or out-of-order inputs follow the documented behavior.

Tests exercise the implementation and its failure boundaries. Synthetic fixtures are clearly separated from operational data. See the repository's CI workflow for the repeatable validation command.
