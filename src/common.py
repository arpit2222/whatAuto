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


def active_tokens(phone: str, cid: str = CLINIC_ID) -> list[dict]:
    """All of today's active tokens booked from this phone (patient + family members)."""
    return [t for t in tokens(cid) if t["phone"] == phone and t["status"] in ("waiting", "in_consult")]


def active_token(phone: str, cid: str = CLINIC_ID) -> dict | None:
    for t in tokens(cid):
        if t["phone"] == phone and t["status"] in ("waiting", "in_consult"):
            return t
    return None


def service_minutes(q: str, toks: list | None = None, cl: dict | None = None) -> tuple[int, int, int]:
    """Self-learning consult time for a queue.
    Returns (effective_min, configured_min, samples). Blends the configured value with the average of today's
    completed visits (called_at -> done_at); after 4+ visits today's real pace dominates."""
    cfg = int(queue_cfg(q, cl).get("service_min", 10))
    toks = toks if toks is not None else tokens(q=q)
    durs = [m for t in toks if t.get("queue") == q and t["status"] == "done"
            and (m := minutes_between(t.get("called_at"), t.get("done_at"))) is not None and 0.5 <= m <= 90]
    durs = durs[-8:]
    if not durs:
        return cfg, cfg, 0
    learned = sum(durs) / len(durs)
    w = min(len(durs), 4) / 4            # 1 visit -> 25% weight, 4+ -> 100%
    eff = round(cfg * (1 - w) + learned * w)
    return max(2, min(60, eff)), cfg, len(durs)


def minute_of_day(hhmm: str | None = None) -> int:
    if hhmm:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    n = now_ist()
    return n.hour * 60 + n.minute


def slot_times(cl: dict | None = None) -> list[str]:
    """Laser appointment grid for today (30-min slots, 10:00-19:30 unless configured)."""
    cl = cl if cl is not None else clinic()
    if cl.get("laser_slots"):
        return list(cl["laser_slots"])
    return [f"{h:02d}:{m:02d}" for h in range(10, 20) for m in (0, 30)]


def free_slots(limit: int = 10) -> list[str]:
    taken = {i["sk"].split("#", 1)[1] for i in clean(ddb.query(
        KeyConditionExpression=Key("pk").eq(day_pk()) & Key("sk").begins_with("SLOT#"))["Items"])
        if i.get("status") != "cancelled"}
    now = minute_of_day()
    return [t for t in slot_times() if minute_of_day(t) >= now + 20 and t not in taken][:limit]


def eta_minutes(tok: dict, cid: str = CLINIC_ID) -> tuple[int, int]:
    """(people_ahead, eta_minutes) for a token, within its own queue.
    Uses the self-learned consult time, adds any announced doctor delay, and never
    promises earlier than a booked laser slot."""
    q = tok.get("queue", "OPD")
    toks = tokens(cid, q)
    svc, _, _ = service_minutes(q, toks)
    ahead = [t for t in toks if t["status"] in ("waiting", "in_consult")
             and t["pos"] < tok.get("pos", tok.get("token", 0)) and t["sk"] != tok.get("sk")]
    delay = int(counter(q, cid).get("delay_min", 0) or 0)
    eta = len(ahead) * svc + delay
    if tok.get("slot"):
        eta = max(eta, minute_of_day(tok["slot"]) - minute_of_day())
    return len(ahead), max(0, eta)


def upi_link(amount: int, note: str, cl: dict | None = None) -> str | None:
    cl = cl if cl is not None else clinic()
    vpa = cl.get("upi_vpa")
    if not vpa:
        return None
    from urllib.parse import quote
    return (f"upi://pay?pa={quote(vpa)}&pn={quote(cl.get('name', 'Clinic'))}&am={amount}&cu=INR&tn={quote(note)}")


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


