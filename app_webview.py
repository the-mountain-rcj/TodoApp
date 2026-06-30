from __future__ import annotations

import calendar
import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import webview


APP_TITLE = "TodoApp"
APP_SIZE = (1580, 900)


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name.lower() == "dist":
            return exe_dir.parent
        return exe_dir
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return app_root()


def user_data_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_TITLE
    return Path.home() / ".todoapp"


ROOT_DIR = app_root()
RESOURCE_DIR = resource_root()
LEGACY_DATA_DIR = ROOT_DIR / "data"
DATA_DIR = user_data_root()
DATA_FILE = DATA_DIR / "todos.json"
HOLIDAY_CACHE_DIR = DATA_DIR / "holidays"
ASSET_DIR = RESOURCE_DIR / "assets"
FONT_DIR = ASSET_DIR / "fonts"
LOGO_DIR = ASSET_DIR / "logo" / "todolist-logo"
HOLIDAY_COUNTRY = "CN"
CHINA_HOLIDAY_API_URL = "https://api.jiejiariapi.com/v1/holidays/{year}"
NAGER_HOLIDAY_API_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"


def migrate_legacy_data() -> None:
    legacy_file = LEGACY_DATA_DIR / "todos.json"
    if DATA_FILE.exists() or not legacy_file.exists():
        return
    try:
        if legacy_file.resolve() == DATA_FILE.resolve():
            return
    except OSError:
        pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_file, DATA_FILE)
    legacy_holidays = LEGACY_DATA_DIR / "holidays"
    if legacy_holidays.exists() and not HOLIDAY_CACHE_DIR.exists():
        shutil.copytree(legacy_holidays, HOLIDAY_CACHE_DIR)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_key() -> str:
    return date.today().isoformat()


