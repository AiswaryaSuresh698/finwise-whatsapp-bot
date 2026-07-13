import os
import re
import json
import time
import hashlib
import threading
import requests
import pandas as pd


from io import BytesIO
from datetime import datetime, timedelta
from difflib import get_close_matches

from storage_utils import clean_phone, get_owner_phone_for_uploader

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from PIL import Image
from calendar import monthrange

load_dotenv()

from receipt_ai import get_client, extract_bill_details

from storage_utils import (
    init_db,
    append_entry,
    save_image_to_folder,
    ensure_storage,
    apply_vendor_memory,
    clean_phone,
    get_owner_phone_for_uploader,
    update_vendor_memory,
    engine,
    normalize_date_ddmmyyyy,
    get_entry_by_reference,
    soft_delete_entry,
    restore_deleted_entry,
)

from sqlalchemy import text




load_dotenv()


app = Flask(__name__)
_initialized = False
twilio_client = None
client = None

def lazy_init():
    global _initialized, twilio_client, client

    if _initialized:
        return

    init_db()
    ensure_storage()

    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client = get_client(OPENAI_API_KEY)

    _initialized = True

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


PENDING_CATEGORY_FILE = "data/pending_category.json"



TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+15559208533")


def send_whatsapp_message(to_number, message):
    if not str(to_number).startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_number,
        body=message
    )

def init_pending_category_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pending_category (
                phone TEXT PRIMARY KEY,
                entry_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))


def get_pending_entry(phone):
    init_pending_category_table()

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT entry_json
                FROM pending_category
                WHERE phone = :phone
                LIMIT 1
            """),
            {"phone": str(phone)}
        ).fetchone()

    if not row:
        return None

    try:
        return json.loads(row.entry_json)
    except Exception:
        return None


def save_pending_entry(phone, entry):
    init_pending_category_table()

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO pending_category (phone, entry_json)
                VALUES (:phone, :entry_json)
                ON CONFLICT (phone)
                DO UPDATE SET
                    entry_json = EXCLUDED.entry_json,
                    created_at = CURRENT_TIMESTAMP
            """),
            {
                "phone": str(phone),
                "entry_json": json.dumps(entry)
            }
        )


def clear_pending_category(phone):
    init_pending_category_table()

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM pending_category WHERE phone = :phone"),
            {"phone": str(phone)}
        )
CATEGORY_OPTIONS = [
    "Grocery",
    "Gas",
    "Internet",
    "Utilities",
    "Meals",
    "Rent",
    "Salary",
    "Software",
    "Office Supplies",
    "Vehicle",
    "Professional Fees",
    "Insurance",
    "Travel",
    "Income",
    "Uncategorized",
    "Milk",
    "Chicken",
    "Rice",
    "Frozen Foods",
    "Ice Cream",
    "Cylinder",
    "Marketing",
]

CATEGORY_ALIASES = {
    "groceries": "Grocery",
    "grocery": "Grocery",
    "frozen": "Frozen Foods",
    "icecream": "Ice Cream",
    "ice cream": "Ice Cream",
    "ive cream": "Ice Cream",
    "soap oil": "Utilities",
    "soup oil": "Utilities",
    "cylinder": "Cylinder",
    "gas cylinder": "Cylinder",
}

GREETING_WORDS = {
    "hi", "hii", "hello", "hey", "good morning", "good afternoon",
    "good evening", "gm", "namaste", "vanakkam"
}

THANKS_WORDS = {
    "thanks", "thank you", "thx", "thank u", "ok thanks"
}

HELP_WORDS = {
    "help", "menu", "options", "commands", "what can you do",
    "how to use", "how does this work"
}


def normalize_chat_text(text):
    text = str(text or "").lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def is_greeting_message(text):
    msg = normalize_chat_text(text)
    return msg in GREETING_WORDS or re.fullmatch(r"(hi+|hey+|hello+)", msg)


def is_thanks_message(text):
    msg = normalize_chat_text(text)
    return msg in THANKS_WORDS


def is_help_message(text):
    msg = normalize_chat_text(text)
    return any(word in msg for word in HELP_WORDS)


def get_greeting_reply():
    return (
        "👋 Hello! I'm FinWise.\n\n"
        "I can help you:\n"
        "📸 Save bill photos\n"
        "💬 Add expenses by text\n"
        "📊 Check profit or expenses\n"
        "Examples:\n"
        "• Chicken 850\n"
        "• Costco vegetables 1000\n"
        "• Profit today\n"
        "• Expenses this month\n"
        "How can I help today?"
    )


def get_help_reply():
    return (
        "Here’s what you can send me:\n\n"
        "📸 Bill photo\n"
        "💬 Text expense: Milk 250\n"
        "💬 Text expense: Paid 500 to Walmart for chicken\n"
        "📊 Report: Profit today\n"
        "📊 Report: Expenses this month\n"
        "🔎 Search: Show chicken bills this month\n"
        "✅ To-do: to do pay supplier tomorrow\n\n"
        "You can type naturally."
    )


def get_thanks_reply():
    return "😊 You're welcome! Send a bill, expense, or question anytime."


def get_friendly_unknown_reply():
    return (
        "🤔 I couldn't understand that fully yet.\n\n"
        "You can try:\n"
        "• Chicken 850\n"
        "• Milk 250\n"
        "• Costco vegetables 1000\n"
        "• Profit today\n"
        "• Expenses this month\n"
        "• Help"
    )

def match_category(user_text):
    text = str(user_text or "").lower().strip()

    for alias, category in CATEGORY_ALIASES.items():
        if alias in text:
            return category

    for category in CATEGORY_OPTIONS:
        if category.lower() in text:
            return category

    category_names = [c.lower() for c in CATEGORY_OPTIONS]
    words = re.findall(r"[a-zA-Z]+", text)

    for word in words:
        match = get_close_matches(word.lower(), category_names, n=1, cutoff=0.72)
        if match:
            matched_name = match[0]
            return CATEGORY_OPTIONS[category_names.index(matched_name)]

    return None


def extract_amount(text):
    amounts = re.findall(r"\d+(?:\.\d+)?", str(text or ""))
    if not amounts:
        return None
    return float(amounts[0])


def extract_purchase_date(text):
    value = str(text or "").strip().lower()

    if "today" in value:
        return datetime.now().strftime("%Y-%m-%d")

    if "yesterday" in value:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # YYYY-MM-DD
    iso_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", value)
    if iso_match:
        return iso_match.group(0)

    # DD/MM/YYYY or DD-MM-YYYY
    local_match = re.search(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b",
        value
    )

    if local_match:
        day, month, year = local_match.groups()

        if len(year) == 2:
            year = f"20{year}"

        try:
            parsed = datetime(
                int(year),
                int(month),
                int(day)
            )
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return datetime.now().strftime("%Y-%m-%d")

