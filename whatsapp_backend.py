import os
import requests
from io import BytesIO
import json
from storage_utils import read_sheet, write_sheet
import pandas as pd
from twilio.rest import Client as TwilioClient
import re
from datetime import datetime, timedelta
from difflib import get_close_matches
import time
import threading

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
from PIL import Image
from storage_utils import load_entries, save_entries, update_vendor_memory, get_owner_phone_for_uploader
import pandas as pd
import hashlib
from storage_utils import load_entries

from receipt_ai import get_client, extract_bill_details
from storage_utils import (
    append_entry,
    save_image_to_folder,
    ensure_storage,
    apply_vendor_memory,
    clean_phone,
    get_owner_phone_for_uploader,
)


load_dotenv()
ensure_storage()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = get_client(OPENAI_API_KEY)

PENDING_CATEGORY_FILE = "data/pending_category.json"


twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")


def send_whatsapp_message(to_number, message):
    if not str(to_number).startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        to=to_number,
        body=message
    )


def load_pending_category():
    df = read_sheet("pending_category")

    if df.empty:
        return {}

    pending = {}

    for _, row in df.iterrows():
        phone = str(row.get("phone", "")).strip()
        entry_json = row.get("entry_json", "")

        if phone and entry_json:
            try:
                pending[phone] = json.loads(entry_json)
            except Exception:
                pass

    return pending


def save_pending_category(pending):
    rows = []

    for phone, entry in pending.items():
        rows.append({
            "phone": str(phone),
            "entry_json": json.dumps(entry)
        })

    df = pd.DataFrame(rows)
    write_sheet("pending_category", df)


def clear_pending_category(phone):
    pending = load_pending_category()
    phone = str(phone)

    if phone in pending:
        del pending[phone]

    save_pending_category(pending)
CATEGORY_OPTIONS = [
    "Milk", "Chicken", "Rice", "Frozen Foods", "Ice Cream", "Cylinder",
    "Salary", "Marketing", "Utilities", "Rent", "Software", "Vehicle",
    "Insurance", "Travel", "Professional Fees", "Office Supplies",
    "Income", "Uncategorized"
]

CATEGORY_ALIASES = {
    "groceries": "Uncategorized",
    "grocery": "Uncategorized",
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
    return "FinWise WhatsApp bot is running."

def make_expense_duplicate_key(from_number, extracted):
    vendor = str(extracted.get("vendor", "")).strip().lower()
    date = str(extracted.get("date", "")).strip()
    total = str(round(float(extracted.get("total", 0) or 0), 2))
    description = str(extracted.get("description", "")).strip().lower()

    raw_key = f"{from_number}|{date}|{vendor}|{total}|{description}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def is_duplicate_expense(duplicate_key):
    entries_df = load_entries()

    if entries_df.empty:
        return False

    if "duplicate_key" not in entries_df.columns:
        return False

    return duplicate_key in entries_df["duplicate_key"].astype(str).values

def process_bill_in_background(raw_from, owner_phone, uploader_phone, media_url, media_type):
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
            pending = load_pending_category()
            pending[uploader_phone] = entry
            save_pending_category(pending)

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
        pending = load_pending_category()
        print("Checking pending for:", uploader_phone, flush=True)
        print("Pending keys:", list(pending.keys()), flush=True)

        if uploader_phone in pending:
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

            pending_entry = pending[uploader_phone]
            pending_entry["category"] = category
            pending_entry["folder"] = category

            print("Saving pending entry:", pending_entry, flush=True)

            append_entry(pending_entry)

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

            pending = load_pending_category()
            pending[uploader_phone] = entry
            save_pending_category(pending)

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

        