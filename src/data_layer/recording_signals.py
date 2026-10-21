"""Bounded signal reads using the existing run connection and path policy."""
from pathlib import Path

from .recording import open_run_readonly


def iter_recording_signals(database_path: str | Path, *, maximum_seq: int, minimum_seq: int = 0, chunk_size: int = 8192, include_lock_in: bool = False):
    """Stream a fixed review watermark without modifying a live or sealed run.

    Rows contain seq, segment_id, monotonic_ns, utc_ns, current_a, x_m, y_m, z_m.
    With include_lock_in, append lock_in_x_a, lock_in_y_a. Old databases and
    four-channel runs return None for unavailable quadratures, never zero.
    The caller owns review budgets and must close this generator if stopping early.
    Separate from the writer module so an already-running recorder can lazily
    load new review functionality without reloading its active writer classes.
    """
    if chunk_size < 1 or chunk_size > 65536:
        raise ValueError("invalid signal chunk size")
    with open_run_readonly(database_path) as connection:
        extra = ""
        if include_lock_in:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_samples)")}
            extra = "".join("," + (name if name in columns else "NULL")
                            for name in ("lock_in_x_a", "lock_in_y_a"))
        cursor = connection.execute(
            "SELECT seq,segment_id,monotonic_ns,utc_ns,current_a,x_m,y_m,z_m" + extra + " "
            "FROM signal_samples WHERE seq>=? AND seq<=? ORDER BY seq", (int(minimum_seq), int(maximum_seq))
        )
        while rows := cursor.fetchmany(chunk_size):
            yield from rows
