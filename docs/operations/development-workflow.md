# Development workflow

Use the shared default branch as the integration baseline. Create a short-lived topic branch for each change, and merge it only after review and the required offline checks pass.

```powershell
git switch main
git pull --ff-only
git switch -c codex/<topic>
```

Keep independent changes in separate branches or commits. A change should document the behavior, its scope and assumptions, and the evidence used to validate it. Where work is handed between a local checkout and another environment, record the branch, commit, changed files, validation performed, and any uncommitted work.

Run the offline test suite before merge:

```powershell
python -B -m unittest discover -s tests -v
git diff --check
```

Live instrument checks are separate, explicitly authorized acceptance work. Never infer that an operation is safe or complete from a successful fake test. Keep site-specific configuration, measurements, and unreviewed instrument logs out of source control.

Research literature and laboratory observations have different redistribution terms. Include a paper or dataset only after checking its license and sharing permission. When source data remain private, keep the source out of the repository and state that a historical reference cannot be reproduced from this checkout.
