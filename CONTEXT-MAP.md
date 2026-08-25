# III-Drone Context Map

The workspace has two bounded-context documents:

- [`CONTEXT.md`](CONTEXT.md): workspace integration, runtime ownership,
  package boundaries, operator-control language, and architecture decisions.
- [`deployment/CONTEXT.md`](deployment/CONTEXT.md): release, activation,
  persistent configuration, recovery, evidence, and deployment cutover language.

Operational and subsystem detail is indexed by [`docs/README.md`](docs/README.md).
The field inspection operator workflow is authoritative in
[`docs/field-inspection-operations.md`](docs/field-inspection-operations.md).

Add a package-local `CONTEXT.md` here only when that bounded context has its own
stable language and decisions; list it in this map in the same change.