def parse_comma_text_expense(text):
    parts = [p.strip() for p in str(text or "").split(",")]

    if len(parts) < 3:
        return None

    vendor = parts[0]
    amount = extract_amount(parts[1])
    item = parts[2]
    date_text = parts[3] if len(parts) >= 4 else "today"

    if not item or not vendor or amount is None:
        return None

    return {
        "item": item.title(),
        "vendor": vendor.title(),
        "amount": amount,
        "date": extract_purchase_date(date_text),
        "category": match_category(item) or "Uncategorized"
    }

def parse_structured_text_expense(text):
    """
    Supports messages such as:

    Vendor: Uma Chicken
    Category: Chicken
    Amount: 850
    Date: 05/07/2026

    Also supports:
    Vendor-Uma Chicken
    Category-Chicken
    850
    Date-05/07/2026
    """

    raw_text = str(text or "").strip()

    if not raw_text:
        return None

    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    vendor = ""
    category = ""
    amount = None
    date_value = ""

    for line in lines:
        vendor_match = re.match(
            r"^\s*vendor\s*[:\-–—]\s*(.+?)\s*$",
            line,
            flags=re.I
        )
        if vendor_match:
            vendor = vendor_match.group(1).strip()
            continue

        category_match = re.match(
            r"^\s*category\s*[:\-–—]\s*(.+?)\s*$",
            line,
            flags=re.I
        )
        if category_match:
            category = category_match.group(1).strip()
            continue

        amount_match = re.match(
            r"^\s*(?:amount|total)\s*[:\-–—]\s*"
            r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)\s*$",
            line,
            flags=re.I
        )
        if amount_match:
            amount = float(amount_match.group(1).replace(",", ""))
            continue

        date_match = re.match(
            r"^\s*date\s*[:\-–—]\s*(.+?)\s*$",
            line,
            flags=re.I
        )
        if date_match:
            date_value = date_match.group(1).strip()
            continue

        # Allow an amount-only line such as "90"
        if amount is None and is_amount_only_message(line):
            amount = extract_amount(line)

    # A complete structured expense requires vendor and amount.
    if not vendor or amount is None or amount <= 0:
        return None

    matched_category = (
        match_category(category)
        or match_category(vendor)
        or category
        or "Uncategorized"
    )

    return {
        "intent": "expense_entry",
        "vendor": vendor.title(),
        "description": matched_category,
        "category": matched_category,
        "amount": float(amount),
        "date": extract_purchase_date(date_value or "today"),
        "currency": "INR",
    }

def parse_free_text_expense_with_ai(user_text):
    """
    Extract expense details from any natural WhatsApp text.
    Examples:
    - "paid 500 to walmart for chicken yesterday"
    - "today milk 1200 from devapaul"
    - "bought rice from metro for 800"
    """

    prompt = f"""
Extract expense details from this WhatsApp message.

Message:
{user_text}

Return ONLY valid JSON with this structure:
{{
  "is_expense": true/false,
  "date": "YYYY-MM-DD",
  "vendor": "vendor name or Manual Entry",
  "description": "short description",
  "category": "one of: Grocery, Gas, Internet, Utilities, Meals, Rent, Salary, Software, Office Supplies, Vehicle, Professional Fees, Insurance, Travel, Income, Milk, Chicken, Rice, Frozen Foods, Ice Cream, Cylinder, Marketing, Uncategorized",
  "amount": number,
  "currency": "INR"
}}

Rules:
- If amount is missing, set is_expense=false.
- If date is missing, use today's date: {datetime.now().strftime("%Y-%m-%d")}.
- If vendor is unclear, use "Manual Entry".
- If category is unclear, use "Uncategorized".
- Do not include markdown.
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You extract structured expense data from informal WhatsApp messages."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0
        )

        content = result.choices[0].message.content.strip()
        data = json.loads(content)

        if not data.get("is_expense"):
            return None

        amount = data.get("amount")
        if amount is None:
            return None

        return {
            "item": str(data.get("description", "Manual expense")).strip(),
            "vendor": str(data.get("vendor", "Manual Entry")).strip().title(),
            "amount": float(amount),
            "date": str(data.get("date", datetime.now().strftime("%Y-%m-%d"))),
            "category": str(data.get("category", "Uncategorized")).strip()
        }

    except Exception as e:
        print("AI TEXT PARSE ERROR:", str(e), flush=True)
        return None


def extract_vendor(text, amount, category):
    vendor = str(text or "")

    if amount is not None:
        vendor = vendor.replace(str(int(amount)), "")
        vendor = vendor.replace(str(amount), "")

    if category:
        vendor = re.sub(category, "", vendor, flags=re.IGNORECASE)

    for word in ["today", "yesterday", "paid", "purchase", "bought", "for"]:
        vendor = re.sub(rf"\b{word}\b", "", vendor, flags=re.IGNORECASE)

    vendor = vendor.strip(" ,-")

    return vendor if vendor else "Manual Entry"

@app.route("/", methods=["GET"])
def home():
    return "FinWise WhatsApp bot is running.", 200

def make_expense_duplicate_key(from_number, extracted):
    vendor = str(extracted.get("vendor", "")).strip().lower()
    date = str(extracted.get("date", "")).strip()
    total = str(round(float(extracted.get("total", 0) or 0), 2))
    description = str(extracted.get("description", "")).strip().lower()

    raw_key = f"{from_number}|{date}|{vendor}|{total}|{description}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def is_duplicate_expense(duplicate_key):
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT id
                FROM entries
                WHERE duplicate_key = :duplicate_key
                  AND LOWER(TRIM(COALESCE(is_deleted, 'no')))
                    NOT IN ('yes', 'true', '1')
                LIMIT 1
            """),
            {
                "duplicate_key": str(duplicate_key)
            }
        ).fetchone()

    return row is not None

