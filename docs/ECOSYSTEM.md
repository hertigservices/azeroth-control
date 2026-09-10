# Ecosystem and source ownership

Azeroth Control is the canonical source for the multi-realm panel, launcher,
installation tools, status and module controls. Ascension-specific reconstruction
and standalone import/cache commands live in
[Ascension Preservation](https://github.com/hertigservices/Ascension_preservation).
See its [unified setup guide](https://github.com/hertigservices/Ascension_preservation/blob/main/docs/SETUP.md).

Set AZCTL_HOME to use an existing hub from a source checkout. Machine-specific
profiles, SOAP settings, installed-addon state, reference data and module ledgers
stay in the hub. `manifests/deployment.json` lists the files this source owns.

Use tools/deploy.py to create a hash-checked plan, apply it, verify its receipt or
roll back. It preserves unlisted files and makes no process or database changes.
The code root remains independent of the installation root.