# ---------------------------------------------------------------- patient profile & visit history
#   PATIENT#<phone>   VISIT#<date>#<label>   one record per finished visit (or no-show), kept forever
def save_visit(t: dict, status: str = "done") -> dict:
    """Snapshot a finished token into the patient's permanent visit history."""
    phone = t.get("phone", "")
    if not phone or str(phone).startswith("walkin"):
        return {}
    date = t.get("date") or today()
    item = {
        "pk": f"PATIENT#{phone}", "sk": f"VISIT#{date}#{t.get('label', '')}",
        "date": date, "label": t.get("label", ""), "queue": t.get("queue", "OPD"), "status": status,
        "who": t.get("name", ""), "family": bool(t.get("family")), "service": t.get("service", ""),
        "intake": t.get("intake") or {}, "intake_summary": t.get("intake_summary", ""),
        "rx_text": t.get("rx_text", ""), "rx_pdf": t.get("rx_pdf", ""), "followup_due": t.get("followup_due", ""),
        "photos": t.get("photos", []), "package": t.get("package", ""), "slot": t.get("slot", ""),
        "urgent_reason": t.get("urgent_reason", ""), "feedback": t.get("feedback", ""),
        "wait_min": round(minutes_between(t.get("created_at"), t.get("called_at")) or 0),
        "consult_min": round(minutes_between(t.get("called_at"), t.get("done_at") or iso_now()) or 0),
        "saved_at": iso_now(),
    }
    item = {k: v for k, v in item.items() if v not in (None, "", [], {})} | {"family": item["family"]}
    ddb.put_item(Item=item)
    return item


def visits(phone: str, limit: int = 60) -> list[dict]:
    """Newest first."""
    r = ddb.query(KeyConditionExpression=Key("pk").eq(f"PATIENT#{phone}") & Key("sk").begins_with("VISIT#"),
                  ScanIndexForward=False, Limit=limit)
    return clean(r["Items"])


def followups(phone: str) -> list[dict]:
    r = ddb.query(KeyConditionExpression=Key("pk").eq(f"PATIENT#{phone}") & Key("sk").begins_with("FOLLOWUP#"))
    return clean(r["Items"])


def set_visit_feedback(phone: str, kind: str):
    for v in visits(phone, limit=3):
        if v["date"] == today() and v.get("status") == "done":
            ddb.update_item(Key={"pk": v["pk"], "sk": v["sk"]}, UpdateExpression="SET feedback = :f",
                            ExpressionAttributeValues={":f": kind})
            return


def _first_line_rx(rx: str) -> str:
    """'💊 Prescription\n1. Adapalene 0.1% gel at night…' -> 'Adapalene 0.1% gel at night…'"""
    import re as _re
    for ln in (rx or "").splitlines():
        s = _re.sub(r"[*_💊]", "", ln).strip()
        if s and not s.lower().startswith(("prescription", "how to use")) and "·" not in s[:40]:
            return _re.sub(r"^\d+[.)]\s*", "", s)[:80]
    return ""


def patient_history(phone: str, who: str | None = None, family: bool | None = None) -> dict:
    """Everything the clinic knows about a patient, assembled for the doctor / agent / patient.
    `who` + `family` narrow visits to one person when several family members share a phone."""
    prof = patient(phone)
    allv = visits(phone)
    if who is not None:
        w = who.strip().lower()
        mine = [v for v in allv if (v.get("who", "").strip().lower() == w) or (not family and not v.get("family"))]
    else:
        mine = allv
    done = [v for v in mine if v.get("status") == "done"]
    missed = [v for v in mine if v.get("status") == "no_show"]
    pk = packages(phone)
    fus = sorted(followups(phone), key=lambda f: f.get("due", ""))
    upcoming = [f for f in fus if f.get("status") == "pending" and f.get("due", "") >= today()]
    concerns, rx = [], None
    for v in done:
        comp = (v.get("intake") or {}).get("complaint") or (v.get("service") if v.get("queue") != "LASER" else "") or ""
        if comp and comp.lower() not in [x.lower() for x in concerns]:
            concerns.append(comp)
        if not rx and v.get("rx_text"):
            rx = {"date": v["date"], "text": v["rx_text"]}
    allergies = next(((v.get("intake") or {}).get("allergies") for v in done
                      if (v.get("intake") or {}).get("allergies")
                      and not str((v.get("intake") or {}).get("allergies")).lower().startswith(("no", "none", "nahi"))), "")
    fb = [v.get("feedback") for v in done if v.get("feedback")]
    waits = [v["wait_min"] for v in done if v.get("wait_min")]
    return {
        "name": who or prof.get("name") or prof.get("wa_name") or "Patient",
        "visit_count": len(done), "no_shows": len(missed), "returning": len(done) > 0,
        "first_visit": done[-1]["date"] if done else None, "last_visit": done[0] if done else None,
        "visits": mine, "concerns": concerns[:6], "allergies": allergies, "last_rx": rx,
        "packages": [{"name": p["sk"].split("#", 1)[1], "used": int(p.get("used", 0)), "total": int(p.get("total", 0)),
                      "next_due": p.get("next_due"), "queue": p.get("queue", "LASER")} for p in pk],
        "followups": fus, "next_followup": upcoming[0] if upcoming else None,
        "photo_history": prof.get("photo_history") or [],
        "feedback": {"good": fb.count("good"), "ok": fb.count("ok"), "bad": fb.count("bad")},
        "avg_wait_min": round(sum(waits) / len(waits)) if waits else None,
        "family": prof.get("family") or [], "email": bool(prof.get("email")),
    }


