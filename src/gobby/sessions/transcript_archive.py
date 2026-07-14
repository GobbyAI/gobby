"""Transcript backup and restore utilities.

Provides gzip-based archival of session JSONL transcripts so they can be
restored if the original file is deleted (e.g. Claude CLI cleanup).

Archive path is deterministic from external_id:
    {archive_dir}/{external_id}.jsonl.gz
"""

import gzip
import logging
import shutil
import zlib
from pathlib import Path

from gobby.sessions.gzip_seek_index import write_blocked_gzip_archive

logger = logging.getLogger(__name__)

_DEFAULT_ARCHIVE_DIR = "~/.gobby/session_transcripts"


def get_archive_dir(override: str | None = None) -> Path:
    """Resolve and create the transcript archive directory.

    Args:
        override: Custom directory path. If None, uses ~/.gobby/session_transcripts.

    Returns:
        Resolved Path to the archive directory.
    """
    dir_path = Path(override).expanduser() if override else Path(_DEFAULT_ARCHIVE_DIR).expanduser()
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def backup_transcript(
    external_id: str,
    transcript_path: str,
    archive_dir: str | None = None,
) -> str | None:
    """Gzip-compress a JSONL transcript to the archive directory.

    Args:
        external_id: Session external ID (used as archive filename).
        transcript_path: Path to the source JSONL file.
        archive_dir: Override for archive directory.

    Returns:
        Archive file path on success, None on failure.
    """
    source = Path(transcript_path)
    if not source.is_file():
        logger.debug(f"Transcript source not found, skipping backup: {transcript_path}")
        return None

    dest_dir = get_archive_dir(archive_dir)
    dest = dest_dir / f"{external_id}.jsonl.gz"

    try:
        write_blocked_gzip_archive(str(source), str(dest))
        logger.debug(f"Backed up transcript {transcript_path} -> {dest}")
        return str(dest)
    except Exception as e:
        logger.warning(f"Failed to backup transcript {transcript_path}: {e}")
        return None


def restore_transcript(
    external_id: str,
    original_path: str,
    archive_dir: str | None = None,
) -> bool:
    """Decompress an archived transcript back to its original location.

    No-ops if the original file already exists.

    Args:
        external_id: Session external ID (archive filename stem).
        original_path: Path where the JSONL should be restored.
        archive_dir: Override for archive directory.

    Returns:
        True if restored, False if skipped or failed.
    """
    target = Path(original_path)
    if target.is_file():
        return False  # Original still exists, nothing to do

    dest_dir = get_archive_dir(archive_dir)
    archive = dest_dir / f"{external_id}.jsonl.gz"

    if not archive.is_file():
        logger.debug(f"No archive found for {external_id} at {archive}")
        return False

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(archive, "rb") as f_in, open(target, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        logger.info(f"Restored transcript {archive} -> {target}")
        return True
    except (EOFError, zlib.error, gzip.BadGzipFile, OSError) as e:
        logger.warning(f"Failed to restore transcript for {external_id}: {e}")
        # Clean up partial file
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return False
