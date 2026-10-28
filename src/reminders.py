"""Daily package-session reminders (EventBridge schedule -> this Lambda).

Patients are usually outside WhatsApp's 24h customer-service window, so this
uses an approved utility template when REMINDER_TEMPLATE is set; otherwise it
falls back to a free-form text (works only inside the 24h window) + email.
"""
import os

from boto3.dynamodb.conditions import Attr

import common as c

TEMPLATE = os.environ.get("REMINDER_TEMPLATE", "session_reminder")  # Hi {{1}}, your {{2}} session {{3}} is due on {{4}}
TEMPLATE_LANG = os.environ.get("REMINDER_TEMPLATE_LANG", "en")


def due_packages():
    r = c.ddb.scan(FilterExpression=Attr("sk").begins_with("PACKAGE#"))
    for p in c.clean(r["Items"]):
        if p.get("next_due") and p["next_due"] <= c.today() and p["used"] < p["total"]:
            yield p


def lambda_handler(event, context):
    cl = c.clinic()
    sent = []
    for p in due_packages():
        phone = p["pk"].split("#", 1)[1]
        prof = c.patient(phone)
        pname = p["sk"].split("#", 1)[1]
        name = prof.get("name", "there")
        session = p["used"] + 1
        try:
            if TEMPLATE and TEMPLATE != "none":
                c.wa_send({"to": phone, "type": "template", "template": {
                    "name": TEMPLATE, "language": {"code": TEMPLATE_LANG},
                    "components": [{"type": "body", "parameters": [
                        {"type": "text", "text": name}, {"type": "text", "text": pname},
                        {"type": "text", "text": str(session)},
                        {"type": "text", "text": p["next_due"]}]}]}})
            else:
                c.wa_buttons(phone, f"Hi {name}, your {pname} session {session}/{p['total']} is due at "
                                    f"{cl.get('name')}. Want a token for today?",
                             [("book_pkg", "Book today"), ("later", "Remind later")])
        except Exception:  # template not approved yet -> free-form (works inside 24h window)
            c.log.exception("template reminder failed for %s, trying free-form", phone)
            try:
                c.wa_buttons(phone, f"Hi {name}, your {pname} session {session}/{p['total']} is due at "
                                    f"{cl.get('name')}. Want a token for today?",
                             [("book_pkg", "Book today"), ("later", "Remind later")])
            except Exception:
                c.log.exception("free-form reminder failed for %s", phone)
        c.email(prof.get("email"), f"Your {pname} session is due",
                f"Hi {name},\n\nSession {session} of {p['total']} of your {pname} package is due.\n"
                f"Reply on WhatsApp to get a token.\n\n{cl.get('name')}")
        sent.append(phone[-4:])
    return {"reminded": sent}
