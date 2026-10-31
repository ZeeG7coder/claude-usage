#!/usr/bin/env python3
"""
claude-usage — локальный сервер статистики токенов Claude Code.

Читает JSONL-транскрипты из ~/.claude/projects, складывает usage в SQLite
(инкрементально, с дедупликацией по id сообщения) и отдаёт дашборд + JSON API.

Зависимости: только стандартная библиотека Python 3.9+.

Запуск:
    python3 server.py                # http://127.0.0.1:8787
    python3 server.py --port 9000
    python3 server.py --once         # сводка в терминал и выход
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = Path(os.environ.get("CLAUDE_USAGE_CONFIG", APP_DIR / "config.json"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-usage"
DB_PATH = CACHE_DIR / "usage.db"

DEFAULT_CONFIG = {
    "claude_dir": str(CLAUDE_DIR),
    "db_path": str(DB_PATH),
    "timezone": None,  # None = локальная зона системы
    "refresh_seconds": 20,      # автообновление страницы
    "ingest_seconds": 30,       # перечитывание JSONL в фоне
    # Всё, что ниже, редактируется прямо в интерфейсе (шестерёнка).
    "weights": {            # сколько "биллинговых" единиц стоит один токен
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.1,
    },
    "limits": {
        "session": {
            "label": "Окно 5 часов",
            "mode": "rolling",
            "hours": 5,
            "limit": 8000000,
        },
        "day": {
            "label": "Сегодня",
            "mode": "calendar_day",
            "limit": 12000000,
        },
        "week": {
            "label": "Неделя (скользящие 7 дней)",
            "mode": "rolling",
            "days": 7,
            "limit": 60000000,
        },
        "calendar_week": {
            "label": "Календарная неделя (с Пн)",
            "mode": "calendar_week",
            "limit": 0,
        },
    },
    "prices": {  # USD за 1M токенов
        "opus": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5},
        "sonnet": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
        "haiku": {"input": 0.8, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08},
        "default": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
    },
}

WRITE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
CURRENT: dict = {"cfg": None}  # актуальный конфиг для фоновых потоков


# --------------------------------------------------------------------------- #
# конфиг
# --------------------------------------------------------------------------- #
def deep_merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"[config] не смог прочитать {CONFIG_PATH}: {exc}", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def resolve_tz(cfg: dict):
    name = cfg.get("timezone")
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            pass
    return datetime.now().astimezone().tzinfo


def db_path(cfg: dict) -> Path:
    return Path(cfg.get("db_path") or DB_PATH)


def claude_dir(cfg: dict) -> Path:
    return Path(cfg.get("claude_dir") or CLAUDE_DIR)


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path   TEXT PRIMARY KEY,
    mtime  REAL,
    size   INTEGER,
    offset INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    msg_id      TEXT PRIMARY KEY,
    epoch       REAL NOT NULL,
    ts          TEXT,
    model       TEXT,
    session_id  TEXT,
    project     TEXT,
    cwd         TEXT,
    input       INTEGER DEFAULT 0,
    output      INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    cache_read  INTEGER DEFAULT 0,
    sidechain   INTEGER DEFAULT 0,
    source_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_epoch   ON events(epoch);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_model   ON events(model);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title      TEXT,
    cwd        TEXT,
    last_ts    REAL
);
"""


def connect(cfg: dict) -> sqlite3.Connection:
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# индексация JSONL
# --------------------------------------------------------------------------- #
def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def project_label(cwd: str | None, fallback: str) -> str:
    if not cwd:
        return fallback
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def iter_jsonl(path: Path, offset: int):
    """Отдаёт (объект, новый_offset). Читает только завершённые строки."""
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n")
    if end == -1:
        return
    for raw in data[: end + 1].splitlines():
        if not raw.strip():
            continue
        try:
            yield json.loads(raw), None
        except Exception:  # noqa: BLE001
            continue
    yield None, offset + end + 1