def append_entry_and_get_id(entry):
    """
    Save one entry and return the exact database ID
    assigned to that entry.
    """

    append_entry(entry)

    duplicate_key = str(
        entry.get("duplicate_key", "")
    ).strip()

    if not duplicate_key:
        return None

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT id
                FROM entries
                WHERE duplicate_key = :duplicate_key
                AND LOWER(TRIM(COALESCE(is_deleted, 'no')))
                    NOT IN ('yes', 'true', '1')
                ORDER BY id DESC
                LIMIT 1
            """),
            {
                "duplicate_key": duplicate_key
            }
        ).fetchone()

    return str(row.id) if row else None

def save_text_expense(intent_data, owner_phone, uploader_phone):
    description = intent_data.get("description") or "WhatsApp text expense"
    category = intent_data.get("category") or "Uncategorized"

    matched_category = match_category(description)

    if category == "Uncategorized" and matched_category:
        category = matched_category

    vendor = intent_data.get("vendor") or "Manual Entry"

    if vendor == "Manual Entry":
        vendor = description

    
    # If vendor contains amount, clean it
    vendor = re.sub(r"\d+(?:\.\d+)?", "", vendor).strip(" -,")

    if not vendor:
        vendor = "Manual Entry"

    amount = float(intent_data.get("amount") or 0)
    if amount <= 0:
        extracted_amount = extract_amount(description)
        if extracted_amount:
            amount = extracted_amount
    date_value = normalize_date_ddmmyyyy(
        intent_data.get("date") or datetime.now().strftime("%Y-%m-%d")
    )

    memory_category, memory_folder = apply_vendor_memory(owner_phone, vendor)

    if memory_category:
        category = memory_category

    folder = memory_folder if memory_folder else category

    entry = {
        "date": date_value,
        "transaction_type": "expense",
        "vendor": vendor.title(),
        "user_phone": owner_phone,
        "uploaded_by": uploader_phone,
        "description": description,
        "category": category,
        "folder": folder,
        "subtotal": amount,
        "tax": 0,
        "total": amount,
        "currency": intent_data.get("currency", "INR"),
        "confidence": "ai_text",
        "reason": "Natural WhatsApp text entry",
        "image_path": "",
        "source": "WhatsApp Text",
    }

    entry["duplicate_key"] = make_expense_duplicate_key(owner_phone, entry)

    if is_duplicate_expense(entry["duplicate_key"]):
        return False, "This text expense was already uploaded earlier."

    

    saved_entry_id = append_entry_and_get_id(entry)

    if saved_entry_id:
        try:
            save_conversation_context(
                uploader_phone,
                {
                    "state": "last_expense_saved",
                    "entry_id": saved_entry_id,
                    "vendor": entry.get("vendor", ""),
                    "amount": str(entry.get("total", "")),
                    "date": entry.get("date", ""),
                    "category": entry.get("category", ""),
                }
            )

        except Exception as e:
            print(
                "CONVERSATION CONTEXT SAVE ERROR:",
                str(e),
                flush=True
            )

    try:
        update_vendor_memory(
            user_phone=owner_phone,
            vendor=vendor,
            category=category,
            folder=folder,
        )
    except Exception as e:
        print(
            "VENDOR MEMORY UPDATE ERROR:",
            str(e),
            flush=True
        )

    message_lines = [
        "✅ Expense saved",
        "",
    ]

    if saved_entry_id:
        message_lines.append(f"Reference: EXP-{saved_entry_id}")

    message_lines.extend([
        f"Vendor: {vendor.title()}",
        f"Amount: ₹{amount:,.2f}",
        f"Date: {date_value}",
        f"Category: {category}",
    ])

    if saved_entry_id:
        message_lines.extend([
            "",
            "To delete this record later, send:",
            f"Delete EXP-{saved_entry_id}",
        ])

    return True, "\n".join(message_lines)

def process_bill_in_background(raw_from, owner_phone, uploader_phone, media_url, media_type):
    lazy_init()
    request_start = time.time()

    try:
        print("Downloading image...", flush=True)

        download_start = time.time()

        media_response = requests.get(
            media_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30,
        )

        print(f"IMAGE DOWNLOAD: {round(time.time() - download_start, 2)} sec", flush=True)

        media_response.raise_for_status()

        image_load_start = time.time()

        image_bytes = media_response.content
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((1200, 1200))

        print(f"IMAGE LOAD: {round(time.time() - image_load_start, 2)} sec", flush=True)

        print("Calling OpenAI extraction...", flush=True)

        ai_start = time.time()
        extracted = extract_bill_details(client, image)

        print(f"OPENAI EXTRACTION: {round(time.time() - ai_start, 2)} sec", flush=True)
        print("Extracted:", extracted, flush=True)

        if extracted.get("image_quality") in ["blurry", "unreadable"]:
            send_whatsapp_message(
                raw_from,
                "This image is not clear enough to read. Please upload a clearer bill photo with good lighting."
            )
            return

        duplicate_key = make_expense_duplicate_key(owner_phone, extracted)

        if is_duplicate_expense(duplicate_key):
            send_whatsapp_message(raw_from, "This bill was already uploaded earlier.")
            return

        vendor = extracted.get("vendor", "")
        category = extracted.get("category", "Uncategorized")
        folder = extracted.get("folder", "Uncategorized")

        memory_category, memory_folder = apply_vendor_memory(owner_phone, vendor)
        has_vendor_memory = bool(memory_category or memory_folder)

        if memory_category:
            category = memory_category

        if memory_folder:
            folder = memory_folder

        ext = "png" if "png" in media_type else "jpg"

        image_path = save_image_to_folder(
            image_bytes=image_bytes,
            folder=folder,
            vendor=vendor or "unknown",
            ext=ext,
            bill_date=extracted.get("date", "")
        )

        entry = {
            "date": normalize_date_ddmmyyyy(extracted.get("date", "")),
            "transaction_type": "expense",
            "vendor": vendor,
            "user_phone": owner_phone,
            "uploaded_by": uploader_phone,
            "description": extracted.get("description", ""),
            "category": category,
            "folder": folder,
            "subtotal": extracted.get("subtotal", 0),
            "tax": extracted.get("tax", 0),
            "total": extracted.get("total", 0),
            "currency": extracted.get("currency", "INR"),
            "confidence": extracted.get("confidence", "medium"),
            "reason": extracted.get("reason", ""),
            "image_path": image_path,
            "duplicate_key": duplicate_key,
        }

        if not has_vendor_memory:
            save_pending_entry(uploader_phone, entry)

            send_whatsapp_message(
                raw_from,
                f"🧾 I found a new vendor.\n\n"
                f"Vendor: {entry['vendor']}\n"
                f"Total: ₹{safe_float(entry['total']):,.2f}\n\n"
                f"Which category should I save it under?\n\n"
                f"Examples:\n"
                f"• Grocery\n"
                f"• Chicken\n"
                f"• Milk\n"
                f"• Rice\n"
                f"• Meals\n"
                f"• Utilities"
            )
            return

        save_start = time.time()

        saved_entry_id = append_entry_and_get_id(entry)

        print(
            f"SAVE ENTRY: {round(time.time() - save_start, 2)} sec",
            flush=True
        )

        print(f"FULL REQUEST TIME: {round(time.time() - request_start, 2)} sec", flush=True)

        message_lines = [
            "✅ Bill saved",
            "",
        ]

        if saved_entry_id:
            message_lines.append(
                f"Reference: EXP-{saved_entry_id}"
            )

        message_lines.extend([
            f"Vendor: {entry['vendor']}",
            f"Total: ₹{safe_float(entry['total']):,.2f}",
            f"Category: {entry['category']}",
        ])

        if saved_entry_id:
            message_lines.extend([
                "",
                "To delete this record later, send:",
                f"Delete EXP-{saved_entry_id}",
            ])

        send_whatsapp_message(
            raw_from,
            "\n".join(message_lines)
        )

    except Exception as e:
        print("ERROR PROCESSING BILL:", str(e), flush=True)
        send_whatsapp_message(raw_from, f"Could not process the bill. Error: {str(e)}")

def classify_whatsapp_text(user_text):
    prompt = f"""
