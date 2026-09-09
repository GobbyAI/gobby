"""Portable atomic export of immutable Ask publication bundles."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from gobby.ask.publication import replay_publication
from gobby.utils.durable_file import exclusive_file_lock


class ExportCollisionError(FileExistsError):
    """The requested export destination already exists."""


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif path.is_dir():
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_publication(publication_root: Path, destination: Path) -> Path:
    """Verify and atomically copy a bundle without replacing any destination."""
    publication_root = publication_root.resolve()
    destination = destination.absolute()
    if destination == publication_root or destination.is_relative_to(publication_root):
        raise ValueError("export destination must be outside the publication")
    replay_publication(publication_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}-",
            suffix=".tmp",
            dir=destination.parent,
        )
    )
    try:
        shutil.copytree(publication_root, temporary, dirs_exist_ok=True, symlinks=False)
        for path in temporary.rglob("*"):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        _fsync_tree(temporary)
        with exclusive_file_lock(destination):
            if os.path.lexists(destination):
                raise ExportCollisionError(f"export destination exists: {destination}")
            os.rename(temporary, destination)
            parent_descriptor = os.open(
                destination.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    replay_publication(destination)
    return destination
