"""Allowlist CONNECT proxy for dependency setup (PRD 3 section 14.3 `setup_scoped`).

Runs in its own locked-down container that is the only peer reachable from
the setup container's internal (no-route) Docker network. It tunnels TLS only
to exact allowlisted host:port pairs and refuses everything else, so package
scripts in setup cannot reach arbitrary destinations. Standard library only.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

MAX_CONNECTIONS = 64
IDLE_SECONDS = 120
HEADER_LIMIT = 16 * 1024


def log(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=IDLE_SECONDS)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class Proxy:
    def __init__(self, allow: set) -> None:
        self.allow = allow
        self.active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= MAX_CONNECTIONS:
            writer.close()
            return
        self.active += 1
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
                writer.close()
                return
            if len(head) > HEADER_LIMIT:
                writer.close()
                return
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = request_line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                log(f"DENY non-CONNECT {request_line[:120]}")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            target = parts[1].lower()
            if target not in self.allow:
                log(f"DENY {target}")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            host, _, port = target.rpartition(":")
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=30)
            except (OSError, asyncio.TimeoutError, ValueError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            log(f"ALLOW {target}")
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        finally:
            self.active -= 1


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args()
    allow = {item.strip().lower() for item in args.allow if item.strip()}
    host, _, port = args.listen.rpartition(":")
    proxy = Proxy(allow)
    server = await asyncio.start_server(proxy.handle, host, int(port), limit=HEADER_LIMIT)
    log("READY " + ",".join(sorted(allow)))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
