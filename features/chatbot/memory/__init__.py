from features.common.db import (
    get_filing_notes_for_customer,
    get_last_filing_note,
    initialize_auth_schema,
)

from .persistence import (
    append_user_activity,
    create_user,
    get_customer_memories,
    get_user_by_email,
    get_user_by_id,
    get_user_recent_activity,
    initialize_memory_schemas,
    reset_password,
    set_temp_password,
    store_customer_memory,
    upsert_customer_memory,
    verify_password,
)

__all__ = [
    "append_user_activity",
    "create_user",
    "get_customer_memories",
    "get_filing_notes_for_customer",
    "get_last_filing_note",
    "get_user_by_email",
    "get_user_by_id",
    "get_user_recent_activity",
    "initialize_auth_schema",
    "initialize_memory_schemas",
    "reset_password",
    "set_temp_password",
    "store_customer_memory",
    "upsert_customer_memory",
    "verify_password",
]