def history_brief(h: dict) -> dict:
    """Compact one-glance context for a queue row / the agent prompt."""
    lv = h.get("last_visit") or {}
    done = [v for v in h.get("visits", []) if v.get("status") == "done"]
    lc = next(((v.get("intake") or {}).get("complaint") for v in done if (v.get("intake") or {}).get("complaint")), None)
    return {
        "visit_no": h["visit_count"] + 1, "returning": h["returning"], "since": h.get("first_visit"),
        "last_date": lv.get("date"), "last_service": lv.get("service"),
        "last_concern": lc or lv.get("service"),
        "last_rx": _first_line_rx((h.get("last_rx") or {}).get("text", "")),
        "last_rx_date": (h.get("last_rx") or {}).get("date"), "allergies": h.get("allergies"),
        "no_shows": h.get("no_shows", 0),
        "packages": [f"{p['name']} {p['used']}/{p['total']}" for p in h.get("packages", []) if p["used"] < p["total"]],
    }


# ---------------------------------------------------------------- richer WhatsApp messages
def wa_reaction(to: str, message_id: str, emoji: str = "👍", phone_id: str | None = None):
    try:
        wa_send({"to": to, "type": "reaction", "reaction": {"message_id": message_id, "emoji": emoji}}, phone_id)
    except Exception:
        log.exception("reaction failed")


def wa_upload_media(key: str, phone_id: str | None = None) -> str:
    """S3 object -> WhatsApp media id (AWS End User Messaging Social PostWhatsAppMessageMedia)."""
    r = sm.post_whatsapp_message_media(originationPhoneNumberId=phone_id or PHONE_ID,
                                       sourceS3File={"bucketName": MEDIA_BUCKET, "key": key})
    return r["mediaId"]


def wa_document(to: str, key: str, filename: str, caption: str = "", phone_id: str | None = None):
    mid = wa_upload_media(key, phone_id)
    return wa_send({"to": to, "type": "document",
                    "document": {"id": mid, "filename": filename, "caption": caption[:1024]}}, phone_id)


def wa_clinic_pin(to: str, cl: dict | None = None, phone_id: str | None = None):
    cl = cl if cl is not None else clinic()
    return wa_send({"to": to, "type": "location", "location": {
        "latitude": str(cl.get("lat", "28.6460")), "longitude": str(cl.get("lon", "77.3695")),
        "name": cl.get("name", "Clinic"), "address": cl.get("address", "")}}, phone_id)


def maps_url(cl: dict | None = None) -> str:
    cl = cl if cl is not None else clinic()
    return f"https://www.google.com/maps/dir/?api=1&destination={cl.get('lat', '28.6460')},{cl.get('lon', '77.3695')}"


def wa_cta(to: str, text: str, label: str, url: str, phone_id: str | None = None):
    return wa_send({"to": to, "type": "interactive", "interactive": {
        "type": "cta_url", "body": {"text": text[:1024]},
        "action": {"name": "cta_url", "parameters": {"display_text": label[:20], "url": url}}}}, phone_id)


def wa_contact_card(to: str, cl: dict | None = None, phone_id: str | None = None) -> bool:
    cl = cl if cl is not None else clinic()
    ph = cl.get("phone")
    if not ph:
        return False
    nm = cl.get("name", "Clinic")
    wa_send({"to": to, "type": "contacts", "contacts": [{
        "name": {"formatted_name": nm, "first_name": nm},
        "org": {"company": nm}, "phones": [{"phone": ph, "type": "WORK"}],
        "addresses": [{"street": cl.get("address", ""), "type": "WORK"}]}]}, phone_id)
    return True


