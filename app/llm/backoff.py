import asyncio

import httpx

_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUSES
    return False


async def with_retry(fn, attempts: int = 3, base_delay: float = 1.0, sleep=None):
    """Retry transient failures with exponential backoff (base * 4**i).

    Never counts against retry_counts (spec §5). Non-transient errors re-raise
    immediately; exhausted attempts re-raise the last exception.
    """
    if sleep is None:
        sleep = asyncio.sleep
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - classified below
            if not is_transient(e) or i == attempts - 1:
                raise
            await sleep(base_delay * (4 ** i))
