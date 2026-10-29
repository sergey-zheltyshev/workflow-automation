# Workflow Automation — case study

## The problem

Browser workflows outlive individual sessions. A timeout may happen before submission, after submission or during verification; blindly retrying the same step can duplicate an external action.

## The challenge

Separate observation from decision and execution, bind decisions to current source revisions, and recover from uncertain outcomes without treating an old browser session or report as business truth.

## The solution

Persist process state in SQLite. Use bounded READ → SEMANTIC → APPLY/RECONCILE phases, validate decision evidence before building an action queue, and route unknown outcomes into reconciliation. The implementation applies this design to recruitment operations.

## Decisions that shaped the system

**SQLite owns the handoff.** A browser is a temporary executor. Durable state lets a later process resume without reconstructing decisions from chat history.

**Decision requires current evidence.** Source revision, profile/policy versions, exact vacancy fragments and verified candidate facts travel together. A score alone never authorizes an external action.

**Unknown outcome is a distinct state.** An uncertain submit does not become a failed submit eligible for a blind retry. The process enters reconciliation and waits for observable proof.

**Transactions and resources are explicit.** SQLite scopes commit or roll back atomically and explicitly close connections. This matters for repeatable cleanup and recovery on Windows as well as Linux.

## The result

The public offline scenario proves a durable review handoff, rejects execution with an empty approved queue and restores the same phase through a new orchestrator instance. The repository also includes contract tests for application queues, gateway behavior and candidate isolation.

## Evidence to inspect

Run the README demonstration, inspect its generated output, then follow the [requirements and verification matrix](requirements.md) to the tests. [Contracts](contracts.md) describe the meanings and boundaries of the output.

## Contribution

Problem decomposition, system modeling, integration design, implementation and verification are presented together. Public-release engineering includes synthetic fixtures, portable configuration and executable checks. Commercial metrics and third-party service behavior are not inferred from a local demonstration.

[Return to overview](../README.md)
