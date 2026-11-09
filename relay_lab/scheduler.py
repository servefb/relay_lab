"""Recurring experiments.

The API key is deliberately memory-only: a schedule survives a restart, the
credential does not, and the UI says so instead of silently running without it.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import (
    ContractRequest,
    ParallelRequest,
    QualityRequest,
    Schedule,
    ScheduleRequest,
    SpeedRequest,
    StartRequest,
)
from .search import CapacityLab

INTERVAL_SECONDS = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class Scheduler:
    """Owns the schedule list and fires due experiments into the lab."""

    def __init__(self, root: Path, lab: CapacityLab) -> None:
        self.root = root
        self.lab = lab
        self.path = root / 'schedules.json'
        self.lock = threading.RLock()
        self.schedules: list[Schedule] = []
        self.keys: dict[str, str] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return
        for item in payload.get('schedules', []):
            try:
                self.schedules.append(Schedule.model_validate(item))
            except ValueError:
                continue

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(
                {'schedules': [item.model_dump(mode='json') for item in self.schedules]}, ensure_ascii=False, indent=2
            ),
            encoding='utf-8',
        )
        temporary.replace(self.path)

    # ---------- crud ----------

    def list(self) -> list[Schedule]:
        with self.lock:
            return sorted(self.schedules, key=lambda item: item.created_at, reverse=True)

    def create(self, request: ScheduleRequest) -> Schedule:
        with self.lock:
            schedule = Schedule(
                schedule_id=f'sch-{uuid.uuid4().hex[:8]}',
                label=request.label,
                kind=request.kind,
                interval_minutes=request.interval_minutes,
                created_at=_iso(_now()),
                next_run_at=_iso(_now() + timedelta(minutes=request.interval_minutes)),
                capacity=request.capacity,
                speed=request.speed,
                parallel=request.parallel,
                quality=request.quality,
                contract=request.contract,
            )
            if request.api_key:
                self.keys[schedule.schedule_id] = request.api_key.get_secret_value()
            self.schedules.append(schedule)
            self._save()
            return schedule

    def remove(self, schedule_id: str) -> None:
        with self.lock:
            self.schedules = [item for item in self.schedules if item.schedule_id != schedule_id]
            self.keys.pop(schedule_id, None)
            self._save()

    def toggle(self, schedule_id: str, enabled: bool) -> Schedule:
        with self.lock:
            schedule = self._find(schedule_id)
            schedule.enabled = enabled
            if enabled:
                schedule.needs_key = schedule.schedule_id not in self.keys
                schedule.next_run_at = _iso(_now() + timedelta(minutes=schedule.interval_minutes))
            self._save()
            return schedule

    def set_key(self, schedule_id: str, api_key: str) -> Schedule:
        with self.lock:
            schedule = self._find(schedule_id)
            self.keys[schedule.schedule_id] = api_key
            schedule.needs_key = False
            self._save()
            return schedule

    def run_now(self, schedule_id: str, api_key: str | None = None) -> Schedule:
        with self.lock:
            schedule = self._find(schedule_id)
        if api_key:
            self.set_key(schedule_id, api_key)
        self._fire(schedule)
        return schedule

    def _find(self, schedule_id: str) -> Schedule:
        for item in self.schedules:
            if item.schedule_id == schedule_id:
                return item
        raise KeyError(schedule_id)

    # ---------- firing ----------

    def _fire(self, schedule: Schedule) -> None:
        key = self.keys.get(schedule.schedule_id)
        with self.lock:
            schedule.last_status = 'starting'
            self._save()
        try:
            if not key:
                with self.lock:
                    schedule.needs_key = True
                    schedule.last_status = 'needs_key'
                    self._save()
                return
            if schedule.kind == 'capacity' and schedule.capacity:
                run = self.lab.start(StartRequest(**schedule.capacity.model_dump(), api_key=key, authorized=True))
            elif schedule.kind == 'speed' and schedule.speed:
                run = self.lab.start_speed(SpeedRequest(**schedule.speed.model_dump(), api_key=key, authorized=True))
            elif schedule.kind == 'parallel' and schedule.parallel:
                run = self.lab.start_parallel(
                    ParallelRequest(**schedule.parallel.model_dump(), api_key=key, authorized=True)
                )
            elif schedule.kind == 'quality' and schedule.quality:
                run = self.lab.start_quality(
                    QualityRequest(**schedule.quality.model_dump(), api_key=key, authorized=True)
                )
            elif schedule.kind == 'contract' and schedule.contract:
                run = self.lab.start_contract(
                    ContractRequest(**schedule.contract.model_dump(), api_key=key, authorized=True)
                )
            else:
                with self.lock:
                    schedule.last_status = 'invalid'
                    self._save()
                return
        except RuntimeError:
            # Another run is in flight; try again on the next tick without shifting the plan.
            with self.lock:
                schedule.last_status = 'busy'
                self._save()
            return
        with self.lock:
            schedule.last_run_id = run.run_id
            schedule.last_status = run.status
            self._save()

    def _refresh_status(self) -> None:
        with self.lock:
            pending = [item for item in self.schedules if item.last_run_id]
        if not pending:
            return
        runs = {run.run_id: run for run in self.lab.history()}
        with self.lock:
            changed = False
            for item in pending:
                run = runs.get(item.last_run_id or '')
                if run and item.last_status != run.status:
                    item.last_status = run.status
                    changed = True
            if changed:
                self._save()

    def tick(self) -> None:
        """Fire everything that is due; called by the loop and by tests."""
        self._refresh_status()
        due: list[Schedule] = []
        with self.lock:
            now = _now()
            for item in self.schedules:
                if not item.enabled:
                    continue
                try:
                    when = datetime.fromisoformat(item.next_run_at)
                except ValueError:
                    when = now
                if when <= now:
                    item.next_run_at = _iso(now + timedelta(minutes=item.interval_minutes))
                    due.append(item)
            if due:
                self._save()
        for item in due:
            self._fire(item)

    def loop(self) -> None:
        while True:
            time.sleep(INTERVAL_SECONDS)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a scheduler must never die on one bad schedule
                continue

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True).start()
