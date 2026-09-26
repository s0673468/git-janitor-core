# Export boundary

This source distribution retains the scanner, classification and reconciliation logic,
explicit execution engine, branch preservation/deletion proofs, PR delivery
gates, receipt integrity code, and their synthetic tests. Account identifiers
and machine paths in fixtures have been replaced with invented examples.

Excluded components:

- The original personal `config.toml`, repository inventory, and generated reports.
- The original Git history, owner-specific instructions, and assistant skills.
- Machine deployment scripts, LaunchAgents, background service installers, and
  their deployment-only tests.
- The private deep-verification workflow and external watchdog consumer test;
  in-package receipt producer and malformed/delayed receipt tests remain.

The export does not replace its absent private components with successful
stubs. Optional integrations require explicit operator configuration. Mutation
flags and category allowlists remain disabled in the example configuration.

The source retains its existing licensing status; this export adds no license
grant. Third-party dependencies retain their own licenses. No private runtime
content or original Git history is part of this distribution.
