import os
from datetime import date, datetime, time


def now() -> datetime:
    """
    Current time for date defaults.

    If FINFLOW_AS_OF_DATE (YYYY-MM-DD) is set in backend/.env, returns the end of that day
    instead, so the service behaves as if it were running on the date the synthetic data
    was generated for (see setup_data.py --as-of).
    """
    as_of = os.getenv("FINFLOW_AS_OF_DATE")
    if as_of:
        return datetime.combine(date.fromisoformat(as_of), time(23, 59, 59))
    return datetime.now()
