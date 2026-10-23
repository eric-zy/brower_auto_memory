"""Own Chrome's asyncio Process through shutdown, including failed starts.

Browser Use 0.13 converts its local asyncio Process to a psutil PID wrapper,
losing ownership of stdout/stderr pipes. Its CDP discovery does not read those
pipes. Limit this compatibility shim to our own Chrome launch context; Node
workers and unrelated subprocesses retain their existing pipe configuration.
"""
import asyncio
from contextvars import ContextVar
from pathlib import Path
from weakref import WeakKeyDictionary

_owner = ContextVar('upload_owned_chrome', default=None)
_locks = WeakKeyDictionary()
_BROWSERS = {'chrome.exe','chrome','chromium.exe','chromium','msedge.exe','msedge','google-chrome','google-chrome-stable'}


def chrome_launch(program, args):
    return Path(str(program)).name.lower() in _BROWSERS and any(str(a).startswith('--remote-debugging-port=') for a in args)


async def start_owned_browser(browser, processes):
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        original = asyncio.create_subprocess_exec
        token = _owner.set(processes)

        async def spawn(program, *args, **kwargs):
            owned = _owner.get() is processes and chrome_launch(program, args)
            if owned:
                kwargs = {**kwargs, 'stdout':asyncio.subprocess.DEVNULL, 'stderr':asyncio.subprocess.DEVNULL}
            process = await original(program, *args, **kwargs)
            if owned:
                processes.append(process)
            return process

        asyncio.create_subprocess_exec = spawn
        try:
            await browser.start()
        finally:
            asyncio.create_subprocess_exec = original
            _owner.reset(token)


async def close_owned_processes(processes):
    for process in list(processes):
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(process.wait(), timeout=5)
        processes.remove(process)
    await asyncio.sleep(0)  # Let process-exit callbacks finish before asyncio.run closes the loop.
