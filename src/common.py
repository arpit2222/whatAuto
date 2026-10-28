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
#   CLINIC#<id>                 META                 name, address, hours, queues{OPD,LASER}, lat, lon, doctor_email
#   CLINIC#<id>#DAY#<date>      COUNTER#<Q>          last_token, now_serving          (Q = OPD | LASER)
#   CLINIC#<id>#DAY#<date>      TOKEN#<Q>#<nnn>      queue, token, label, pos, phone, name, service, status,
#                                                    nudged, photos, created/nudged_at/called_at/arrived_at/done_at
#   CLINIC#<id>#DAY#<date>      STATS                status_checks, nudges, photos, package_bookings, late, ...
#   PATIENT#<phone>             PROFILE              name, email, travel_min, travel_source, lat, lon
#   PATIENT#<phone>             PACKAGE#<name>       total, used, interval_days, next_due, queue
#   PATIENT#<phone>             CONV                 turns (recent chat history, when AgentCore Memory is off)
QUEUES = ("OPD", "LASER")
DEFAULT_QUEUES = {"OPD": {"label": "Doctor consultation (OPD)", "service_min": 6, "prefix": "O"},
                  "LASER": {"label": "Laser / procedure", "service_min": 20, "prefix": "L"}}


def clinic(cid: str = CLINIC_ID) -> dict:
    return clean(ddb.get_item(Key={"pk": f"CLINIC#{cid}", "sk": "META"}).get("Item", {}))


def queue_cfg(q: str, cl: dict | None = None) -> dict:
    cl = cl if cl is not None else clinic()
    cfg = dict(DEFAULT_QUEUES.get(q, DEFAULT_QUEUES["OPD"]))
    cfg.update((cl.get("queues") or {}).get(q, {}))
    return cfg


def norm_queue(q) -> str:
    q = str(q or "OPD").upper()
    return "LASER" if q.startswith(("L", "P")) else "OPD"   # laser / procedure -> LASER


def day_pk(cid: str = CLINIC_ID, date: str | None = None) -> str:
    return f"CLINIC#{cid}#DAY#{date or today()}"


def _norm_token(t: dict) -> dict:
    """Back-fill fields for tokens created before queues existed."""
    t.setdefault("queue", "OPD")
    t.setdefault("pos", t.get("token", 0))
    t.setdefault("label", f"{DEFAULT_QUEUES[t['queue']]['prefix']}{t.get('token', 0)}")
    return t


def counter(q: str = "OPD", cid: str = CLINIC_ID) -> dict:
    item = ddb.get_item(Key={"pk": day_pk(cid), "sk": f"COUNTER#{q}"}).get("Item")
    return clean(item) if item else {"last_token": 0, "now_serving": 0, "now_serving_label": ""}


def tokens(cid: str = CLINIC_ID, q: str | None = None) -> list[dict]:
    r = ddb.query(KeyConditionExpression=Key("pk").eq(day_pk(cid)) & Key("sk").begins_with("TOKEN#"))
    toks = [_norm_token(t) for t in clean(r["Items"])]
    if q:
        toks = [t for t in toks if t["queue"] == q]
    return sorted(toks, key=lambda t: (t["queue"], t["pos"]))


def waiting(cid: str = CLINIC_ID, q: str | None = None) -> list[dict]:
    return [t for t in tokens(cid, q) if t["status"] == "waiting"]


def set_token(t: dict, **fields):
    """Update attributes on a token row (by its sk)."""
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    ddb.update_item(Key={"pk": day_pk(), "sk": t["sk"]}, UpdateExpression=expr,
                    ExpressionAttributeNames={f"#{k}": k for k in fields},
                    ExpressionAttributeValues={f":{k}": (Decimal(str(v)) if isinstance(v, float) else v)
                                               for k, v in fields.items()})


def bump(stat: str, n: int = 1):
    """Increment a daily impact counter (STATS row)."""
    try:
        ddb.update_item(Key={"pk": day_pk(), "sk": "STATS"}, UpdateExpression="ADD #s :n",
                        ExpressionAttributeNames={"#s": stat}, ExpressionAttributeValues={":n": n})
    except Exception:
        log.exception("stat bump failed")


def stats(date: str | None = None) -> dict:
    return clean(ddb.get_item(Key={"pk": day_pk(date=date), "sk": "STATS"}).get("Item", {}))


def patient(phone: str) -> dict:
    return clean(ddb.get_item(Key={"pk": f"PATIENT#{phone}", "sk": "PROFILE"}).get("Item", {}))


