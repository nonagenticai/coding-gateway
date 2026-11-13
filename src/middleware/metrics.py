"""Request timing middleware for the coding-gateway.

Delegates to ``coding_client.api.metrics``. Kept as a thin re-export so
existing ``from middleware.metrics import add_process_time_header`` call
sites keep working.
"""

from __future__ import annotations

from coding_client.api.metrics import add_process_time_header

__all__ = ["add_process_time_header"]
