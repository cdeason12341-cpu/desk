"""In-process message bus: one asyncio.Queue per downstream agent."""
import asyncio


class Bus:
    def __init__(self):
        self.auditor: asyncio.Queue = asyncio.Queue()
        self.analyst: asyncio.Queue = asyncio.Queue()
        self.executor: asyncio.Queue = asyncio.Queue()
        self.events: asyncio.Queue = asyncio.Queue()   # publish triggers (signal, fill, exit, audit)

    def event(self, kind: str, mint: str | None = None):
        self.events.put_nowait((kind, mint))
