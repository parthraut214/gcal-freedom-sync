import json
import os
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class BlockState:
    active: bool = False
    event_id: Optional[str] = None
    event_summary: Optional[str] = None
    blocklist: Optional[str] = None
    schedule_id: Optional[str] = None
    started_at: Optional[str] = None
    ends_at: Optional[str] = None
    locked: bool = False


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self._state = self._load()

    def _load(self) -> BlockState:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return BlockState(**json.load(f))
            except Exception:
                pass
        return BlockState()

    def _save(self):
        with open(self.path, 'w') as f:
            json.dump(asdict(self._state), f, indent=2)

    @property
    def is_active(self) -> bool:
        return self._state.active

    @property
    def current_event_id(self) -> Optional[str]:
        return self._state.event_id

    @property
    def schedule_id(self) -> Optional[str]:
        return self._state.schedule_id

    @property
    def event_summary(self) -> Optional[str]:
        return self._state.event_summary

    @property
    def started_at(self) -> Optional[str]:
        return self._state.started_at

    @property
    def locked(self) -> bool:
        return self._state.locked

    def set_active(self, event_id: str, event_summary: str, blocklist: str, ends_at: str,
                   schedule_id: Optional[str] = None, locked: bool = False):
        from datetime import datetime, timezone
        self._state = BlockState(
            active=True,
            event_id=event_id,
            event_summary=event_summary,
            blocklist=blocklist,
            schedule_id=schedule_id,
            started_at=datetime.now(timezone.utc).isoformat(),
            ends_at=ends_at,
            locked=locked,
        )
        self._save()

    def set_inactive(self):
        self._state = BlockState(active=False)
        self._save()