def ingest(cfg: dict, verbose: bool = False) -> dict:
    """Дочитывает новые строки из всех JSONL. Возвращает счётчики."""
    root = claude_dir(cfg) / "projects"
    stats = {"files": 0, "new_events": 0, "new_sessions": 0, "elapsed": 0.0}
    if not root.exists():
        return stats
    started = time.time()
    with WRITE_LOCK:
        conn = connect(cfg)
        try:
            known = {
                row["path"]: row
                for row in conn.execute("SELECT path, mtime, size, offset FROM files")
            }
            for path in sorted(root.rglob("*.jsonl")):
                key = str(path)
                try:
                    st = path.stat()
                except OSError:
                    continue
                prev = known.get(key)
                offset = 0
                if prev is not None:
                    offset = prev["offset"] or 0
                    if st.st_size < offset:  # файл обрезали/переписали
                        offset = 0
                    if st.st_size == offset and abs(st.st_mtime - (prev["mtime"] or 0)) < 1e-6:
                        continue
                before_events = stats["new_events"]
                before_sessions = stats["new_sessions"]
                for obj, new_offset in iter_jsonl(path, offset):
                    if obj is None:
                        if new_offset is not None:
                            offset = new_offset
                        continue
                    stats["new_events"] += ingest_record(conn, obj, path, stats)
                conn.execute(
                    "INSERT INTO files(path, mtime, size, offset) VALUES (?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
                    "size=excluded.size, offset=excluded.offset",
                    (key, st.st_mtime, st.st_size, offset),
                )
                stats["files"] += 1
                if verbose and stats["new_events"] > before_events:
                    print(
                        f"[index] {path.name}: +{stats['new_events'] - before_events}"
                        f" событий, +{stats['new_sessions'] - before_sessions} сессий"
                    )
            conn.commit()
        finally:
            conn.close()
    stats["elapsed"] = round(time.time() - started, 3)
    return stats


def ingest_record(conn: sqlite3.Connection, obj: dict, path: Path, stats: dict) -> int:
    kind = obj.get("type")
    session_id = obj.get("sessionId") or obj.get("session_id")
    cwd = obj.get("cwd")

    if kind == "ai-title":
        title = (obj.get("aiTitle") or "").strip()
        if session_id and title:
            cur = conn.execute(
                "INSERT INTO sessions(session_id, title) VALUES (?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title "
                "WHERE excluded.title IS NOT NULL AND excluded.title <> ''",
                (session_id, title),
            )
            if cur.rowcount:
                stats["new_sessions"] += 1
        return 0

    if kind != "assistant":
        return 0

    message = obj.get("message")
    if not isinstance(message, dict):
        return 0
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0

    msg_id = message.get("id") or obj.get("uuid")
    if not msg_id:
        return 0
    epoch = parse_ts(obj.get("timestamp"))
    if epoch is None:
        return 0

    model = message.get("model") or obj.get("requestedModel")
    project = project_label(cwd, path.parent.name.replace("-", "/", 1))
    added = 0
    try:
        conn.execute(
            "INSERT INTO events(msg_id, epoch, ts, model, session_id, project, cwd,"
            " input, output, cache_write, cache_read, sidechain, source_file)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(msg_id)[:120],
                epoch,
                obj.get("timestamp"),
                model,
                session_id,
                project,
                cwd,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_creation_input_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                1 if obj.get("isSidechain") else 0,
                str(path),
            ),
        )
        added = 1
    except sqlite3.IntegrityError:
        added = 0

    if session_id:
        conn.execute(
            "INSERT INTO sessions(session_id, cwd, last_ts) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "cwd=COALESCE(sessions.cwd, excluded.cwd), "
            "last_ts=MAX(COALESCE(sessions.last_ts,0), excluded.last_ts)",
            (session_id, cwd, epoch),
        )
    return added


# --------------------------------------------------------------------------- #
# агрегация
# --------------------------------------------------------------------------- #
def price_for(model: str | None, prices: dict) -> dict:
    name = (model or "").lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in name:
            return prices.get(family, prices["default"])
    return prices["default"]


def enrich(event: sqlite3.Row | dict, cfg: dict) -> dict:
    weights = cfg["weights"]
    prices = cfg["prices"]
    e = dict(event)
    price = price_for(e.get("model"), prices)
    units = (
        e["input"] * weights["input"]
        + e["output"] * weights["output"]
        + e["cache_write"] * weights["cache_write"]
        + e["cache_read"] * weights["cache_read"]
    )
    cost = (
        e["input"] * price["input"]
        + e["output"] * price["output"]
        + e["cache_write"] * price["cache_write"]
        + e["cache_read"] * price["cache_read"]
    ) / 1_000_000
    e["units"] = units
    e["cost"] = cost
    e["total_tokens"] = e["input"] + e["output"] + e["cache_write"] + e["cache_read"]
    return e