Classify this WhatsApp message for a finance assistant.

Message:
{user_text}

Return ONLY valid JSON:
{{
  "intent": "expense_entry" | "category_reply" | "finance_question" | "unknown",
  "category": "",
  "date": "YYYY-MM-DD",
  "vendor": "",
  "description": "",
  "amount": null,
  "currency": "INR"
}}

Rules:
- If user is recording spending, use expense_entry.
- If user only gives category like grocery/chicken/meals, use category_reply.
- If user asks "how much", "show", "total", "spent", use finance_question.
- If date missing for expense, use today: {datetime.now().strftime("%Y-%m-%d")}.
- If category unclear, use "Uncategorized".
- If vendor unclear, use "Manual Entry".
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You classify WhatsApp finance assistant messages."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )

        return json.loads(result.choices[0].message.content.strip())

    except Exception as e:
        print("TEXT CLASSIFY ERROR:", str(e), flush=True)
        return {"intent": "unknown"}
    
def init_finance_context_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS finance_context (
                phone TEXT PRIMARY KEY,
                context_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))


def get_finance_context(phone):
    init_finance_context_table()

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT context_json
                FROM finance_context
                WHERE phone = :phone
                LIMIT 1
            """),
            {"phone": str(phone)}
        ).fetchone()

    if not row:
        return {}

    try:
        return json.loads(row.context_json)
    except Exception:
        return {}


def save_finance_context(phone, context):
    init_finance_context_table()

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO finance_context (phone, context_json)
                VALUES (:phone, :context_json)
                ON CONFLICT (phone)
                DO UPDATE SET
                    context_json = EXCLUDED.context_json,
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "phone": str(phone),
                "context_json": json.dumps(context)
            }
        )
    
def extract_finance_question_intent(user_text, owner_phone, previous_context=None):
    today = datetime.now().date()

    prompt = f"""
You convert finance questions into safe structured JSON.

Today is {today}.

User question:
{user_text}

Previous finance context:
{json.dumps(previous_context or {}, default=str)}

Return ONLY valid JSON:
{{
  "intent": "total_expense" | "category_expense" | "vendor_expense" | "top_vendors" | "top_categories" | "income_total" | "net_total" | "recent_expenses" | "search_transactions" | "unknown",
  "category": "",
  "vendor": "",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "limit": 5,
  "confidence": 0.0,
  "needs_clarification": false,
  "clarification_question": ""
}}

Rules:
- Never calculate money.
- Never invent totals.
- Correct spelling mistakes silently.
- If user gives a follow-up like "not this month", "entirely", "overall", "all time", reuse previous intent/category/vendor.
- If user says "all time", "entirely", "overall", "from beginning", use start_date="1900-01-01" and end_date=today.
- If user gives any time phrase, follow that phrase.
- Only default to this month if no time period is mentioned at all.
- If user says "May month", use full May of the current year.
- If user says "this month", use first day of current month to today.
- If user says "last month", use full previous month.
- If question is too unclear, set needs_clarification=true.
- If user asks "do I have", "is there any bill", "any vendor bill called", "show bills", use intent="search_transactions".
- If user says "uncategorized bill", use intent="search_transactions" and category="Uncategorized".
- If user says "how about X", reuse previous intent/date range and set vendor or category to X.
- For known category names like Grocery, Chicken, Gas, Milk, Rice, use category.
- For business/vendor names like Devapaul, Durain Vegetables, Walmart, Costco, use vendor.
- If user asks profit, net, balance, income minus expenses, use intent="net_total".
- If user asks income, sales, revenue, Petpooja sales, use intent="income_total".
- If user asks "what is my profit", still use intent="net_total".
"""

    try:
        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You only extract structured finance query intent. You never answer with money."
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )

        return json.loads(result.choices[0].message.content.strip())

    except Exception as e:
        print("FINANCE INTENT ERROR:", str(e), flush=True)
        return {
            "intent": "unknown",
            "needs_clarification": True,
            "clarification_question": "Can you ask that another way? For example: How much did I spend on chicken this month?"
        }

def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0

def get_petpooja_sales_total(conn, owner_phone, start_date, end_date):
    cols = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'petpooja_entries'
    """)).fetchall()

    col_names = [c.column_name for c in cols]

    amount_candidates = [
        "total", "Total", "amount", "Amount", "sales", "Sales",
        "net_sales", "Net Sales", "grand_total", "Grand Total"
    ]

    date_candidates = [
        "date", "Date", "business_date", "Business Date",
        "sale_date", "Sale Date"
    ]

    amount_col = next((c for c in amount_candidates if c in col_names), None)
    date_col = next((c for c in date_candidates if c in col_names), None)

    if not amount_col or not date_col:
        print("PETPOOJA COLUMN MISSING:", col_names, flush=True)
        return 0.0

    row = conn.execute(
        text(f"""
            SELECT COALESCE(SUM("{amount_col}"::numeric), 0) AS total
            FROM petpooja_entries
            WHERE user_phone = :owner_phone
            AND "{date_col}"::date BETWEEN :start_date AND :end_date
        """),
        {
            "owner_phone": str(owner_phone),
            "start_date": start_date,
            "end_date": end_date,
        }
    ).fetchone()

    return safe_float(row.total)


