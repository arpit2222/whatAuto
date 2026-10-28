"""Shared helpers: config, DynamoDB data model, WhatsApp + SES senders."""
import datetime as dt
import json
import logging
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = os.environ.get("TABLE_NAME", "clinicflow")
MEDIA_BUCKET = os.environ.get("MEDIA_BUCKET", "")
PHONE_ID = os.environ.get("ORIGINATION_PHONE_NUMBER_ID", "")
META_API_VERSION = os.environ.get("META_API_VERSION", "v20.0")
SES_FROM = os.environ.get("SES_FROM", "")
CLINIC_ID = os.environ.get("CLINIC_ID", "glowskin")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ddb = boto3.resource("dynamodb").Table(TABLE)
sm = boto3.client("socialmessaging")
ses = boto3.client("sesv2")


def today() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d")


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def clean(o):
    """Convert DynamoDB Decimals to int/float for JSON."""
    if isinstance(o, list):
        return [clean(i) for i in o]
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    return o


# ---------------------------------------------------------------- WhatsApp
def wa_send(payload: dict, phone_id: str | None = None) -> dict:
    body = {"messaging_product": "whatsapp", **payload}
    if body.get("to") and not str(body["to"]).startswith("+"):  # AWS EUM wants E.164 with '+'
        body["to"] = "+" + str(body["to"])
    resp = sm.send_whatsapp_message(
        originationPhoneNumberId=phone_id or PHONE_ID,
        message=json.dumps(body).encode("utf-8"),
        metaApiVersion=META_API_VERSION,
    )
    log.info("wa sent %s", resp.get("messageId"))
    return resp


def wa_text(to: str, text: str, phone_id: str | None = None) -> dict:
    return wa_send({"to": to, "type": "text", "text": {"body": text[:4000]}}, phone_id)


def wa_buttons(to: str, text: str, buttons: list[tuple[str, str]], phone_id: str | None = None):
    """Up to 3 quick-reply buttons: [(id, title)]."""
    return wa_send({
        "to": to, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]
            ]},
        },
    }, phone_id)


def wa_mark_read(message_id: str, phone_id: str | None = None):
    try:
        wa_send({"status": "read", "message_id": message_id}, phone_id)
    except Exception:
        log.exception("mark read failed")


def wa_fetch_media(media_id: str, key: str, phone_id: str | None = None) -> str:
    sm.get_whatsapp_message_media(
        mediaId=media_id,
        originationPhoneNumberId=phone_id or PHONE_ID,
        destinationS3File={"bucketName": MEDIA_BUCKET, "key": key},
    )
    return f"s3://{MEDIA_BUCKET}/{key}"


# ---------------------------------------------------------------- Email (SES)
def email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    if not (SES_FROM and to):
        return False
    try:
        ses.send_email(
            FromEmailAddress=SES_FROM,
            Destination={"ToAddresses": [to]},
            Content={"Simple": {
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": text}, **({"Html": {"Data": html}} if html else {})},
            }},
        )
        return True
    except Exception:
        log.exception("ses send failed")
        return False


# ---------------------------------------------------------------- Data model
# Single table, pk/sk:
#   CLINIC#<id>                 META                 name, avg_consult_min, address, hours
#   CLINIC#<id>#DAY#<date>      COUNTER              last_token, now_serving
#   CLINIC#<id>#DAY#<date>      TOKEN#<nnn>          phone, name, service, status, nudged, photos
#   PATIENT#<phone>             PROFILE              name, email, travel_min
#   PATIENT#<phone>             PACKAGE#<name>       total, used, interval_days, next_due
#   PATIENT#<phone>             CONV                 turns (recent chat history)
def clinic(cid: str = CLINIC_ID) -> dict:
    return clean(ddb.get_item(Key={"pk": f"CLINIC#{cid}", "sk": "META"}).get("Item", {}))


def day_pk(cid: str = CLINIC_ID, date: str | None = None) -> str:
    return f"CLINIC#{cid}#DAY#{date or today()}"


def counter(cid: str = CLINIC_ID) -> dict:
    item = ddb.get_item(Key={"pk": day_pk(cid), "sk": "COUNTER"}).get("Item")
    return clean(item) if item else {"last_token": 0, "now_serving": 0}