def load_events(cfg: dict, start: float | None = None, end: float | None = None) -> list[dict]:
    conn = connect(cfg)
    try:
        # проект определяем по первому cwd сессии, иначе по cwd самой записи
        session_cwd = {
            row["session_id"]: row["cwd"]
            for row in conn.execute("SELECT session_id, cwd FROM sessions WHERE cwd IS NOT NULL")
        }
        sql = "SELECT * FROM events"
        args: list[float] = []
        clauses = []
        if start is not None:
            clauses.append("epoch >= ?")
            args.append(start)
        if end is not None:
            clauses.append("epoch <= ?")
            args.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY epoch"
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        event = enrich(row, cfg)
        cwd = session_cwd.get(event["session_id"]) or event.get("cwd")
        event["project"] = project_label(cwd, event["project"])
        events.append(event)
    return events


def sum_events(events: list[dict]) -> dict:
    agg = {
        "input": 0,
        "output": 0,
        "cache_write": 0,
        "cache_read": 0,
        "tokens": 0,
        "units": 0.0,
        "cost": 0.0,
        "messages": len(events),
        "sidechain_messages": 0,
    }
    for e in events:
        agg["input"] += e["input"]
        agg["output"] += e["output"]
        agg["cache_write"] += e["cache_write"]
        agg["cache_read"] += e["cache_read"]
        agg["units"] += e["units"]
        agg["cost"] += e["cost"]
        agg["sidechain_messages"] += 1 if e["sidechain"] else 0
    agg["tokens"] = agg["input"] + agg["output"] + agg["cache_write"] + agg["cache_read"]
    return agg


def window_bounds(spec: dict, now: datetime) -> tuple[datetime, datetime | None, float, bool]:
    """Возвращает (start, reset_at, длина_окна_в_секундах, похоже_на_скользящее)."""
    mode = spec.get("mode", "rolling")
    if mode == "calendar_day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1), 86400.0, False
    if mode == "calendar_week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, start + timedelta(days=7), 7 * 86400.0, False
    duration = timedelta(hours=spec.get("hours", 0), days=spec.get("days", 0))
    if duration.total_seconds() <= 0:
        duration = timedelta(hours=5)
    return now - duration, None, duration.total_seconds(), True


def model_matches(event_model: str | None, pattern: str | None) -> bool:
    if not pattern:
        return True
    return pattern.lower() in (event_model or "").lower()


def build_windows(cfg: dict, events: list[dict], now: datetime) -> list[dict]:
    result = []
    for key, spec in cfg["limits"].items():
        if not isinstance(spec, dict) or spec.get("hidden"):
            continue
        start, reset_at, duration, rolling = window_bounds(spec, now)
        start_epoch = start.timestamp()
        now_epoch = now.timestamp()
        selected = [
            e
            for e in events
            if start_epoch <= e["epoch"] <= now_epoch
            and model_matches(e["model"], spec.get("model"))
        ]
        agg = sum_events(selected)
        if rolling and selected:
            oldest = min(e["epoch"] for e in selected)
            reset_at = datetime.fromtimestamp(oldest + duration, start.tzinfo)
        limit = float(spec.get("limit") or 0)
        percent = (agg["units"] / limit * 100.0) if limit > 0 else None
        result.append(
            {
                "key": key,
                "label": spec.get("label") or key,
                "mode": spec.get("mode", "rolling"),
                "model": spec.get("model"),
                "start": start.isoformat(),
                "reset_at": reset_at.isoformat() if reset_at else None,
                "limit": limit,
                "percent": percent,
                "duration_seconds": duration,
                "reset_in": max(0.0, (reset_at.timestamp() - now_epoch)) if reset_at else None,
                "usage": agg,
            }
        )
    return result