def query_finance_answer(intent_data, owner_phone):
    intent = intent_data.get("intent", "unknown")
    category = str(intent_data.get("category") or "").strip()
    vendor = str(intent_data.get("vendor") or "").strip()
    start_date = intent_data.get("start_date")
    end_date = intent_data.get("end_date")
    limit = int(intent_data.get("limit") or 5)

    if intent_data.get("needs_clarification"):
        return {
            "type": "clarification",
            "message": intent_data.get("clarification_question") or "Can you clarify your question?"
        }

    if not start_date or not end_date:
        return {
            "type": "clarification",
            "message": "Which period should I check? Today, this week, this month, or a custom date range?"
        }

    base_filter = """
        user_phone = :owner_phone

        AND LOWER(TRIM(COALESCE(is_deleted, 'no')))
            NOT IN ('yes', 'true', '1')

        AND date::date BETWEEN :start_date AND :end_date
    """

    params = {
        "owner_phone": str(owner_phone),
        "start_date": start_date,
        "end_date": end_date,
        "limit": limit,
    }

    with engine.begin() as conn:

        if intent in ["total_expense", "category_expense"]:
            search_filter = ""

            if intent == "category_expense" and category:
                params["search"] = f"%{category}%"
                search_filter = """
                    AND (
                        category ILIKE :search
                        OR vendor ILIKE :search
                        OR description ILIKE :search
                    )
                """

            row = conn.execute(
                text(f"""
                    SELECT
                        COUNT(*) AS count,
                        COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                    {search_filter}
                """),
                params
            ).fetchone()

            return {
                "type": "amount",
                "intent": intent,
                "category": category,
                "vendor": vendor,
                "start_date": start_date,
                "end_date": end_date,
                "count": int(row.count or 0),
                "amount": safe_float(row.total),
                "label": category if category else "expenses"
            }

        if intent == "vendor_expense":
            if not vendor:
                return {"type": "clarification", "message": "Which vendor should I check?"}

            params["search"] = f"%{vendor}%"

            row = conn.execute(
                text(f"""
                    SELECT
                        COUNT(*) AS count,
                        COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                    AND vendor ILIKE :search
                """),
                params
            ).fetchone()

            return {
                "type": "amount",
                "intent": intent,
                "vendor": vendor,
                "start_date": start_date,
                "end_date": end_date,
                "count": int(row.count or 0),
                "amount": safe_float(row.total),
                "label": vendor
            }

        if intent == "top_vendors":
            rows = conn.execute(
                text(f"""
                    SELECT
                        vendor,
                        COUNT(*) AS count,
                        COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                    GROUP BY vendor
                    ORDER BY total DESC
                    LIMIT :limit
                """),
                params
            ).fetchall()

            return {
                "type": "ranking",
                "title": "Top vendors",
                "start_date": start_date,
                "end_date": end_date,
                "rows": [
                    {
                        "name": r.vendor or "Unknown Vendor",
                        "count": int(r.count or 0),
                        "amount": safe_float(r.total)
                    }
                    for r in rows
                ]
            }

        if intent == "top_categories":
            rows = conn.execute(
                text(f"""
                    SELECT
                        category,
                        COUNT(*) AS count,
                        COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                    GROUP BY category
                    ORDER BY total DESC
                    LIMIT :limit
                """),
                params
            ).fetchall()

            return {
                "type": "ranking",
                "title": "Top categories",
                "start_date": start_date,
                "end_date": end_date,
                "rows": [
                    {
                        "name": r.category or "Uncategorized",
                        "count": int(r.count or 0),
                        "amount": safe_float(r.total)
                    }
                    for r in rows
                ]
            }

        if intent == "income_total":
            entry_income = conn.execute(
                text(f"""
                    SELECT COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, '')) = 'income'
                """),
                params
            ).fetchone()

            petpooja_income = get_petpooja_sales_total(
                conn, owner_phone, start_date, end_date
            )

            total_income = safe_float(entry_income.total) + petpooja_income

            return {
                "type": "amount",
                "intent": intent,
                "label": "income / sales",
                "start_date": start_date,
                "end_date": end_date,
                "count": None,
                "amount": total_income
            }

        if intent == "net_total":
            expense = conn.execute(
                text(f"""
                    SELECT COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                """),
                params
            ).fetchone()

            entry_income = conn.execute(
                text(f"""
                    SELECT COALESCE(SUM(total::numeric), 0) AS total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, '')) = 'income'
                """),
                params
            ).fetchone()

            petpooja_income = get_petpooja_sales_total(
                conn, owner_phone, start_date, end_date
            )

            total_income = safe_float(entry_income.total) + petpooja_income
            total_expense = safe_float(expense.total)

            return {
                "type": "net",
                "start_date": start_date,
                "end_date": end_date,
                "income": total_income,
                "expense": total_expense,
                "net": total_income - total_expense
            }

        if intent == "recent_expenses":
            rows = conn.execute(
                text(f"""
                    SELECT date, vendor, category, description, total
                    FROM entries
                    WHERE {base_filter}
                    AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                    ORDER BY date::date DESC, id DESC
                    LIMIT :limit
                """),
                params
            ).fetchall()

            return {
                "type": "recent",
                "start_date": start_date,
                "end_date": end_date,
                "rows": [
                    {
                        "date": str(r.date),
                        "vendor": r.vendor or "Unknown Vendor",
                        "category": r.category or "Uncategorized",
                        "description": r.description or "",
                        "amount": safe_float(r.total)
                    }
                    for r in rows
                ]
            }
        if intent == "search_transactions":
                search_conditions = []
                
                if category:
                    params["category_search"] = f"%{category}%"
                    search_conditions.append("""
                        (
                            category ILIKE :category_search
                            OR description ILIKE :category_search
                        )
                    """)

                if vendor:
                    params["vendor_search"] = f"%{vendor}%"
                    search_conditions.append("""
                        (
                            vendor ILIKE :vendor_search
                            OR description ILIKE :vendor_search
                        )
                    """)

                if not search_conditions:
                    return {
                        "type": "clarification",
                        "message": "What bill/vendor/category should I search for?"
                    }

                search_filter = " AND (" + " OR ".join(search_conditions) + ")"

                rows = conn.execute(
                    text(f"""
                        SELECT date, vendor, category, description, total
                        FROM entries
                        WHERE {base_filter}
                        AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                        {search_filter}
                        ORDER BY date::date DESC, id DESC
                        LIMIT :limit
                    """),
                    params
                ).fetchall()

                total_row = conn.execute(
                    text(f"""
                        SELECT
                            COUNT(*) AS count,
                            COALESCE(SUM(total::numeric), 0) AS total
                        FROM entries
                        WHERE {base_filter}
                        AND LOWER(COALESCE(transaction_type, 'expense')) = 'expense'
                        {search_filter}
                    """),
                    params
                ).fetchone()

                return {
                    "type": "search",
                    "category": category,
                    "vendor": vendor,
                    "start_date": start_date,
                    "end_date": end_date,
                    "count": int(total_row.count or 0),
                    "amount": safe_float(total_row.total),
                    "rows": [
                        {
                            "date": str(r.date),
                            "vendor": r.vendor or "Unknown Vendor",
                            "category": r.category or "Uncategorized",
                            "description": r.description or "",
                            "amount": safe_float(r.total)
                        }
                        for r in rows
                    ]
                }

    return {
        "type": "clarification",
        "message": "I can answer spending, income, vendor, category, net, and recent expense questions. Can you ask it another way?"
    }


