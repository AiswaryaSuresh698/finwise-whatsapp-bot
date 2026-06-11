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

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from PIL import Image

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



TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")


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
    text = str(text or "").lower()

    if "today" in text:
        return datetime.now().strftime("%Y-%m-%d")

    if "yesterday" in text:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if date_match:
        return date_match.group(0)

    return datetime.now().strftime("%Y-%m-%d")

def parse_comma_text_expense(text):
    parts = [p.strip() for p in str(text or "").split(",")]

    if len(parts) < 3:
        return None

    item = parts[0]
    vendor = parts[1]
    amount = extract_amount(parts[2])
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
                LIMIT 1
            """),
            {"duplicate_key": str(duplicate_key)}
        ).fetchone()

    return row is not None

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
            "date": extracted.get("date", ""),
            "transaction_type": extracted.get("transaction_type", "expense"),
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
                f"I found a new vendor and need category confirmation.\n\n"
                f"Vendor: {entry['vendor']}\n"
                f"Total: ₹{entry['total']}\n\n"
                f"Which category should I save this under?\n"
                f"Example: Grocery, Gas, Meals, Salary, Utilities."
            )
            return

        save_start = time.time()
        append_entry(entry)

        print(f"SAVE ENTRY: {round(time.time() - save_start, 2)} sec", flush=True)
        print(f"FULL REQUEST TIME: {round(time.time() - request_start, 2)} sec", flush=True)

        send_whatsapp_message(
            raw_from,
            f"Saved bill ✅\n"
            f"Vendor: {entry['vendor']}\n"
            f"Total: ₹{entry['total']}\n"
            f"Category: {entry['category']}"
        )

    except Exception as e:
        print("ERROR PROCESSING BILL:", str(e), flush=True)
        send_whatsapp_message(raw_from, f"Could not process the bill. Error: {str(e)}")

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    lazy_init()
    response = MessagingResponse()

    print("\n========== NEW WHATSAPP REQUEST ==========", flush=True)

    raw_from = request.form.get("From", "")
    from_number = raw_from.replace("whatsapp:", "")
    phone_key = clean_phone(from_number)
    uploader_phone = phone_key
    owner_phone = get_owner_phone_for_uploader(uploader_phone)

    if owner_phone is None:
        response.message(
            "This WhatsApp number is not linked to any FinWise restaurant account. "
            "Please ask the restaurant owner to add this number."
        )
        return str(response)
    incoming_msg = request.form.get("Body", "").strip()
    num_media = int(request.form.get("NumMedia", "0") or 0)

    print("From:", raw_from, flush=True)
    print("Clean From:", phone_key, flush=True)
    print("Body:", incoming_msg, flush=True)
    print("Media count:", num_media, flush=True)

    allowed_categories = [
        "Grocery", "Gas", "Internet", "Utilities", "Meals", "Rent","Salary",
        "Software", "Office Supplies", "Vehicle", "Professional Fees",
        "Insurance", "Travel", "Income", "Uncategorized"
    ]

    if num_media == 0:
        pending_entry = get_pending_entry(uploader_phone)

        print("Checking pending for:", uploader_phone, flush=True)
        print("Has pending:", pending_entry is not None, flush=True)

        if pending_entry:
            category = incoming_msg.title()

            if not category:
                response.message("Please reply with a valid category. Example: Grocery, Gas, Meals, Utilities.")
                return str(response)

            if category not in allowed_categories:
                response.message(
                    f"'{category}' is not a valid category.\n\n"
                    "Please reply with one of these:\n"
                    "Grocery, Gas, Internet, Utilities, Meals, Rent, Software, "
                    "Office Supplies, Vehicle, Professional Fees, Insurance, Travel."
                )
                return str(response)


            pending_entry["category"] = category
            pending_entry["folder"] = category

            print("Saving pending entry:", pending_entry, flush=True)

            save_start = time.time()
            append_entry(pending_entry)
            print("CONFIRMED ENTRY SAVE:", round(time.time() - save_start, 2), "sec", flush=True)

            update_vendor_memory(
                user_phone=owner_phone,
                vendor=pending_entry.get("vendor", ""),
                category=category,
                folder=category,
            )

            clear_pending_category(uploader_phone)

            send_whatsapp_message(
                raw_from,
                f"Saved bill ✅\n"
                f"Vendor: {pending_entry.get('vendor', '')}\n"
                f"Total: ₹{pending_entry.get('total', '')}\n"
                f"Category saved as: {category}"
            )

            return str(response)

        

        parsed_text = parse_comma_text_expense(incoming_msg)

        if parsed_text is None:
            response.message(
                "Please enter your expense:\n\n"
                "Item, vendor, amount, date of purchase\n\n"
                "Example:\n"
                "chicken, walmart, 1500, on June 2\n\n"
                "Or upload a bill image."
            )
            return str(response)

        amount = parsed_text["amount"]
        category = parsed_text["category"]
        date_value = parsed_text["date"]
        vendor = parsed_text["vendor"]
        item = parsed_text["item"]

        memory_category, memory_folder = apply_vendor_memory(owner_phone, vendor)

        if memory_category:
            category = memory_category

        if not category:
            entry = {
                "date": date_value,
                "transaction_type": "expense",
                "vendor": vendor,
                "user_phone": owner_phone,
                "uploaded_by": uploader_phone,
                "description": item,
                "category": "Uncategorized",
                "folder": "Uncategorized",
                "subtotal": amount,
                "tax": 0,
                "total": amount,
                "currency": "INR",
                "confidence": "manual",
                "reason": "WhatsApp text entry",
                "image_path": "",
                "source": "WhatsApp Text",
                "duplicate_key": make_expense_duplicate_key(
                    owner_phone,
                    {
                        "vendor": vendor,
                        "date": date_value,
                        "total": amount,
                        "description": item,
                    }
                ),
            }

            save_pending_entry(uploader_phone, entry)

            response.message(
                f"I found a new vendor/text expense.\n\n"
                f"Vendor: {vendor}\n"
                f"Amount: ₹{amount}\n"
                f"Date: {date_value}\n\n"
                f"Which category should I save this under?"
            )
            return str(response)

        folder = memory_folder if memory_folder else category

        entry = {
            "date": date_value,
            "transaction_type": "expense",
            "vendor": vendor,
            "user_phone": owner_phone,
            "uploaded_by": uploader_phone,
            "description": item,
            "category": category,
            "folder": folder,
            "subtotal": amount,
            "tax": 0,
            "total": amount,
            "currency": "INR",
            "confidence": "manual",
            "reason": "WhatsApp text entry",
            "image_path": "",
            "source": "WhatsApp Text",
            "duplicate_key": make_expense_duplicate_key(
                owner_phone,
                {
                    "vendor": vendor,
                    "date": date_value,
                    "total": amount,
                    "description": item,
                }
            ),
        }

        if is_duplicate_expense(entry["duplicate_key"]):
            response.message("This text expense was already uploaded earlier.")
            return str(response)

        append_entry(entry)

        update_vendor_memory(
            user_phone=owner_phone,
            vendor=vendor,
            category=category,
            folder=folder,
        )

        response.message(
            f"Saved text expense ✅\n"
            f"Vendor: {vendor}\n"
            f"Amount: ₹{amount}\n"
            f"Date: {date_value}\n"
            f"Category: {category}"
        )

        return str(response)

    media_url = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0", "")

    print("Media URL:", media_url, flush=True)
    print("Media Type:", media_type, flush=True)

    if not media_url:
        response.message("Please upload a bill image.")
        return str(response)
    
    threading.Thread(
        target=process_bill_in_background,
        args=(raw_from, owner_phone, uploader_phone, media_url, media_type),
        daemon=True
    ).start()

    response.message("Bill received ✅\nProcessing now...")

    return str(response)

        