"""Command line entry point: python -m relay_lab."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .api import create_app, resolve_web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""This deployment's own root, so the folder can sit anywhere on disk."""


def main() -> None:
    parser = argparse.ArgumentParser(description='Relay capacity lab backend')
    parser.add_argument('--host', default='127.0.0.1', help='bind address, loopback by default')
    parser.add_argument('--port', type=int, default=9011)
    parser.add_argument('--data', default=str(PROJECT_ROOT / 'data'), help='directory for experiment reports')
    parser.add_argument('--web', default=None, help='built console directory; defaults to ./web/dist')
    parser.add_argument('--no-scheduler', action='store_true', help='do not run recurring schedules')
    options = parser.parse_args()
    data = Path(options.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    app = create_app(data, web_dir=options.web, schedule_loop=not options.no_scheduler)
    console = resolve_web(options.web)
    print(f'Relay Lab API: http://{options.host}:{options.port}/   (docs: /api/docs)')
    print(f'  console: {console if console else "未构建（接口仍可用）"}')
    print(f'  reports: {data}')
    uvicorn.run(app, host=options.host, port=options.port, log_level='info')


if __name__ == '__main__':
    main()
