"""ClinicFlow – WhatsApp inbound handler.

patient -> WhatsApp -> AWS End User Messaging Social -> SNS -> this Lambda
  - text / button replies  -> Bedrock agent (agent.respond) -> SendWhatsAppMessage
  - photos                 -> GetWhatsAppMessageMedia -> S3, attached to today's token
"""
import json
import os

import common as c

PHASE1_ECHO = os.environ.get("ECHO_ONLY") == "1"
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_ac = None


def ask_agent(phone: str, text: str) -> str:
    """Route the turn to the ClinicFlow agent hosted on Amazon Bedrock AgentCore Runtime
    (one isolated microVM session per patient). Falls back to the in-Lambda agent."""
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
        return (i.get("button_reply") or i.get("list_reply") or {}).get("title")
    if t == "button":  # template quick reply
        return msg["button"].get("text")
    return None


def handle_photo(pid: str, msg: dict, phone: str) -> str:
    media = msg.get("image") or msg.get("document") or {}
    ext = "jpg" if msg.get("type") == "image" else "bin"
    key = f"photos/{c.today()}/{phone}/{msg['id']}.{ext}"
    uri = c.wa_fetch_media(media["id"], key, pid)
    tok = c.active_token(phone)
    if tok:
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"TOKEN#{tok['token']:03d}"},
                          UpdateExpression="SET photos = list_append(if_not_exists(photos, :e), :p)",
                          ExpressionAttributeValues={":p": [key], ":e": []})
        return f"Photo received 📷 and attached to token #{tok['token']} for the doctor to review."
    return "Photo received 📷. Take a token (just say 'token') and I'll attach it to your visit."


def lambda_handler(event, context):
    for record in event.get("Records", []):
        for pid, profile_name, msg in iter_inbound(record):
            phone = msg["from"]
            c.wa_mark_read(msg["id"], pid)
            try:
                if msg.get("type") in ("image", "document"):
                    reply = handle_photo(pid, msg, phone)
                    caption = (msg.get("image") or msg.get("document") or {}).get("caption")
                    if caption:
                        reply += "\n" + ask_agent(phone, caption)
                else:
                    text = text_of(msg)
                    if not text:
                        reply = "Sorry, I can read text, buttons and photos for now 🙏"
                    elif PHASE1_ECHO:
                        reply = f"ClinicFlow is live ✅\nYou said: {text}"
                    else:
                        if profile_name and not c.patient(phone).get("wa_name"):
                            c.upsert_patient(phone, wa_name=profile_name)
                        reply = ask_agent(phone, text)
            except Exception:
                c.log.exception("handling failed")
                reply = "Sorry, something went wrong on our side. Please try again in a minute 🙏"
            c.wa_text(phone, reply, pid)
    return {"ok": True}
