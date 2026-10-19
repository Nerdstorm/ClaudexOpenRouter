"""Process-group cleanup shared by optional native CLI workers."""

import asyncio
import os
import signal


async def stop_process(process: asyncio.subprocess.Process) -> None:
    # Terminate descendants even if the CLI parent has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    # A descendant can survive the parent or ignore SIGTERM. Do not leave it running.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
