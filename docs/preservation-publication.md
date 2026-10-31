# Preservation publication

`preservation_publication.py` contains the existing crash-recoverable publication
mechanism, extracted from `safe_delete.py`. The CLI keeps its directory lock,
branch checks, saved-history checks, filenames and result format.

The caller verifies a staged bundle and matching manifest, holds the output
directory lock, and supplies both destination paths and the journal path. Staging
and journal must be on the same filesystem as the outputs. `publish_pair` flushes
both files and their directory before atomically promoting staging to the journal.
`recover_pair` publishes each output while retaining the journal originals, then
removes the completion marker only after both destination entries are durable.

Recovery runs before the caller looks up the source branch again. It also handles
process death during journal cleanup. Publication replaces destination aliases;
it does not write through them. The helper does not inspect Git, choose filenames,
allocate snapshots, acquire locks or grant cleanup authority.

The portable contract tests use uniquely named snapshots and a separately named
journal. They exercise real process death, repeated interruptions, a deleted source
branch, alias replacement and write failures. Every recovered pair is verified
and restored into an independent Git repository; earlier snapshots stay intact.
The existing CLI race and failure tests continue to exercise fixed public filenames.
