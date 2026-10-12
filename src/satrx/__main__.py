from __future__ import annotations

import argparse
import sys

from satrx.core.config import get_settings


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "doctor":
        from satrx.doctor import main as doctor_main

        raise SystemExit(doctor_main(argv[1:]))

    import uvicorn

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="satrx", description="Satellite receiver service (use 'satrx doctor' to check the setup)")
    parser.add_argument("--host", default=settings.server.host)
    parser.add_argument("--port", type=int, default=settings.server.port)
    args = parser.parse_args(argv)
    # a nyitott SSE/WebSocket kapcsolatok ne akasszák meg a leállítást (Ctrl+C)
    uvicorn.run("satrx.api.app:create_app", factory=True, host=args.host, port=args.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
