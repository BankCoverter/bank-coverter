import os
import json
import csv
import io
import stripe
from fastapi import FastAPI, Request, HTTPException, Header
import google.generativeai as genai
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

app = FastAPI()

# Configuration
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

SYSTEM_PROMPT = """
You are a precision financial data extractor. Extract all transaction line items from the provided bank statement text/document.

You MUST respond ONLY with a valid, strictly formatted JSON object matching this schema:
{
  "bank_name": "string or null",
  "account_last4": "string or null",
  "statement_period": "string or null",
  "transactions": [
    {
      "date": "YYYY-MM-DD",
      "description": "string",
      "amount": float (negative for debits/withdrawals, positive for credits/deposits),
      "type": "debit" or "credit",
      "category_suggestion": "string"
    }
  ]
}

DO NOT include any markdown formatting, preambles, or conversational text. Output raw JSON only.
"""

@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook Error: {str(e)}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_email = session.get("customer_details", {}).get("email")
        statement_text = session.get("metadata", {}).get("statement_text", "")

        if customer_email and statement_text:
            process_and_deliver(statement_text, customer_email)

    return {"status": "success"}


def process_and_deliver(statement_text: str, customer_email: str):
    model = genai.GenerativeModel("gemini-1.5-flash")
    
    response = model.generate_content(
        f"{SYSTEM_PROMPT}\n\nBANK STATEMENT TEXT:\n{statement_text}",
        generation_config={"temperature": 0.0, "max_output_tokens": 2048}
    )

    try:
        data = json.loads(response.text.strip())
        transactions = data.get("transactions", [])
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Date", "Description", "Amount", "Type", "Category Suggestion"])
        
        for tx in transactions:
            writer.writerow([
                tx.get("date", ""),
                tx.get("description", ""),
                tx.get("amount", 0.0),
                tx.get("type", ""),
                tx.get("category_suggestion", "")
            ])
            
        csv_data = output.getvalue()
        send_email(customer_email, csv_data)

    except json.JSONDecodeError:
        print("Schema validation failed.")


def send_email(to_email: str, csv_content: str):
    import base64
    encoded_csv = base64.b64encode(csv_content.encode()).decode()
    
    message = Mail(
        from_email="no-reply@yourconversionsite.com",
        to_emails=to_email,
        subject="Your Converted Bank Statement CSV is Ready",
        html_content="<p>Attached is your formatted CSV file ready for import into QuickBooks or Xero.</p>"
    )
    
    attachment = Attachment(
        FileContent(encoded_csv),
        FileName("converted_statement.csv"),
        FileType("text/csv"),
        Disposition("attachment")
    )
    message.attachment = attachment

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    sg.send(message)