def format_finance_answer(user_question, result):
    if result["type"] == "clarification":
        return result["message"]

    if result["type"] == "amount":
        if result.get("count") == 0:
            return f"I couldn't find any {result.get('label', 'records')} from {result['start_date']} to {result['end_date']}."

        if result.get("count") and result.get("amount") == 0:
            return (
                f"I found {result.get('count')} {result.get('label', 'record(s)')} "
                f"from {result['start_date']} to {result['end_date']}, "
                f"but the saved amount is ₹0.00. Please check if the bill amount was captured correctly."
            )

        return (
            f"You spent ₹{result['amount']:,.2f} on {result.get('label', 'expenses')} "
            f"from {result['start_date']} to {result['end_date']}."
            if result.get("intent") != "income_total"
            else
            f"Your income/sales total is ₹{result['amount']:,.2f} "
            f"from {result['start_date']} to {result['end_date']}."
        )
        
        
    if result["type"] == "ranking":
        rows = result.get("rows", [])

        if not rows:
            return f"I couldn't find records for {result['start_date']} to {result['end_date']}."

        lines = [
            f"{result['title']} from {result['start_date']} to {result['end_date']}:"
        ]

        for i, row in enumerate(rows, start=1):
            lines.append(f"{i}. {row['name']} - ₹{row['amount']:,.2f} ({row['count']} bills)")

        return "\n".join(lines)

    if result["type"] == "net":
        return (
            f"Based on uploaded records from {result['start_date']} to {result['end_date']}:\n\n"
            f"Income/Sales: ₹{result['income']:,.2f}\n"
            f"Expenses: ₹{result['expense']:,.2f}\n"
            f"Net based on uploaded records: ₹{result['net']:,.2f}"
        )

    if result["type"] == "recent":
        rows = result.get("rows", [])

        if not rows:
            return f"I couldn't find recent expenses for {result['start_date']} to {result['end_date']}."

        lines = [f"Recent expenses from {result['start_date']} to {result['end_date']}:"]
        for row in rows:
            lines.append(
                f"- {row['date']} | {row['vendor']} | {row['category']} | ₹{row['amount']:,.2f}"
            )

        return "\n".join(lines)
    
    if result["type"] == "search":
            rows = result.get("rows", [])
            label = result.get("vendor") or result.get("category") or "matching bills"

            if not rows:
                return f"I couldn't find any {label} from {result['start_date']} to {result['end_date']}."

            lines = [
                f"I found {result['count']} {label} bill(s) from {result['start_date']} to {result['end_date']}.",
                f"Total: ₹{result['amount']:,.2f}",
                "",
                "Latest records:"
            ]

            for row in rows[:5]:
                lines.append(
                    f"- {row['date']} | {row['vendor']} | {row['category']} | ₹{row['amount']:,.2f}"
                )

            return "\n".join(lines)

    return "I couldn't answer that yet. Try asking: How much did I spend this month?"

import re
from datetime import datetime




def init_conversation_context_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS conversation_context (
                phone TEXT PRIMARY KEY,
                context_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))


def save_conversation_context(phone, context):
    init_conversation_context_table()

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO conversation_context (phone, context_json)
                VALUES (:phone, :context_json)
                ON CONFLICT (phone)
                DO UPDATE SET
                    context_json = EXCLUDED.context_json,
                    updated_at = CURRENT_TIMESTAMP
            """),
            {
                "phone": str(phone),
                "context_json": json.dumps(context)
            }
        )


def get_conversation_context(phone, max_age_minutes=10):
    init_conversation_context_table()

    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT context_json, updated_at
                FROM conversation_context
                WHERE phone = :phone
                LIMIT 1
            """),
            {"phone": str(phone)}
        ).fetchone()

    if not row:
        return {}

    try:
        age = datetime.now() - row.updated_at
        if age.total_seconds() > max_age_minutes * 60:
            clear_conversation_context(phone)
            return {}

        return json.loads(row.context_json)
    except Exception:
        return {}


def clear_conversation_context(phone):
    init_conversation_context_table()

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM conversation_context WHERE phone = :phone"),
            {"phone": str(phone)}
        )


def get_last_saved_expense(owner_phone, uploader_phone):
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT *
                FROM entries
                WHERE user_phone = :owner_phone
                  AND uploaded_by = :uploader_phone
                  AND LOWER(
                        COALESCE(
                            transaction_type,
                            'expense'
                        )
                      ) = 'expense'
                  AND LOWER(
                        TRIM(
                            COALESCE(
                                is_deleted,
                                'no'
                            )
                        )
                      ) NOT IN (
                        'yes',
                        'true',
                        '1'
                      )
                ORDER BY id DESC
                LIMIT 1
            """),
            {
                "owner_phone": str(owner_phone),
                "uploader_phone": str(uploader_phone),
            }
        ).mappings().fetchone()

    return dict(row) if row else None


def update_last_expense_field(
    entry_id,
    field_name,
    value
):
    allowed_fields = {
        "date",
        "vendor",
        "description",
        "category",
        "folder",
        "total",
        "subtotal",
    }

    if field_name not in allowed_fields:
        return 0

    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                UPDATE entries
                SET {field_name} = :value
                WHERE id = :entry_id
                  AND LOWER(
                        TRIM(
                            COALESCE(
                                is_deleted,
                                'no'
                            )
                        )
                      ) NOT IN (
                        'yes',
                        'true',
                        '1'
                      )
            """),
            {
                "entry_id": str(entry_id),
                "value": str(value),
            }
        )

    return result.rowcount


def is_amount_only_message(message):
    msg = str(message or "").strip()
    return bool(re.fullmatch(r"(₹|rs\.?|inr)?\s*\d+(?:,\d+)*(?:\.\d+)?", msg, flags=re.I))


def is_date_only_message(message):
    msg = str(message or "").strip().lower()

    if msg in ["today", "yesterday"]:
        return True

    return bool(re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}", msg))


