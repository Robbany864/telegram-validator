"""
============================================================
  Telegram Number Validator — Complete Backend
  Flask + Telethon (MTProto User API)
============================================================
"""

import os
import re
import csv
import io
import asyncio
import logging
from datetime import datetime

from flask import Flask, render_template, request, jsonify, Response
from telethon import TelegramClient
from telethon.tl.functions.contacts import (
    ImportContactsRequest,
    DeleteContactsRequest,
)
from telethon.tl.types import InputPhoneContact
from telethon.errors import FloodWaitError, PhoneNumberInvalidError
from dotenv import load_dotenv

# ================= CONFIG =================
load_dotenv()

API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "validator_session")
SECRET_KEY   = os.getenv("SECRET_KEY", "dev-key-change-me")

BATCH_SIZE   = int(os.getenv("BATCH_SIZE", "5"))
BATCH_DELAY  = float(os.getenv("BATCH_DELAY", "1.5"))
MAX_NUMBERS  = int(os.getenv("MAX_NUMBERS", "500"))

if not API_ID or API_ID == 0:
    raise SystemExit(
        "\n❌ ERROR: API_ID is missing in .env file.\n"
        "   Open .env and set API_ID from https://my.telegram.org\n"
    )
if not API_HASH or API_HASH == "your_new_api_hash_here":
    raise SystemExit(
        "\n❌ ERROR: API_HASH is missing in .env file.\n"
        "   Open .env and set API_HASH from https://my.telegram.org\n"
    )

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("validator")

# ================= FLASK APP =================
app = Flask(__name__)
app.secret_key = SECRET_KEY

# ================= TELEGRAM CLIENT =================
_client = None


async def get_client():
    """Return a shared Telethon client (lazy init)."""
    global _client
    if _client is None or not _client.is_connected():
        log.info("🔌 Connecting to Telegram...")
        _client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await _client.start()
        me = await _client.get_me()
        log.info("✅ Logged in as: %s (id=%s)", me.first_name, me.id)
    return _client


# ================= HELPERS =================
PHONE_RE = re.compile(r"^\+?\d{7,15}$")


def clean_numbers(raw_list):
    """Split, trim, dedupe and validate phone numbers."""
    seen = set()
    valid, invalid = [], []

    for raw in raw_list:
        for p in re.split(r"[,\n;\t ]+", raw):
            p = p.strip()
            if not p:
                continue

            normalized = re.sub(r"[\s\-()]", "", p)
            if not normalized.startswith("+"):
                normalized = "+" + normalized

            if normalized in seen:
                continue
            seen.add(normalized)

            if PHONE_RE.match(normalized):
                valid.append(normalized)
            else:
                invalid.append(p)

    return valid, invalid


async def verify_batch(client, batch):
    """
    Verify a batch of phone numbers.

    IMPORTANT NOTE ON TELEGRAM PRIVACY:
    Telegram hides real accounts when the user's privacy setting
    "Who can find me by my number?" is set to "Nobody" or "My Contacts".
    In that case Telegram returns them as NOT found — this is a Telegram
    server-side rule and no tool can bypass it.
    """
    contacts = [
        InputPhoneContact(client_id=i, phone=num, first_name="Val", last_name=".")
        for i, num in enumerate(batch)
    ]

    try:
        res = await client(ImportContactsRequest(contacts))
    except FloodWaitError as fw:
        log.warning("⏳ FloodWait %ds — sleeping...", fw.seconds)
        await asyncio.sleep(fw.seconds + 2)
        return await verify_batch(client, batch)
    except PhoneNumberInvalidError:
        return [], [{"phone": n, "reason": "invalid_phone_format"} for n in batch]
    except Exception as e:
        log.error("Batch failed: %s", e)
        return [], [{"phone": n, "reason": "api_error"} for n in batch]

    imported_map = {imp.client_id: imp.user_id for imp in res.imported}
    users_by_id  = {u.id: u for u in res.users}
    retry_contacts = getattr(res, "retry_contacts", []) or []

    live, invalid = [], []

    for i, num in enumerate(batch):
        uid  = imported_map.get(i)
        user = users_by_id.get(uid) if uid else None

        if user:
            live.append({
                "phone":    num,
                "user_id":  user.id,
                "username": f"@{user.username}" if user.username else None,
                "name":     " ".join(filter(None, [user.first_name, user.last_name])) or None,
            })
        elif i in retry_contacts:
            invalid.append({
                "phone": num,
                "reason": "retry_later",
            })
        else:
            invalid.append({
                "phone": num,
                "reason": "not_found_or_private",
            })

    # Cleanup contacts
    if users_by_id:
        try:
            await client(DeleteContactsRequest(id=list(users_by_id.values())))
        except Exception as e:
            log.warning("Cleanup failed: %s", e)

    return live, invalid


