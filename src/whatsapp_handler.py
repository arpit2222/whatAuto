"""ClinicFlow – WhatsApp gateway Lambda.

patient -> WhatsApp -> AWS End User Messaging Social -> SNS -> this Lambda
  - text / buttons / list picks -> ClinicFlow agent on Amazon Bedrock AgentCore Runtime
  - photos                      -> GetWhatsAppMessageMedia -> S3, attached to today's token
  - shared location             -> Amazon Location Service drive time -> "leave now" timing
Every reply goes back with SendWhatsAppMessage, wrapped in context-aware interactive UI
(reply buttons when the patient holds a token, a list menu otherwise).
"""
import json
import os

import common as c

PHASE1_ECHO = os.environ.get("ECHO_ONLY") == "1"
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_ac = None

# interactive ids -> what the patient "said"
INTENTS = {
    "m_opd": "token opd", "m_laser": "token laser", "m_status": "status", "m_pkg": "package",
    "m_cancel": "cancel", "m_late": "running late", "m_here": "I'm here",
    "b_status": "status", "b_late": "running late", "b_cancel": "cancel", "b_here": "I'm here",
    "book_pkg": "book session", "later": "remind me later",
}

MENU_ROWS = [
    ("m_opd", "Doctor consultation", "OPD token: skin, acne, hair, follow-up"),
    ("m_laser", "Laser / procedure", "Laser, peel, PRP: book today's slot"),
    ("m_status", "My wait time", "Live position and ETA"),
    ("m_pkg", "My packages", "Sessions done, next due, book next"),
    ("m_cancel", "Cancel my token", "Free your slot for someone else"),
]


def ask_agent(phone: str, text: str) -> str:
    """Route the turn to the ClinicFlow agent hosted on Amazon Bedrock AgentCore Runtime
    (one isolated session per patient). Falls back to the in-Lambda agent."""
    global _ac
    import agent
    if not AGENT_RUNTIME_ARN:
        return agent.respond(phone, text)
    try:
        if _ac is None:
            import boto3
            _ac = boto3.client("bedrock-agentcore")
        r = _ac.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=f"clinicflow-patient-session-{phone}-{c.today()}",  # >= 33 chars
            payload=json.dumps({"phone": phone, "text": text}).encode(),
            contentType="application/json", accept="application/json")
        out = json.loads(r["response"].read())
        c.log.info("agentcore reply via %s", out.get("engine"))
        return out["reply"]
    except Exception:
        c.log.exception("AgentCore Runtime invoke failed; using in-Lambda agent")
        return agent.respond(phone, text)


def iter_inbound(sns_record: dict):
    """Yield (aws_phone_number_id, contact_name, message) for each inbound message."""
    envelope = json.loads(sns_record["Sns"]["Message"])
    entry = json.loads(envelope["whatsAppWebhookEntry"])
    ids = envelope.get("context", {}).get("MetaPhoneNumberIds", [])
    # ARN ends in ".../phone-number-id/<hex>"; the API wants "phone-number-id-<hex>"
    meta_to_aws = {i["metaPhoneNumberId"]: "phone-number-id-" + i["arn"].split("/")[-1] for i in ids}
    for change in entry.get("changes", []):
        value = change.get("value", {})
        meta_pid = value.get("metadata", {}).get("phone_number_id")
        aws_pid = meta_to_aws.get(meta_pid) or c.PHONE_ID
        names = {ct.get("wa_id"): ct.get("profile", {}).get("name") for ct in value.get("contacts", [])}
        for msg in value.get("messages", []):
            yield aws_pid, names.get(msg.get("from")), msg
        for st in value.get("statuses", []):
            c.log.info("status %s for %s", st.get("status"), st.get("id"))


def text_of(msg: dict) -> str | None:
    t = msg.get("type")
    if t == "text":
        return msg["text"]["body"]
    if t == "interactive":
        i = msg["interactive"]
        r = i.get("button_reply") or i.get("list_reply") or {}
        return INTENTS.get(r.get("id")) or r.get("title")
    if t == "button":  # template quick reply
        return msg["button"].get("text")
    return None