def is_edit_message(message):
    msg = str(message or "").lower().strip()

    return any(x in msg for x in [
        "actually",
        "wrong",
        "change",
        "update",
        "edit",
        "correct",
        "mistake"
    ])


def handle_conversation_context(
    raw_from,
    owner_phone,
    uploader_phone,
    incoming_msg
):
    msg = str(incoming_msg or "").strip()
    context = get_conversation_context(uploader_phone)

    # Let the delete-confirmation function handle YES or NO.
    if context.get("state") == "confirm_delete_entry":
        return False

    # Handle corrections to the most recently saved expense.
    if context.get("state") == "last_expense_saved":
        entry_id = context.get("entry_id")

        # Example: "Yesterday" or "05/07/2026"
        if is_date_only_message(msg) and entry_id:
            new_date = normalize_date_ddmmyyyy(
                extract_purchase_date(msg)
            )

            update_last_expense_field(
                entry_id,
                "date",
                new_date
            )

            context["date"] = new_date

            save_conversation_context(
                uploader_phone,
                context
            )

            send_whatsapp_message(
                raw_from,
                f"✅ Updated the last expense date to {new_date}."
            )
            return True

        # Example: "Actually amount is 950"
        if is_edit_message(msg) and entry_id:
            new_amount = extract_amount(msg)

            if new_amount:
                update_last_expense_field(
                    entry_id,
                    "total",
                    new_amount
                )

                update_last_expense_field(
                    entry_id,
                    "subtotal",
                    new_amount
                )

                context["amount"] = new_amount

                save_conversation_context(
                    uploader_phone,
                    context
                )

                send_whatsapp_message(
                    raw_from,
                    (
                        "✅ Updated the last expense "
                        f"amount to ₹{new_amount:,.2f}."
                    )
                )
                return True

    # Clear old incomplete conversation states.
    if context.get("state") in {
        "awaiting_amount",
        "awaiting_item_for_amount",
    }:
        clear_conversation_context(uploader_phone)

    return False

def parse_expense_reference(message):
    """
    Accepts:
    Delete EXP-123
    delete exp 123
    Restore EXP-123
    """
    match = re.search(
        r"\bEXP[\s\-_:]*([0-9]+)\b",
        str(message or ""),
        flags=re.I
    )

    if not match:
        return None

    return match.group(1)


def is_delete_request(message):
    normalized = normalize_chat_text(message)

    return (
        normalized.startswith("delete ")
        or normalized.startswith("remove ")
        or normalized.startswith("delete expense ")
        or normalized.startswith("remove expense ")
    )


def is_restore_request(message):
    normalized = normalize_chat_text(message)

    return (
        normalized.startswith("restore ")
        or normalized.startswith("recover ")
    )


def is_yes_message(message):
    normalized = normalize_chat_text(message)

    return normalized in {
        "yes",
        "y",
        "confirm",
        "yes delete",
        "delete it",
        "confirm delete",
    }


def is_no_message(message):
    normalized = normalize_chat_text(message)

    return normalized in {
        "no",
        "n",
        "cancel",
        "dont delete",
        "do not delete",
    }

def handle_whatsapp_delete_flow(
    raw_from,
    owner_phone,
    uploader_phone,
    incoming_msg,
):
    context = get_conversation_context(uploader_phone)

    # User is confirming a previously requested deletion.
    if context.get("state") == "confirm_delete_entry":
        entry_id = str(context.get("entry_id", "")).strip()

        if is_yes_message(incoming_msg):
            deleted = soft_delete_entry(
                entry_id=entry_id,
                owner_phone=owner_phone,
                deleted_by=uploader_phone,
                delete_source="whatsapp",
            )

            clear_conversation_context(uploader_phone)

            if deleted:
                send_whatsapp_message(
                    raw_from,
                    (
                        f"✅ EXP-{entry_id} moved to "
                        f"Recently Deleted.\n\n"
                        f"It can be restored from the "
                        f"FinWise dashboard for 30 days."
                    )
                )
            else:
                send_whatsapp_message(
                    raw_from,
                    (
                        "I could not delete that record. "
                        "It may already be deleted or may "
                        "belong to another account."
                    )
                )

            return True

        if is_no_message(incoming_msg):
            clear_conversation_context(uploader_phone)

            send_whatsapp_message(
                raw_from,
                "Deletion cancelled."
            )
            return True

        send_whatsapp_message(
            raw_from,
            (
                f"Reply YES to move EXP-{entry_id} "
                f"to Recently Deleted, or NO to cancel."
            )
        )
        return True

    # New delete request.
    if is_delete_request(incoming_msg):
        entry_id = parse_expense_reference(incoming_msg)

        if not entry_id:
            send_whatsapp_message(
                raw_from,
                (
                    "Please include the expense reference.\n\n"
                    "Example:\n"
                    "Delete EXP-10452"
                )
            )
            return True

        entry = get_entry_by_reference(
            entry_id=entry_id,
            owner_phone=owner_phone,
            include_deleted=False,
        )

        if not entry:
            send_whatsapp_message(
                raw_from,
                (
                    f"I couldn't find active record "
                    f"EXP-{entry_id} in this account."
                )
            )
            return True

        save_conversation_context(
            uploader_phone,
            {
                "state": "confirm_delete_entry",
                "entry_id": str(entry_id),
                "vendor": entry.get("vendor", ""),
                "description": entry.get("description", ""),
                "category": entry.get("category", ""),
                "amount": entry.get("total", 0),
                "date": entry.get("date", ""),
            }
        )

        send_whatsapp_message(
            raw_from,
            (
                "⚠️ Confirm deletion\n\n"
                f"Reference: EXP-{entry_id}\n"
                f"Vendor: {entry.get('vendor', '')}\n"
                f"Category: {entry.get('category', '')}\n"
                f"Amount: ₹{safe_float(entry.get('total')):,.2f}\n"
                f"Date: {entry.get('date', '')}\n\n"
                "Reply YES to move it to Recently Deleted.\n"
                "Reply NO to cancel."
            )
        )
        return True

    # Optional restore through WhatsApp.
    if is_restore_request(incoming_msg):
        entry_id = parse_expense_reference(incoming_msg)

        if not entry_id:
            send_whatsapp_message(
                raw_from,
                "Example: Restore EXP-10452"
            )
            return True

        restored = restore_deleted_entry(
            entry_id=entry_id,
            owner_phone=owner_phone,
        )

        if restored:
            send_whatsapp_message(
                raw_from,
                f"♻️ EXP-{entry_id} restored successfully."
            )
        else:
            send_whatsapp_message(
                raw_from,
                (
                    f"I couldn't restore EXP-{entry_id}. "
                    "It may not be in Recently Deleted."
                )
            )

        return True

    return False
    
