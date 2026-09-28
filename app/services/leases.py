import asyncio
from collections.abc import Awaitable, Callable

WaitCallback = Callable[[], Awaitable[None]] | None


class LeasePool:
    """In-process scheduler for the Maritime plan limits (spec phase-4 §4).

    VM leases: at most `max_vms` agents awake at once. Sleeping agents are free.
    An agent is owned by one task at a time, which doubles as the per-repo mutex
    so two tasks never interleave inside one workspace. Re-entrant per owner.

    Computer leases: at most `max_computers` computers running at once.
    """

    def __init__(self, max_vms: int = 2, max_computers: int = 1):
        self.max_vms = max_vms
        self.max_computers = max_computers
        self._cond = asyncio.Condition()
        self._vm_owner: dict[str, str] = {}      # agent_key -> owner (task id)
        self._computer_owners: set[str] = set()

    @property
    def awake(self) -> dict[str, str]:
        return dict(self._vm_owner)

    @property
    def computer_owners(self) -> set[str]:
        return set(self._computer_owners)

    async def acquire_vm(self, agent_key: str, owner: str, on_wait: WaitCallback = None) -> bool:
        """Returns True if the caller had to queue."""
        waited = False
        async with self._cond:
            while True:
                cur = self._vm_owner.get(agent_key)
                if cur == owner:
                    return waited
                if cur is None and len(self._vm_owner) < self.max_vms:
                    self._vm_owner[agent_key] = owner
                    return waited
                if not waited and on_wait is not None:
                    waited = True
                    self._cond.release()
                    try:
                        await on_wait()
                    finally:
                        await self._cond.acquire()
                    continue
                waited = True
                await self._cond.wait()

    async def release_vm(self, agent_key: str, owner: str | None = None) -> None:
        async with self._cond:
            cur = self._vm_owner.get(agent_key)
            if cur is not None and (owner is None or cur == owner):
                del self._vm_owner[agent_key]
                self._cond.notify_all()

    def holds_vm(self, agent_key: str, owner: str) -> bool:
        return self._vm_owner.get(agent_key) == owner

    async def acquire_computer(self, owner: str, on_wait: WaitCallback = None) -> bool:
        waited = False
        async with self._cond:
            while True:
                if owner in self._computer_owners:
                    return waited
                if len(self._computer_owners) < self.max_computers:
                    self._computer_owners.add(owner)
                    return waited
                if not waited and on_wait is not None:
                    waited = True
                    self._cond.release()
                    try:
                        await on_wait()
                    finally:
                        await self._cond.acquire()
                    continue
                waited = True
                await self._cond.wait()

    async def release_computer(self, owner: str) -> None:
        async with self._cond:
            if owner in self._computer_owners:
                self._computer_owners.discard(owner)
                self._cond.notify_all()
