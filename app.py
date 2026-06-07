import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path

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


load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

LINE_REPLY_API_URL = "https://api.line.me/v2/bot/message/reply"
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_API_BASE_URL = os.getenv("AI_API_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "openrouter/auto")
APP_NAME = os.getenv("APP_NAME", "nong-kan-line-bot")
APP_URL = os.getenv("APP_URL", "")

KNOWLEDGE_BASE_PATH = Path(os.getenv("KNOWLEDGE_BASE_PATH", "knowledge_base.json"))
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "20"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

FALLBACK_MESSAGE = (
    "ขออภัยค่ะ น้องก้านยังไม่มีข้อมูลเรื่องนี้ในระบบ "
    "จะส่งต่อให้เจ้าหน้าที่ติดต่อกลับนะคะ"
)
AI_UNAVAILABLE_MESSAGE = (
    "ขอโทษนะคะ ตอนนี้น้องก้านมึนหัวอยู่ "
    "กลับมาสอบถามน้องก้านใหม่ตอนน้องก้านมีสติแล้วนะคะ"
)
NON_TEXT_MESSAGE = "รบกวนพิมพ์คำถามเป็นข้อความนะคะ น้องก้านจะช่วยดูข้อมูลให้ค่ะ"


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


def load_knowledge_base() -> dict:
    if not KNOWLEDGE_BASE_PATH.exists():
        logger.warning("Knowledge base file not found: %s", KNOWLEDGE_BASE_PATH)
        return {}

    try:
        return json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.exception("Knowledge base JSON is invalid.")
        return {}


def save_knowledge_base(knowledge_base: dict) -> None:
    formatted_json = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    KNOWLEDGE_BASE_PATH.write_text(formatted_json + "\n", encoding="utf-8")


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
        return AI_UNAVAILABLE_MESSAGE

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
        return AI_UNAVAILABLE_MESSAGE

    if is_low_information_answer(answer):
        return FALLBACK_MESSAGE

    return answer


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


def handle_event(event: dict) -> None:
    if event.get("type") != "message":
        return

    reply_token = event.get("replyToken")
    message = event.get("message", {})
    if not reply_token:
        return

    if message.get("type") != "text":
        reply_to_line(reply_token, NON_TEXT_MESSAGE)
        return

    customer_text = message.get("text", "").strip()
    if not customer_text:
        reply_to_line(reply_token, NON_TEXT_MESSAGE)
        return

    answer = ask_ai(customer_text)
    reply_to_line(reply_token, answer)


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

    knowledge_base = load_knowledge_base()
    knowledge_text = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
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
        knowledge_text=knowledge_text,
        message=request.args.get("message", ""),
        test_answer="",
        test_question="",
    )


@app.post("/admin/save")
def admin_save_knowledge_base():
    require_admin()
    verify_csrf_token()

    raw_json = request.form.get("knowledge_base", "").strip()
    try:
        knowledge_base = json.loads(raw_json)
    except json.JSONDecodeError as exc:
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
            knowledge_text=raw_json,
            message=f"JSON ยังไม่ถูกต้อง: {exc}",
            test_answer="",
            test_question="",
        ), 400

    if not isinstance(knowledge_base, dict):
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
            knowledge_text=raw_json,
            message="ฐานความรู้ต้องเป็น JSON object เท่านั้นค่ะ",
            test_answer="",
            test_question="",
        ), 400

    save_knowledge_base(knowledge_base)
    return redirect(url_for("admin_dashboard", message="บันทึกฐานความรู้เรียบร้อยค่ะ"))


@app.post("/admin/test")
def admin_test_question():
    require_admin()
    verify_csrf_token()

    question = request.form.get("question", "").strip()
    knowledge_base = load_knowledge_base()
    knowledge_text = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
    answer = ask_ai(question) if question else "กรุณาพิมพ์คำถามที่ต้องการทดสอบค่ะ"

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
        knowledge_text=knowledge_text,
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