def send_clinic_location(to: str, phone_id: str | None = None, intro: str = ""):
    """Pin + one-tap Google Maps directions (+ contact card to call, if the clinic phone is configured)."""
    cl = clinic()
    try:
        wa_clinic_pin(to, cl, phone_id)
        wa_cta(to, intro or f"📍 {cl.get('name', 'Clinic')}, {cl.get('address', '')}\nTap for turn-by-turn directions.",
               "🗺️ Directions", maps_url(cl), phone_id)
        wa_contact_card(to, cl, phone_id)
    except Exception:
        log.exception("clinic location send failed")


# ---------------------------------------------------------------- Amazon Transcribe (voice notes)
_tr = None


def transcribe_s3(key: str, fmt: str = "ogg", languages: tuple = ("hi-IN", "en-IN"), wait_s: int = 75) -> dict:
    """Batch-transcribe an S3 audio object, auto-identifying Hindi vs Indian English. Blocks until done."""
    global _tr
    _tr = _tr or boto3.client("transcribe")
    job = f"clinicflow-voice-{int(time.time() * 1000)}"
    _tr.start_transcription_job(TranscriptionJobName=job, MediaFormat=fmt,
                                Media={"MediaFileUri": f"s3://{MEDIA_BUCKET}/{key}"},
                                IdentifyLanguage=True, LanguageOptions=list(languages),
                                OutputBucketName=MEDIA_BUCKET, OutputKey=f"voice-text/{job}.json")
    t0 = time.time()
    while time.time() - t0 < wait_s:
        time.sleep(2)
        j = _tr.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
        st = j["TranscriptionJobStatus"]
        if st == "COMPLETED":
            s3c = boto3.client("s3")
            obj = s3c.get_object(Bucket=MEDIA_BUCKET, Key=f"voice-text/{job}.json")
            data = json.loads(obj["Body"].read())
            for k in (key, f"voice-text/{job}.json"):   # privacy: keep the text, not the recording
                try:
                    s3c.delete_object(Bucket=MEDIA_BUCKET, Key=k)
                except Exception:
                    log.exception("voice cleanup failed")
            text = " ".join(x.get("transcript", "") for x in data.get("results", {}).get("transcripts", [])).strip()
            return {"ok": bool(text), "text": text, "language": j.get("LanguageCode", ""), "secs": round(time.time() - t0)}
        if st == "FAILED":
            return {"ok": False, "error": j.get("FailureReason", "")}
    return {"ok": False, "error": "timeout"}