def bucket_series(events: list[dict], now: datetime, bucket: str, count: int) -> list[dict]:
    """Ряд последних N бакетов, выровненных по границе часа/дня."""
    tzinfo = now.tzinfo
    if bucket == "hour":
        cursor = now.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
    else:
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
    buckets = []
    for i in range(count - 1, -1, -1):
        start = cursor - step * i
        buckets.append(
            {
                "start": start,
                "end": start + step,
                "label": start.strftime("%H:%M" if bucket == "hour" else "%d.%m"),
                "usage": {
                    "input": 0,
                    "output": 0,
                    "cache_write": 0,
                    "cache_read": 0,
                    "units": 0.0,
                    "cost": 0.0,
                    "messages": 0,
                    "tokens": 0,
                    "sidechain_messages": 0,
                },
            }
        )
    first = buckets[0]["start"].timestamp()
    for e in events:
        if e["epoch"] < first:
            continue
        index = int((e["epoch"] - first) // step.total_seconds())
        if index >= count:
            continue
        u = buckets[index]["usage"]
        u["input"] += e["input"]
        u["output"] += e["output"]
        u["cache_write"] += e["cache_write"]
        u["cache_read"] += e["cache_read"]
        u["units"] += e["units"]
        u["cost"] += e["cost"]
        u["messages"] += 1
    for b in buckets:
        u = b["usage"]
        u["tokens"] = u["input"] + u["output"] + u["cache_write"] + u["cache_read"]
        b["start"] = b["start"].isoformat()
        b["end"] = b["end"].isoformat()
        b["label"] = datetime.fromisoformat(b["start"]).strftime(
            "%H:%M" if bucket == "hour" else "%d.%m"
        )
    return buckets


def group_by(events: list[dict], key: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for e in events:
        name = e.get(key) or "—"
        g = groups.setdefault(name, {"key": name, "usage": sum_events([]), "last_ts": 0})
        u = g["usage"]
        u["input"] += e["input"]
        u["output"] += e["output"]
        u["cache_write"] += e["cache_write"]
        u["cache_read"] += e["cache_read"]
        u["units"] += e["units"]
        u["cost"] += e["cost"]
        u["messages"] += 1
        u["sidechain_messages"] += 1 if e["sidechain"] else 0
        g["last_ts"] = max(g["last_ts"], e["epoch"])
    out = []
    for g in groups.values():
        u = g["usage"]
        u["tokens"] = u["input"] + u["output"] + u["cache_write"] + u["cache_read"]
        g["last_ts"] = datetime.fromtimestamp(g["last_ts"], datetime.now().astimezone().tzinfo).isoformat()
        out.append(g)
    out.sort(key=lambda g: g["usage"]["units"], reverse=True)
    return [g for g in out if g["usage"]["tokens"] > 0]


def session_rows(cfg: dict, days: int = 14, limit: int = 40) -> list[dict]:
    now = datetime.now(resolve_tz(cfg))
    start = (now - timedelta(days=days)).timestamp()
    events = load_events(cfg, start=start)
    conn = connect(cfg)
    try:
        titles = {row["session_id"]: row["title"] for row in conn.execute("SELECT session_id, title FROM sessions")}
    finally:
        conn.close()
    grouped = group_by(events, "session_id")
    rows = []
    for g in grouped[:limit]:
        sid = g["key"]
        rows.append(
            {
                "session_id": sid,
                "title": titles.get(sid) or "Без названия",
                "usage": g["usage"],
                "last_ts": g["last_ts"],
                "project": next(
                    (e["project"] for e in events if e["session_id"] == sid),
                    "—",
                ),
            }
        )
    return rows


def build_summary(cfg: dict) -> dict:
    now = datetime.now(resolve_tz(cfg))
    all_events = load_events(cfg)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = (now - timedelta(days=7)).timestamp()
    last_24h = (now - timedelta(hours=24)).timestamp()
    today_events = [e for e in all_events if e["epoch"] >= today_start]
    week_events = [e for e in all_events if e["epoch"] >= week_start]
    last_events = [e for e in all_events if e["epoch"] >= last_24h]

    sessions = session_rows(cfg, days=14, limit=30)
    return {
        "now": now.isoformat(),
        "timezone": str(now.tzinfo),
        "totals": {
            "today": sum_events(today_events),
            "week": sum_events(week_events),
            "last_24h": sum_events(last_events),
            "all": sum_events(all_events),
        },
        "windows": build_windows(cfg, all_events, now),
        "series": {
            "hour": bucket_series(all_events, now, "hour", 24),
            "day": bucket_series(all_events, now, "day", 14),
        },
        "models": group_by(week_events, "model"),
        "projects": group_by(week_events, "project"),
        "sessions": sessions,
        "config": {"weights": cfg["weights"], "limits": cfg["limits"], "prices": cfg["prices"], "refresh_seconds": cfg["refresh_seconds"]},
    }


def session_detail(cfg: dict, session_id: str) -> dict | None:
    events = [e for e in load_events(cfg) if e["session_id"] == session_id]
    if not events:
        return None
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    finally:
        conn.close()
    return {
        "session_id": session_id,
        "title": (row["title"] if row else None) or "Без названия",
        "cwd": (row["cwd"] if row else None) or events[0].get("cwd"),
        "usage": sum_events(events),
        "events": [
            {
                "ts": e["ts"],
                "model": e["model"],
                "input": e["input"],
                "output": e["output"],
                "cache_write": e["cache_write"],
                "cache_read": e["cache_read"],
                "units": e["units"],
                "cost": e["cost"],
                "sidechain": bool(e["sidechain"]),
            }
            for e in events
        ],
    }


def breakdown(cfg: dict, range_key: str, by: str) -> dict:
    now = datetime.now(resolve_tz(cfg))
    spans = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
        "24h": (now - timedelta(hours=24)).timestamp(),
        "7d": (now - timedelta(days=7)).timestamp(),
        "30d": (now - timedelta(days=30)).timestamp(),
        "all": None,
    }
    start = spans.get(range_key, spans["7d"])
    events = load_events(cfg, start=start)
    return {"range": range_key, "by": by, "groups": group_by(events, by), "totals": sum_events(events)}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-usage/1.0"
    cfg: dict = DEFAULT_CONFIG

    def log_message(self, fmt, *args):  # тише в консоли
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message}, status)

    def serve_static(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            if rel in ("", "index.html"):
                target = STATIC_DIR / "index.html"
            else:
                self.send_error_json("not found", 404)
                return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/summary":
                self.send_json(build_summary(self.cfg))
            elif path == "/api/breakdown":
                by = (query.get("by") or ["model"])[0]
                rng = (query.get("range") or ["7d"])[0]
                if by not in ("model", "project", "session_id"):
                    return self.send_error_json("by должен быть model|project|session_id")
                self.send_json(breakdown(self.cfg, rng, by))
            elif path == "/api/session":
                sid = (query.get("id") or [""])[0]
                detail = session_detail(self.cfg, sid)
                if detail is None:
                    return self.send_error_json("сессия не найдена", 404)
                self.send_json(detail)
            elif path == "/api/config":
                self.send_json(self.cfg)
            elif path == "/api/status":
                conn = connect(self.cfg)
                try:
                    total = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
                    files = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
                finally:
                    conn.close()
                self.send_json({"events": total, "files": files, "db": str(db_path(self.cfg))})
            elif path in ("/", "/index.html"):
                self.serve_static("index.html")
            elif path.startswith("/static/"):
                self.serve_static(path[len("/static/"):])
            elif path == "/favicon.ico":
                self.serve_static("favicon.svg")
            else:
                self.serve_static(path.lstrip("/"))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/refresh":
                self.send_json(ingest(self.cfg, verbose=True))
                return
            if parsed.path == "/api/config":
                payload = self.read_json()
                if not isinstance(payload, dict):
                    return self.send_error_json("ожидался объект")
                with CONFIG_LOCK:
                    cfg = load_config()
                    for key in ("limits", "weights", "prices", "timezone", "refresh_seconds", "ingest_seconds"):
                        if key in payload:
                            if isinstance(DEFAULT_CONFIG.get(key), dict) and isinstance(payload[key], dict):
                                deep_merge(cfg[key] if isinstance(cfg.get(key), dict) else {}, payload[key])
                            else:
                                cfg[key] = payload[key]
                    validate_config(cfg)
                    save_config(cfg)
                    CURRENT["cfg"] = cfg
                    Handler.cfg = cfg
                self.send_json({"ok": True, "config": cfg})
                return
            self.send_error_json("not found", 404)
        except ValueError as exc:
            self.send_error_json(str(exc))
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(f"{type(exc).__name__}: {exc}", 500)


def validate_config(cfg: dict) -> None:
    for field, number in cfg["weights"].items():
        if not isinstance(number, (int, float)) or number < 0:
            raise ValueError(f"weights.{field}: нужно неотрицательное число")
    for family, price in cfg["prices"].items():
        if not isinstance(price, dict):
            raise ValueError(f"prices.{family}: ожидался объект")
        for field, number in price.items():
            if not isinstance(number, (int, float)) or number < 0:
                raise ValueError(f"prices.{family}.{field}: нужно неотрицательное число")
    for key, spec in cfg["limits"].items():
        if not isinstance(spec, dict):
            raise ValueError(f"limits.{key}: ожидался объект")
        limit = spec.get("limit", 0)
        if not isinstance(limit, (int, float)) or limit < 0:
            raise ValueError(f"limits.{key}.limit: нужно неотрицательное число")
        mode = spec.get("mode", "rolling")
        if mode not in ("rolling", "calendar_day", "calendar_week"):
            raise ValueError(f"limits.{key}.mode: rolling|calendar_day|calendar_week")


# --------------------------------------------------------------------------- #
# фоновая индексация
# --------------------------------------------------------------------------- #
def background_ingest(interval: int):
    while True:
        time.sleep(interval)
        try:
            stats = ingest(CURRENT["cfg"])
            if stats["new_events"]:
                print(f"[index] +{stats['new_events']} событий за {stats['elapsed']}s")
        except Exception as exc:  # noqa: BLE001
            print(f"[index] ошибка: {exc}", file=sys.stderr)


def run_once(cfg: dict) -> None:
    stats = ingest(cfg, verbose=True)
    print(f"[index] файлов: {stats['files']}, новых событий: {stats['new_events']}, {stats['elapsed']}s\n")
    summary = build_summary(cfg)
    now = datetime.fromisoformat(summary["now"])
    print(f"claude-usage · {now.strftime('%d.%m.%Y %H:%M')} · {summary['timezone']}\n")
    for win in summary["windows"]:
        u = win["usage"]
        if win["limit"]:
            pct = win["percent"] or 0
            bar_len = 24
            filled = min(bar_len, int(pct / 100 * bar_len))
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"{win['label']:<32} {bar} {pct:5.1f}%  {u['units']/1e6:7.2f}M / {win['limit']/1e6:.0f}M")
        else:
            print(f"{win['label']:<32} {u['units']/1e6:7.2f}M  (лимит не задан)")
        if win["reset_at"]:
            reset = datetime.fromisoformat(win["reset_at"])
            print(f"{'':<32} сброс через {human_delta(reset - now)}, в {reset.strftime('%H:%M %d.%m')}")
    for key in ("today", "week", "all"):
        u = summary["totals"][key]
        label = {"today": "сегодня", "week": "7 дней", "all": "всего"}[key]
        print(
            f"\n{label:<8} токенов: {u['tokens']/1e6:.2f}M  "
            f"(in {u['input']/1e3:.0f}k, out {u['output']/1e3:.0f}k, "
            f"cache+ {u['cache_write']/1e6:.2f}M, cache↺ {u['cache_read']/1e6:.1f}M)  "
            f"≈ ${u['cost']:.2f}"
        )


def human_delta(delta: timedelta) -> str:
    seconds = int(max(0, delta.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}ч {minutes:02d}м"
    return f"{minutes}м"


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный сервер статистики токенов Claude Code")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--once", action="store_true", help="напечатать сводку и выйти")
    parser.add_argument("--no-open", action="store_true", help="не открывать браузер")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    if not CONFIG_PATH.exists():
        save_config(cfg)

    print("[index] сканирую транскрипты…")
    stats = ingest(cfg, verbose=args.verbose)
    print(f"[index] готово: {stats['files']} файлов, +{stats['new_events']} событий за {stats['elapsed']}s")

    if args.once:
        run_once(cfg)
        return

    CURRENT["cfg"] = cfg
    Handler.cfg = cfg
    thread = threading.Thread(
        target=background_ingest,
        args=(int(cfg.get("ingest_seconds") or 30),),
        daemon=True,
    )
    thread.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[server] {url}")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] остановлен")


if __name__ == "__main__":
    main()
