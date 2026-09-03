"""Low-level SQLite Session storage primitives."""

from .entries import delete_entry_rows, id_exists_in_entries, read_entry_row
from .facts import delete_fact_rows, read_latest_fact
from .lanes import delete_lane_rows, read_lane_row
from .records import delete_record_rows, id_exists_in_records
from .session_sequences import allocate_sequence, create_sequence, read_next_sequence
from .session_stats import add_usage, create_stats, increment_messages, read_stats
from .sessions import (
    delete_session_row,
    read_session_row,
    read_session_rows,
    session_exists,
)
from .writer_leases import (
    WriterLease,
    WriterLeaseError,
    claim_writer_lease,
    release_writer_lease,
    renew_writer_lease,
)

__all__ = [
    "WriterLease",
    "WriterLeaseError",
    "add_usage",
    "allocate_sequence",
    "claim_writer_lease",
    "create_sequence",
    "create_stats",
    "delete_entry_rows",
    "delete_fact_rows",
    "delete_lane_rows",
    "delete_record_rows",
    "delete_session_row",
    "id_exists_in_entries",
    "id_exists_in_records",
    "increment_messages",
    "read_entry_row",
    "read_lane_row",
    "read_latest_fact",
    "read_next_sequence",
    "read_session_row",
    "read_session_rows",
    "read_stats",
    "release_writer_lease",
    "renew_writer_lease",
    "session_exists",
]