# ---------------------------------------------------------------- printable prescription PDF (no dependencies)
def _pdf_txt(s: str) -> str:
    import re as _re
    s = _re.sub(r"[*_`]", "", s or "")
    s = s.replace("•", "-").replace("–", "-").replace("—", "-").replace("’", "'").replace("“", '"').replace("”", '"').replace("…", "...")
    s = s.encode("latin-1", "ignore").decode("latin-1").strip()
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_rx_pdf(cl: dict, patient_name: str, label: str, date_str: str, rx_text: str, visit_no: int | None = None) -> bytes:
    """A clean one-page A4 prescription. Latin text only (standard PDF fonts); the WhatsApp text keeps Hindi."""
    import textwrap
    ops = []
    def text(x, y, s, size=11, bold=False, color=(0.1, 0.13, 0.11)):
        ops.append(f"BT {color[0]} {color[1]} {color[2]} rg /F{2 if bold else 1} {size} Tf {x} {y} Td ({s}) Tj ET")
    ops.append("0.06 0.32 0.2 rg 0 762 595 80 re f")                         # green header band
    text(40, 808, _pdf_txt(cl.get("name", "Clinic")), 20, True, (1, 1, 1))
    text(40, 788, _pdf_txt(cl.get("address", "")), 10, False, (0.9, 0.95, 0.92))
    text(40, 772, _pdf_txt(cl.get("hours", ""))[:90], 9, False, (0.85, 0.92, 0.88))
    text(40, 735, f"Patient: {_pdf_txt(patient_name)}", 12, True)
    text(330, 735, f"Date: {_pdf_txt(date_str)}", 11)
    text(40, 717, f"Token: {_pdf_txt(label)}" + (f"    Visit #{visit_no}" if visit_no else ""), 10, False, (0.35, 0.42, 0.38))
    ops.append("0.8 0.85 0.82 RG 1 w 40 705 m 555 705 l S")
    text(40, 672, "Rx", 28, True, (0.06, 0.32, 0.2))
    y = 645
    for raw in (rx_text or "").splitlines():
        ln = _pdf_txt(raw)
        if ln and not any(ch.isalnum() for ch in ln):   # e.g. a Hindi-only line (kept in the WhatsApp text)
            continue
        if not ln or ln.lower().replace("\\", "") in ("prescription",):
            if not ln:
                y -= 6
            continue
        head = raw.strip().startswith("*") and raw.strip().endswith("*")
        for part in textwrap.wrap(ln, 88) or [""]:
            if y < 90:
                break
            text(48, y, part, 12 if head else 11, head)
            y -= 17
    ops.append("0.8 0.85 0.82 RG 40 70 m 555 70 l S")
    text(40, 52, "As advised by your doctor. Call the clinic if you have side effects.", 9, False, (0.35, 0.42, 0.38))
    text(40, 38, "Sent via ClinicFlow on WhatsApp. Digitally generated; valid with the clinic's records.", 8, False, (0.5, 0.55, 0.52))
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)


def save_rx_pdf(phone: str, label: str, patient_name: str, rx_text: str, visit_no: int | None = None) -> str:
    cl = clinic()
    key = f"rx-pdf/{today()}/{phone}/{label}.pdf"
    pdf = make_rx_pdf(cl, patient_name, label, now_ist().strftime("%d %b %Y"), rx_text, visit_no)
    boto3.client("s3").put_object(Bucket=MEDIA_BUCKET, Key=key, Body=pdf, ContentType="application/pdf")
    return key


# ---------------------------------------------------------------- privacy: consent, opt-out, erasure (DPDP Act 2023)
PRIVACY_TEXT = ("🔒 *Your privacy*\n{name} uses ClinicFlow to run its WhatsApp front desk. We store your name, phone number, "
                "visit details, photos and prescriptions you share only to manage your visits, and we never sell or share them.\n"
                "You can stop reminders or *delete all your data* anytime: just type *delete my data*.")


def delete_patient_data(phone: str) -> dict:
    """Right to erasure: patient profile, visits, packages, follow-ups, chat history, photos, Rx PDFs; today's tokens anonymised."""
    s3 = boto3.client("s3")
    prof = patient(phone)
    keys = [h["key"] for h in (prof.get("photo_history") or []) if h.get("key")]
    items = clean(ddb.query(KeyConditionExpression=Key("pk").eq(f"PATIENT#{phone}"))["Items"])
    for it in items:
        keys += [k for k in it.get("photos", []) if isinstance(k, str)]
        if it.get("rx_pdf"):
            keys.append(it["rx_pdf"])
    removed_files = 0
    for k in set(keys):
        try:
            s3.delete_object(Bucket=MEDIA_BUCKET, Key=k)
            removed_files += 1
        except Exception:
            log.exception("delete object failed")
    for it in items:
        ddb.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
    anon = 0
    for t in tokens():
        if t["phone"] == phone:
            set_token(t, status="cancelled" if t["status"] in ("waiting", "in_consult") else t["status"],
                      name="(deleted)", phone=f"deleted-{abs(hash(phone)) % 100000}", intake={}, intake_summary="",
                      rx_text="", photos=[])
            anon += 1
    bump("data_deletions")
    return {"records": len(items), "files": removed_files, "tokens_anonymised": anon}


def maybe_send_directions(phone: str, phone_id: str | None = None):
    """Once a day, with the leave-now nudge: clinic pin + Google Maps directions button."""
    try:
        if patient(phone).get("dir_sent") == today():
            return
        upsert_patient(phone, dir_sent=today())
        send_clinic_location(phone, phone_id, "🗺️ Directions to the clinic, one tap to start navigation.")
    except Exception:
        log.exception("directions failed")
