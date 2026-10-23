"""Small helpers for replacing text files without exposing partial writes."""

import os
import stat
import tempfile


def write_text_atomic(path, text, *, encoding="utf-8", errors="strict", newline=None):
    """Write text beside its destination, then atomically replace the destination.

    The temporary file is removed if writing or replacement fails. Existing POSIX
    permission bits are retained; a new file keeps tempfile's owner-only mode.
    """
    target = os.path.realpath(os.fspath(path))
    directory = os.path.dirname(target)
    try:
        existing_mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        existing_mode = None

    fd, temporary = tempfile.mkstemp(prefix=".aiset-", suffix=".tmp", dir=directory)
    try:
        stream = os.fdopen(fd, "w", encoding=encoding, errors=errors, newline=newline)
        fd = None
        with stream as f:
            f.write(text)
        if existing_mode is not None and os.name != "nt":
            os.chmod(temporary, existing_mode)
        os.replace(temporary, target)
        temporary = None
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
