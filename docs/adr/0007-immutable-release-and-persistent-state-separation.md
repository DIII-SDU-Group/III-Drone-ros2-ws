# Immutable Release And Persistent State Separation

## Decision

GC and drone releases are immutable, content-identified, signed, installed side by
side, and activated through atomic root-owned selectors. Application release slots
never own or overwrite aircraft configuration, calibration, tuning journals,
captures, credentials, deployment state, or retained diagnostics.

Activation binds code, a compatible configuration checkpoint, and the installed
mission catalog as one receiver-owned transaction. The receiver persists every
irreversible boundary, evaluates health with monotonic deadlines, and restores the
last accepted compatible pair after an interrupted or failed candidate. After
durable acceptance, later faults do not silently switch software versions.

## Consequences

Normal releases cannot mutate host packages or stable systemd units. Configuration
reconciliation plans preserve current-schema values, seed new defaults, retire
removed values into persistent shadow state, and block reintroduction until an
explicit bound review is resolved. Rollback compatibility can be evaluated before
runtime shutdown or writable-state mutation.