def handle_photo(pid: str, msg: dict, phone: str) -> str:
    media = msg.get("image") or msg.get("document") or {}
    ext = "jpg" if msg.get("type") == "image" else "bin"
    key = f"photos/{c.today()}/{phone}/{msg['id']}.{ext}"
    c.wa_fetch_media(media["id"], key, pid)
    c.bump("photos")
    tok = c.active_token(phone)
    if tok:
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": tok["sk"]},
                          UpdateExpression="SET photos = list_append(if_not_exists(photos, :e), :p)",
                          ExpressionAttributeValues={":p": [key], ":e": []})
        return f"Photo received 📷 and attached to token {tok['label']} for the doctor to review."
    return "Photo received 📷. Take a token and I'll attach it to your visit."


def handle_location(msg: dict, phone: str) -> str:
    loc = msg.get("location") or {}
    lat, lon = float(loc["latitude"]), float(loc["longitude"])
    minutes, source = c.travel_minutes(lat, lon)
    c.upsert_patient(phone, travel_min=minutes, travel_source=source, lat=lat, lon=lon, pending="none")
    c.bump("locations")
    how = "by car (Amazon Location Service)" if source == "amazon-location" else "by road (estimate)"
    tok = c.active_token(phone)
    if tok and tok["status"] == "waiting":
        ahead, eta = c.eta_minutes(tok)
        if eta <= minutes + 5:
            return (f"📍 You're ~{minutes} min away {how}. Your wait is ~{eta} min, so "
                    f"*please leave now* 🚶 for token {tok['label']}.")
        return (f"📍 You're ~{minutes} min away {how}. Token {tok['label']} has ~{eta} min to go. "
                f"Relax at home, and I'll ping you about {eta - minutes - 5} min from now when it's time to leave.")
    return f"📍 Saved: you're ~{minutes} min away {how}. I'll use this to tell you when to leave."


def send_with_ui(phone: str, reply: str, pid: str, asked: str = ""):
    """Wrap the reply in interactive UI based on the patient's state."""
    tok = c.active_token(phone)
    prof = c.patient(phone)
    try:
        if tok and tok["status"] == "in_consult":
            c.wa_text(phone, reply, pid)
        elif tok:
            if tok.get("arrived"):
                buttons = [("b_status", "🔄 Refresh wait"), ("b_cancel", "❌ Cancel")]
            else:
                buttons = [("b_status", "🔄 Refresh wait"), ("b_late", "⏰ Running late"), ("b_here", "📍 I'm here")]
            c.wa_buttons(phone, reply, buttons, pid)
            if not prof.get("travel_min") and not prof.get("loc_asked"):
                c.upsert_patient(phone, loc_asked=c.today())
                c.wa_location_request(phone, "Tap below to share your location once. ClinicFlow uses Amazon Location "
                                             "Service to work out your drive time and tells you exactly when to leave.", pid)
        else:
            c.wa_list(phone, reply, "Menu", MENU_ROWS, pid)
    except Exception:  # interactive failed (e.g. body too long) -> plain text
        c.log.exception("interactive send failed; sending text")
        c.wa_text(phone, reply, pid)


def lambda_handler(event, context):
    for record in event.get("Records", []):
        for pid, profile_name, msg in iter_inbound(record):
            phone = msg["from"]
            c.wa_mark_read(msg["id"], pid)
            text = None
            try:
                mtype = msg.get("type")
                if mtype in ("image", "document"):
                    reply = handle_photo(pid, msg, phone)
                    caption = (msg.get("image") or msg.get("document") or {}).get("caption")
                    if caption:
                        reply += "\n" + ask_agent(phone, caption)
                elif mtype == "location":
                    reply = handle_location(msg, phone)
                else:
                    text = text_of(msg)
                    if not text:
                        reply = "Sorry, I can read text, buttons, photos and locations for now 🙏"
                    elif PHASE1_ECHO:
                        reply = f"ClinicFlow is live ✅\nYou said: {text}"
                    else:
                        if profile_name and not c.patient(phone).get("wa_name"):
                            c.upsert_patient(phone, wa_name=profile_name)
                        reply = ask_agent(phone, text)
            except Exception:
                c.log.exception("handling failed")
                reply = "Sorry, something went wrong on our side. Please try again in a minute 🙏"
            send_with_ui(phone, reply, pid, text or "")
    return {"ok": True}