def parse_date(value: str | None) -> date:
    try:
        return datetime.strptime(value or today_key(), "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def pretty_date(value: str) -> str:
    dt = parse_date(value)
    return dt.strftime("%Y.%m.%d")


def weekday_name(value: str) -> str:
    return "一二三四五六日"[parse_date(value).weekday()]


def calendar_visible_range(value: str) -> tuple[date, date]:
    selected = parse_date(value)
    first = selected.replace(day=1)
    start = first - timedelta(days=first.weekday())
    return start, start + timedelta(days=41)


def normalize_subtask(data: dict | None = None) -> dict:
    data = data or {}
    stamp = now_iso()
    return {
        "id": data.get("id") or str(uuid.uuid4()),
        "text": str(data.get("text") or "").strip(),
        "created_at": data.get("created_at") or stamp,
        "updated_at": data.get("updated_at") or stamp,
        "completed_at": data.get("completed_at"),
        "deleted_at": data.get("deleted_at"),
    }


@dataclass
class Todo:
    id: str
    text: str
    date: str
    created_at: str
    updated_at: str
    completed_at: str | None = None
    deleted_at: str | None = None
    carried_from_previous_day: bool = False
    source_id: str | None = None
    subtasks: list[dict] | None = None
    due_scope: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Todo":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            text=str(data.get("text") or "").strip(),
            date=data.get("date") or today_key(),
            created_at=data.get("created_at") or now_iso(),
            updated_at=data.get("updated_at") or now_iso(),
            completed_at=data.get("completed_at"),
            deleted_at=data.get("deleted_at"),
            carried_from_previous_day=bool(data.get("carried_from_previous_day", False)),
            source_id=data.get("source_id"),
            subtasks=[normalize_subtask(item) for item in data.get("subtasks", [])],
            due_scope=data.get("due_scope"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "date": self.date,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "deleted_at": self.deleted_at,
            "carried_from_previous_day": self.carried_from_previous_day,
            "source_id": self.source_id,
            "subtasks": [normalize_subtask(item) for item in self.subtasks or []],
            "due_scope": self.due_scope,
        }

    @property
    def completed(self) -> bool:
        return bool(self.completed_at)

    @property
    def deleted(self) -> bool:
        return bool(self.deleted_at)

    def active_subtasks(self) -> list[dict]:
        return [item for item in self.subtasks or [] if not item.get("deleted_at")]

    def sync_parent_completion(self) -> None:
        active = self.active_subtasks()
        if not active:
            return
        all_done = all(item.get("completed_at") for item in active)
        if all_done and not self.completed_at:
            self.completed_at = now_iso()
        if not all_done and self.completed_at:
            self.completed_at = None
        self.updated_at = now_iso()


class TodoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.version = 1
        self.last_open_date = today_key()
        self.todos: list[Todo] = []
        self.lock = threading.RLock()
        self.load()

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            backup = self.path.with_suffix(f".broken-{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
            self.path.replace(backup)
            raw = {}
        self.version = raw.get("version", 1)
        self.last_open_date = raw.get("last_open_date", today_key())
        self.todos = [Todo.from_dict(item) for item in raw.get("todos", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.version,
            "last_open_date": self.last_open_date,
            "todos": [todo.to_dict() for todo in self.todos],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def find(self, todo_id: str) -> Todo | None:
        return next((todo for todo in self.todos if todo.id == todo_id), None)

    def find_subtask(self, todo: Todo, subtask_id: str) -> dict | None:
        return next((item for item in todo.subtasks or [] if item.get("id") == subtask_id), None)

    def visible_for_date(self, value: str) -> list[Todo]:
        return [todo for todo in self.todos if todo.date == value and not todo.deleted]

    def incomplete_before_today(self, source_date: str) -> list[Todo]:
        return [
            todo
            for todo in self.todos
            if todo.date == source_date and not todo.deleted and not todo.completed
        ]

    def add(self, text: str, value: str, *, source_id: str | None = None, carried: bool = False, subtasks: list[dict] | None = None) -> Todo:
        stamp = now_iso()
        copied_subtasks = []
        for item in subtasks or []:
            if item.get("deleted_at"):
                continue
            copied_subtasks.append(
                {
                    "id": str(uuid.uuid4()),
                    "text": item.get("text", ""),
                    "created_at": stamp,
                    "updated_at": stamp,
                    "completed_at": item.get("completed_at"),
                    "deleted_at": None,
                }
            )
        todo = Todo(
            id=str(uuid.uuid4()),
            text=text.strip(),
            date=value,
            created_at=stamp,
            updated_at=stamp,
            carried_from_previous_day=carried,
            source_id=source_id,
            subtasks=copied_subtasks,
        )
        self.todos.append(todo)
        self.save()
        return todo

    def update_text(self, todo_id: str, text: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        todo.text = text.strip() or todo.text
        todo.updated_at = now_iso()
        self.save()

    def delete(self, todo_id: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        todo.deleted_at = now_iso()
        todo.updated_at = now_iso()
        self.save()

    def restore(self, todo_id: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        todo.deleted_at = None
        todo.updated_at = now_iso()
        self.save()

    def purge(self, todo_id: str) -> None:
        before = len(self.todos)
        self.todos = [todo for todo in self.todos if todo.id != todo_id]
        if len(self.todos) != before:
            self.save()

    def toggle(self, todo_id: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        stamp = now_iso()
        next_value = None if todo.completed_at else stamp
        todo.completed_at = next_value
        for item in todo.active_subtasks():
            item["completed_at"] = next_value
            item["updated_at"] = stamp
        todo.updated_at = stamp
        self.save()

    def set_due_scope(self, todo_id: str, scope: str | None) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        todo.due_scope = None if todo.due_scope == scope else scope
        todo.updated_at = now_iso()
        self.save()

    def add_subtask(self, todo_id: str, text: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        text = text.strip()
        if not text:
            return
        if todo.subtasks is None:
            todo.subtasks = []
        todo.subtasks.append(normalize_subtask({"text": text}))
        todo.completed_at = None
        todo.updated_at = now_iso()
        self.save()

    def update_subtask_text(self, todo_id: str, subtask_id: str, text: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        item = self.find_subtask(todo, subtask_id)
        if not item:
            return
        item["text"] = text.strip() or item.get("text", "")
        item["updated_at"] = now_iso()
        self.save()

    def toggle_subtask(self, todo_id: str, subtask_id: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        item = self.find_subtask(todo, subtask_id)
        if not item:
            return
        item["completed_at"] = None if item.get("completed_at") else now_iso()
        item["updated_at"] = now_iso()
        todo.sync_parent_completion()
        self.save()

    def delete_subtask(self, todo_id: str, subtask_id: str) -> None:
        todo = self.find(todo_id)
        if not todo:
            return
        item = self.find_subtask(todo, subtask_id)
        if not item:
            return
        item["deleted_at"] = now_iso()
        item["updated_at"] = now_iso()
        todo.sync_parent_completion()
        self.save()

    def reorder_todo_group(self, value: str, completed: bool, ordered_ids: list[str]) -> None:
        visible = [
            todo for todo in self.todos
            if todo.date == value and not todo.deleted and todo.completed == completed
        ]
        if len(visible) < 2:
            return
        visible_by_id = {todo.id: todo for todo in visible}
        next_group = [visible_by_id[item_id] for item_id in ordered_ids if item_id in visible_by_id]
        next_group.extend(todo for todo in visible if todo.id not in {item.id for item in next_group})
        if [todo.id for todo in next_group] == [todo.id for todo in visible]:
            return

        replacement = iter(next_group)
        next_todos = []
        for todo in self.todos:
            if todo.date == value and not todo.deleted and todo.completed == completed:
                next_todos.append(next(replacement))
            else:
                next_todos.append(todo)
        self.todos = next_todos
        stamp = now_iso()
        for todo in next_group:
            todo.updated_at = stamp
        self.save()

    def reorder_subtasks(self, todo_id: str, ordered_ids: list[str]) -> None:
        todo = self.find(todo_id)
        if not todo or not todo.subtasks:
            return
        active = [item for item in todo.subtasks if not item.get("deleted_at")]
        if len(active) < 2:
            return
        active_by_id = {item.get("id"): item for item in active}
        next_active = [active_by_id[item_id] for item_id in ordered_ids if item_id in active_by_id]
        next_active.extend(item for item in active if item not in next_active)
        if [item.get("id") for item in next_active] == [item.get("id") for item in active]:
            return

        replacement = iter(next_active)
        next_subtasks = []
        for item in todo.subtasks:
            if item.get("deleted_at"):
                next_subtasks.append(item)
            else:
                next_item = next(replacement)
                next_item["updated_at"] = now_iso()
                next_subtasks.append(next_item)
        todo.subtasks = next_subtasks
        todo.updated_at = now_iso()
        self.save()

    def progress_counts_for_date(self, value: str) -> tuple[int, int]:
        remaining = 0
        done = 0
        for todo in self.visible_for_date(value):
            units = [todo.completed]
            units.extend(bool(item.get("completed_at")) for item in todo.active_subtasks())
            for unit_done in units:
                if unit_done:
                    done += 1
                else:
                    remaining += 1
        return remaining, done

    def deleted_for_date(self, value: str) -> list[Todo]:
        return [todo for todo in self.todos if todo.date == value and todo.deleted]

    def carry_unfinished_to_today(self) -> None:
        today = today_key()
        if self.last_open_date == today:
            return
        source = self.last_open_date
        existing_sources = {
            todo.source_id
            for todo in self.todos
            if todo.date == today and not todo.deleted and todo.source_id
        }
        for todo in self.incomplete_before_today(source):
            if todo.id in existing_sources:
                continue
            self.add(
                todo.text,
                today,
                source_id=todo.id,
                carried=True,
                subtasks=todo.active_subtasks(),
            )
        self.last_open_date = today
        self.save()


class HolidayProvider:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.memory: dict[int, dict[str, dict[str, str | bool]]] = {}
        self.lock = threading.RLock()

    def holidays_for_range(self, start: date, end: date) -> dict[str, dict[str, str | bool]]:
        years = range(start.year, end.year + 1)
        holidays: dict[str, dict[str, str | bool]] = {}
        for year in years:
            holidays.update(self.holidays_for_year(year))
        start_key = start.isoformat()
        end_key = end.isoformat()
        return {key: value for key, value in holidays.items() if start_key <= key <= end_key}

    def holidays_for_year(self, year: int) -> dict[str, dict[str, str | bool]]:
        with self.lock:
            if year in self.memory:
                return self.memory[year]

        cache_file = self.cache_dir / f"{HOLIDAY_COUNTRY}-{year}.json"
        cached_payload = self._read_cache(cache_file)
        payload = cached_payload
        if not cached_payload or self._cache_is_stale(cache_file):
            fetched_payload = self._fetch_year(year)
            if fetched_payload is not None:
                payload = fetched_payload
                self._write_cache(cache_file, fetched_payload)

        holidays = self._normalize_payload(payload or [])
        with self.lock:
            self.memory[year] = holidays
        return holidays

    def _cache_is_stale(self, cache_file: Path) -> bool:
        if not cache_file.exists():
            return True
        age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
        return age > timedelta(days=7)

    def _read_cache(self, cache_file: Path) -> list[dict] | dict | None:
        try:
            if cache_file.exists():
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(payload, (list, dict)):
                    return payload
        except (json.JSONDecodeError, OSError):
            return None
        return None

    def _write_cache(self, cache_file: Path, payload: list[dict] | dict) -> None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(cache_file)
        except OSError:
            return

    def _fetch_year(self, year: int) -> list[dict] | dict | None:
        payload = self._fetch_json(CHINA_HOLIDAY_API_URL.format(year=year))
        if payload is not None:
            return payload
        return self._fetch_json(NAGER_HOLIDAY_API_URL.format(year=year, country=HOLIDAY_COUNTRY))

    def _fetch_json(self, url: str) -> list[dict] | dict | None:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 TodoApp/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, (list, dict)) else None

    def _normalize_payload(self, payload: list[dict] | dict) -> dict[str, dict[str, str | bool]]:
        if isinstance(payload, dict):
            return self._normalize_china_payload(payload)
        return self._normalize_nager_payload(payload)

    def _normalize_china_payload(self, payload: dict) -> dict[str, dict[str, str | bool]]:
        holidays: dict[str, dict[str, str | bool]] = {}
        for key, item in payload.items():
            if not isinstance(item, dict):
                continue
            date_key = str(item.get("date") or key or "")
            if not date_key:
                continue
            name = str(item.get("name") or "节假日").strip()
            is_off_day = bool(item.get("isOffDay"))
            if not is_off_day and parse_date(date_key).weekday() < 5:
                continue
            label = name if is_off_day else "班"
            holidays[date_key] = {
                "name": name or label,
                "label": label,
                "off": is_off_day,
                "workday": not is_off_day,
                "source": "Jiejiari API",
            }
        return holidays

    def _normalize_nager_payload(self, payload: list[dict]) -> dict[str, dict[str, str | bool]]:
        holidays: dict[str, dict[str, str | bool]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = str(item.get("date") or "")
            if not key:
                continue
            name = str(item.get("localName") or item.get("name") or "节假日").strip()
            if not name:
                continue
            existing = holidays.get(key)
            if existing:
                names = existing["name"].split(" / ")
                if name not in names:
                    existing["name"] = f'{existing["name"]} / {name}'
                    existing["label"] = existing["name"]
                continue
            holidays[key] = {"name": name, "label": name, "off": True, "workday": False, "source": "Nager.Date"}
        return holidays


class Api:
    def __init__(self) -> None:
        migrate_legacy_data()
        self.store = TodoStore(DATA_FILE)
        self.holidays = HolidayProvider(HOLIDAY_CACHE_DIR)
        with self.store.lock:
            self.store.carry_unfinished_to_today()

    def get_state(self, current_date: str | None = None) -> dict:
        with self.store.lock:
            selected = parse_date(current_date).isoformat()
            todos = [self.serialize_todo(todo) for todo in self.store.visible_for_date(selected)]
            deleted_todos = [self.serialize_deleted_todo(todo) for todo in self.store.deleted_for_date(selected)]
            progress_by_date = {}
            all_dates = sorted({todo.date for todo in self.store.todos if not todo.deleted} | {today_key(), selected})
            for value in all_dates:
                remaining, done = self.store.progress_counts_for_date(value)
                progress_by_date[value] = {"remaining": remaining, "done": done, "total": remaining + done}
            remaining, done = self.store.progress_counts_for_date(selected)
            calendar_start, calendar_end = calendar_visible_range(selected)
            holidays_by_date = self.holidays.holidays_for_range(calendar_start, calendar_end)
            return {
                "today": today_key(),
                "current_date": selected,
                "pretty_date": pretty_date(selected),
                "weekday": weekday_name(selected),
                "is_today": selected == today_key(),
                "data_file": str(DATA_FILE),
                "todos": todos,
                "deleted_todos": deleted_todos,
                "progress": {"remaining": remaining, "done": done, "total": remaining + done},
                "deleted_count": len(deleted_todos),
                "progress_by_date": progress_by_date,
                "holidays_by_date": holidays_by_date,
                "holiday_source": {"name": "Jiejiari API / Nager.Date fallback", "country": HOLIDAY_COUNTRY},
            }

    def serialize_todo(self, todo: Todo) -> dict:
        active = todo.active_subtasks()
        sub_done = sum(1 for item in active if item.get("completed_at"))
        return {
            "id": todo.id,
            "text": todo.text,
            "date": todo.date,
            "completed": todo.completed,
            "completed_at": todo.completed_at,
            "carried": todo.carried_from_previous_day,
            "due_scope": todo.due_scope,
            "subtask_done": sub_done,
            "subtask_total": len(active),
            "subtasks": [
                {
                    "id": item.get("id"),
                    "text": item.get("text") or "",
                    "completed": bool(item.get("completed_at")),
                }
                for item in active
            ],
        }

    def serialize_deleted_todo(self, todo: Todo) -> dict:
        active = todo.active_subtasks()
        return {
            "id": todo.id,
            "text": todo.text,
            "date": todo.date,
            "completed": todo.completed,
            "deleted_at": todo.deleted_at,
            "subtask_total": len(active),
            "subtask_done": sum(1 for item in active if item.get("completed_at")),
        }

    def add_todo(self, text: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.add(text, parse_date(current_date).isoformat())
            return self.get_state(current_date)

    def update_todo_text(self, todo_id: str, text: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.update_text(todo_id, text)
            return self.get_state(current_date)

    def toggle_todo(self, todo_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.toggle(todo_id)
            return self.get_state(current_date)

    def delete_todo(self, todo_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.delete(todo_id)
            return self.get_state(current_date)

    def restore_todo(self, todo_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.restore(todo_id)
            return self.get_state(current_date)

    def purge_todo(self, todo_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.purge(todo_id)
            return self.get_state(current_date)

    def set_due_scope(self, todo_id: str, scope: str | None, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.set_due_scope(todo_id, scope)
            return self.get_state(current_date)

    def add_subtask(self, todo_id: str, text: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.add_subtask(todo_id, text)
            return self.get_state(current_date)

    def update_subtask_text(self, todo_id: str, subtask_id: str, text: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.update_subtask_text(todo_id, subtask_id, text)
            return self.get_state(current_date)

    def toggle_subtask(self, todo_id: str, subtask_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.toggle_subtask(todo_id, subtask_id)
            return self.get_state(current_date)

    def delete_subtask(self, todo_id: str, subtask_id: str, current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.delete_subtask(todo_id, subtask_id)
            return self.get_state(current_date)

    def reorder_todo_group(self, current_date: str, completed: bool, ordered_ids: list[str]) -> dict:
        with self.store.lock:
            self.store.reorder_todo_group(parse_date(current_date).isoformat(), bool(completed), list(ordered_ids or []))
            return self.get_state(current_date)

    def reorder_subtasks(self, todo_id: str, ordered_ids: list[str], current_date: str | None = None) -> dict:
        with self.store.lock:
            self.store.reorder_subtasks(todo_id, list(ordered_ids or []))
            return self.get_state(current_date)


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    @font-face {
      font-family: "TodoInter";
      font-style: normal;
      font-weight: 100 900;
      font-display: swap;
      src: url("__INTER_FONT__") format("woff2");
    }
    @font-face {
      font-family: "TodoNotoSC";
      font-style: normal;
      font-weight: 100 900;
      font-display: swap;
      src: url("__NOTO_FONT__") format("truetype");
    }
    :root {
      --bg: #f5f7fb;
      --bg2: #eef4ff;
      --surface: rgba(255,255,255,.86);
      --surface-solid: #ffffff;
      --text: #1d1d1f;
      --muted: #777d8b;
      --soft: #9aa1af;
      --line: #e3e6ec;
      --line-strong: #d9dee8;
      --blue: #0a84ff;
      --blue2: #5e5ce6;
      --red: #ff3b30;
      --red-soft: #fff0ee;
      --green: #30d158;
      --shadow: 0 26px 54px rgba(11,27,58,.10), 0 3px 12px rgba(11,27,58,.06);
      --button-shadow: 0 12px 22px rgba(29,29,31,.10);
      --font: "TodoInter", "Segoe UI Variable Display", "TodoNotoSC", "Microsoft YaHei UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body {
      font-family: var(--font);
      color: var(--text);
      background:
        radial-gradient(circle at 18% 8%, rgba(255,255,255,.95), rgba(255,255,255,0) 30%),
        linear-gradient(132deg, #fbfbfd 0%, var(--bg) 52%, var(--bg2) 100%);
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
      user-select: none;
    }
    body::after {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .12;
      background-image: linear-gradient(rgba(29,29,31,.035) 1px, transparent 1px);
      background-size: 100% 3px;
      mix-blend-mode: multiply;
    }
    .app {
      position: relative;
      display: flex;
      height: 100vh;
      min-width: 1040px;
      overflow: hidden;
    }
    .main {
      flex: 1 1 auto;
      min-width: 0;
      padding: 54px 42px 54px 96px;
      overflow: auto;
      scrollbar-width: none;
      -ms-overflow-style: none;
    }
    .main::-webkit-scrollbar { width: 0; height: 0; display: none; }
    .main-scroll-fade {
      position: absolute;
      left: var(--main-left, 0px);
      width: var(--main-width, 100%);
      pointer-events: none;
      z-index: 16;
      opacity: 0;
      transition: opacity .24s ease;
    }
    .main-scroll-fade.show { opacity: 1; }
    .main-scroll-fade.top {
      top: 0;
      height: 78px;
      background: linear-gradient(180deg, rgba(248,250,255,.96), rgba(248,250,255,0));
    }
    .main-scroll-fade.bottom {
      bottom: 0;
      height: 104px;
      background: linear-gradient(0deg, rgba(248,250,255,.96), rgba(248,250,255,0));
    }
    .hero {
      position: sticky;
      top: 0;
      z-index: 20;
      isolation: isolate;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 28px;
      padding-right: 8px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 28px;
      background: rgba(248,250,255,.985);
      backdrop-filter: blur(18px) saturate(1.16);
    }
    .hero::before {
      content: "";
      position: absolute;
      inset: -54px -42px 0 -96px;
      z-index: -1;
      background: rgba(248,250,255,.985);
      pointer-events: none;
    }
    .hero::after {
      content: "";
      position: absolute;
      left: -96px;
      right: -42px;
      bottom: -36px;
      height: 36px;
      z-index: -1;
      background: linear-gradient(180deg, rgba(248,250,255,.94), rgba(248,250,255,0));
      pointer-events: none;
    }
    .hero-copy {
      --logo-width: clamp(360px, 36vw, 500px);
      --logo-mark-shift: clamp(-96px, -6.75vw, -72px);
      min-width: 0;
    }
    h1 {
      margin: 0;
      font-size: clamp(44px, 4.2vw, 66px);
      line-height: .96;
      letter-spacing: 0;
      font-weight: 820;
      color: var(--text);
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .title-logo {
      display: block;
      width: var(--logo-width);
      height: 142px;
      margin: 8px 0 -2px var(--logo-mark-shift);
      user-select: none;
      border: 0;
      padding: 0;
      background: transparent;
      cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .title-logo svg {
      width: 100%;
      height: 100%;
      display: block;
      overflow: visible;
      text-rendering: geometricPrecision;
    }
    .title-logo:active { transform: scale(.992); }
    .title-logo:focus-visible {
      outline: 3px solid rgba(0,132,255,.24);
      outline-offset: 6px;
      border-radius: 28px;
    }
    __LOGO_MOTION_CSS__
    .hero-actions {
      display: flex;
      align-items: center;
      gap: 16px;
      padding-top: 4px;
    }
    button {
      font: inherit;
      border: 0;
      outline: 0;
      cursor: pointer;
      color: inherit;
    }
    .pill {
      height: 48px;
      padding: 0 22px;
      border-radius: 999px;
      background: rgba(255,255,255,.9);
      border: 1px solid rgba(227,230,236,.9);
      box-shadow: var(--button-shadow);
      font-size: 15px;
      font-weight: 760;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      transition: transform .18s ease, background .18s ease, box-shadow .18s ease;
    }
    .pill:hover { transform: translateY(-1px); box-shadow: 0 16px 30px rgba(29,29,31,.12); }
    .plus {
      width: 56px;
      height: 48px;
      border-radius: 18px;
      background: #eef1f7;
      box-shadow: none;
      justify-content: center;
      padding: 0;
    }
    .plus-icon, .mini-plus {
      position: relative;
      display: inline-grid;
      place-items: center;
    }
    .plus-icon {
      width: 22px;
      height: 22px;
    }
    .plus-icon::before, .plus-icon::after,
    .mini-plus::before, .mini-plus::after {
      content: "";
      position: absolute;
      border-radius: 999px;
      background: currentColor;
    }
    .plus-icon::before { width: 16px; height: 2px; }
    .plus-icon::after { width: 2px; height: 16px; }
    .calendar-icon {
      width: 17px;
      height: 17px;
      border: 2px solid currentColor;
      border-radius: 5px;
      display: inline-block;
      position: relative;
    }
    .calendar-icon::before {
      content: "";
      position: absolute;
      left: -2px;
      right: -2px;
      top: 4px;
      height: 2px;
      background: currentColor;
    }
    .list {
      display: grid;
      gap: 24px;
      padding: 44px 8px 36px 0;
    }
    .task-card, .add-card {
      position: relative;
      min-height: 178px;
      border-radius: 32px;
      border: 1px solid var(--line-strong);
      background: linear-gradient(145deg, rgba(255,255,255,.96), rgba(248,250,255,.9));
      box-shadow: var(--shadow);
      padding: 34px 34px 28px 34px;
      display: grid;
      grid-template-columns: 34px minmax(0, 1fr) auto;
      gap: 24px;
      align-items: start;
      transform: translateY(0);
      transition: opacity .84s cubic-bezier(.22,1,.36,1), border-color .68s ease, background .68s ease, transform .68s cubic-bezier(.22,1,.36,1), box-shadow .68s ease;
      animation: cardIn .28s cubic-bezier(.18,.9,.2,1);
      touch-action: none;
    }
    .task-card:hover {
      transform: translateY(-2px);
      border-color: #cfd6e4;
      box-shadow: 0 34px 68px rgba(11,27,58,.12), 0 5px 18px rgba(11,27,58,.07);
    }
    .task-card.done {
      opacity: .40;
      border-color: rgba(217,222,232,.54);
      background: linear-gradient(145deg, rgba(255,255,255,.72), rgba(248,250,255,.54));
      box-shadow: 0 12px 26px rgba(11,27,58,.045);
    }
    .task-card.done:hover {
      opacity: .52;
      box-shadow: 0 16px 34px rgba(11,27,58,.06);
    }
    .task-card.dragging, .subtask.dragging {
      cursor: grabbing;
      opacity: .96 !important;
      pointer-events: none;
      box-sizing: border-box;
      margin: 0;
      box-shadow: 0 38px 78px rgba(11,27,58,.18), 0 8px 22px rgba(11,27,58,.10);
      transform: scale(1.025);
      z-index: 50;
    }
    .subtask.dragging {
      max-width: none;
      transform: none;
    }
    .subtask.dragging.drag-ready {
      animation: none;
    }
    .task-card.drag-ready, .subtask.drag-ready {
      animation: liftWiggle .52s ease-in-out infinite alternate;
    }
    body.reorder-active .task-card:not(.dragging):not(.settling),
    body.reorder-active .subtask:not(.dragging):not(.settling) {
      animation: organizeFloat 1.1s ease-in-out infinite alternate;
      will-change: transform;
    }
    .task-card.urgent {
      border-color: rgba(255,69,58,.48);
      background:
        linear-gradient(145deg, rgba(255,255,255,.94), rgba(248,250,255,.88)) padding-box,
        linear-gradient(135deg, rgba(255,59,48,.44), rgba(255,45,85,.48)) border-box;
      box-shadow: 0 28px 58px rgba(255,59,48,.12), 0 5px 18px rgba(255,45,85,.06);
    }
    .task-card.urgent:hover {
      border-color: rgba(255,69,58,.62);
      box-shadow: 0 34px 68px rgba(255,59,48,.15), 0 6px 22px rgba(255,45,85,.08);
    }
    .task-card.week {
      border-color: rgba(94,92,230,.48);
      background:
        linear-gradient(145deg, rgba(255,255,255,.94), rgba(248,250,255,.88)) padding-box,
        linear-gradient(135deg, rgba(10,132,255,.45), rgba(94,92,230,.50)) border-box;
      box-shadow: 0 28px 58px rgba(94,92,230,.12), 0 5px 18px rgba(10,132,255,.06);
    }
    .task-card.week:hover {
      border-color: rgba(94,92,230,.62);
      box-shadow: 0 34px 68px rgba(94,92,230,.15), 0 6px 22px rgba(10,132,255,.08);
    }
    .task-card.urgent.week {
      border-color: rgba(255,59,48,.56);
      background:
        linear-gradient(145deg, rgba(255,255,255,.94), rgba(248,250,255,.88)) padding-box,
        linear-gradient(135deg, rgba(255,59,48,.44), rgba(255,45,85,.48)) border-box;
      box-shadow: 0 28px 58px rgba(255,59,48,.13), 0 5px 18px rgba(11,27,58,.06);
    }
    .check {
      width: 29px;
      height: 29px;
      border-radius: 10px;
      border: 1.7px solid #aeb4c0;
      background: rgba(255,255,255,.78);
      margin-top: 3px;
      display: grid;
      place-items: center;
      transition: all .2s cubic-bezier(.2,.9,.2,1);
    }
    .check::after {
      content: "";
      width: 11px;
      height: 6px;
      border-left: 2px solid white;
      border-bottom: 2px solid white;
      transform: rotate(-45deg) scale(.4);
      opacity: 0;
      transition: all .18s ease;
    }
    .task-card.done .check, .subtask.done .sub-check {
      background: var(--blue);
      border-color: var(--blue);
      transform: scale(1.04);
    }
    .task-card.done .check::after, .subtask.done .sub-check::after {
      opacity: 1;
      transform: rotate(-45deg) scale(1);
    }
    .task-body { min-width: 0; }
    .task-title {
      display: inline-block;
      max-width: 100%;
      min-width: 64px;
      min-height: 32px;
      outline: none;
      font-size: clamp(22px, 1.65vw, 29px);
      line-height: 1.25;
      font-weight: 760;
      letter-spacing: 0;
      word-break: break-word;
      border-radius: 12px;
      padding: 1px 4px;
      margin: -1px -4px;
    }
    .task-title:focus, .sub-text:focus, .add-input:focus {
      background: rgba(10,132,255,.08);
      box-shadow: 0 0 0 4px rgba(10,132,255,.08);
    }
    .task-card.done .task-title, .subtask.done .sub-text {
      color: var(--soft);
      text-decoration: line-through;
      text-decoration-thickness: 2px;
      text-decoration-color: rgba(138,144,160,.8);
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 13px;
      align-items: center;
    }
    .chip {
      height: 29px;
      border-radius: 11px;
      padding: 0 12px;
      display: inline-flex;
      align-items: center;
      background: #f2f4f8;
      color: #6f7685;
      font-size: 13px;
      font-weight: 780;
    }
    .chip.red { color: var(--red); background: var(--red-soft); }
    .chip.blue { color: var(--blue); background: #eef5ff; }
    .subtasks {
      margin-top: 20px;
      display: grid;
      gap: 12px;
    }
    .subtask {
      min-height: 54px;
      border-radius: 22px;
      background: rgba(247,249,253,.92);
      border: 1px solid #edf0f6;
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr) 24px;
      align-items: center;
      gap: 12px;
      padding: 11px 14px 11px 16px;
      transition: transform .24s cubic-bezier(.22,1,.36,1), box-shadow .24s ease, background .2s ease;
      touch-action: none;
    }
    .sub-check {
      width: 24px;
      height: 24px;
      border-radius: 9px;
      border: 1.5px solid #b7becb;
      background: white;
      display: grid;
      place-items: center;
      transition: all .18s ease;
    }
    .sub-check::after {
      content: "";
      width: 9px;
      height: 5px;
      border-left: 2px solid white;
      border-bottom: 2px solid white;
      transform: rotate(-45deg) scale(.4);
      opacity: 0;
      transition: all .18s ease;
    }
    .sub-text {
      outline: none;
      min-height: 26px;
      border-radius: 10px;
      font-size: clamp(17px, 1.18vw, 22px);
      line-height: 1.3;
      font-weight: 700;
      color: #303644;
      word-break: break-word;
    }
    .sub-delete, .icon-btn {
      width: 30px;
      height: 30px;
      border-radius: 999px;
      background: transparent;
      color: #9ca3b1;
      display: grid;
      place-items: center;
      transition: background .16s ease, color .16s ease, transform .16s ease;
    }
    .sub-delete {
      width: 24px;
      height: 24px;
    }
    .remove-icon {
      position: relative;
      width: 19px;
      height: 19px;
      border-radius: 999px;
      background: #eef1f6;
      box-shadow: inset 0 0 0 1px rgba(172,180,195,.42);
      transition: background .16s ease, box-shadow .16s ease;
    }
    .remove-icon::before {
      content: "";
      position: absolute;
      left: 5.5px;
      right: 5.5px;
      top: 8.5px;
      height: 1.8px;
      border-radius: 999px;
      background: #8f97a6;
    }
    .remove-icon.small {
      width: 17px;
      height: 17px;
    }
    .remove-icon.small::before {
      left: 5px;
      right: 5px;
      top: 7.6px;
    }
    .sub-delete:hover, .icon-btn:hover { background: #f0f2f7; color: #3e4656; transform: translateY(-1px); }
    .sub-delete:hover .remove-icon, .icon-btn:hover .remove-icon {
      background: #ff453a;
      box-shadow: inset 0 0 0 1px rgba(255,69,58,.22);
    }
    .sub-delete:hover .remove-icon::before, .icon-btn:hover .remove-icon::before {
      background: white;
    }
    .sub-add {
      min-height: 50px;
      border-radius: 22px;
      border: 1px dashed #d9dee8;
      color: #8a90a0;
      background: rgba(255,255,255,.58);
      display: grid;
      grid-template-columns: 26px minmax(0, 1fr);
      gap: 14px;
      align-items: center;
      padding: 10px 16px;
      opacity: .74;
      transition: opacity .16s ease, border-color .16s ease, background .16s ease;
    }
    .sub-add:hover, .task-card.editing-sub .sub-add {
      opacity: 1;
      border-color: rgba(10,132,255,.34);
      background: rgba(238,245,255,.68);
    }
    .mini-plus {
      width: 26px;
      height: 26px;
      border-radius: 999px;
      border: 1px solid #d0d6e1;
      color: var(--blue);
      font-weight: 850;
      background: rgba(255,255,255,.74);
      font-size: 0;
    }
    .mini-plus::before { width: 11px; height: 1.8px; }
    .mini-plus::after { width: 1.8px; height: 11px; }
    .drop-slot {
      border: 1px dashed rgba(10,132,255,.38);
      background: rgba(10,132,255,.055);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.66);
    }
    .drop-slot.task-slot {
      min-height: 178px;
      border-radius: 32px;
    }
    .drop-slot.sub-slot {
      min-height: 54px;
      border-radius: 22px;
    }
    .reorder-active, .reorder-active * {
      cursor: grabbing !important;
      user-select: none !important;
    }
    .sub-input {
      width: 100%;
      border: 0;
      outline: 0;
      background: transparent;
      font: inherit;
      color: var(--text);
      font-size: 17px;
      font-weight: 720;
    }
    .task-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      padding-top: 1px;
    }
    .due-btn {
      width: 45px;
      height: 45px;
      border-radius: 16px;
      background: #f4f6fa;
      color: #c0c6d2;
      font-size: 16px;
      font-weight: 880;
      transition: all .18s ease;
    }
    .due-btn.urgent.active {
      background: linear-gradient(135deg, var(--red), #ff2d55);
      color: white;
      box-shadow: 0 14px 24px rgba(255,59,48,.26);
    }
    .due-btn.urgent:not(.active) {
      color: var(--red);
      background: var(--red-soft);
    }
    .due-btn.week.active {
      background: linear-gradient(135deg, var(--blue), var(--blue2));
      color: white;
      box-shadow: 0 14px 24px rgba(94,92,230,.22);
    }
    .empty {
      min-height: 260px;
      border-radius: 34px;
      border: 1px dashed #d8deea;
      background: rgba(255,255,255,.48);
      display: grid;
      place-items: center;
      color: var(--soft);
      text-align: center;
      font-size: 18px;
      font-weight: 720;
    }
    .add-card {
      min-height: 132px;
      grid-template-columns: 42px minmax(0,1fr) auto;
      align-items: center;
      border-style: dashed;
      background: rgba(255,255,255,.64);
      box-shadow: none;
    }
    .add-input {
      width: 100%;
      border: 0;
      outline: 0;
      background: transparent;
      color: var(--text);
      font-size: clamp(22px, 1.6vw, 29px);
      font-weight: 760;
      font-family: var(--font);
    }
    .ghost { color: #a8afbd; }
    .save-add {
      min-width: 78px;
      height: 44px;
      border-radius: 16px;
      background: var(--blue);
      color: white;
      font-weight: 800;
      box-shadow: 0 16px 28px rgba(10,132,255,.22);
    }
    .splitter {
      width: 12px;
      flex: 0 0 12px;
      cursor: col-resize;
      position: relative;
      background: transparent;
    }
    .splitter::before {
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: 5px;
      width: 1px;
      background: #cfd4df;
    }
    .splitter::after {
      content: "";
      position: absolute;
      top: 50%;
      left: 3px;
      width: 6px;
      height: 38px;
      border-radius: 999px;
      background: rgba(207,212,223,.7);
      transform: translateY(-50%);
      opacity: 0;
      transition: opacity .16s ease;
    }
    .splitter:hover::after { opacity: 1; }
    .calendar-pane {
      flex: 0 0 var(--cal-width, 520px);
      width: var(--cal-width, 520px);
      min-width: 520px;
      max-width: 720px;
      padding: 44px 38px 50px 24px;
      transition: width .18s ease, flex-basis .18s ease, opacity .18s ease, transform .18s ease;
      overflow: hidden;
    }
    .app.calendar-hidden .calendar-pane, .app.calendar-hidden .splitter {
      width: 0;
      flex-basis: 0;
      min-width: 0;
      padding-left: 0;
      padding-right: 0;
      opacity: 0;
      pointer-events: none;
      transform: translateX(16px);
    }
    .calendar-card {
      height: 100%;
      min-height: 610px;
      border-radius: 32px;
      border: 1px solid #edf0f6;
      background: rgba(255,255,255,.78);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px) saturate(1.25);
      padding: 38px;
      overflow: hidden;
    }
    .progress {
      height: 7px;
      border-radius: 999px;
      background: #e5e8ef;
      overflow: hidden;
      margin-bottom: 54px;
    }
    .progress > span {
      display: block;
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, var(--blue), var(--blue2));
      border-radius: inherit;
      transition: width .32s cubic-bezier(.2,.9,.2,1);
    }
    .calendar-nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 24px;
    }
    .calendar-title-wrap {
      min-width: 0;
      text-align: center;
    }
    .calendar-month {
      margin-top: 0;
      color: var(--muted);
      font-size: 15px;
      font-weight: 800;
    }
    .month-arrow {
      width: 38px;
      height: 38px;
      flex: 0 0 38px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background: rgba(247,249,253,.92);
      border: 1px solid rgba(227,230,236,.92);
      color: #333b49;
      font-size: 24px;
      line-height: 1;
      font-weight: 720;
      box-shadow: 0 10px 20px rgba(11,27,58,.055);
      transition: transform .18s ease, background .18s ease, box-shadow .18s ease;
    }
    .month-arrow:hover {
      transform: translateY(-1px);
      background: rgba(255,255,255,.98);
      box-shadow: 0 14px 26px rgba(11,27,58,.08);
    }
    .calendar-head, .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(40px, 1fr));
      gap: 12px 7px;
      text-align: center;
      min-width: 322px;
    }
    .calendar-head {
      margin-bottom: 16px;
      color: #3d4656;
      font-size: 14px;
      font-weight: 820;
    }
    .day {
      position: relative;
      min-width: 40px;
      height: 52px;
      border-radius: 16px;
      background: transparent;
      color: #3d4656;
      font-size: 17px;
      font-weight: 710;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 3px;
      padding: 5px 2px 7px;
      transition: background .16s ease, color .16s ease, transform .16s ease;
      overflow: hidden;
    }
    .day:hover { background: rgba(10,132,255,.08); transform: translateY(-1px); }
    .day.out { color: #c5cad4; }
    .day-number {
      line-height: 1;
      white-space: nowrap;
    }
    .holiday-name {
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 9.5px;
      line-height: 1;
      font-weight: 830;
      color: var(--red);
    }
    .day.holiday:not(.selected) .day-number { color: var(--red); }
    .day.workday:not(.selected) .day-number { color: #687283; }
    .day.workday .holiday-name { color: #7d8798; }
    .day.out .holiday-name { color: rgba(255,59,48,.36); }
    .day.out.workday .holiday-name { color: rgba(125,135,152,.36); }
    .day.selected {
      background: var(--blue);
      color: white;
      font-weight: 880;
      box-shadow: 0 12px 22px rgba(10,132,255,.26);
    }
    .day.selected .holiday-name { color: rgba(255,255,255,.92); }
    .day.today:not(.selected) {
      color: var(--blue);
      box-shadow: inset 0 0 0 1px rgba(10,132,255,.55);
    }
    .dot {
      position: absolute;
      bottom: 3px;
      width: 6px;
      height: 6px;
      border-radius: 999px;
      background: var(--blue);
    }
    .dot.complete { background: var(--green); }
    .calendar-summary {
      margin-top: 34px;
      display: flex;
      gap: 14px;
    }
    .summary-box {
      flex: 1;
      border-radius: 24px;
      background: rgba(247,249,253,.88);
      border: 1px solid #edf0f6;
      padding: 18px 18px;
      text-align: left;
      color: var(--text);
      transition: transform .18s ease, background .18s ease, border-color .18s ease, box-shadow .18s ease;
    }
    .summary-box.actionable:hover {
      transform: translateY(-2px);
      background: rgba(255,255,255,.96);
      border-color: rgba(10,132,255,.22);
      box-shadow: 0 12px 24px rgba(10,132,255,.08);
    }
    .summary-box.deleted strong { color: #8f97a6; }
    .summary-box strong {
      display: block;
      font-size: 25px;
      line-height: 1;
      margin-bottom: 9px;
    }
    .summary-box span {
      color: var(--muted);
      font-weight: 750;
      font-size: 13px;
    }
    .deleted-panel {
      position: fixed;
      right: 54px;
      bottom: 54px;
      width: min(420px, calc(100vw - 108px));
      max-height: min(520px, calc(100vh - 108px));
      border-radius: 30px;
      border: 1px solid rgba(227,230,236,.92);
      background: rgba(255,255,255,.86);
      backdrop-filter: blur(24px) saturate(1.22);
      box-shadow: 0 34px 72px rgba(11,27,58,.16), 0 6px 20px rgba(11,27,58,.08);
      padding: 24px;
      z-index: 30;
      opacity: 0;
      transform: translateY(18px) scale(.98);
      pointer-events: none;
      transition: opacity .24s ease, transform .24s cubic-bezier(.22,1,.36,1);
    }
    .deleted-panel.show {
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }
    .deleted-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }
    .deleted-head h3 {
      margin: 0;
      font-size: 20px;
      line-height: 1.1;
    }
    .deleted-list {
      display: grid;
      gap: 12px;
      overflow: auto;
      max-height: 380px;
      padding-right: 2px;
    }
    .deleted-item {
      border-radius: 20px;
      border: 1px solid #edf0f6;
      background: rgba(247,249,253,.86);
      padding: 15px 15px 15px 17px;
      display: grid;
      grid-template-columns: minmax(0,1fr) auto;
      gap: 12px;
      align-items: center;
    }
    .deleted-title {
      font-size: 15px;
      font-weight: 800;
      color: #3d4656;
      word-break: break-word;
    }
    .deleted-meta {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
      font-weight: 700;
    }
    .restore-btn {
      height: 36px;
      padding: 0 14px;
      border-radius: 999px;
      background: #eef5ff;
      color: var(--blue);
      font-weight: 820;
      transition: transform .16s ease, background .16s ease;
    }
    .restore-btn:hover { transform: translateY(-1px); background: #e2efff; }
    .purge-btn {
      height: 36px;
      padding: 0 14px;
      border-radius: 999px;
      background: #fff0ee;
      color: var(--red);
      font-weight: 820;
      transition: transform .16s ease, background .16s ease;
    }
    .purge-btn:hover { transform: translateY(-1px); background: #ffe1dd; }
    .deleted-actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
    .deleted-empty {
      min-height: 116px;
      border-radius: 22px;
      border: 1px dashed #d8deea;
      display: grid;
      place-items: center;
      color: var(--soft);
      font-size: 14px;
      font-weight: 760;
      text-align: center;
    }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 28px;
      transform: translate(-50%, 20px);
      opacity: 0;
      pointer-events: none;
      padding: 13px 18px;
      border-radius: 999px;
      background: rgba(29,29,31,.88);
      color: white;
      font-size: 14px;
      font-weight: 760;
      box-shadow: 0 18px 34px rgba(29,29,31,.22);
      transition: opacity .22s ease, transform .22s ease;
      z-index: 20;
    }
    .toast.show { opacity: 1; transform: translate(-50%, 0); }
    @keyframes cardIn {
      from { opacity: 0; transform: translateY(12px) scale(.992); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes liftWiggle {
      from { transform: translateY(-1px) rotate(-.18deg); }
      to { transform: translateY(1px) rotate(.18deg); }
    }
    @keyframes organizeFloat {
      from { transform: translateY(-.5px) rotate(-.08deg); }
      to { transform: translateY(.5px) rotate(.08deg); }
    }
    @media (max-width: 1180px) {
      .main { padding-left: 58px; }
      .calendar-pane { padding-right: 24px; }
      .task-actions { flex-wrap: wrap; justify-content: flex-end; }
    }
  </style>
</head>
<body>
  <div class="app" id="app">
    <main class="main" id="main">
      <header class="hero">
        <div class="hero-copy">
          <button class="title-logo" id="titleLogo" type="button" title="播放 Logo 动画" aria-label="播放 TodoApp Logo 动画">
            __LOGO_MARKUP__
          </button>
          <h1 id="title" class="sr-only">今日任务</h1>
        </div>
        <div class="hero-actions">
          <button class="pill" id="calendarToggle" title="显示或隐藏日历"><span class="calendar-icon"></span><span>日历</span></button>
          <button class="pill plus" id="addButton" title="添加任务"><span class="plus-icon"></span></button>
        </div>
      </header>
      <section class="list" id="list"></section>
    </main>
    <div class="main-scroll-fade top" id="mainFadeTop"></div>
    <div class="main-scroll-fade bottom" id="mainFadeBottom"></div>
    <div class="splitter" id="splitter" title="拖动调整日历宽度"></div>
    <aside class="calendar-pane" id="calendarPane">
      <section class="calendar-card">
        <div class="progress"><span id="progressBar"></span></div>
        <div class="calendar-nav">
          <button class="month-arrow" id="prevMonth" title="上个月">‹</button>
          <div class="calendar-title-wrap">
            <div class="calendar-month" id="calendarMonth"></div>
          </div>
          <button class="month-arrow" id="nextMonth" title="下个月">›</button>
        </div>
        <div class="calendar-head"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
        <div class="calendar-grid" id="calendarGrid"></div>
        <div class="calendar-summary">
          <div class="summary-box"><strong id="remainingCount">0</strong><span>未完成</span></div>
          <div class="summary-box"><strong id="doneCount">0</strong><span>已完成</span></div>
          <button class="summary-box deleted actionable" id="deletedSummary"><strong id="deletedCount">0</strong><span>已删除</span></button>
        </div>
      </section>
    </aside>
  </div>
  <section class="deleted-panel" id="deletedPanel">
    <div class="deleted-head">
      <h3>已删除任务</h3>
      <button class="sub-delete" id="closeDeletedPanel" title="关闭"><span class="remove-icon small"></span></button>
    </div>
    <div class="deleted-list" id="deletedList"></div>
  </section>
  <div class="toast" id="toast"></div>
  <script>
    const app = document.getElementById('app');
    const main = document.getElementById('main');
    const list = document.getElementById('list');
    const title = document.getElementById('title');
    const calendarGrid = document.getElementById('calendarGrid');
    const calendarMonth = document.getElementById('calendarMonth');
    const prevMonth = document.getElementById('prevMonth');
    const nextMonth = document.getElementById('nextMonth');
    const progressBar = document.getElementById('progressBar');
    const remainingCount = document.getElementById('remainingCount');
    const doneCount = document.getElementById('doneCount');
    const deletedCount = document.getElementById('deletedCount');
    const deletedSummary = document.getElementById('deletedSummary');
    const deletedPanel = document.getElementById('deletedPanel');
    const deletedList = document.getElementById('deletedList');
    const closeDeletedPanel = document.getElementById('closeDeletedPanel');
    const calendarPane = document.getElementById('calendarPane');
    const splitter = document.getElementById('splitter');
    const mainFadeTop = document.getElementById('mainFadeTop');
    const mainFadeBottom = document.getElementById('mainFadeBottom');
    const toast = document.getElementById('toast');
    const titleLogo = document.getElementById('titleLogo');
    let state = null;
    let adding = false;
    let toastTimer = null;
    let logoTimer = null;
    let scrollFrame = null;
    let dragState = null;
    const LONG_PRESS_MS = 420;
    const CALENDAR_MIN_WIDTH = 520;
    const CALENDAR_MAX_WIDTH = 720;

    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }

    function notify(message) {
      toast.textContent = message;
      toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove('show'), 1300);
    }

    function playTitleLogo() {
      if (!titleLogo) return;
      titleLogo.classList.remove('playing');
      void titleLogo.offsetWidth;
      titleLogo.classList.add('playing');
      clearTimeout(logoTimer);
      logoTimer = setTimeout(() => titleLogo.classList.remove('playing'), 3200);
    }

    function updateScrollChrome(active = false) {
      if (!mainFadeTop || !mainFadeBottom) return;
      const maxScroll = Math.max(0, main.scrollHeight - main.clientHeight);
      const appRect = app.getBoundingClientRect();
      const mainRect = main.getBoundingClientRect();
      app.style.setProperty('--main-left', `${mainRect.left - appRect.left}px`);
      app.style.setProperty('--main-width', `${mainRect.width}px`);
      const canScroll = maxScroll > 2;
      const atTop = main.scrollTop <= 2;
      const atBottom = !canScroll || main.scrollTop >= maxScroll - 2;
      mainFadeTop.classList.toggle('show', canScroll && !atTop);
      mainFadeBottom.classList.toggle('show', canScroll && !atBottom);
    }

    function scheduleScrollChrome(active = false) {
      if (scrollFrame) cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(() => {
        updateScrollChrome(active);
        scrollFrame = null;
      });
    }


    function openDeletedPanel() {
      renderDeletedPanel();
      deletedPanel.classList.add('show');
    }

    function closeDeleted() {
      deletedPanel.classList.remove('show');
    }

    function renderDeletedPanel() {
      const deleted = state?.deleted_todos || [];
      if (!deleted.length) {
        deletedList.innerHTML = `<div class="deleted-empty">这一天没有已删除任务</div>`;
        return;
      }
      deletedList.innerHTML = deleted.map(todo => {
        const summary = todo.subtask_total
          ? `${todo.subtask_done}/${todo.subtask_total} 子任务 · ${todo.completed ? '已完成' : '未完成'}`
          : `${todo.completed ? '已完成' : '未完成'}`;
        return `
          <div class="deleted-item" data-id="${todo.id}">
            <div>
              <div class="deleted-title">${esc(todo.text)}</div>
              <div class="deleted-meta">${summary}</div>
            </div>
            <div class="deleted-actions">
              <button class="restore-btn" data-action="restore">恢复</button>
              <button class="purge-btn" data-action="purge">彻底删除</button>
            </div>
          </div>
        `;
      }).join('');
      deletedList.querySelectorAll('[data-action="restore"]').forEach(button => {
        button.onclick = async () => {
          const id = button.closest('.deleted-item').dataset.id;
          state = await window.pywebview.api.restore_todo(id, state.current_date);
          render();
          notify('任务已恢复');
        };
      });
      deletedList.querySelectorAll('[data-action="purge"]').forEach(button => {
        button.onclick = async () => {
          const id = button.closest('.deleted-item').dataset.id;
          if (!confirm('彻底删除后无法恢复，确定删除这个任务吗？')) return;
          state = await window.pywebview.api.purge_todo(id, state.current_date);
          render();
          notify('任务已彻底删除');
        };
      });
    }

    async function load(currentDate) {
      state = await window.pywebview.api.get_state(currentDate || (state && state.current_date));
      render();
    }

    async function mutate(promise, message) {
      state = await promise;
      render();
      if (message) notify(message);
    }

    function render() {
      if (!state) return;
      title.textContent = state.is_today ? '今日任务' : '这一天的待办';
      remainingCount.textContent = state.progress.remaining;
      doneCount.textContent = state.progress.done;
      deletedCount.textContent = state.deleted_count || 0;
      const total = Math.max(1, state.progress.total);
      progressBar.style.width = `${Math.round(state.progress.done / total * 100)}%`;
      renderTasks();
      renderCalendar();
      if (deletedPanel.classList.contains('show')) renderDeletedPanel();
      requestAnimationFrame(() => updateScrollChrome(false));
    }

    function renderTasks() {
      const previousRects = captureTaskRects();
      const blocks = [];
      if (adding) blocks.push(addCardTemplate());
      if (!state.todos.length && !adding) {
        blocks.push(`<div class="empty"><div>今天还很清爽<br><span class="ghost">点右上角 + 添加第一件事</span></div></div>`);
      }
      for (const todo of displayTodos()) blocks.push(taskTemplate(todo));
      list.innerHTML = blocks.join('');
      wireTaskEvents();
      animateTaskMoves(previousRects);
      const input = document.getElementById('newTaskInput');
      if (input) {
        input.focus();
        input.select();
      }
    }

    function displayTodos() {
      const pending = [];
      const completed = [];
      for (const todo of state.todos) {
        (todo.completed ? completed : pending).push(todo);
      }
      return pending.concat(completed);
    }

    function captureTaskRects() {
      const rects = new Map();
      document.querySelectorAll('.task-card[data-id]').forEach(card => {
        rects.set(card.dataset.id, card.getBoundingClientRect());
      });
      return rects;
    }

    function animateTaskMoves(previousRects) {
      if (!previousRects || !previousRects.size) return;
      requestAnimationFrame(() => {
        document.querySelectorAll('.task-card[data-id]').forEach(card => {
          const previous = previousRects.get(card.dataset.id);
          if (!previous) return;
          const next = card.getBoundingClientRect();
          const dx = previous.left - next.left;
          const dy = previous.top - next.top;
          if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
          const done = card.classList.contains('done');
          const distance = Math.hypot(dx, dy);
          card.animate(
            [
              { transform: `translate(${dx}px, ${dy}px) scale(.996)`, opacity: done ? .72 : .88, offset: 0 },
              { transform: `translate(${dx * .16}px, ${dy * .16}px) scale(.999)`, opacity: done ? .46 : .98, offset: .76 },
              { transform: 'translate(0, 0) scale(1)', opacity: done ? .40 : 1, offset: 1 }
            ],
            {
              duration: Math.min(2480, Math.max(1440, 960 + distance * 1.12)),
              easing: 'cubic-bezier(.22,1,.36,1)'
            }
          );
        });
      });
    }

    function startLongPressReorder(event, item, kind, options = {}) {
      if (dragState || (event.pointerType === 'mouse' && event.button !== 0)) return;
      if (event.target.closest('button, input')) return;
      if (kind === 'task' && event.target.closest('.subtasks')) return;
      if (options.preventDefault) event.preventDefault();
      const originX = event.clientX;
      const originY = event.clientY;
      let latestX = originX;
      let latestY = originY;
      let armed = true;
      const timer = setTimeout(() => {
        if (!armed) return;
        beginReorderDrag(event, item, kind, latestX, latestY);
      }, options.delay ?? LONG_PRESS_MS);

      const cancel = () => {
        armed = false;
        clearTimeout(timer);
        document.removeEventListener('pointerup', cancel);
        document.removeEventListener('pointercancel', cancel);
        document.removeEventListener('pointermove', watchBeforeDrag);
      };
      const watchBeforeDrag = moveEvent => {
        latestX = moveEvent.clientX;
        latestY = moveEvent.clientY;
        if (!options.tolerateMove && Math.hypot(latestX - originX, latestY - originY) > 12) cancel();
      };

      document.addEventListener('pointerup', cancel);
      document.addEventListener('pointercancel', cancel);
      document.addEventListener('pointermove', watchBeforeDrag);
    }

    function beginReorderDrag(event, item, kind, originX, originY) {
      item.querySelector('[contenteditable="true"]')?.blur();
      window.getSelection?.().removeAllRanges();
      const rect = item.getBoundingClientRect();
      const sourceParent = item.parentNode;
      const sourceNext = item.nextSibling;
      const slot = document.createElement('div');
      slot.className = kind === 'task' ? 'drop-slot task-slot' : 'drop-slot sub-slot';
      slot.style.height = `${rect.height}px`;
      sourceParent.insertBefore(slot, item);
      document.body.appendChild(item);
      item.classList.add('dragging', 'drag-ready');
      Object.assign(item.style, {
        position: 'fixed',
        left: `${rect.left}px`,
        top: `${rect.top}px`,
        width: `${rect.width}px`,
        height: `${rect.height}px`,
        zIndex: 1000
      });
      document.body.classList.add('reorder-active');
      dragState = {
        kind,
        item,
        slot,
        sourceParent,
        sourceNext,
        startLeft: rect.left,
        startTop: rect.top,
        startWidth: rect.width,
        offsetX: originX - rect.left,
        offsetY: originY - rect.top,
        moved: false,
        completed: item.classList.contains('done'),
        parentId: item.dataset.parent || null
      };
      document.addEventListener('pointermove', onReorderMove, { passive: false });
      document.addEventListener('pointerup', finishReorderDrag);
      document.addEventListener('pointercancel', cancelReorderDrag);
      notify(kind === 'task' ? '拖动卡片调整顺序' : '拖动子任务调整顺序');
    }

    function onReorderMove(event) {
      if (!dragState) return;
      event.preventDefault();
      dragState.moved = true;
      const left = dragState.kind === 'subtask' ? dragState.startLeft : event.clientX - dragState.offsetX;
      const top = event.clientY - dragState.offsetY;
      dragState.item.style.left = `${left}px`;
      dragState.item.style.top = `${top}px`;
      if (dragState.kind === 'subtask') dragState.item.style.width = `${dragState.startWidth}px`;
      if (dragState.kind === 'task') moveTaskSlot(event.clientY);
      else moveSubtaskSlot(event.clientY);
    }

    function reorderAnimatedElements() {
      return Array.from(document.querySelectorAll('.task-card[data-id]:not(.dragging), .subtask[data-sub]:not(.dragging), .drop-slot'));
    }

    function elementAnimationKey(element) {
      if (element.classList.contains('drop-slot')) return 'slot';
      if (element.dataset.id) return `task:${element.dataset.id}`;
      if (element.dataset.sub) return `sub:${element.dataset.sub}`;
      return null;
    }

    function captureReorderLayout() {
      const rects = new Map();
      for (const element of reorderAnimatedElements()) {
        const key = elementAnimationKey(element);
        if (key) rects.set(key, element.getBoundingClientRect());
      }
      return rects;
    }

    function playReorderBounce(previousRects) {
      requestAnimationFrame(() => {
        for (const element of reorderAnimatedElements()) {
          const key = elementAnimationKey(element);
          const previous = key ? previousRects.get(key) : null;
          if (!previous) continue;
          const next = element.getBoundingClientRect();
          const dx = previous.left - next.left;
          const dy = previous.top - next.top;
          if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
          element.classList.add('settling');
          const animation = element.animate(
            [
              { transform: `translate(${dx}px, ${dy}px) scale(.992)`, offset: 0 },
              { transform: `translate(${dx * .18}px, ${dy * .18}px) scale(1.003)`, offset: .58 },
              { transform: `translate(${-dx * .055}px, ${-dy * .055}px) scale(1.008)`, offset: .82 },
              { transform: 'translate(0, 0) scale(1)', offset: 1 }
            ],
            {
              duration: Math.min(2320, Math.max(1520, 1040 + Math.abs(dy) * 1.36)),
              easing: 'cubic-bezier(.16,1,.3,1)'
            }
          );
          animation.onfinish = () => element.classList.remove('settling');
          animation.oncancel = () => element.classList.remove('settling');
        }
      });
    }

    function moveSlotWithBounce(parent, before) {
      if (!dragState || !parent) return;
      const slot = dragState.slot;
      const normalizedBefore = before === slot ? slot.nextSibling : before;
      if (slot.parentNode === parent && slot.nextSibling === normalizedBefore) return;
      const previousRects = captureReorderLayout();
      parent.insertBefore(slot, normalizedBefore || null);
      playReorderBounce(previousRects);
    }

    function moveTaskSlot(pointerY) {
      const sameGroupCards = Array.from(list.querySelectorAll('.task-card[data-id]:not(.dragging)'))
        .filter(card => card.classList.contains('done') === dragState.completed);
      const before = sameGroupCards.find(card => pointerY < card.getBoundingClientRect().top + card.getBoundingClientRect().height / 2);
      if (before) {
        moveSlotWithBounce(list, before);
        return;
      }
      if (dragState.completed) {
        moveSlotWithBounce(list, null);
        return;
      }
      const firstDone = Array.from(list.querySelectorAll('.task-card.done:not(.dragging)'))[0];
      if (firstDone) moveSlotWithBounce(list, firstDone);
      else moveSlotWithBounce(list, null);
    }

    function moveSubtaskSlot(pointerY) {
      const container = dragState.slot.parentElement;
      const rows = Array.from(container.querySelectorAll('.subtask[data-sub]:not(.dragging)'));
      const before = rows.find(row => pointerY < row.getBoundingClientRect().top + row.getBoundingClientRect().height / 2);
      if (before) {
        moveSlotWithBounce(container, before);
        return;
      }
      const addRow = container.querySelector('.sub-add');
      moveSlotWithBounce(container, addRow || null);
    }

    async function finishReorderDrag() {
      if (!dragState) return;
      const current = dragState;
      cleanupReorderDrag(true);
      if (!current.moved) return;
      try {
        if (current.kind === 'task') {
          const ids = Array.from(list.querySelectorAll('.task-card[data-id]'))
            .filter(card => card.classList.contains('done') === current.completed)
            .map(card => card.dataset.id);
          state = await window.pywebview.api.reorder_todo_group(state.current_date, current.completed, ids);
        } else {
          const container = current.item.closest('.subtasks');
          const ids = Array.from(container.querySelectorAll('.subtask[data-sub]')).map(row => row.dataset.sub);
          state = await window.pywebview.api.reorder_subtasks(current.parentId, ids, state.current_date);
        }
        render();
        notify('顺序已保存');
      } catch (error) {
        notify('顺序保存失败');
        render();
      }
    }

    function cancelReorderDrag() {
      cleanupReorderDrag(false);
    }

    function cleanupReorderDrag(commit) {
      if (!dragState) return;
      document.removeEventListener('pointermove', onReorderMove);
      document.removeEventListener('pointerup', finishReorderDrag);
      document.removeEventListener('pointercancel', cancelReorderDrag);
      document.body.classList.remove('reorder-active');
      const { item, slot, sourceParent, sourceNext } = dragState;
      if (commit && slot.parentNode) {
        slot.parentNode.insertBefore(item, slot);
      } else if (sourceParent?.isConnected) {
        if (sourceNext && sourceNext.parentNode === sourceParent) sourceParent.insertBefore(item, sourceNext);
        else sourceParent.appendChild(item);
      }
      item.classList.remove('dragging', 'drag-ready');
      item.removeAttribute('style');
      if (slot.parentNode) slot.remove();
      dragState = null;
    }

    function addCardTemplate() {
      return `
        <article class="add-card">
          <div class="mini-plus"></div>
          <input class="add-input" id="newTaskInput" placeholder="输入新任务，回车保存" />
          <button class="save-add" id="saveNewTask">添加</button>
        </article>
      `;
    }

    function taskTemplate(todo) {
      const cls = ['task-card'];
      if (todo.completed) cls.push('done');
      if (todo.due_scope === 'today') cls.push('urgent');
      if (todo.due_scope === 'week') cls.push('week');
      const subSummary = todo.subtask_total ? `<span class="chip blue">${todo.subtask_done}/${todo.subtask_total} 子任务</span>` : '';
      const carried = todo.carried ? `<span class="chip">自动带入</span>` : '';
      const dueChip = todo.due_scope === 'today'
        ? `<span class="chip red">今日必须完成</span>`
        : todo.due_scope === 'week'
          ? `<span class="chip blue">本周必须完成</span>`
          : '';
      const subtasks = todo.subtasks.map(item => subtaskTemplate(todo, item)).join('');
      return `
        <article class="${cls.join(' ')}" data-id="${todo.id}">
          <button class="check" data-action="toggle" title="完成/取消完成"></button>
          <div class="task-body">
            <div class="task-title" contenteditable="true" spellcheck="false" data-action="edit-title">${esc(todo.text)}</div>
            <div class="meta">${dueChip}${subSummary}${carried}</div>
            <div class="subtasks">
              ${subtasks}
              <div class="sub-add" data-action="focus-subadd">
                <div class="mini-plus"></div>
                <input class="sub-input" placeholder="添加子任务" data-action="sub-input" />
              </div>
            </div>
          </div>
          <div class="task-actions">
            <button class="due-btn urgent ${todo.due_scope === 'today' ? 'active' : ''}" data-action="due-today" title="今日必须完成">!</button>
            <button class="due-btn week ${todo.due_scope === 'week' ? 'active' : ''}" data-action="due-week" title="本周必须完成">周</button>
            <button class="icon-btn" data-action="delete" title="删除"><span class="remove-icon"></span></button>
          </div>
        </article>
      `;
    }

    function subtaskTemplate(todo, item) {
      return `
        <div class="subtask ${item.completed ? 'done' : ''}" data-parent="${todo.id}" data-sub="${item.id}">
          <button class="sub-check" data-action="toggle-sub" title="完成/取消完成"></button>
          <div class="sub-text" contenteditable="true" spellcheck="false" data-action="edit-sub">${esc(item.text)}</div>
          <button class="sub-delete" data-action="delete-sub" title="删除子任务"><span class="remove-icon small"></span></button>
        </div>
      `;
    }

    function wireTaskEvents() {
      const saveNew = document.getElementById('saveNewTask');
      const newInput = document.getElementById('newTaskInput');
      if (saveNew && newInput) {
        saveNew.onclick = () => saveNewTask();
        newInput.addEventListener('keydown', event => {
          if (event.key === 'Enter') {
            event.preventDefault();
            saveNewTask();
          }
          if (event.key === 'Escape') {
            adding = false;
            renderTasks();
          }
        });
      }
      document.querySelectorAll('.task-card').forEach(card => {
        const id = card.dataset.id;
        card.addEventListener('dblclick', event => {
          if (event.target.closest('[contenteditable], button, input')) return;
          const input = card.querySelector('.sub-input');
          if (input) {
            card.classList.add('editing-sub');
            input.focus();
          }
        });
        card.querySelector('[data-action="toggle"]').onclick = () => mutate(window.pywebview.api.toggle_todo(id, state.current_date), '状态已更新');
        card.querySelector('[data-action="due-today"]').onclick = () => mutate(window.pywebview.api.set_due_scope(id, 'today', state.current_date), '已标记今日必须完成');
        card.querySelector('[data-action="due-week"]').onclick = () => mutate(window.pywebview.api.set_due_scope(id, 'week', state.current_date), '已标记本周必须完成');
        card.querySelector('[data-action="delete"]').onclick = () => mutate(window.pywebview.api.delete_todo(id, state.current_date), '已删除，可从数据文件找回');
        const titleNode = card.querySelector('[data-action="edit-title"]');
        bindEditable(titleNode, text => window.pywebview.api.update_todo_text(id, text, state.current_date));
        const subInput = card.querySelector('[data-action="sub-input"]');
        subInput.addEventListener('keydown', event => {
          if (event.key === 'Enter') {
            event.preventDefault();
            saveSubtask(id, subInput);
          }
          if (event.key === 'Escape') {
            subInput.value = '';
            card.classList.remove('editing-sub');
          }
        });
        subInput.addEventListener('blur', () => {
          if (subInput.value.trim()) saveSubtask(id, subInput);
          card.classList.remove('editing-sub');
        });
      });
      document.querySelectorAll('.subtask').forEach(row => {
        const parent = row.dataset.parent;
        const sub = row.dataset.sub;
        row.querySelector('[data-action="toggle-sub"]').onclick = () => mutate(window.pywebview.api.toggle_subtask(parent, sub, state.current_date), '子任务已更新');
        row.querySelector('[data-action="delete-sub"]').onclick = () => mutate(window.pywebview.api.delete_subtask(parent, sub, state.current_date), '子任务已删除');
        bindEditable(row.querySelector('[data-action="edit-sub"]'), text => window.pywebview.api.update_subtask_text(parent, sub, text, state.current_date));
      });
      wireLongPressReorder();
    }

    function wireLongPressReorder() {
      document.querySelectorAll('.task-card[data-id]').forEach(card => {
        card.addEventListener('pointerdown', event => startLongPressReorder(event, card, 'task'));
      });
      document.querySelectorAll('.subtask[data-sub]').forEach(row => {
        row.addEventListener('pointerdown', event => startLongPressReorder(event, row, 'subtask', {
          delay: 260,
          tolerateMove: true
        }));
      });
    }

    function bindEditable(node, saveFactory) {
      let before = node.innerText.trim();
      node.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          node.blur();
        }
        if (event.key === 'Escape') {
          node.innerText = before;
          node.blur();
        }
      });
      node.addEventListener('focus', () => before = node.innerText.trim());
      node.addEventListener('blur', async () => {
        const text = node.innerText.trim();
        if (!text || text === before) {
          node.innerText = before || text;
          return;
        }
        await mutate(saveFactory(text), '已保存');
      });
    }

    async function saveNewTask() {
      const input = document.getElementById('newTaskInput');
      const text = input ? input.value.trim() : '';
      if (!text) {
        adding = false;
        renderTasks();
        return;
      }
      adding = false;
      await mutate(window.pywebview.api.add_todo(text, state.current_date), '已添加任务');
    }

    async function saveSubtask(id, input) {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      await mutate(window.pywebview.api.add_subtask(id, text, state.current_date), '已添加子任务');
    }

    function renderCalendar() {
      const selected = dateFromKey(state.current_date);
      calendarMonth.textContent = `${selected.getFullYear()}年${selected.getMonth() + 1}月`;
      const first = new Date(selected.getFullYear(), selected.getMonth(), 1);
      const start = new Date(first);
      const mondayOffset = (first.getDay() + 6) % 7;
      start.setDate(first.getDate() - mondayOffset);
      const days = [];
      for (let i = 0; i < 42; i++) {
        const day = new Date(start);
        day.setDate(start.getDate() + i);
        const key = keyFromDate(day);
        const progress = state.progress_by_date[key] || {remaining: 0, done: 0, total: 0};
        const holiday = (state.holidays_by_date || {})[key];
        const cls = ['day'];
        if (day.getMonth() !== selected.getMonth()) cls.push('out');
        if (key === state.current_date) cls.push('selected');
        if (key === state.today) cls.push('today');
        if (holiday) cls.push(holiday.workday ? 'workday' : 'holiday');
        const dot = progress.total ? `<span class="dot ${progress.remaining === 0 ? 'complete' : ''}"></span>` : '';
        const holidayName = holiday ? esc(holiday.name) : '';
        const holidayLabelText = holiday ? esc(holiday.label || holiday.name) : '';
        const title = holidayName ? ` title="${holiday.workday ? `${holidayName} · 调休上班` : holidayName}"` : '';
        const holidayLabel = holidayLabelText ? `<span class="holiday-name">${holidayLabelText}</span>` : '';
        days.push(`<button class="${cls.join(' ')}" data-date="${key}"${title}><span class="day-number">${day.getDate()}</span>${holidayLabel}${dot}</button>`);
      }
      calendarGrid.innerHTML = days.join('');
      calendarGrid.querySelectorAll('.day').forEach(button => {
        button.onclick = () => load(button.dataset.date);
      });
    }

    function dateFromKey(key) {
      const [y, m, d] = key.split('-').map(Number);
      return new Date(y, m - 1, d);
    }

    function keyFromDate(date) {
      const y = date.getFullYear();
      const m = String(date.getMonth() + 1).padStart(2, '0');
      const d = String(date.getDate()).padStart(2, '0');
      return `${y}-${m}-${d}`;
    }

    function shiftCalendarMonth(delta) {
      if (!state) return;
      const selected = dateFromKey(state.current_date);
      const desiredDay = selected.getDate();
      const target = new Date(selected.getFullYear(), selected.getMonth() + delta, 1);
      const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
      target.setDate(Math.min(desiredDay, lastDay));
      load(keyFromDate(target));
    }

    document.getElementById('calendarToggle').onclick = () => {
      app.classList.toggle('calendar-hidden');
      setTimeout(() => updateScrollChrome(false), 220);
    };
    prevMonth.onclick = () => shiftCalendarMonth(-1);
    nextMonth.onclick = () => shiftCalendarMonth(1);
    document.getElementById('addButton').onclick = () => {
      adding = true;
      renderTasks();
    };
    titleLogo?.addEventListener('click', playTitleLogo);
    deletedSummary.onclick = () => openDeletedPanel();
    closeDeletedPanel.onclick = () => closeDeleted();
    document.addEventListener('pointerdown', event => {
      if (!deletedPanel.classList.contains('show')) return;
      if (deletedPanel.contains(event.target) || deletedSummary.contains(event.target)) return;
      closeDeleted();
    });

    let resizing = false;
    let startX = 0;
    let startWidth = 500;
    splitter.addEventListener('mousedown', event => {
      resizing = true;
      startX = event.clientX;
      startWidth = calendarPane.getBoundingClientRect().width;
      document.body.style.cursor = 'col-resize';
      event.preventDefault();
    });
    window.addEventListener('mousemove', event => {
      if (!resizing) return;
      const next = Math.max(CALENDAR_MIN_WIDTH, Math.min(CALENDAR_MAX_WIDTH, startWidth - (event.clientX - startX)));
      app.style.setProperty('--cal-width', `${next}px`);
      scheduleScrollChrome(false);
    });
    window.addEventListener('mouseup', () => {
      resizing = false;
      document.body.style.cursor = '';
    });
    main.addEventListener('scroll', () => scheduleScrollChrome(false), { passive: true });
    window.addEventListener('resize', () => scheduleScrollChrome(false));

    window.addEventListener('pywebviewready', () => load().then(() => updateScrollChrome(false)));
  </script>
</body>
</html>
"""


def font_uri(path: Path) -> str:
    if path.exists():
        return path.resolve().as_uri()
    return ""


def logo_markup() -> str:
    path = LOGO_DIR / "logo.svg"
    if not path.exists():
        return '<span class="title-logo-fallback">TodoApp</span>'
    svg = path.read_text(encoding="utf-8")
    svg = svg.replace('aria-labelledby="title desc"', 'aria-labelledby="todoLogoTitle todoLogoDesc"', 1)
    svg = svg.replace('id="title"', 'id="todoLogoTitle"', 1)
    svg = svg.replace('id="desc"', 'id="todoLogoDesc"', 1)
    svg = svg.replace("<svg ", '<svg class="title-logo-svg" ', 1)
    return svg


def scoped_logo_motion_css() -> str:
    path = LOGO_DIR / "motion.css"
    if not path.exists():
        return ""
    css = path.read_text(encoding="utf-8")
    if "@keyframes" not in css:
        return css
    selectors, keyframes = css.split("@keyframes", 1)
    scoped_lines: list[str] = []
    for line in selectors.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("#", ".")):
            indent = line[: len(line) - len(stripped)]
            scoped_lines.append(f"{indent}.title-logo.playing {stripped}")
        else:
            scoped_lines.append(line)
    return "\n".join(scoped_lines) + "\n@keyframes" + keyframes


def build_html() -> str:
    return (
        HTML.replace("__INTER_FONT__", font_uri(FONT_DIR / "InterVariable.woff2"))
        .replace("__NOTO_FONT__", font_uri(FONT_DIR / "NotoSansSC-VF.ttf"))
        .replace("__LOGO_MARKUP__", logo_markup())
        .replace("__LOGO_MOTION_CSS__", scoped_logo_motion_css())
    )


def main() -> None:
    api = Api()
    webview.create_window(
        APP_TITLE,
        html=build_html(),
        js_api=api,
        width=APP_SIZE[0],
        height=APP_SIZE[1],
        min_size=(1180, 680),
        resizable=True,
        maximized=True,
        background_color="#f5f7fb",
        text_select=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