async def check_all(numbers):
    client = await get_client()
    live, invalid = [], []
    total_batches = (len(numbers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(numbers), BATCH_SIZE):
        batch = numbers[i:i + BATCH_SIZE]
        b_num = i // BATCH_SIZE + 1
        log.info("🔍 Batch %d/%d (%d numbers)", b_num, total_batches, len(batch))

        l, inv = await verify_batch(client, batch)
        live.extend(l)
        invalid.extend(inv)

        if i + BATCH_SIZE < len(numbers):
            await asyncio.sleep(BATCH_DELAY)

    return {"live": live, "invalid": invalid}


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ================= ROUTES =================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check-numbers", methods=["POST"])
def check_numbers():
    data = request.get_json(silent=True) or {}
    raw  = data.get("numbers", "")
    raw_list = raw if isinstance(raw, list) else [raw]

    valid, invalid_fmt = clean_numbers(raw_list)

    if not valid and not invalid_fmt:
        return jsonify({"error": "No phone numbers provided."}), 400
    if len(valid) > MAX_NUMBERS:
        return jsonify({"error": f"Too many numbers (max {MAX_NUMBERS})."}), 400

    log.info("📥 Received %d valid numbers", len(valid))

    try:
        result = run_async(check_all(valid))
    except Exception as e:
        log.exception("Check failed")
        return jsonify({"error": str(e)}), 500

    invalid_list = result["invalid"] + [
        {"phone": n, "reason": "invalid_format"} for n in invalid_fmt
    ]

    log.info("✅ Done. Live: %d | Invalid: %d", len(result["live"]), len(invalid_list))

    return jsonify({
        "success":       True,
        "total_checked": len(valid) + len(invalid_fmt),
        "live_count":    len(result["live"]),
        "invalid_count": len(invalid_list),
        "live_numbers":  result["live"],
        "invalid_numbers": invalid_list,
        "timestamp":     datetime.utcnow().isoformat(),
    })


@app.route("/export-csv", methods=["POST"])
def export_csv():
    data = request.get_json(silent=True) or {}
    rows = data.get("rows", [])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["phone", "status", "name", "username", "reason"])
    for r in rows:
        writer.writerow([
            r.get("phone", ""),
            r.get("status", ""),
            r.get("name", ""),
            r.get("username", ""),
            r.get("reason", ""),
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=validator_results.csv"},
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


# ================= MAIN =================
if __name__ == "__main__":
    print("\n" + "=" * 58)
    print("🚀  Telegram Number Validator")
    print("=" * 58)
    print(f"   API_ID      : {API_ID}")
    print(f"   Session     : {SESSION_NAME}.session")
    print(f"   Batch size  : {BATCH_SIZE}")
    print(f"   Max numbers : {MAX_NUMBERS}")
    print(f"   URL         : http://127.0.0.1:5000")
    print("=" * 58)
    print("   First run will ask for phone + OTP in this terminal.")
    print("=" * 58 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
