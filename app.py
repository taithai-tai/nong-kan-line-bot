import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO


load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

LINE_REPLY_API_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_API_URL = "https://api.line.me/v2/bot/message/push"
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_API_BASE_URL = os.getenv("AI_API_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "openrouter/auto")
APP_NAME = os.getenv("APP_NAME", "nong-kan-line-bot")
APP_URL = os.getenv("APP_URL", "")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "")).expanduser() if os.getenv("DATA_DIR") else None


def resolve_storage_path(env_name: str, default_name: str) -> Path:
    configured = Path(os.getenv(env_name, default_name)).expanduser()
    if configured.is_absolute() or DATA_DIR is None:
        return configured
    return DATA_DIR / configured


KNOWLEDGE_BASE_PATH = resolve_storage_path("KNOWLEDGE_BASE_PATH", "knowledge_base.json")
TRAINING_HISTORY_PATH = resolve_storage_path("TRAINING_HISTORY_PATH", "training_history.json")
CUSTOMER_CHATS_PATH = resolve_storage_path("CUSTOMER_CHATS_PATH", "customer_chats.json")
CUSTOMER_AI_SETTINGS_PATH = resolve_storage_path("CUSTOMER_AI_SETTINGS_PATH", "customer_ai_settings.json")
RESPONSE_TEMPLATES_PATH = resolve_storage_path("RESPONSE_TEMPLATES_PATH", "response_templates.json")
SQLITE_DATABASE_PATH = resolve_storage_path("SQLITE_DATABASE_PATH", "nong_kan.sqlite3")
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "20"))
AI_TRAINING_TIMEOUT_SECONDS = int(os.getenv("AI_TRAINING_TIMEOUT_SECONDS", "45"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

FALLBACK_MESSAGE = (
    "ขออภัยค่ะ น้องก้านยังไม่มีข้อมูลเรื่องนี้ในระบบ "
    "จะส่งต่อให้เจ้าหน้าที่ติดต่อกลับนะคะ"
)
AI_UNAVAILABLE_MESSAGE = (
    "ตอนนี้น้องก้านมึนหัวอยู่ น้องก้านจะกลับมาตอบตอนมีสตินะคะ"
)
NON_TEXT_MESSAGE = "รบกวนพิมพ์คำถามเป็นข้อความนะคะ น้องก้านจะช่วยดูข้อมูลให้ค่ะ"
DEFAULT_RESPONSE_TEMPLATES = [
    {
        "id": "default_handoff",
        "title": "ส่งต่อเจ้าหน้าที่",
        "text": "ขออภัยค่ะ น้องก้านยังไม่มีข้อมูลเรื่องนี้ในระบบ จะส่งต่อให้เจ้าหน้าที่ติดต่อกลับนะคะ",
        "created_at": "2026-06-08T00:00:00+00:00",
    },
    {
        "id": "default_wait",
        "title": "กำลังตรวจสอบ",
        "text": "รับเรื่องแล้วนะคะ เดี๋ยวเจ้าหน้าที่ตรวจสอบข้อมูลให้ค่ะ",
        "created_at": "2026-06-08T00:00:00+00:00",
    },
    {
        "id": "default_thanks",
        "title": "ขอบคุณ",
        "text": "ขอบคุณที่ติดต่อมานะคะ",
        "created_at": "2026-06-08T00:00:00+00:00",
    },
]


def uses_postgres() -> bool:
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))


def sql_placeholders(sql: str) -> str:
    return sql.replace("?", "%s") if uses_postgres() else sql


