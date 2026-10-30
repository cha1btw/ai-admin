"""Run everything the bot needs in development with one command, and keep it running.

    make dev              # API (:8000, auto-reload), worker, Telegram polling
    make dev TUNNEL=1     # also the temporary public address of the admin panel

Before this, four terminals had to stay open, and a worker that died left the bot silent. Here
every process is restarted when it exits, and its log lines carry its name. Ctrl+C stops all.
Development only: polling removes the bots' webhooks, so never run it against production bots.
"""

import asyncio
import contextlib
import os
import signal
import sys
import time

PROCESSES = {
    "api": ["uv", "run", "uvicorn", "app.api.main:app", "--reload", "--port", "8000"],
    "worker": ["uv", "run", "python", "-m", "app.worker.main"],
    "poll": ["uv", "run", "python", "-m", "app.worker.telegram_poll"],
}
TUNNEL = ["uv", "run", "python", "scripts/tunnel.py"]
RESTART_AFTER_S = 3
GIVE_UP_AFTER = 5  # crashes within a minute
COLORS = {"api": "36", "worker": "32", "poll": "35", "tunnel": "33"}


def say(name: str, line: str) -> None:
    color = COLORS.get(name, "0")
    print(f"\033[{color}m{name:>6}\033[0m | {line}", flush=True)


async def pump(name: str, stream: asyncio.StreamReader) -> None:
    while line := await stream.readline():
        say(name, line.decode(errors="replace").rstrip())


async def supervise(name: str, command: list[str], stop: asyncio.Event) -> None:
    crashes: list[float] = []
    while not stop.is_set():
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # Ctrl+C reaches us only; we stop the children ourselves
        )
        say(name, f"started (pid {proc.pid})")
        reader = asyncio.create_task(pump(name, proc.stdout))
        waiter = asyncio.create_task(proc.wait())
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            with_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                with_group(proc, signal.SIGKILL)
            reader.cancel()
            return
        stopper.cancel()
        await reader
        now = time.monotonic()
        crashes = [t for t in crashes if now - t < 60] + [now]
        if len(crashes) >= GIVE_UP_AFTER:
            say(name, f"exited {len(crashes)} times in a minute, giving up")
            return
        say(name, f"exited with code {proc.returncode}, restarting in {RESTART_AFTER_S}s")
        await asyncio.sleep(RESTART_AFTER_S)


def with_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, sig)  # uv spawns the real process: stop the whole group


async def main() -> int:
    processes = dict(PROCESSES)
    if os.environ.get("TUNNEL") == "1":
        processes["tunnel"] = TUNNEL
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    say("dev", f"starting {', '.join(processes)}. Ctrl+C stops everything.")
    await asyncio.gather(*(supervise(n, c, stop) for n, c in processes.items()))
    say("dev", "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
