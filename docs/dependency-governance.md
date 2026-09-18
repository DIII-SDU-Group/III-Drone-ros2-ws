# Dependency Notes

The workspace retains `deps/submodule-lock.txt` as a useful reproducibility
reference.  It is not a deployment authorization gate: an attending developer
may deploy the editable workspace, including local changes, directly to the Pi.

Use ordinary Git review and tests when sharing changes.  Field deployment does
not require protected branches, release tags, signatures, or qualification
evidence.
