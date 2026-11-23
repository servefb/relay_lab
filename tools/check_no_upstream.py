"""Prove the migrated backend never needs the upstream package at runtime.

The interpreter used here still has `evalscope` installed (editable), so a stray
import would otherwise succeed silently. This installs an import blocker first,
then runs one full capacity experiment through the real subprocess engine.
"""

from __future__ import annotations

import sys
from pathlib import Path


class Blocker:
    """Refuse any import whose top-level package is *banned*."""

    def __init__(self, banned: set[str]) -> None:
        self.banned = banned

    def find_spec(self, name: str, path=None, target=None):
        if name.split('.')[0] in self.banned:
            raise ImportError(f'blocked import: {name}')
        return None


for banned in ('evalscope',):
    sys.meta_path.insert(0, Blocker({banned}))

before = set(sys.modules)

import threading  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from relay_lab.api import create_app  # noqa: E402
from relay_lab.demo import DemoServer  # noqa: E402


def main() -> None:
    server = DemoServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = TestClient(create_app(Path('.selfcheck'), schedule_loop=False))
    response = client.post(
        '/api/demo',
        json={'protocol': 'openai'},
    )
    app = client.app
    print('demo ->', response.status_code)
    run_id = response.json()['run_id']
    app.state.lab.jobs[run_id].done.wait(240)
    report = client.get(f'/api/runs/{run_id}').json()
    print('status:', report['status'])
    for stage in report['stages']:
        print(
            f'  {stage["phase"]} p={stage["pressure"]} ok={stage["succeeded"]}/{stage["requests"]} '
            f'rpm={stage["rpm"]:.1f} -> {stage["status"]}'
        )
    leaked = sorted({m.split('.')[0] for m in set(sys.modules) - before if m.split('.')[0] == 'evalscope'})
    print('evalscope modules loaded:', leaked or 'none')
    server.shutdown()
    server.server_close()


if __name__ == '__main__':
    main()
