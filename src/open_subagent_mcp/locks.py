from __future__ import annotations

import asyncio
from pathlib import Path


class RepositoryLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_lock(self, cwd: Path) -> asyncio.Lock:
        key = str(cwd.resolve())
        async with self._guard:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def run_with_write_lock(self, cwd: Path, coro):
        lock = await self.get_lock(cwd)
        async with lock:
            return await coro()
