"""Launch one uvicorn web process and stop it when a sentinel file appears.

``scripts/concurrency_demo.py`` uses this instead of sending signals, so the
demo also works in sandboxes that deny ``kill`` on child processes.

Usage:
    python scripts/web_worker.py <db_path> <port> <instance> <stop_file>
"""

from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> None:
    db_path, port, instance, stop_file = sys.argv[1:5]
    os.environ["BOM_DB_PATH"] = db_path
    os.environ["BOM_INSTANCE"] = instance
    os.environ["BOM_DEMO_MODE"] = "1"
    os.environ["BOM_SEED_DEMO"] = "0"

    import uvicorn
    from app.main import create_app

    config = uvicorn.Config(
        create_app(instance=instance, demo_mode=True, seed_demo=False),
        host="127.0.0.1",
        port=int(port),
        log_level="warning",
    )
    server = uvicorn.Server(config)

    def watch() -> None:
        while not os.path.exists(stop_file):
            time.sleep(0.1)
        server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