def tokens(cid: str = CLINIC_ID) -> list[dict]:
    r = ddb.query(KeyConditionExpression=Key("pk").eq(day_pk(cid)) & Key("sk").begins_with("TOKEN#"))
    return sorted(clean(r["Items"]), key=lambda t: t["token"])


def waiting(cid: str = CLINIC_ID) -> list[dict]:
    return [t for t in tokens(cid) if t["status"] == "waiting"]


def patient(phone: str) -> dict:
    return clean(ddb.get_item(Key={"pk": f"PATIENT#{phone}", "sk": "PROFILE"}).get("Item", {}))


def upsert_patient(phone: str, **fields):
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    if not fields:
        return
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    ddb.update_item(
        Key={"pk": f"PATIENT#{phone}", "sk": "PROFILE"},
        UpdateExpression=expr,
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


def packages(phone: str) -> list[dict]:
    r = ddb.query(KeyConditionExpression=Key("pk").eq(f"PATIENT#{phone}") & Key("sk").begins_with("PACKAGE#"))
    return clean(r["Items"])


def active_token(phone: str, cid: str = CLINIC_ID) -> dict | None:
    for t in tokens(cid):
        if t["phone"] == phone and t["status"] in ("waiting", "in_consult"):
            return t
    return None


def eta_minutes(tok: dict, cid: str = CLINIC_ID) -> tuple[int, int]:
    """(people_ahead, eta_minutes) for a waiting token."""
    avg = int(clinic(cid).get("avg_consult_min", 10))
    ahead = [t for t in tokens(cid) if t["status"] in ("waiting", "in_consult") and t["token"] < tok["token"]]
    return len(ahead), len(ahead) * avg


MEMORY_ID = os.environ.get("MEMORY_ID", "")
_mem = None


def _memory():
    global _mem
    if _mem is None:
        _mem = boto3.client("bedrock-agentcore")
    return _mem


def _mem_session(phone: str) -> str:
    return f"wa-{phone}-{today()}"  # one AgentCore Memory session per patient per day


def load_conv(phone: str) -> list:
    """Recent turns. AgentCore Memory (short-term events) when MEMORY_ID is set, else DynamoDB."""
    if MEMORY_ID:
        try:
            r = _memory().list_events(memoryId=MEMORY_ID, actorId=f"patient-{phone}",
                                      sessionId=_mem_session(phone), includePayloads=True, maxResults=20)
            turns = []
            for ev in sorted(r.get("events", []), key=lambda e: e["eventTimestamp"]):
                for item in ev.get("payload", []):
                    conv = item.get("conversational")
                    if conv and conv["role"] in ("USER", "ASSISTANT"):
                        turns.append({"role": conv["role"].lower(), "text": conv["content"]["text"]})
            return turns[-12:]
        except Exception:
            log.exception("AgentCore Memory read failed; using DynamoDB")
    return _load_conv_ddb(phone)


def save_turn(phone: str, user_text: str, reply: str, history: list):
    if MEMORY_ID:
        try:
            _memory().create_event(
                memoryId=MEMORY_ID, actorId=f"patient-{phone}", sessionId=_mem_session(phone),
                eventTimestamp=dt.datetime.now(dt.timezone.utc),
                payload=[{"conversational": {"role": "USER", "content": {"text": user_text}}},
                         {"conversational": {"role": "ASSISTANT", "content": {"text": reply}}}])
            return
        except Exception:
            log.exception("AgentCore Memory write failed; using DynamoDB")
    save_conv(phone, history + [{"role": "user", "text": user_text}, {"role": "assistant", "text": reply}])


def _load_conv_ddb(phone: str) -> list:
    item = ddb.get_item(Key={"pk": f"PATIENT#{phone}", "sk": "CONV"}).get("Item")
    if not item:
        return []
    if time.time() - float(item.get("ts", 0)) > 6 * 3600:  # fresh context after 6h
        return []
    return json.loads(item["turns"])


def save_conv(phone: str, turns: list):
    ddb.put_item(Item={"pk": f"PATIENT#{phone}", "sk": "CONV",
                       "turns": json.dumps(turns[-12:]), "ts": Decimal(str(time.time()))})