def upsert_patient(phone: str, **fields):
    fields = {k: (Decimal(str(v)) if isinstance(v, float) else v) for k, v in fields.items() if v not in (None, "")}
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
    """(people_ahead, eta_minutes) for a token, within its own queue."""
    q = tok.get("queue", "OPD")
    svc = int(queue_cfg(q).get("service_min", 10))
    ahead = [t for t in tokens(cid, q) if t["status"] in ("waiting", "in_consult")
             and t["pos"] < tok.get("pos", tok.get("token", 0)) and t["sk"] != tok.get("sk")]
    return len(ahead), len(ahead) * svc


def wa_link(text: str = "status") -> str:
    """Cross-channel deep link: opens the clinic's WhatsApp chat with text pre-filled."""
    num = "".join(ch for ch in os.environ.get("CLINIC_WA_NUMBER", "917217811862") if ch.isdigit())
    return f"https://wa.me/{num}?text={text.replace(' ', '%20')}"


def iso_now() -> str:
    return now_ist().isoformat(timespec="seconds")


def minutes_between(a: str | None, b: str | None) -> float | None:
    if not (a and b):
        return None
    try:
        return (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 60
    except Exception:
        return None


# ---------------------------------------------------------------- Amazon Location Service
def travel_minutes(lat: float, lon: float, cl: dict | None = None) -> tuple[int, str]:
    """Drive time from patient to clinic. Amazon Location Service (geo-routes) first,
    haversine estimate at city speed as a fallback. Returns (minutes, source)."""
    cl = cl if cl is not None else clinic()
    clat, clon = float(cl.get("lat", 28.6460)), float(cl.get("lon", 77.3695))
    try:
        geo = boto3.client("geo-routes")
        r = geo.calculate_routes(Origin=[lon, lat], Destination=[clon, clat], TravelMode="Car",
                                 DepartNow=True, LegAdditionalFeatures=["Summary"])
        route = r["Routes"][0]
        secs = (route.get("Summary") or {}).get("Duration")
        if not secs:
            secs = sum(((l.get("VehicleLegDetails") or {}).get("Summary") or {}).get("Overview", {}).get("Duration", 0)
                       for l in route.get("Legs", []))
        if secs:
            return max(1, round(secs / 60)), "amazon-location"
    except Exception:
        log.exception("Amazon Location route failed; using distance estimate")
    import math
    R = 6371.0
    p1, p2 = math.radians(lat), math.radians(clat)
    dphi, dl = math.radians(clat - lat), math.radians(clon - lon)
    km = 2 * R * math.asin(math.sqrt(math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))
    return max(3, round(km * 1.3 / 25 * 60)), "estimate"   # 1.3x road factor, 25 km/h city traffic


def refresh_travel(phone: str, prof: dict | None = None, max_age_min: float = 3) -> tuple[int | None, str, float | None]:
    """Traffic-aware drive time from the patient's last shared location.
    Re-queries Amazon Location Service (DepartNow = current traffic) if the estimate is older than max_age_min.
    Returns (minutes, source, location_age_min)."""
    prof = prof if prof is not None else patient(phone)
    loc_age = minutes_between(prof.get("loc_at"), iso_now()) if prof.get("loc_at") else None
    if prof.get("lat") is None or prof.get("lon") is None:
        return (int(prof["travel_min"]) if prof.get("travel_min") else None), prof.get("travel_source", "patient"), None
    est_age = minutes_between(prof.get("travel_at"), iso_now()) if prof.get("travel_at") else None
    if est_age is not None and est_age < max_age_min:
        return int(prof["travel_min"]), prof.get("travel_source", "amazon-location"), loc_age
    minutes, source = travel_minutes(float(prof["lat"]), float(prof["lon"]))
    upsert_patient(phone, travel_min=minutes, travel_source=source, travel_at=iso_now())
    return minutes, source, loc_age


# ---------------------------------------------------------------- WhatsApp UI helpers
def wa_list(to: str, text: str, button: str, rows: list[tuple[str, str, str]], phone_id: str | None = None):
    """Interactive list: rows = [(id, title<=24, description<=72)]."""
    return wa_send({
        "to": to, "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": text[:1024]},
            "action": {"button": button[:20], "sections": [{"title": "ClinicFlow", "rows": [
                {"id": i, "title": t[:24], "description": d[:72]} for i, t, d in rows[:10]]}]},
        },
    }, phone_id)


def wa_location_request(to: str, text: str, phone_id: str | None = None):
    """Native WhatsApp 'Send location' button."""
    return wa_send({"to": to, "type": "interactive", "interactive": {
        "type": "location_request_message", "body": {"text": text[:1024]},
        "action": {"name": "send_location"}}}, phone_id)


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