def process_text_in_background(raw_from, owner_phone, uploader_phone, incoming_msg):
    lazy_init()

    try:
        incoming_msg = str(incoming_msg or "").strip()

        # Friendly messages should work anytime
        if is_greeting_message(incoming_msg):
            send_whatsapp_message(raw_from, get_greeting_reply())
            return

        if is_help_message(incoming_msg):
            send_whatsapp_message(raw_from, get_help_reply())
            return

        if is_thanks_message(incoming_msg):
            send_whatsapp_message(raw_from, get_thanks_reply())
            return
        
        delete_handled = handle_whatsapp_delete_flow(
            raw_from=raw_from,
            owner_phone=owner_phone,
            uploader_phone=uploader_phone,
            incoming_msg=incoming_msg,
        )

        if delete_handled:
            return

        pending_entry = get_pending_entry(uploader_phone)

        # A full structured template must override stale conversation state.
        structured_expense = parse_structured_text_expense(incoming_msg)

        if structured_expense:
            clear_conversation_context(uploader_phone)

            success, message = save_text_expense(
                structured_expense,
                owner_phone,
                uploader_phone
            )

            send_whatsapp_message(raw_from, message)
            return

        comma_expense = parse_comma_text_expense(incoming_msg)

        if comma_expense:
            clear_conversation_context(uploader_phone)

            intent_data = {
                "intent": "expense_entry",
                "vendor": comma_expense["vendor"],
                "description": comma_expense["item"],
                "category": comma_expense["category"],
                "amount": comma_expense["amount"],
                "date": comma_expense["date"],
                "currency": "INR",
            }

            success, message = save_text_expense(
                intent_data,
                owner_phone,
                uploader_phone
            )

            send_whatsapp_message(raw_from, message)
            return

        if not pending_entry:
            handled = handle_conversation_context(
                raw_from=raw_from,
                owner_phone=owner_phone,
                uploader_phone=uploader_phone,
                incoming_msg=incoming_msg
            )

            if handled:
                return
            

        

        quick_category = match_category(incoming_msg)

        if pending_entry and quick_category and len(incoming_msg.split()) <= 3:
            intent_data = {
                "intent": "category_reply",
                "category": quick_category,
                "date": "",
                "vendor": "",
                "description": "",
                "amount": None,
                "currency": "INR",
            }
        else:
            intent_data = classify_whatsapp_text(incoming_msg)

        print("TEXT INTENT:", intent_data, flush=True)

        intent = intent_data.get("intent", "unknown")

        if pending_entry and intent == "category_reply":
            category = match_category(intent_data.get("category") or incoming_msg)

            pending_entry["category"] = category
            pending_entry["folder"] = category

            saved_entry_id = append_entry_and_get_id(
                pending_entry
            )

            update_vendor_memory(
                user_phone=owner_phone,
                vendor=pending_entry.get("vendor", ""),
                category=category,
                folder=category,
            )

            clear_pending_category(uploader_phone)

            
            message_lines = [
                "✅ Bill saved",
                "",
            ]

            if saved_entry_id:
                message_lines.append(
                    f"Reference: EXP-{saved_entry_id}"
                )

            message_lines.extend([
                f"Vendor: {pending_entry.get('vendor', '')}",
                f"Total: ₹{safe_float(pending_entry.get('total')):,.2f}",
                f"Category: {category}",
            ])

            if saved_entry_id:
                message_lines.extend([
                    "",
                    "To delete this record later, send:",
                    f"Delete EXP-{saved_entry_id}",
                ])

            send_whatsapp_message(
                raw_from,
                "\n".join(message_lines)
            )
            return

        if intent == "expense_entry":
            success, message = save_text_expense(
                intent_data,
                owner_phone,
                uploader_phone
            )

            send_whatsapp_message(
                raw_from,
                message
            )
            return


        if intent == "finance_question":
            previous_context = get_finance_context(uploader_phone)

            finance_intent = extract_finance_question_intent(
                incoming_msg,
                owner_phone,
                previous_context
)
            print("FINANCE INTENT:", finance_intent, flush=True)

            finance_result = query_finance_answer(finance_intent, owner_phone)
            print("FINANCE RESULT:", finance_result, flush=True)

            if not finance_intent.get("needs_clarification") and finance_intent.get("intent") != "unknown":
                save_finance_context(uploader_phone, finance_intent)

            answer = format_finance_answer(incoming_msg, finance_result)

            send_whatsapp_message(raw_from, answer)
            return

        send_whatsapp_message(raw_from, get_friendly_unknown_reply())

    except Exception as e:
        print("TEXT PROCESS ERROR:", str(e), flush=True)
        send_whatsapp_message(raw_from, f"Could not process your message. Error: {str(e)}")

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    response = MessagingResponse()

    print("\n========== NEW WHATSAPP REQUEST ==========", flush=True)

    raw_from = request.form.get("From", "")
    from_number = raw_from.replace("whatsapp:", "")
    uploader_phone = clean_phone(from_number)

    incoming_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", "0") or 0)

    print("From:", raw_from, flush=True)
    print("Uploader Phone:", uploader_phone, flush=True)
    print("Body:", incoming_msg, flush=True)
    print("Media count:", num_media, flush=True)

    # Simple messages should NOT touch RDS
    if num_media == 0:
        if is_greeting_message(incoming_msg):
            response.message(get_greeting_reply())
            return str(response)

        if is_help_message(incoming_msg):
            response.message(get_help_reply())
            return str(response)

        if is_thanks_message(incoming_msg):
            response.message(get_thanks_reply())
            return str(response)

    # Only initialize DB/OpenAI/Twilio after simple replies
    lazy_init()

    owner_phone = get_owner_phone_for_uploader(uploader_phone)

    if owner_phone is None:
        owner_phone = uploader_phone

    print("Owner Phone:", owner_phone, flush=True)

    if num_media == 0:
        threading.Thread(
            target=process_text_in_background,
            args=(raw_from, owner_phone, uploader_phone, incoming_msg),
            daemon=True
        ).start()

        response.message("Message received ✅\nProcessing now...")
        return str(response)

    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0", "")

    if not media_url:
        response.message("Please upload a bill image.")
        return str(response)

    print("Media URL:", media_url, flush=True)
    print("Media Type:", media_type, flush=True)

    threading.Thread(
        target=process_bill_in_background,
        args=(raw_from, owner_phone, uploader_phone, media_url, media_type),
        daemon=True
    ).start()

    response.message("Bill received ✅\nProcessing now...")
    return str(response)
