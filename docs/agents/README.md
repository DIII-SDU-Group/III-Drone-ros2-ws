# Agent Documentation Index

The workspace root carries two parallel agent instruction files: `AGENTS.md`
(Codex, including its subagent/backlog orchestration) and `CLAUDE.md` (Claude
Code, same workspace policy without the Codex-specific orchestration). Shared
policy (runtime model, build/test, dependency governance, submodule edit
policy, HIL and deployment boundaries) must be changed in both files together.
This directory only supplies the small project-specific routing contracts
referenced from them:

- [Issue tracker](issue-tracker.md) defines where issues and PRDs live.
- [Triage labels](triage-labels.md) defines the shared issue-state vocabulary.
- [Domain documentation](domain.md) explains how `CONTEXT-MAP.md` routes agents
  to bounded-context language.
- [Editable repository workflow](editable-repositories.md) defines safe edit,
  test, branch, PR, and external-mutation boundaries for every owned submodule.

Operational commands belong in the [documentation index](../README.md) and must
follow the [automation-ready authoring contract](../automation-ready-authoring-contract.md).
Repository, PR, release, and external-mutation policy remains in root
`AGENTS.md`/`CLAUDE.md` and [dependency governance](../dependency-governance.md).
