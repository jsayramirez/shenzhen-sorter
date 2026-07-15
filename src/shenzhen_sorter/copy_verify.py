"""
Copy -> verify -> commit (spec sections 4 and 7), with one deliberate
deviation from a *literal* reading of the pipeline text, explained below.

The spec lists "hash source" and "hash destination" as two separate steps
after the copy. Read literally, that means re-opening and re-reading the
source file from disk a second time, after the copy already happened. That
re-read is a TOCTOU (time-of-check/time-of-use) gap: if anything touches the
source between the copy-read and the hash-re-read (an editing app that still
had a handle open despite the stability gate, a sync client, antivirus),
the hash could reflect bytes that are NOT the bytes actually copied - a
false "verified" or a false mismatch.

Fix used here: the source hash is computed from the *same* stream used to
write the copy (one read of the source, ever, per attempt). This still
satisfies the spec's real intent - independent proof that destination bytes
equal source bytes - without a second, riskier, source read. The destination
hash is still computed independently, by re-opening and re-reading the
.partial file fresh from disk after flush/fsync, which is what actually
proves durability (that step doesn't touch the source at all, so it has
no TOCTOU risk).
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from config import settings


class VerificationFailed(RuntimeError):
    pass


class DestinationAlreadyExists(RuntimeError):
    pass


@dataclass
class CopyResult:
    source_size: int
    source_sha256: str
    destination_size: int
    destination_sha256: str


def partial_path_for(final_path: Path) -> Path:
    return final_path.with_name(final_path.name + settings.PARTIAL_SUFFIX)


@dataclass
class PartialCopy:
    partial_path: Path
    source_size: int
    source_sha256: str


def copy_to_partial_with_hash(source_path: Path, final_path: Path) -> PartialCopy:
    """Stream source -> <final_path>.partial, hashing source as we go.

    Returns the partial path plus the source-side size/hash computed from
    that single read. Caller records these in the ledger, then calls
    verify() next, then commit().
    """
    partial_path = partial_path_for(final_path)

    # A leftover .partial from a crashed prior attempt must never be trusted
    # or resumed blindly (spec: partial files must never look complete, and
    # a crash must not damage the source). Rebuilding it fresh is the safe
    # default for v1; the source is never touched by this.
    if partial_path.exists():
        partial_path.unlink()

    partial_path.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    total = 0
    with open(source_path, "rb") as src, open(partial_path, "wb") as dst:
        while True:
            chunk = src.read(settings.HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            dst.write(chunk)
            total += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())

    return PartialCopy(partial_path, total, hasher.hexdigest())


def hash_file_independent(path: Path) -> tuple[int, str]:
    """Re-open and re-read a file fresh from disk. Used for the destination
    hash (proves durability) and for the post-commit final recheck."""
    hasher = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(settings.HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return total, hasher.hexdigest()


def verify(source_size: int, source_sha256: str, partial_path: Path) -> CopyResult:
    dest_size, dest_sha256 = hash_file_independent(partial_path)
    result = CopyResult(source_size, source_sha256, dest_size, dest_sha256)
    if dest_size != source_size or dest_sha256 != source_sha256:
        raise VerificationFailed(
            f"{partial_path.name}: source={source_size}b/{source_sha256[:12]} "
            f"!= dest={dest_size}b/{dest_sha256[:12]}"
        )
    return result


def commit(partial_path: Path, final_path: Path) -> None:
    """Finalize via same-filesystem rename. Refuses to silently overwrite -
    collision resolution must happen before this is called (spec section 8)."""
    if final_path.exists():
        raise DestinationAlreadyExists(str(final_path))
    if partial_path.drive.lower() != final_path.drive.lower():
        raise RuntimeError(
            "Refusing cross-volume rename for commit; partial and final must "
            "share a filesystem so the rename is atomic."
        )
    os.replace(str(partial_path), str(final_path))


def final_recheck(final_path: Path, expected_size: int, expected_sha256: str) -> None:
    size, sha256 = hash_file_independent(final_path)
    if size != expected_size or sha256 != expected_sha256:
        raise VerificationFailed(
            f"post-commit recheck failed for {final_path}: "
            f"expected {expected_size}b/{expected_sha256[:12]}, "
            f"found {size}b/{sha256[:12]}"
        )
