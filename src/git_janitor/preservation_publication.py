"""Durable publication of a verified preservation bundle and its manifest.

The caller holds the output directory lock and verifies the staged pair before
calling this module. Staging, journal and both outputs share that directory's
filesystem. Paths belong to the caller: a stable journal may recover either
fixed public filenames or a uniquely named snapshot without choosing new names.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil


def publish_pair(staging: Path, journal: Path, bundle: Path, manifest: Path) -> None:
    """Make both staged files durable, then publish through a recoverable journal."""
    for artifact in (staging / bundle.name, staging / manifest.name):
        with artifact.open("rb") as handle:
            os.fsync(handle.fileno())
    _sync_directory(staging)
    # One atomic rename makes BOTH candidates recoverable. A caller's temporary
    # directory cleanup only owns the old staging name after this point.
    staging.rename(journal)
    _sync_directory(bundle.parent)
    recover_pair(journal, bundle, manifest)


def recover_pair(journal: Path, bundle: Path, manifest: Path) -> None:
    """Replay publication under the directory lock, including after process death.

    Journal originals survive both renames. Only after both destination entries
    are durable do we remove the journal manifest (the completion marker), then
    clean up. A crash during cleanup can therefore also be recovered safely.
    """
    if journal.is_symlink() or not journal.is_dir():
        raise OSError(f"invalid preservation journal: {journal}")
    saved_manifest = journal / manifest.name
    if saved_manifest.exists():
        for destination in (bundle, manifest):
            source = journal / destination.name
            if source.is_symlink() or not source.is_file():
                raise OSError(f"incomplete preservation journal: {journal}")
            # Keep a durable original in the journal until BOTH outputs exist.
            # Rename replaces aliases instead of writing through them.
            pending = source.with_name(source.name + ".publish")
            pending.unlink(missing_ok=True)
            os.link(source, pending)
            pending.replace(destination)
            _sync_directory(destination.parent)
        saved_manifest.unlink()
        _sync_directory(journal)
    shutil.rmtree(journal)
    _sync_directory(bundle.parent)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
