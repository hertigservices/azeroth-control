# CLAUDE.md

The install and contribution instructions for this repo live in **[AGENTS.md](AGENTS.md)**.
Read that file before running anything.

Quick orientation:

- **What this is** — a local control panel and launcher for an AzerothCore 3.3.5a server.
  Pure Python standard library, no dependencies. See [README.md](README.md).
- **Installing it** — [AGENTS.md](AGENTS.md), which is phased and has a verification step
  at every checkpoint. Do not skip the verifications; several of the failure modes exit zero.
- **The version pins are load-bearing.** CMake 3.31 (not 4.x), OpenSSL 3.5.8 (not 3.6+).
  They look like downgrades and are not. AGENTS.md §0 explains each.
- **Debugging an error** — [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) is indexed by
  verbatim symptom. Check it before improvising.

If you are changing code rather than installing:

- `control.py` owns what is safe to edit (`SETTABLE`, `RATES`); `panel.py` rebases those tables
  onto the selected realm rather than re-listing them. Add a key in one place, not two.
- Keep the no-third-party-dependency rule. It is why this installs in one step.
- Never commit `realms/profiles.json`, `credentials.txt`, `control/soap.json`, or `refdata*.json`.
