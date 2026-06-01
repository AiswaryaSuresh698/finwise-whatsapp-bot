import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")

BETA_USERS = [
    "whatsapp:+14373241463",  # your number
]

def send_daily_reminder():
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    message_body = (
        "Hi 👋 Please send today’s bills to FinWise. "
        "I’ll save them in your expense folders."
    )

    for user in BETA_USERS:
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=user,
            body=message_body
        )

        print("Sent to:", user)
        print("Message SID:", message.sid)
        print("Status:", message.status)

if __name__ == "__main__":
    send_daily_reminder()