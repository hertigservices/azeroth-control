# Source reconciliation — 2026-09-10

The canonical code repositories are Azeroth Control and Ascension Preservation.
The running installation is a deployment, not a second development checkout.

Azeroth Control combines runtime process-path scoping, unified status, item browsing,
and module lifecycle controls with the public repository's configurable MySQL
host/port/credentials and portable interpreter discovery. Machine-specific branding,
paths, credentials and reference data are not copied into source.

Preservation retains the public bridge and world server: their runtime differences
were publication cleanup rather than missing features. The existing CoA helper and
optional in-process key-reader source are retained. Stock-client generators and four
authored shim modules remain experimental; their presence does not establish client
compatibility. Generated stock-client datasets remain in the local preservation archive.
At final remote reconciliation, published commits 877651e and 0365bbd promoted
the reviewed AuthGate package to the default. Those commits and their attribution
were merged intact; the current authentication guide takes precedence over the
initial backup snapshot. Consolidation did not reinstall or modify the client.

Before consolidation, all four repositories and the AuthGate worktree were backed up
as complete Git bundles plus tracked/untracked source snapshots. Private reports retain
per-file comparisons and all original paths. No runtime state or game assets were
merged into these source changes.
