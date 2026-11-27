# Local Operation Records

The III CLI and the configuration system keep local operation records: the
retained plan and state of each mutating `iii` command, and the configuration
reconciliation and tuning journals. They are operator-owned working state on
the machine that ran the operation. They are never committed to Git and are not
a backup.

## Location

The CLI keeps operation plans and states in the first of:

1. `$III_OPERATION_STATE_DIR`;
2. `$III_REGISTRY_ROOT/operations`;
3. `$WORKSPACE_DIR/.iii/operations` in a sourced workspace shell;
4. `$XDG_STATE_HOME/iii/operations`, or `~/.local/state/iii/operations`.

Configuration operations use `$III_OPERATIONS_ROOT`, otherwise
`$WORKSPACE_DIR/.iii/operations`, otherwise `~/.local/state/iii/operations`.
The workspace `.iii/` directory is ignored by Git.

The ground-control companion mirrors the aircraft's configuration journals into
`$WORKSPACE_DIR/.iii/operations/<session-id>` (or its `--operations-root`) and
keeps its own status and mirror cursor under `$XDG_STATE_HOME/iii/gc`, by
default `~/.local/state/iii/gc`.

## Retention

Nothing prunes operation records automatically. Delete a record directory only
when no retained operation, configuration session, or mirror still refers to
it.