def get_db_connection():
    if uses_postgres():
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)

    SQLITE_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(SQLITE_DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def db_execute(sql: str, params: tuple = (), *, fetch: str = ""):
    with get_db_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(sql_placeholders(sql), params)
        if not uses_postgres():
            connection.commit()
        if fetch == "one":
            row = cursor.fetchone()
            return dict(row) if row else None
        if fetch == "all":
            return [dict(row) for row in cursor.fetchall()]
    return None


def init_database() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS knowledge_base_store (
            id INTEGER PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS customer_chats (
            customer_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS customer_messages (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_customer_messages_customer_created ON customer_messages (customer_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_customer_chats_updated ON customer_chats (updated_at)",
        """
        CREATE TABLE IF NOT EXISTS training_history (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            instruction TEXT NOT NULL,
            result_message TEXT NOT NULL,
            status TEXT NOT NULL,
            before_snapshot TEXT,
            after_snapshot TEXT,
            reverted INTEGER NOT NULL DEFAULT 0,
            reverted_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_training_history_created ON training_history (created_at)",
        """
        CREATE TABLE IF NOT EXISTS response_templates (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS broadcast_history (
            id TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            target_mode TEXT NOT NULL,
            target_query TEXT,
            sent_count INTEGER NOT NULL,
            failed_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    ]
    for statement in statements:
        db_execute(statement)


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def json_loads(value: str, fallback):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        logger.exception("Stored JSON is invalid.")
        return fallback


def is_admin_logged_in() -> bool:
    return session.get("admin_logged_in") is True


def require_admin() -> None:
    if not ADMIN_PASSWORD:
        abort(503, description="ADMIN_PASSWORD is not configured")
    if not is_admin_logged_in():
        abort(401, description="Admin login required")


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def secure_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_csrf_token() -> None:
    token = request.form.get("csrf_token", "")
    if not hmac.compare_digest(token, session.get("csrf_token", "")):
        abort(400, description="Invalid form token")


def bootstrap_storage_file(path: Path, seed_path: Path) -> None:
    if path.exists() or path == seed_path or not seed_path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seed_path, path)


def load_knowledge_base() -> dict:
    row = db_execute("SELECT data FROM knowledge_base_store WHERE id = 1", fetch="one")
    if row:
        return json_loads(row["data"], {})

    seed = {}
    if KNOWLEDGE_BASE_PATH.exists():
        seed = json_loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"), {})
    save_knowledge_base(seed)
    return seed


def save_knowledge_base(knowledge_base: dict) -> None:
    db_execute(
        """
        INSERT INTO knowledge_base_store (id, data, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
        """,
        (json_dumps(knowledge_base), utc_now_iso()),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_file(path: Path, fallback):
    if not path.exists():
        return fallback

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.exception("JSON file is invalid: %s", path)
        return fallback
    return data


def save_json_file(path: Path, data) -> None:
    formatted_json = json.dumps(data, ensure_ascii=False, indent=2)
    write_text_file_safely(path, formatted_json + "\n")


def write_text_file_safely(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    backup_path = path.with_suffix(f"{path.suffix}.bak")
    temp_path.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copyfile(path, backup_path)
    temp_path.replace(path)


def load_customer_chats() -> dict:
    chats = {}
    chat_rows = db_execute(
        "SELECT customer_id, display_name, created_at, updated_at FROM customer_chats",
        fetch="all",
    )
    message_rows = db_execute(
        "SELECT id, customer_id, role, text, created_at FROM customer_messages ORDER BY created_at ASC",
        fetch="all",
    )
    for row in chat_rows:
        chats[row["customer_id"]] = {
            "customer_id": row["customer_id"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": [],
        }
    for row in message_rows:
        customer_id = row["customer_id"]
        chat = chats.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "display_name": customer_id,
                "created_at": row["created_at"],
                "updated_at": row["created_at"],
                "messages": [],
            },
        )
        chat["messages"].append(
            {
                "id": row["id"],
                "role": row["role"],
                "text": row["text"],
                "created_at": row["created_at"],
            }
        )
    return chats


def save_customer_chats(chats: dict) -> None:
    db_execute("DELETE FROM customer_messages")
    db_execute("DELETE FROM customer_chats")
    for customer_id, chat in chats.items():
        db_execute(
            """
            INSERT INTO customer_chats (customer_id, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                customer_id,
                chat.get("display_name") or customer_id,
                chat.get("created_at") or utc_now_iso(),
                chat.get("updated_at") or utc_now_iso(),
            ),
        )
        for message in chat.get("messages", []):
            db_execute(
                """
                INSERT INTO customer_messages (id, customer_id, role, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.get("id") or uuid.uuid4().hex,
                    customer_id,
                    message.get("role", ""),
                    message.get("text", ""),
                    message.get("created_at") or utc_now_iso(),
                ),
            )


def load_customer_ai_settings() -> dict:
    settings = {}
    rows = db_execute(
        "SELECT scope, key, value, updated_at FROM app_settings WHERE scope IN ('global', 'customer_ai')",
        fetch="all",
    )
    for row in rows:
        if row["scope"] == "global":
            settings.setdefault("__global__", {})[row["key"]] = json_loads(row["value"], row["value"])
            settings["__global__"]["updated_at"] = row["updated_at"]
        if row["scope"] == "customer_ai":
            settings.setdefault(row["key"], {})["ai_enabled"] = bool(json_loads(row["value"], True))
            settings[row["key"]]["updated_at"] = row["updated_at"]
    return settings


def save_customer_ai_settings(settings: dict) -> None:
    db_execute("DELETE FROM app_settings WHERE scope IN ('global', 'customer_ai')")
    global_settings = settings.get("__global__", {})
    for key, value in global_settings.items():
        if key == "updated_at":
            continue
        db_execute(
            """
            INSERT INTO app_settings (scope, key, value, updated_at)
            VALUES ('global', ?, ?, ?)
            ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json_dumps(value), global_settings.get("updated_at") or utc_now_iso()),
        )
    for customer_id, customer_settings in settings.items():
        if customer_id == "__global__":
            continue
        db_execute(
            """
            INSERT INTO app_settings (scope, key, value, updated_at)
            VALUES ('customer_ai', ?, ?, ?)
            ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (
                customer_id,
                json_dumps(customer_settings.get("ai_enabled", True)),
                customer_settings.get("updated_at") or utc_now_iso(),
            ),
        )


def get_ai_unavailable_message() -> str:
    row = db_execute(
        "SELECT value FROM app_settings WHERE scope = 'global' AND key = 'offline_message'",
        fetch="one",
    )
    message = str(json_loads(row["value"], "")).strip() if row else ""
    return message or AI_UNAVAILABLE_MESSAGE


def set_ai_unavailable_message(message: str) -> None:
    db_execute(
        """
        INSERT INTO app_settings (scope, key, value, updated_at)
        VALUES ('global', 'offline_message', ?, ?)
        ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json_dumps(message.strip() or AI_UNAVAILABLE_MESSAGE), utc_now_iso()),
    )
    socketio.emit("settings_updated", {"type": "offline_message"})


def is_customer_ai_enabled(customer_id: str) -> bool:
    row = db_execute(
        "SELECT value FROM app_settings WHERE scope = 'customer_ai' AND key = ?",
        (customer_id,),
        fetch="one",
    )
    return bool(json_loads(row["value"], True)) if row else True


def is_global_ai_enabled() -> bool:
    row = db_execute(
        "SELECT value FROM app_settings WHERE scope = 'global' AND key = 'ai_enabled'",
        fetch="one",
    )
    return bool(json_loads(row["value"], True)) if row else True


def set_global_ai_enabled(enabled: bool) -> None:
    db_execute(
        """
        INSERT INTO app_settings (scope, key, value, updated_at)
        VALUES ('global', 'ai_enabled', ?, ?)
        ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json_dumps(enabled), utc_now_iso()),
    )
    socketio.emit("settings_updated", {"type": "global_ai", "global_ai_enabled": enabled})


def set_customer_ai_enabled(customer_id: str, enabled: bool) -> None:
    db_execute(
        """
        INSERT INTO app_settings (scope, key, value, updated_at)
        VALUES ('customer_ai', ?, ?, ?)
        ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (customer_id, json_dumps(enabled), utc_now_iso()),
    )
    socketio.emit(
        "chat_updated",
        {"customer_id": customer_id, "reason": "customer_ai", "ai_enabled": enabled},
    )


def append_customer_message(customer_id: str, role: str, text: str) -> None:
    if not customer_id:
        return

    now = utc_now_iso()
    db_execute(
        """
        INSERT INTO customer_chats (customer_id, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (customer_id, customer_id, now, now),
    )
    db_execute(
        """
        INSERT INTO customer_messages (id, customer_id, role, text, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (uuid.uuid4().hex, customer_id, role, text, now),
    )
    socketio.emit("chat_updated", {"customer_id": customer_id, "reason": "message"})


def load_response_templates() -> list[dict]:
    rows = db_execute(
        "SELECT id, title, text, created_at FROM response_templates ORDER BY created_at ASC",
        fetch="all",
    )
    if rows:
        return rows

    save_response_templates(DEFAULT_RESPONSE_TEMPLATES)
    return DEFAULT_RESPONSE_TEMPLATES


def save_response_templates(templates: list[dict]) -> None:
    db_execute("DELETE FROM response_templates")
    for item in templates:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        db_execute(
            """
            INSERT INTO response_templates (id, title, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(item.get("id") or uuid.uuid4().hex),
                str(item.get("title") or text[:40]),
                text,
                str(item.get("created_at") or utc_now_iso()),
            ),
        )


def add_response_template(title: str, text: str) -> dict:
    template = {
        "id": uuid.uuid4().hex,
        "title": title.strip() or text.strip()[:40],
        "text": text.strip(),
        "created_at": utc_now_iso(),
    }
    db_execute(
        """
        INSERT INTO response_templates (id, title, text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (template["id"], template["title"], template["text"], template["created_at"]),
    )
    socketio.emit("templates_updated", {"type": "created"})
    return template


def delete_response_template(template_id: str) -> bool:
    row = db_execute(
        "SELECT id FROM response_templates WHERE id = ?",
        (template_id,),
        fetch="one",
    )
    if not row:
        return False
    db_execute("DELETE FROM response_templates WHERE id = ?", (template_id,))
    socketio.emit("templates_updated", {"type": "deleted"})
    return True


def get_event_customer_id(event: dict) -> str:
    source = event.get("source", {})
    return source.get("userId") or source.get("groupId") or source.get("roomId") or "unknown"


def customer_chat_summary(customer_id: str, chat: dict, settings: dict) -> dict:
    messages = chat.get("messages", [])
    ai_enabled = settings.get(customer_id, {}).get("ai_enabled", True)
    return {
        "customer_id": customer_id,
        "display_name": chat.get("display_name") or customer_id,
        "updated_at": chat.get("updated_at", ""),
        "last_message": messages[-1].get("text", "") if messages else "",
        "ai_enabled": ai_enabled,
        "is_pinned": not ai_enabled,
    }


def public_customer_chat(customer_id: str, chat: dict, settings: dict) -> dict:
    ai_enabled = settings.get(customer_id, {}).get("ai_enabled", True)
    return {
        "customer_id": customer_id,
        "display_name": chat.get("display_name") or customer_id,
        "updated_at": chat.get("updated_at", ""),
        "ai_enabled": ai_enabled,
        "is_pinned": not ai_enabled,
        "messages": chat.get("messages", []),
    }


def normalize_search_text(text: str) -> str:
    return " ".join((text or "").casefold().split())


def chat_matches_query(customer_id: str, chat: dict, query: str) -> bool:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return True

    searchable_parts = [
        customer_id,
        chat.get("display_name", ""),
    ]
    for message in chat.get("messages", []):
        searchable_parts.append(message.get("text", ""))
        searchable_parts.append(message.get("role", ""))

    return normalized_query in normalize_search_text(" ".join(searchable_parts))


def sort_customer_summaries(customers: list[dict]) -> list[dict]:
    customers.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    customers.sort(key=lambda item: 0 if item.get("is_pinned") else 1)
    return customers


def get_broadcast_recipients(mode: str, query: str, selected_ids: list[str]) -> list[str]:
    chats = load_customer_chats()
    if mode == "selected":
        return [customer_id for customer_id in selected_ids if customer_id in chats]
    if mode == "search":
        return [
            customer_id
            for customer_id, chat in chats.items()
            if chat_matches_query(customer_id, chat, query)
        ]
    return list(chats.keys())


def save_broadcast_history(
    *, message: str, target_mode: str, target_query: str, sent_count: int, failed_count: int
) -> None:
    db_execute(
        """
        INSERT INTO broadcast_history (
            id, message, target_mode, target_query, sent_count, failed_count, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            message,
            target_mode,
            target_query,
            sent_count,
            failed_count,
            utc_now_iso(),
        ),
    )


def calculate_admin_analytics() -> dict:
    chats = load_customer_chats()
    messages = []
    for customer_id, chat in chats.items():
        for message in chat.get("messages", []):
            messages.append({**message, "customer_id": customer_id})
    messages.sort(key=lambda item: item.get("created_at", ""))

    today = datetime.now(timezone.utc).date().isoformat()
    active_today = {
        item["customer_id"]
        for item in messages
        if item.get("role") == "customer" and item.get("created_at", "").startswith(today)
    }
    customer_messages_today = [
        item
        for item in messages
        if item.get("role") == "customer" and item.get("created_at", "").startswith(today)
    ]

    response_seconds = []
    for customer_id, chat in chats.items():
        chat_messages = sorted(chat.get("messages", []), key=lambda item: item.get("created_at", ""))
        for index, message in enumerate(chat_messages):
            if message.get("role") != "customer":
                continue
            customer_time = parse_iso_datetime(message.get("created_at", ""))
            if customer_time is None:
                continue
            for candidate in chat_messages[index + 1 :]:
                if candidate.get("role") not in {"nong_kan", "admin"}:
                    continue
                response_time = parse_iso_datetime(candidate.get("created_at", ""))
                if response_time is not None:
                    response_seconds.append(max(0, (response_time - customer_time).total_seconds()))
                break

    bot_answers = [item for item in messages if item.get("role") == "nong_kan"]
    bot_successes = [
        item
        for item in bot_answers
        if not is_low_information_answer(item.get("text", ""))
        and item.get("text", "").strip() != get_ai_unavailable_message()
    ]
    recent_broadcasts = db_execute(
        """
        SELECT id, message, target_mode, target_query, sent_count, failed_count, created_at
        FROM broadcast_history
        ORDER BY created_at DESC
        LIMIT 10
        """,
        fetch="all",
    )
    return {
        "total_customers": len(chats),
        "active_users_today": len(active_today),
        "customer_messages_today": len(customer_messages_today),
        "average_response_seconds": round(sum(response_seconds) / len(response_seconds), 1)
        if response_seconds
        else 0,
        "bot_success_rate": round((len(bot_successes) / len(bot_answers)) * 100, 1)
        if bot_answers
        else 0,
        "recent_broadcasts": recent_broadcasts,
    }


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_training_history() -> list[dict]:
    rows = db_execute(
        """
        SELECT id, created_at, instruction, result_message, status, before_snapshot,
               after_snapshot, reverted, reverted_at
        FROM training_history
        ORDER BY created_at ASC
        """,
        fetch="all",
    )
    history = []
    for row in rows:
        history.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "instruction": row["instruction"],
                "result_message": row["result_message"],
                "status": row["status"],
                "before_snapshot": json_loads(row["before_snapshot"], None),
                "after_snapshot": json_loads(row["after_snapshot"], None),
                "reverted": bool(row["reverted"]),
                "reverted_at": row.get("reverted_at"),
            }
        )
    return history


def save_training_history(history: list[dict]) -> None:
    db_execute("DELETE FROM training_history")
    for entry in history:
        db_execute(
            """
            INSERT INTO training_history (
                id, created_at, instruction, result_message, status, before_snapshot,
                after_snapshot, reverted, reverted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.get("id") or uuid.uuid4().hex,
                entry.get("created_at") or utc_now_iso(),
                entry.get("instruction", ""),
                entry.get("result_message", ""),
                entry.get("status", ""),
                json_dumps(entry.get("before_snapshot")) if entry.get("before_snapshot") is not None else None,
                json_dumps(entry.get("after_snapshot")) if entry.get("after_snapshot") is not None else None,
                1 if entry.get("reverted") else 0,
                entry.get("reverted_at"),
            ),
        )


def add_training_history_entry(
    *,
    instruction: str,
    result_message: str,
    status: str,
    before_snapshot: Optional[dict] = None,
    after_snapshot: Optional[dict] = None,
) -> dict:
    entry = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instruction": instruction,
        "result_message": result_message,
        "status": status,
        "before_snapshot": before_snapshot,
        "after_snapshot": after_snapshot,
        "reverted": False,
    }
    db_execute(
        """
        INSERT INTO training_history (
            id, created_at, instruction, result_message, status, before_snapshot,
            after_snapshot, reverted, reverted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)
        """,
        (
            entry["id"],
            entry["created_at"],
            entry["instruction"],
            entry["result_message"],
            entry["status"],
            json_dumps(before_snapshot) if before_snapshot is not None else None,
            json_dumps(after_snapshot) if after_snapshot is not None else None,
        ),
    )
    return entry


def update_training_history_entry(entry_id: str, **updates) -> Optional[dict]:
    row = db_execute("SELECT id FROM training_history WHERE id = ?", (entry_id,), fetch="one")
    if row is None:
        return None

    allowed_columns = {
        "result_message",
        "status",
        "before_snapshot",
        "after_snapshot",
        "reverted",
        "reverted_at",
    }
    for key, value in updates.items():
        if key not in allowed_columns:
            continue
        stored_value = value
        if key in {"before_snapshot", "after_snapshot"}:
            stored_value = json_dumps(value) if value is not None else None
        if key == "reverted":
            stored_value = 1 if value else 0
        db_execute(f"UPDATE training_history SET {key} = ? WHERE id = ?", (stored_value, entry_id))

    history = load_training_history()
    return next((entry for entry in history if entry.get("id") == entry_id), None)


def public_training_history_entry(entry: dict) -> dict:
    status = entry.get("status", "")
    status_label = {
        "success": "สำเร็จ",
        "failed": "ไม่สำเร็จ",
        "pending": "กำลังเทรน",
    }.get(status, status or "-")
    return {
        "id": entry.get("id", ""),
        "created_at": entry.get("created_at", ""),
        "instruction": entry.get("instruction", ""),
        "result_message": entry.get("result_message", ""),
        "status": status,
        "status_label": status_label,
        "reverted": bool(entry.get("reverted")),
        "can_revert": status == "success" and not entry.get("reverted"),
    }


def training_history_to_messages(history: list[dict]) -> list[dict]:
    messages = []
    for entry in history[-12:]:
        instruction = entry.get("instruction", "")
        result_message = entry.get("result_message", "")
        if instruction:
            messages.append({"role": "user", "text": instruction})
        if result_message:
            messages.append({"role": "bot", "text": result_message})
    return messages


def delete_training_history_entry(entry_id: str) -> bool:
    row = db_execute("SELECT id FROM training_history WHERE id = ?", (entry_id,), fetch="one")
    if not row:
        return False
    db_execute("DELETE FROM training_history WHERE id = ?", (entry_id,))
    return True


def revert_training_history_entry(entry_id: str) -> bool:
    history = load_training_history()
    for entry in history:
        if entry.get("id") != entry_id:
            continue

        before_snapshot = entry.get("before_snapshot")
        if not isinstance(before_snapshot, dict):
            return False

        save_knowledge_base(before_snapshot)
        entry["reverted"] = True
        entry["reverted_at"] = datetime.now(timezone.utc).isoformat()
        save_training_history(history)
        return True
    return False


def table_row_count(table_name: str) -> int:
    row = db_execute(f"SELECT COUNT(*) AS count FROM {table_name}", fetch="one")
    return int(row["count"]) if row else 0


def load_json_path(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.exception("Could not migrate invalid JSON file: %s", path)
        return fallback


def migrate_json_files_to_database() -> None:
    if table_row_count("knowledge_base_store") == 0 and KNOWLEDGE_BASE_PATH.exists():
        knowledge_base = load_json_path(KNOWLEDGE_BASE_PATH, {})
        if isinstance(knowledge_base, dict):
            save_knowledge_base(knowledge_base)

    if table_row_count("customer_chats") == 0 and CUSTOMER_CHATS_PATH.exists():
        chats = load_json_path(CUSTOMER_CHATS_PATH, {})
        if isinstance(chats, dict):
            save_customer_chats(chats)

    if table_row_count("app_settings") == 0 and CUSTOMER_AI_SETTINGS_PATH.exists():
        settings = load_json_path(CUSTOMER_AI_SETTINGS_PATH, {})
        if isinstance(settings, dict):
            save_customer_ai_settings(settings)

    if table_row_count("training_history") == 0 and TRAINING_HISTORY_PATH.exists():
        history = load_json_path(TRAINING_HISTORY_PATH, [])
        if isinstance(history, list):
            save_training_history(history)

    if table_row_count("response_templates") == 0 and RESPONSE_TEMPLATES_PATH.exists():
        templates = load_json_path(RESPONSE_TEMPLATES_PATH, [])
        if isinstance(templates, list):
            save_response_templates(templates)


def extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI did not return a JSON object")

    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI returned JSON, but it was not an object")
    return parsed


def train_knowledge_base_with_ai(instruction: str) -> tuple[Optional[dict], str]:
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not configured.")
        return None, get_ai_unavailable_message()

    current_knowledge_base = load_knowledge_base()
    current_text = json.dumps(current_knowledge_base, ensure_ascii=False, indent=2)
    system_prompt = f"""
คุณคือระบบแก้ไขฐานความรู้ของ LINE OA Bot ชื่อ "น้องก้าน"

หน้าที่:
- อ่านฐานความรู้ JSON ปัจจุบัน
- อ่านคำสั่งเทรนจากผู้ดูแล
- คืนค่า JSON object ใหม่ทั้งก้อนเท่านั้น

กฎสำคัญ:
- ห้ามตอบเป็นคำอธิบาย ห้ามใส่ Markdown ห้ามครอบด้วย ```json
- ห้ามเดาข้อมูลที่ผู้ดูแลไม่ได้ให้มา
- ถ้าผู้ดูแลให้ข้อมูลสินค้า ราคา โปรโมชัน วิธีสั่งซื้อ การจัดส่ง หรือข้อมูลบริษัท ให้ใส่ลง JSON ให้เหมาะสม
- ถ้าข้อมูลใหม่ไม่เข้าหมวดเดิม ให้เพิ่ม key ที่เหมาะสมได้ เช่น faqs หรือ training_notes
- ต้องรักษาข้อมูลเดิมไว้ ยกเว้นผู้ดูแลสั่งแก้ ลบ หรือแทนที่ชัดเจน
- ถ้าผู้ดูแลสั่งลบ ให้ลบเฉพาะข้อมูลที่ระบุชัดเจน

ฐานความรู้ JSON ปัจจุบัน:
{current_text}
""".strip()

    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction},
        ],
        "temperature": 0.1,
        "max_tokens": 1600,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": APP_NAME,
    }
    if APP_URL:
        headers["HTTP-Referer"] = APP_URL

    try:
        response = requests.post(
            f"{AI_API_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=(10, AI_TRAINING_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return extract_json_object(content), "น้องก้านเรียนรู้และอัปเดตฐานความรู้เรียบร้อยค่ะ"
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        logger.exception("Knowledge training request failed.")
        return None, "ขอโทษค่ะ รอบนี้น้องก้านยังอัปเดตฐานความรู้ไม่สำเร็จ ลองเขียนคำสั่งให้ชัดขึ้นอีกครั้งนะคะ"


def verify_line_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def build_system_prompt(knowledge_base: dict) -> str:
    knowledge_text = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    return f"""
คุณคือ "น้องก้าน" ผู้ช่วยตอบคำถามลูกค้าของบริษัทน้องก้านผ่าน LINE Official Account

บุคลิกและรูปแบบคำตอบ:
- พูดภาษาไทยสุภาพ เป็นกันเอง
- ตอบสั้น กระชับ อ่านง่าย
- ใช้ประโยคที่ลูกค้าทั่วไปเข้าใจง่าย
- ห้ามอ้างว่าตัวเองเป็น AI

กฎสำคัญ:
- ตอบตามฐานความรู้บริษัทด้านล่างเป็นหลักเท่านั้น
- ห้ามเดาข้อมูลสินค้า ราคา โปรโมชัน เงื่อนไข การจัดส่ง หรือข้อมูลบริษัท
- ถ้าฐานความรู้ไม่มีคำตอบที่ชัดเจน ให้ตอบว่า: "{FALLBACK_MESSAGE}"
- ถ้าข้อมูลในฐานความรู้ยังเป็นข้อความตัวอย่างหรือยังไม่ได้กรอกจริง ให้ถือว่าไม่มีข้อมูล
- ห้ามแต่งเบอร์โทร ลิงก์ ช่องทางติดต่อ ราคา หรือระยะเวลาจัดส่งเอง

ฐานความรู้บริษัท:
{knowledge_text}
""".strip()


def is_low_information_answer(answer: str) -> bool:
    normalized = answer.strip().lower()
    if not normalized:
        return True

    fallback_signals = [
        "ไม่มีข้อมูล",
        "ยังไม่มีข้อมูล",
        "ไม่พบข้อมูล",
        "ส่งต่อให้เจ้าหน้าที่",
        "เจ้าหน้าที่จะติดต่อกลับ",
    ]
    return any(signal in normalized for signal in fallback_signals)


def ask_ai(customer_message: str) -> str:
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY is not configured.")
        return get_ai_unavailable_message()

    knowledge_base = load_knowledge_base()
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(knowledge_base)},
            {"role": "user", "content": customer_message},
        ],
        "temperature": 0.2,
        "max_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": APP_NAME,
    }
    if APP_URL:
        headers["HTTP-Referer"] = APP_URL

    try:
        response = requests.post(
            f"{AI_API_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        answer = data["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        logger.exception("AI API request failed.")
        return get_ai_unavailable_message()

    if is_low_information_answer(answer):
        return FALLBACK_MESSAGE

    return answer


def ask_nong_bai(question: str, selected_customer_id: str = "") -> str:
    if not OPENROUTER_API_KEY:
        return get_ai_unavailable_message()

    chats = load_customer_chats()
    settings = load_customer_ai_settings()
    knowledge_base = load_knowledge_base()
    selected_chat = chats.get(selected_customer_id, {}) if selected_customer_id else {}
    compact_chats = []
    for customer_id, chat in sorted(
        chats.items(),
        key=lambda item: item[1].get("updated_at", ""),
        reverse=True,
    )[:30]:
        compact_chats.append(
            {
                "customer_id": customer_id,
                "ai_enabled": settings.get(customer_id, {}).get("ai_enabled", True),
                "recent_messages": chat.get("messages", [])[-12:],
            }
        )

    system_prompt = f"""
คุณคือ "น้องใบ" AI ผู้ช่วยหลังบ้านของแอดมินบริษัทน้องก้าน

หน้าที่:
- ตอบคำถามแอดมินเกี่ยวกับแชทลูกค้า ฐานความรู้ และสถานะหลังบ้าน
- สรุปว่าลูกค้าชอบถามอะไร ลูกค้าคนไหนต้องตอบเอง หรือควรปรับฐานความรู้อะไร
- พูดไทยสุภาพ กระชับ เป็นกันเอง
- ห้ามบอกว่าทำสิ่งที่ระบบยังทำไม่ได้ เช่น ส่งข้อความแทนแอดมินหรือแก้ข้อมูลโดยตรง

ฐานความรู้ปัจจุบัน:
{json.dumps(knowledge_base, ensure_ascii=False, indent=2)}

สถานะ AI รายลูกค้า:
{json.dumps(settings, ensure_ascii=False, indent=2)}

แชทลูกค้าล่าสุด:
{json.dumps(compact_chats, ensure_ascii=False, indent=2)}

แชทลูกค้าที่กำลังเปิดอยู่:
{json.dumps(selected_chat, ensure_ascii=False, indent=2)}
""".strip()
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Title": APP_NAME,
    }
    if APP_URL:
        headers["HTTP-Referer"] = APP_URL

    try:
        response = requests.post(
            f"{AI_API_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        logger.exception("Nong Bai request failed.")
        return "ขอโทษค่ะ ตอนนี้น้องใบตอบไม่ได้ ลองถามใหม่อีกครั้งนะคะ"


def reply_to_line(reply_token: str, text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN is not configured.")
        return

    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            LINE_REPLY_API_URL,
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("LINE reply API request failed.")


def push_to_line(customer_id: str, text: str) -> bool:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN is not configured.")
        return False

    payload = {
        "to": customer_id,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            LINE_PUSH_API_URL,
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("LINE push API request failed.")
        return False


def handle_event(event: dict) -> None:
    if event.get("type") != "message":
        return

    customer_id = get_event_customer_id(event)
    reply_token = event.get("replyToken")
    message = event.get("message", {})
    if not reply_token:
        return

    if message.get("type") != "text":
        append_customer_message(customer_id, "customer", "[ส่งข้อความที่ไม่ใช่ตัวอักษร]")
        if not is_global_ai_enabled():
            offline_message = get_ai_unavailable_message()
            reply_to_line(reply_token, offline_message)
            append_customer_message(customer_id, "nong_kan", offline_message)
            return
        if not is_customer_ai_enabled(customer_id):
            return
        reply_to_line(reply_token, NON_TEXT_MESSAGE)
        append_customer_message(customer_id, "nong_kan", NON_TEXT_MESSAGE)
        return

    customer_text = message.get("text", "").strip()
    if not customer_text:
        append_customer_message(customer_id, "customer", "[ข้อความว่าง]")
        if not is_global_ai_enabled():
            offline_message = get_ai_unavailable_message()
            reply_to_line(reply_token, offline_message)
            append_customer_message(customer_id, "nong_kan", offline_message)
            return
        if not is_customer_ai_enabled(customer_id):
            return
        reply_to_line(reply_token, NON_TEXT_MESSAGE)
        append_customer_message(customer_id, "nong_kan", NON_TEXT_MESSAGE)
        return

    append_customer_message(customer_id, "customer", customer_text)
    if not is_global_ai_enabled():
        offline_message = get_ai_unavailable_message()
        reply_to_line(reply_token, offline_message)
        append_customer_message(customer_id, "nong_kan", offline_message)
        return

    if not is_customer_ai_enabled(customer_id):
        return

    answer = ask_ai(customer_text)
    reply_to_line(reply_token, answer)
    append_customer_message(customer_id, "nong_kan", answer)


@app.get("/")
def health_check():
    return jsonify(
        {
            "status": "ok",
            "service": "nong-kan-line-bot",
            "bot_name": "น้องก้าน",
        }
    )


@app.get("/admin/login")
def admin_login_page():
    if not ADMIN_PASSWORD:
        return render_template(
            "admin_setup.html",
            app_name=APP_NAME,
        ), 503

    if is_admin_logged_in():
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html", error="")


@app.post("/admin/login")
def admin_login():
    if not ADMIN_PASSWORD:
        return render_template(
            "admin_setup.html",
            app_name=APP_NAME,
        ), 503

    password = request.form.get("password", "")
    if secure_text_equal(password, ADMIN_PASSWORD):
        session["admin_logged_in"] = True
        session.pop("csrf_token", None)
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html", error="รหัสผ่านไม่ถูกต้องค่ะ"), 401


@app.post("/admin/logout")
def admin_logout():
    require_admin()
    verify_csrf_token()
    session.clear()
    return redirect(url_for("admin_login_page"))


@app.get("/admin")
def admin_dashboard():
    if not ADMIN_PASSWORD:
        return render_template(
            "admin_setup.html",
            app_name=APP_NAME,
        ), 503

    if not is_admin_logged_in():
        return redirect(url_for("admin_login_page"))

    return render_admin_dashboard(message=request.args.get("message", ""))


def render_admin_dashboard(
    *,
    message: str = "",
    status_code: int = 200,
    test_answer: str = "",
    test_question: str = "",
    train_messages: Optional[list[dict]] = None,
    knowledge_base: Optional[dict] = None,
    raw_knowledge_text: Optional[str] = None,
):
    if raw_knowledge_text is None:
        if knowledge_base is None:
            knowledge_base = load_knowledge_base()
        raw_knowledge_text = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    training_history = load_training_history()

    return render_template(
        "admin_dashboard.html",
        ai_model=AI_MODEL,
        ai_api_base_url=AI_API_BASE_URL,
        app_name=APP_NAME,
        app_url=APP_URL,
        csrf_token=get_csrf_token(),
        has_line_channel_access_token=bool(LINE_CHANNEL_ACCESS_TOKEN),
        has_line_channel_secret=bool(LINE_CHANNEL_SECRET),
        has_openrouter_api_key=bool(OPENROUTER_API_KEY),
        knowledge_path=str(KNOWLEDGE_BASE_PATH),
        knowledge_text=raw_knowledge_text,
        message=message,
        training_history=list(reversed(training_history)),
        test_answer=test_answer,
        test_question=test_question,
        train_messages=train_messages if train_messages is not None else training_history_to_messages(training_history),
    ), status_code


@app.post("/admin/save")
def admin_save_knowledge_base():
    require_admin()
    verify_csrf_token()

    raw_json = request.form.get("knowledge_base", "").strip()
    try:
        knowledge_base = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return render_admin_dashboard(
            message=f"JSON ยังไม่ถูกต้อง: {exc}",
            raw_knowledge_text=raw_json,
            status_code=400,
        )

    if not isinstance(knowledge_base, dict):
        return render_admin_dashboard(
            message="ฐานความรู้ต้องเป็น JSON object เท่านั้นค่ะ",
            raw_knowledge_text=raw_json,
            status_code=400,
        )

    save_knowledge_base(knowledge_base)
    return redirect(url_for("admin_dashboard", message="บันทึกฐานความรู้เรียบร้อยค่ะ"))


@app.post("/admin/train")
def admin_train_knowledge_base():
    require_admin()
    verify_csrf_token()

    is_async_request = request.headers.get("X-Requested-With") == "fetch"
    instruction = request.form.get("instruction", "").strip()
    if not instruction:
        if is_async_request:
            return jsonify(
                {
                    "ok": False,
                    "message": "กรุณาพิมพ์สิ่งที่ต้องการสอนน้องก้านก่อนค่ะ",
                }
            ), 400
        return render_admin_dashboard(
            message="กรุณาพิมพ์สิ่งที่ต้องการสอนน้องก้านก่อนค่ะ",
            status_code=400,
        )

    before_knowledge_base = load_knowledge_base()
    history_entry = add_training_history_entry(
        instruction=instruction,
        result_message="กำลังเทรนน้องก้านอยู่ค่ะ...",
        status="pending",
        before_snapshot=before_knowledge_base,
    )
    updated_knowledge_base, result_message = train_knowledge_base_with_ai(instruction)
    if updated_knowledge_base is None:
        updated_entry = update_training_history_entry(
            history_entry["id"],
            result_message=result_message,
            status="failed",
        ) or history_entry
        if is_async_request:
            return jsonify(
                {
                    "ok": False,
                    "message": result_message,
                    "entry": public_training_history_entry(updated_entry),
                }
            ), 502
        return render_admin_dashboard(
            message=result_message,
            status_code=502,
        )

    save_knowledge_base(updated_knowledge_base)
    updated_entry = update_training_history_entry(
        history_entry["id"],
        result_message=result_message,
        status="success",
        after_snapshot=updated_knowledge_base,
    ) or history_entry
    if is_async_request:
        return jsonify(
            {
                "ok": True,
                "message": result_message,
                "entry": public_training_history_entry(updated_entry),
                "knowledge_text": json.dumps(updated_knowledge_base, ensure_ascii=False, indent=2),
            }
        )
    return render_admin_dashboard(
        message=result_message,
        knowledge_base=updated_knowledge_base,
    )


@app.post("/admin/train/clear")
def admin_clear_train_messages():
    require_admin()
    verify_csrf_token()

    save_training_history([])
    return redirect(url_for("admin_dashboard", message="ล้างประวัติการเทรนเรียบร้อยค่ะ"))


@app.post("/admin/history/delete")
def admin_delete_training_history():
    require_admin()
    verify_csrf_token()

    entry_id = request.form.get("entry_id", "")
    if delete_training_history_entry(entry_id):
        return redirect(url_for("admin_dashboard", message="ลบรายการออกจากประวัติแล้วค่ะ"))
    return redirect(url_for("admin_dashboard", message="ไม่พบรายการประวัติที่ต้องการลบค่ะ"))


@app.post("/admin/history/revert")
def admin_revert_training_history():
    require_admin()
    verify_csrf_token()

    entry_id = request.form.get("entry_id", "")
    if revert_training_history_entry(entry_id):
        return redirect(url_for("admin_dashboard", message="ย้อนฐานความรู้กลับก่อนรายการนี้แล้วค่ะ"))
    return redirect(url_for("admin_dashboard", message="ย้อนรายการนี้ไม่ได้ค่ะ"))


@app.get("/admin/chats")
def admin_customer_chats():
    if not ADMIN_PASSWORD:
        return render_template(
            "admin_setup.html",
            app_name=APP_NAME,
        ), 503

    if not is_admin_logged_in():
        return redirect(url_for("admin_login_page"))

    chats = load_customer_chats()
    settings = load_customer_ai_settings()
    query = request.args.get("q", "").strip()
    customers = []
    for customer_id, chat in chats.items():
        if not chat_matches_query(customer_id, chat, query):
            continue
        customers.append(customer_chat_summary(customer_id, chat, settings))
    customers = sort_customer_summaries(customers)
    selected_customer_id = request.args.get("customer_id") or (customers[0]["customer_id"] if customers else "")
    if selected_customer_id and selected_customer_id not in {customer["customer_id"] for customer in customers}:
        selected_customer_id = customers[0]["customer_id"] if customers else ""
    selected_chat = chats.get(selected_customer_id, {})
    return render_template(
        "admin_chats.html",
        app_name=APP_NAME,
        csrf_token=get_csrf_token(),
        customers=customers,
        selected_customer_id=selected_customer_id,
        selected_chat=selected_chat,
        selected_ai_enabled=settings.get(selected_customer_id, {}).get("ai_enabled", True),
        global_ai_enabled=is_global_ai_enabled(),
        offline_message=get_ai_unavailable_message(),
        response_templates=load_response_templates(),
        analytics=calculate_admin_analytics(),
        nong_bai_messages=session.get("nong_bai_messages", []),
        message=request.args.get("message", ""),
        query=query,
    )


@app.get("/admin/chats/data")
def admin_customer_chats_data():
    require_admin()

    chats = load_customer_chats()
    settings = load_customer_ai_settings()
    query = request.args.get("q", "").strip()
    customers = [
        customer_chat_summary(customer_id, chat, settings)
        for customer_id, chat in chats.items()
        if chat_matches_query(customer_id, chat, query)
    ]
    customers = sort_customer_summaries(customers)
    selected_customer_id = request.args.get("customer_id") or (customers[0]["customer_id"] if customers else "")
    if selected_customer_id and selected_customer_id not in {customer["customer_id"] for customer in customers}:
        selected_customer_id = customers[0]["customer_id"] if customers else ""
    selected_chat = public_customer_chat(
        selected_customer_id,
        chats.get(selected_customer_id, {}),
        settings,
    ) if selected_customer_id else {}
    return jsonify(
        {
            "ok": True,
            "customers": customers,
            "selected_customer_id": selected_customer_id,
            "selected_chat": selected_chat,
            "selected_ai_enabled": settings.get(selected_customer_id, {}).get("ai_enabled", True),
            "global_ai_enabled": is_global_ai_enabled(),
            "offline_message": get_ai_unavailable_message(),
            "response_templates": load_response_templates(),
            "analytics": calculate_admin_analytics(),
            "query": query,
        }
    )


@app.get("/admin/analytics/data")
def admin_analytics_data():
    require_admin()
    return jsonify({"ok": True, "analytics": calculate_admin_analytics()})


@app.post("/admin/chats/toggle-ai")
def admin_toggle_customer_ai():
    require_admin()
    verify_csrf_token()

    customer_id = request.form.get("customer_id", "")
    enabled = request.form.get("enabled") == "true"
    if customer_id:
        set_customer_ai_enabled(customer_id, enabled)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "customer_id": customer_id, "ai_enabled": enabled})
    return redirect(url_for("admin_customer_chats", customer_id=customer_id))


@app.post("/admin/chats/toggle-global-ai")
def admin_toggle_global_ai():
    require_admin()
    verify_csrf_token()

    enabled = request.form.get("enabled") == "true"
    set_global_ai_enabled(enabled)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "global_ai_enabled": enabled})
    return redirect(url_for("admin_customer_chats", customer_id=request.form.get("customer_id", "")))


@app.post("/admin/chats/offline-message")
def admin_update_offline_message():
    require_admin()
    verify_csrf_token()

    message = request.form.get("offline_message", "").strip()
    set_ai_unavailable_message(message)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "offline_message": get_ai_unavailable_message()})
    return redirect(url_for("admin_customer_chats", customer_id=request.form.get("customer_id", "")))


@app.post("/admin/chats/templates")
def admin_add_response_template():
    require_admin()
    verify_csrf_token()

    title = request.form.get("title", "").strip()
    text = request.form.get("text", "").strip()
    if not text:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": "กรุณาใส่ข้อความเท็มเพลตก่อนค่ะ"}), 400
        return redirect(url_for("admin_customer_chats", message="กรุณาใส่ข้อความเท็มเพลตก่อนค่ะ"))

    template = add_response_template(title, text)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "template": template, "templates": load_response_templates()})
    return redirect(url_for("admin_customer_chats", message="เพิ่มเท็มเพลตแล้วค่ะ"))


@app.post("/admin/chats/templates/delete")
def admin_delete_response_template():
    require_admin()
    verify_csrf_token()

    template_id = request.form.get("template_id", "")
    deleted = delete_response_template(template_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": deleted, "templates": load_response_templates()})
    return redirect(url_for("admin_customer_chats", message="ลบเท็มเพลตแล้วค่ะ" if deleted else "ไม่พบเท็มเพลตค่ะ"))


@app.post("/admin/broadcast")
def admin_broadcast():
    require_admin()
    verify_csrf_token()

    message = request.form.get("message", "").strip()
    mode = request.form.get("target_mode", "all").strip()
    query = request.form.get("target_query", "").strip()
    selected_ids = [
        item.strip()
        for item in request.form.get("customer_ids", "").replace("\n", ",").split(",")
        if item.strip()
    ]
    if not message:
        return jsonify({"ok": False, "message": "กรุณาใส่ข้อความ Broadcast ก่อนค่ะ"}), 400

    recipients = get_broadcast_recipients(mode, query, selected_ids)
    sent_count = 0
    failed_count = 0
    for customer_id in recipients:
        if push_to_line(customer_id, message):
            sent_count += 1
            append_customer_message(customer_id, "admin", f"[Broadcast]\n{message}")
        else:
            failed_count += 1

    save_broadcast_history(
        message=message,
        target_mode=mode,
        target_query=query,
        sent_count=sent_count,
        failed_count=failed_count,
    )
    socketio.emit("analytics_updated", calculate_admin_analytics())
    return jsonify(
        {
            "ok": True,
            "message": f"ส่ง Broadcast สำเร็จ {sent_count} คน ไม่สำเร็จ {failed_count} คน",
            "sent_count": sent_count,
            "failed_count": failed_count,
        }
    )


@app.post("/admin/chats/send")
def admin_send_customer_message():
    require_admin()
    verify_csrf_token()

    customer_id = request.form.get("customer_id", "").strip()
    text = request.form.get("message", "").strip()
    if not customer_id or not text:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": "กรุณาเลือกแชทและพิมพ์ข้อความค่ะ"}), 400
        return redirect(url_for("admin_customer_chats", customer_id=customer_id, message="กรุณาเลือกแชทและพิมพ์ข้อความค่ะ"))

    if push_to_line(customer_id, text):
        append_customer_message(customer_id, "admin", text)
        set_customer_ai_enabled(customer_id, False)
        chats = load_customer_chats()
        settings = load_customer_ai_settings()
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(
                {
                    "ok": True,
                    "message": "ส่งข้อความให้ลูกค้าแล้วค่ะ",
                    "selected_chat": public_customer_chat(customer_id, chats.get(customer_id, {}), settings),
                    "selected_ai_enabled": False,
                }
            )
        return redirect(url_for("admin_customer_chats", customer_id=customer_id, message="ส่งข้อความให้ลูกค้าแล้วค่ะ"))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": False, "message": "ส่งข้อความไม่สำเร็จค่ะ"}), 502
    return redirect(url_for("admin_customer_chats", customer_id=customer_id, message="ส่งข้อความไม่สำเร็จค่ะ"))


@app.post("/admin/chats/nong-bai")
def admin_ask_nong_bai():
    require_admin()
    verify_csrf_token()

    question = request.form.get("question", "").strip()
    selected_customer_id = request.form.get("customer_id", "").strip()
    if not question:
        return jsonify({"ok": False, "answer": "พิมพ์คำถามถึงน้องใบก่อนค่ะ"}), 400

    messages = list(session.get("nong_bai_messages", []))
    messages.append({"role": "user", "text": question})
    answer = ask_nong_bai(question, selected_customer_id)
    messages.append({"role": "bot", "text": answer})
    session["nong_bai_messages"] = messages[-20:]
    return jsonify({"ok": True, "answer": answer})


@app.post("/admin/chats/nong-bai/clear")
def admin_clear_nong_bai():
    require_admin()
    verify_csrf_token()

    session["nong_bai_messages"] = []
    return redirect(url_for("admin_customer_chats", customer_id=request.form.get("customer_id", "")))


@app.post("/admin/test")
def admin_test_question():
    require_admin()
    verify_csrf_token()

    question = request.form.get("question", "").strip()
    answer = ask_ai(question) if question else "กรุณาพิมพ์คำถามที่ต้องการทดสอบค่ะ"

    return render_admin_dashboard(
        message="ผลทดสอบคำตอบ",
        test_answer=answer,
        test_question=question,
    )


@app.post("/callback")
def line_callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        abort(400, description="Invalid LINE signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        abort(400, description="Invalid JSON payload")

    for event in payload.get("events", []):
        handle_event(event)

    return "OK"


init_database()
migrate_json_files_to_database()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
