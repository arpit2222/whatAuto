"""ClinicFlow agent: Amazon Bedrock Converse API with tool use.

The model decides which clinic action to take (book token, check queue,
cancel, packages, save travel time / email). Tools read and write DynamoDB
and trigger SES emails, so the conversation actually changes clinic state.
"""
import json
import os

import time

import boto3
from botocore.config import Config

import common as c

MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-pro-v1:0")
BEDROCK_ROLE_ARN = os.environ.get("BEDROCK_ROLE_ARN", "")      # optional cross-account role for model calls
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
_BR_CFG = Config(retries={"max_attempts": 1}, read_timeout=25)
_brt_cache = {"client": None, "exp": 0.0}


def _bedrock():
    """Bedrock runtime client. If BEDROCK_ROLE_ARN is set, assume that role (e.g. in the partner's
    production account where Bedrock is enabled) with short-lived STS credentials, refreshed hourly."""
    if not BEDROCK_ROLE_ARN:
        if _brt_cache["client"] is None:
            _brt_cache["client"] = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=_BR_CFG)
        return _brt_cache["client"]
    if _brt_cache["client"] is None or time.time() > _brt_cache["exp"]:
        cr = boto3.client("sts").assume_role(RoleArn=BEDROCK_ROLE_ARN, RoleSessionName="clinicflow-agent",
                                             DurationSeconds=3600)["Credentials"]
        _brt_cache["client"] = boto3.client(
            "bedrock-runtime", region_name=BEDROCK_REGION, config=_BR_CFG,
            aws_access_key_id=cr["AccessKeyId"], aws_secret_access_key=cr["SecretAccessKey"],
            aws_session_token=cr["SessionToken"])
        _brt_cache["exp"] = time.time() + 3000
    return _brt_cache["client"]
_BEDROCK_DOWN_UNTIL = 0.0  # skip Bedrock for 10 min after an availability error

SYSTEM = """You are ClinicFlow, the WhatsApp front desk for {name}, a skin & laser clinic in {address}.
Clinic hours: {hours}. Today is {today}, time now {now} IST.
Two queues run in parallel: OPD (doctor consultation, ~{opd} min each) and LASER (laser/procedures, ~{laser} min each).
Services: {services}.

You help patients:
- book for family members too (book_token with for_family_member=true and THEIR name), e.g. "mummy ke liye bhi token"
- take a token for today in the right queue (book_token: queue OPD for consults/acne/skin problems, LASER for laser/peel/PRP/procedures)
- see live position / ETA (queue_status), say they are running late (running_late) or have arrived (im_here), cancel (cancel_token)
- check treatment packages and book the next session (my_packages, book_package_session)
- laser/procedures can be booked at a fixed time: show free times (laser_slots) and book one (book_laser_slot); if a deposit_link is returned, share it

- see their own history at this clinic: past visits, last prescription, package progress, next follow-up (my_history)
- RETURNING PATIENTS: the patient history below tells you their visit count, last concern and packages. Greet returning
  patients by name ("Welcome back"), and when booking, ask if it's a follow-up for their last concern; offer the package session
  if one is due. Never read out prescriptions unless they ask for them.
- save travel time so we can ping them "leave now" (set_travel_time); they can also share their WhatsApp location
- save an email for confirmations and visit summaries (save_email)
- run a quick PRE-VISIT INTAKE for the doctor right after booking: ask ONE short question at a time
  (1) main problem, (2) since when, (3) any cream/medicine already tried, (4) any allergies.
  Then call save_intake with what they said (keep their words, translate to English) and invite an optional photo.
  Skip intake if save_intake was already called for today's token.

Notes: patients may send WhatsApp voice notes; you receive the Amazon Transcribe transcript as their message (it may have small
errors, so confirm names/times if unclear). Clinic address/directions, privacy and "delete my data" are handled by the system.

Rules:
- Reply in the patient's language and script (English, Hindi or Hinglish), warm and short: max 3-4 lines, WhatsApp style, at most one emoji.
- ALWAYS use tools for facts about tokens, queue, packages. Never invent token numbers or times. Tokens look like O7 or L2.
- Ask for the patient's name before booking if you don't know it. After booking, ask how far they are or to share location if unknown, then do the intake.
- You are not a doctor: no diagnosis or medicine advice. For medical questions say the doctor will review at the visit; for emergencies call 112 / go to the nearest hospital.
- If a patient sends a photo, it is attached to their token for the doctor; acknowledge it.
Known patient profile: {profile}
Patient history at this clinic: {history}"""

TOOLS = [
    {"name": "book_token", "description": "Book a token for today for this patient in the OPD or LASER queue.",
     "schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Patient name"},
         "queue": {"type": "string", "enum": ["OPD", "LASER"], "description": "OPD = doctor consult, LASER = laser/procedure"},
         "for_family_member": {"type": "boolean", "description": "true if booking for someone else (mother, child, spouse) from this phone"},
         "service": {"type": "string", "description": "Reason / service, e.g. acne consult, laser hair removal"}},
         "required": ["name"]}},
    {"name": "queue_status", "description": "Get the patient's current token, people ahead and ETA (or queue lengths if no token).",
     "schema": {"type": "object", "properties": {}}},
    {"name": "running_late", "description": "Patient is running late: move their token back two places so they don't lose it.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "im_here", "description": "Patient has arrived at the clinic (check-in).",
     "schema": {"type": "object", "properties": {}}},
    {"name": "cancel_token", "description": "Cancel the patient's active token for today.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "my_packages", "description": "List the patient's treatment packages with sessions used/left and next due date.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "book_package_session", "description": "Book a token today for the next session of a package.",
     "schema": {"type": "object", "properties": {"package": {"type": "string"}}, "required": ["package"]}},
    {"name": "set_travel_time", "description": "Save how many minutes it takes the patient to reach the clinic.",
     "schema": {"type": "object", "properties": {"minutes": {"type": "integer"}}, "required": ["minutes"]}},
    {"name": "save_intake", "description": "Save the pre-visit intake for today's token so the doctor sees a summary card.",
     "schema": {"type": "object", "properties": {
         "complaint": {"type": "string", "description": "Main problem in the patient's words, e.g. acne on cheeks"},
         "duration": {"type": "string", "description": "Since when, e.g. 3 months"},
         "tried": {"type": "string", "description": "Creams/medicines already tried, or 'nothing'"},
         "allergies": {"type": "string", "description": "Known allergies, or 'none'"},
         "notes": {"type": "string", "description": "Anything else relevant (optional)"}},
         "required": ["complaint"]}},
    {"name": "laser_slots", "description": "List today's free laser/procedure appointment slots (HH:MM).",
     "schema": {"type": "object", "properties": {}}},
    {"name": "book_laser_slot", "description": "Book a specific laser/procedure appointment slot today.",
     "schema": {"type": "object", "properties": {"time": {"type": "string", "description": "HH:MM from laser_slots"},
                                                 "name": {"type": "string"}}, "required": ["time"]}},
    {"name": "my_history", "description": "The patient's own history at this clinic: number of visits, past visits with concerns, last prescription text, packages progress and next follow-up.",
     "schema": {"type": "object", "properties": {"include_prescription": {"type": "boolean", "description": "true if they asked for their last prescription"}}}},
    {"name": "save_email", "description": "Save the patient's email for confirmations.",
     "schema": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}},
]
TOOL_CONFIG = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                       "inputSchema": {"json": t["schema"]}}} for t in TOOLS]}


# ---------------------------------------------------------------- tool impls
def _book(phone: str, name: str, service: str = "", queue: str = "OPD", package: str | None = None,
          family: bool = False, slot: str | None = None) -> dict:
    mine = c.active_tokens(phone)
    existing = next((t for t in mine if (t.get("name") or "").strip().lower() == (name or "").strip().lower()), None)
    if existing is None and mine and not family and not phone.startswith("walkin"):
        prof_name = (c.patient(phone).get("name") or "").strip().lower()
        if (name or "").strip().lower() in ("", "patient", prof_name):
            existing = mine[0]
    if existing:
        ahead, eta = c.eta_minutes(existing)
        return {"already_booked": True, "token": existing["label"], "queue": existing["queue"],
                "people_ahead": ahead, "eta_min": eta}
    q = c.norm_queue(queue)
    cl = c.clinic()
    cfg = c.queue_cfg(q, cl)
    cnt = c.ddb.update_item(
        Key={"pk": c.day_pk(), "sk": f"COUNTER#{q}"},
        UpdateExpression="ADD last_token :one SET now_serving = if_not_exists(now_serving, :zero)",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    num = int(cnt["last_token"])
    label = f"{cfg.get('prefix', q[0])}{num}"
    # LASER runs by clock time: booked slots at their slot time, walk-ins at the time they joined
    pos = (c.minute_of_day(slot) if slot else c.minute_of_day()) if q == "LASER" else num
    item = {"pk": c.day_pk(), "sk": f"TOKEN#{q}#{num:03d}", "queue": q, "token": num, "label": label, "pos": pos,
            "phone": phone, "name": name, "service": service or cfg["label"], "status": "waiting", "nudged": False,
            "created": c.iso_now(), "photos": []}
    if package:
        item["package"] = package
    if slot:
        item["slot"] = slot
    if mine and existing is None:          # booking for a family member from the same phone
        item["family"] = True
    c.ddb.put_item(Item=item)
    if item.get("family"):
        fam = c.patient(phone).get("family") or []
        if name not in fam:
            c.upsert_patient(phone, family=(fam + [name])[-6:])
        c.bump("family_bookings")
    else:
        c.upsert_patient(phone, name=name)
    c.bump("bookings")
    ahead, eta = c.eta_minutes(item)
    prof = c.patient(phone)
    if prof.get("email"):
        link = c.wa_link("status")
        c.email(prof["email"], f"Your token {label} at {cl.get('name')}",
                f"Hi {name},\n\nYour token for today is {label} ({cfg['label']}).\n"
                f"People ahead: {ahead}. Estimated wait: ~{eta} min.\n"
                f"We'll WhatsApp you when it's time to leave.\n\nCheck your live position on WhatsApp: {link}\n\n"
                f"{cl.get('name')}, {cl.get('address')}",
                f"<div style='font-family:Arial,sans-serif;max-width:480px'><h2 style='color:#0f5132'>Token {label}</h2>"
                f"<p>Hi {name}, your token for today at <b>{cl.get('name')}</b> is <b>{label}</b> ({cfg['label']}).</p>"
                f"<p><b>{ahead}</b> ahead of you · about <b>{eta} min</b> wait.</p>"
                f"<p>We'll WhatsApp you when it's time to leave, so you can wait at home.</p>"
                f"<p><a href='{link}' style='background:#25D366;color:#fff;padding:12px 18px;border-radius:8px;"
                f"text-decoration:none;display:inline-block'>Check my queue on WhatsApp</a></p>"
                f"<p style='color:#888;font-size:12px'>{cl.get('address')}</p></div>")
    return {"token": label, "queue": q, "people_ahead": ahead, "eta_min": eta,
            "travel_min_known": bool(prof.get("travel_min")), "email_sent": bool(prof.get("email"))}


def run_tool(phone: str, name: str, args: dict) -> dict:
    if name == "book_token":
        return _book(phone, args.get("name") or c.patient(phone).get("name", "Patient"),
                     args.get("service", ""), args.get("queue", "OPD"), family=bool(args.get("for_family_member")))
    if name == "queue_status":
        c.bump("status_checks")   # each one is a phone call the receptionist didn't take
        t = c.active_token(phone)
        if not t:
            return {"has_token": False, "queues": {q: {"waiting": len(c.waiting(q=q)),
                                                       "now_serving": c.counter(q).get("now_serving_label", "")}
                                                   for q in c.QUEUES}}
        ahead, eta = c.eta_minutes(t)
        others = []
        for o in c.active_tokens(phone)[1:]:
            oa, oe = c.eta_minutes(o)
            others.append({"token": o["label"], "name": o["name"], "queue": o["queue"], "status": o["status"],
                           "people_ahead": oa, "eta_min": oe})
        return {"has_token": True, "token": t["label"], "name": t["name"], "queue": t["queue"], "status": t["status"],
                "people_ahead": ahead, "eta_min": eta, "now_serving": c.counter(t["queue"]).get("now_serving_label", ""),
                "family_tokens": others}
    if name == "running_late":
        t = c.active_token(phone)
        if not t:
            return {"ok": False, "reason": "no active token"}
        behind = [w for w in c.waiting(q=t["queue"]) if w["pos"] > t["pos"]][:2]
        new_pos = (behind[-1]["pos"] + 0.5) if behind else t["pos"]
        c.set_token(t, pos=float(new_pos), status="waiting", late=True, nudged=False)
        c.bump("late")
        t["pos"] = new_pos
        ahead, eta = c.eta_minutes(t)
        return {"ok": True, "token": t["label"], "moved_back_by": len(behind), "people_ahead": ahead, "eta_min": eta}
    if name == "im_here":
        t = c.active_token(phone)
        if not t:
            return {"ok": False, "reason": "no active token"}
        c.set_token(t, arrived=True, arrived_at=c.iso_now())
        ahead, eta = c.eta_minutes(t)
        return {"ok": True, "token": t["label"], "people_ahead": ahead, "eta_min": eta}
    if name == "cancel_token":
        t = c.active_token(phone)
        if not t:
            return {"cancelled": False, "reason": "no active token"}
        c.set_token(t, status="cancelled", done_at=c.iso_now())
        return {"cancelled": True, "token": t["label"]}
    if name == "my_packages":
        return {"packages": [{"package": p["sk"].split("#", 1)[1], "total": p["total"], "used": p["used"],
                              "left": p["total"] - p["used"], "next_due": p.get("next_due")}
                             for p in c.packages(phone)]}
    if name == "book_package_session":
        pk = next((p for p in c.packages(phone) if args["package"].lower() in p["sk"].lower()), None)
        if not pk:
            return {"booked": False, "reason": "package not found", "packages": [p["sk"] for p in c.packages(phone)]}
        pname = pk["sk"].split("#", 1)[1]
        res = _book(phone, c.patient(phone).get("name", "Patient"), f"{pname} session",
                    queue=pk.get("queue", "LASER"), package=pname)
        if not res.get("already_booked"):
            c.bump("package_bookings")
        return {"booked": True, "package": pname, "session_number": pk["used"] + 1, **res}
    if name == "set_travel_time":
        c.upsert_patient(phone, travel_min=int(args["minutes"]), travel_source="patient", travel_at=c.iso_now())
        return {"saved": True, "travel_min": int(args["minutes"])}
    if name == "save_email":
        c.upsert_patient(phone, email=args["email"].strip())
        return {"saved": True}
    if name == "my_history":
        h = c.patient_history(phone)
        c.bump("history_views")
        out = {"visit_count": h["visit_count"], "since": h["first_visit"], "no_shows": h["no_shows"],
               "visits": [{k: v.get(k) for k in ("date", "queue", "who", "status", "service", "intake_summary", "followup_due")}
                          for v in h["visits"][:6]],
               "packages": h["packages"], "next_followup": (h["next_followup"] or {}).get("due")}
        if args.get("include_prescription") and h["last_rx"]:
            out["last_prescription"] = h["last_rx"]
        return out
    if name == "laser_slots":
        return {"free_slots": c.free_slots()}
    if name == "book_laser_slot":
        tm = str(args["time"]).strip()[:5]
        if tm not in c.free_slots(limit=50):
            return {"booked": False, "reason": "slot not available", "free_slots": c.free_slots()}
        try:
            c.ddb.put_item(Item={"pk": c.day_pk(), "sk": f"SLOT#{tm}", "phone": phone, "status": "booked"},
                           ConditionExpression="attribute_not_exists(sk)")
        except Exception:
            return {"booked": False, "reason": "just taken", "free_slots": c.free_slots()}
        nm = args.get("name") or c.patient(phone).get("name") or "Patient"
        mine = [t for t in c.active_tokens(phone) if t["queue"] == "LASER"]
        if mine:   # move an existing laser token onto the slot
            c.set_token(mine[0], slot=tm, pos=float(c.minute_of_day(tm)))
            res = {"token": mine[0]["label"], "queue": "LASER"}
        else:
            res = _book(phone, nm, "Laser appointment", queue="LASER", slot=tm,
                        family=bool(nm and nm.lower() != (c.patient(phone).get("name") or "").lower()))
        cl = c.clinic()
        dep = int(cl.get("laser_deposit", 200))
        link = c.upi_link(dep, f"Laser slot {tm} deposit")
        tok = next((t for t in c.active_tokens(phone) if t.get("slot") == tm or t["label"] == res.get("token")), None)
        if tok:
            c.set_token(tok, deposit_status="requested" if link else "none", deposit_amount=dep)
        c.bump("slot_bookings")
        return {"booked": True, "slot": tm, "token": res.get("token"), "arrive_by": f"{tm} (please arrive 10 min early)",
                "deposit_link": link, "deposit_amount": dep if link else 0}
    if name == "save_intake":
        t = c.active_token(phone)
        if not t:
            return {"saved": False, "reason": "no active token"}
        intake = {k: str(args.get(k, "")).strip()[:200] for k in ("complaint", "duration", "tried", "allergies", "notes")
                  if str(args.get(k, "")).strip()}
        c.set_token(t, intake=intake, intake_summary=intake_summary(intake), intake_at=c.iso_now())
        c.bump("intakes")
        return {"saved": True, "token": t["label"], "summary": intake_summary(intake)}
    return {"error": f"unknown tool {name}"}


def intake_summary(i: dict) -> str:
    parts = [i.get("complaint", "")]
    if i.get("duration"):
        parts.append(i["duration"])
    if i.get("tried"):
        parts.append("tried " + i["tried"] if not i["tried"].lower().startswith(("no", "nothing", "kuch nahi")) else "nothing tried")
    if i.get("allergies"):
        parts.append("allergies: " + i["allergies"])
    if i.get("notes"):
        parts.append(i["notes"])
    return " · ".join(p for p in parts if p)


INTAKE_Q = [
    ("complaint", "📝 Quick 4 questions so the doctor is ready for you.\n1/4: What's the main problem? (e.g. acne, rash, hair fall, pigmentation)"),
    ("duration", "2/4: Since when? (e.g. 2 weeks, 3 months)"),
    ("tried", "3/4: Any cream or medicine already tried? (or *nothing*)"),
    ("allergies", "4/4: Any allergies to medicines? (or *none*)"),
]


def intake_start(phone: str) -> str | None:
    """Begin the deterministic intake if today's token has none yet."""
    t = c.active_token(phone)
    if not t or t.get("intake") or t.get("intake_skipped"):
        return None
    c.upsert_patient(phone, pending="intake:0", intake_draft="{}")
    return INTAKE_Q[0][1]


def intake_answer(phone: str, text: str, step: int) -> str:
    prof = c.patient(phone)
    draft = json.loads(prof.get("intake_draft") or "{}")
    draft[INTAKE_Q[step][0]] = text.strip()[:200]
    if step + 1 < len(INTAKE_Q):
        c.upsert_patient(phone, pending=f"intake:{step + 1}", intake_draft=json.dumps(draft))
        return INTAKE_Q[step + 1][1]
    c.upsert_patient(phone, pending="none", intake_draft="{}")
    r = run_tool(phone, "save_intake", draft)
    if not r.get("saved"):
        return "Thanks! Take a token first and I'll share this with the doctor."
    return (f"Thank you 🙏 The doctor will see this before you walk in:\n_{r['summary']}_\n"
            "📷 Optional: send a clear photo of the area.")


# ---------------------------------------------------------------- fallback
import re

def fallback(phone: str, text: str) -> str:
    reply = _fallback(phone, text)
    try:
        c.save_turn(phone, text, reply, c.load_conv(phone))
    except Exception:
        c.log.exception("save_turn failed")
    return reply


def _q_from(t: str) -> str:
    return "LASER" if re.search(r"laser|peel|prp|procedure|hair removal|लेज़र", t) else "OPD"


DEVANAGARI_HINTS = {"टोकन": " token ", "लेज़र": " laser ", "लेजर": " laser ", "कैंसल": " cancel ", "रद्द": " cancel ",
                    "स्टेटस": " status ", "कितना": " kitna ", "कब": " kab ", "पैकेज": " package ", "देर": " der ",
                    "पहुँच": " pahunch ", "पहुंच": " pahunch ", "डॉक्टर": " doctor ", "डॉ.": " doctor ", "अपॉइंटमेंट": " appointment ",
                    "चाहिए": " chahiye ", "प्रिस्क्रिप्शन": " prescription ", "पर्ची": " parchi ", "दवाई": " dawai "}


def _fallback(phone: str, text: str) -> str:
    """Deterministic intent router used when Bedrock is unavailable.
    Calls the same tools as the agent, so clinic state stays consistent."""
    t = text.lower().strip()
    for hi, en in DEVANAGARI_HINTS.items():   # Hindi voice notes arrive in Devanagari from Amazon Transcribe
        t = t.replace(hi, en)
    prof = c.patient(phone)
    pending = prof.get("pending", "none")
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    mins = re.search(r"(\d{1,3})\s*(min|minute|mins|मिनट)", t)
    if em:
        run_tool(phone, "save_email", {"email": em.group(0)})
        return f"Saved ✅ We'll email confirmations to {em.group(0)}."
    if re.search(r"late|der ho|delay|deri", t):
        r = run_tool(phone, "running_late", {})
        if not r["ok"]:
            return "You don't have an active token today. Reply *token* to join the queue."
        return (f"No problem 👍 Token {r['token']} moved back {r['moved_back_by']} place(s) so you keep it. "
                f"Now {r['people_ahead']} ahead, ~{r['eta_min']} min.")
    if re.search(r"i.?m here|arrived|pahunch|reached|here now", t):
        r = run_tool(phone, "im_here", {})
        if not r["ok"]:
            return "Welcome! You don't have a token yet. Reply *token* to join the queue."
        return f"Checked in ✅ Token {r['token']}. {r['people_ahead']} ahead of you, ~{r['eta_min']} min. Please have a seat."
    if mins or (pending == "travel" and t.isdigit()):
        m = int(mins.group(1) if mins else t)
        run_tool(phone, "set_travel_time", {"minutes": m})
        c.upsert_patient(phone, pending="none")
        nxt = intake_start(phone)
        return f"Got it – {m} min away. I'll ping you when it's time to leave 🚶" + (f"\n\n{nxt}" if nxt else "")
    if pending.startswith("intake:") and not re.search(
            r"^(status|cancel|menu|hi|hello|token|laser|package|location|refresh)\b|running late|i.?m here|arrived", t):
        if t in ("skip", "skip intake", "later"):
            c.upsert_patient(phone, pending="none")
            tok = c.active_token(phone)
            if tok:
                c.set_token(tok, intake_skipped=True)
            return "No problem, skipped. The doctor will ask at the visit."
        return intake_answer(phone, text, int(pending.split(":")[1]))
    if pending.startswith("name") and len(t) < 40 and "?" not in t and \
            not re.search(r"token|queue|status|cancel|package|menu|hi$|hello|kitna|kab|time|wait|late|here|laser|opd", t):
        c.upsert_patient(phone, name=text.strip().title(), pending="none")
        prof["name"] = text.strip().title()
        t = "token laser" if pending == "name:LASER" else "token opd"
    fam = re.search(r"(for|ke liye|ka|ki)\s*(bhi\s*)?(my\s+)?(mother|mom|mummy|maa|mumma|papa|father|dad|son|beta|daughter|beti|wife|husband|bhai|brother|sister|behen|child|bachche|bacche|family)|"
                    r"(mother|mom|mummy|maa|papa|father|son|beta|daughter|beti|wife|husband|bhai|brother|sister|behen|child)\s*(ke liye|ka|ki|for)", t)
    if fam and re.search(r"token|book|appointment|chahiye|laser|opd|doctor|bhi", t):
        q = _q_from(t)
        c.upsert_patient(phone, pending=f"famname:{q}")
        return "Sure 👨‍👩‍👧 What's their name? (I'll book a separate token for them)"
    if pending.startswith("famname:") and len(t) < 40 and "?" not in t:
        q = pending.split(":")[1]
        c.upsert_patient(phone, pending="none")
        r = run_tool(phone, "book_token", {"name": text.strip().title(), "queue": q, "for_family_member": True})
        return (f"Booked ✅ Token *{r['token']}* for {text.strip().title()} ({'Laser' if r['queue'] == 'LASER' else 'OPD'}). "
                f"{r['people_ahead']} ahead, ~{r['eta_min']} min. I'll message you on this number for them too.")
    if re.search(r"\bslot\b|appointment time|time slot|samay", t) or t.startswith("__slot_"):
        if t.startswith("__slot_"):
            r = run_tool(phone, "book_laser_slot", {"time": t.replace("__slot_", "").strip("_"),
                                                     "name": prof.get("name") or prof.get("wa_name")})
            if not r.get("booked"):
                return "Sorry, that slot just went. Type *slot* to see what's free."
            msg = f"Booked ✅ Laser slot *{r['slot']}*, token {r['token']}. Please arrive 10 min early; I'll tell you when to leave."
            if r.get("deposit_link"):
                msg += f"\n💳 Please pay the ₹{r['deposit_amount']} slot deposit by UPI to confirm: {r['deposit_link']}"
            return msg
        return "__SLOT_LIST__"
    if re.search(r"cancel|रद्द", t):
        r = run_tool(phone, "cancel_token", {})
        return f"Token {r['token']} cancelled." if r["cancelled"] else "You don't have an active token today."
    if re.search(r"histor|my visits?|past visit|previous visit|pichl|purani|records?|prescription|parchi|parcha|dawai|\brx\b", t):
        c.bump("history_views")
        return history_text(phone, with_rx=bool(re.search(r"prescription|parchi|parcha|dawai|\brx\b", t)))
    if t in ("book", "book today", "yes") and c.packages(phone):  # reply to session_reminder template
        t = "book session"
    if re.search(r"package|session|पैकेज", t):
        if re.search(r"book|today|token|yes", t):
            pk = c.packages(phone)
            if pk:
                r = run_tool(phone, "book_package_session", {"package": pk[0]["sk"].split("#", 1)[1]})
                if r.get("booked"):
                    return (f"Booked ✅ {r['package']} session {r['session_number']} – token {r['token']} (laser queue). "
                            f"{r['people_ahead']} ahead, ~{r['eta_min']} min.")
        r = run_tool(phone, "my_packages", {})["packages"]
        if not r:
            return "You don't have any active packages. Ask at the front desk about laser, peel or PRP packages."
        lines = [f"• {p['package']}: {p['used']}/{p['total']} done, next due {p['next_due']}" for p in r]
        return "Your packages:\n" + "\n".join(lines) + "\nReply *book session* to take a laser token for today."
    if re.search(r"status|queue|kitna|kab|wait|eta|position|number|turn|refresh", t):
        r = run_tool(phone, "queue_status", {})
        if not r["has_token"]:
            qs = r["queues"]
            return (f"No token yet. OPD: {qs['OPD']['waiting']} waiting · Laser: {qs['LASER']['waiting']} waiting.\n"
                    "Reply *token* (doctor) or *laser* to join.")
        state = "You're with the doctor now 🩺" if r["status"] == "in_consult" else \
            f"{r['people_ahead']} ahead of you, ~{r['eta_min']} min."
        out = f"Token {r['token']} ({'Laser' if r['queue'] == 'LASER' else 'OPD'}): {state} Now serving {r['now_serving'] or '–'}."
        for o in r.get("family_tokens", []):
            out += f"\n👨‍👩‍👧 {o['name']}: token {o['token']}, {o['people_ahead']} ahead, ~{o['eta_min']} min."
        return out
    if re.search(r"token|book|appointment|appoint|visit|doctor|chahiye|lena|opd|laser|peel|prp", t):
        q = _q_from(t)
        if not prof.get("name"):
            c.upsert_patient(phone, pending=f"name:{q}")
            return f"Sure! {'Laser/procedure' if q == 'LASER' else 'Doctor consultation'} token coming up. What's the patient's name?"
        r = run_tool(phone, "book_token", {"name": prof["name"], "queue": q})
        msg = (f"You already have token {r['token']}." if r.get("already_booked")
               else f"Booked ✅ Token *{r['token']}* for {prof['name']} ({'Laser' if r['queue'] == 'LASER' else 'OPD'}).")
        msg += f" {r['people_ahead']} ahead, ~{r['eta_min']} min wait."
        if not prof.get("travel_min"):
            c.upsert_patient(phone, pending="travel")
            msg += "\nShare your location 📍 (or type minutes, e.g. *20 min*) and I'll tell you exactly when to leave."
        elif not r.get("already_booked"):
            nxt = intake_start(phone)
            if nxt:
                msg += f"\n\n{nxt}"
        return msg
    cl = c.clinic()
    return (f"Hi{(' ' + prof['name']) if prof.get('name') else ''}! 👋 Welcome to {cl.get('name', 'the clinic')}.\n"
            "I can book your token, tell you your live wait time, and ping you when it's time to leave. "
            "Tap *Menu* below or just type what you need.")


# ---------------------------------------------------------------- secondary model: Azure OpenAI (Foundry)
AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")      # e.g. https://x.openai.azure.com/openai/v1
AZURE_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_SECRET_ID = os.environ.get("AZURE_OPENAI_SECRET_ID", "clinicflow/azure-openai-key")
_azure_key = {"v": None, "t": 0.0}
LAST_ENGINE = {"name": "fallback-router"}


def _azure_api_key() -> str:
    if _azure_key["v"] is None or time.time() - _azure_key["t"] > 900:
        sec = boto3.client("secretsmanager").get_secret_value(SecretId=AZURE_SECRET_ID)["SecretString"].strip()
        if sec.startswith("{"):
            sec = json.loads(sec).get("api_key", "")
        _azure_key.update(v=sec, t=time.time())
    return _azure_key["v"]


def _azure_call(body: dict) -> dict:
    """Azure OpenAI Responses API (gpt-6 supports function tools only on /v1/responses)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{AZURE_ENDPOINT}/responses", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "api-key": _azure_api_key()})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        c.log.error("Azure HTTP %s: %s", e.code, e.read().decode()[:500])
        raise


def _wa_markdown(t: str) -> str:
    """Model markdown -> WhatsApp formatting (*bold*, no headings)."""
    t = t.replace("**", "*").replace("__", "_")
    return re.sub(r"^#+\s*", "", t, flags=re.M).strip()


def _respond_azure(phone: str, user_text: str) -> str:
    system, history = _system_and_history(phone)
    tools = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["schema"]}
             for t in TOOLS]
    base = {"model": AZURE_DEPLOYMENT, "tools": tools,
            "reasoning": {"effort": os.environ.get("AZURE_REASONING_EFFORT", "low")}}
    r = _azure_call({**base, "instructions": system,
                     "input": [{"role": m["role"], "content": m["text"]} for m in history] +
                              [{"role": "user", "content": user_text}]})
    reply = ""
    for _ in range(6):
        calls = [o for o in r.get("output", []) if o.get("type") == "function_call"]
        texts = [p_.get("text", "") for o in r.get("output", []) if o.get("type") == "message"
                 for p_ in (o.get("content") or []) if p_.get("type") in ("output_text", "text")]
        if not calls:
            reply = "".join(texts).strip()
            break
        outs = []
        for fc in calls:
            try:
                res = run_tool(phone, fc["name"], json.loads(fc.get("arguments") or "{}"))
            except Exception as e:
                c.log.exception("tool %s failed", fc.get("name"))
                res = {"error": str(e)}
            c.log.info("tool %s(%s) -> %s", fc.get("name"), fc.get("arguments"), res)
            outs.append({"type": "function_call_output", "call_id": fc["call_id"], "output": json.dumps(res)})
        r = _azure_call({**base, "previous_response_id": r["id"], "input": outs})
    reply = _wa_markdown(reply) or "Sorry, I had trouble there. Please try again 🙏"
    c.save_turn(phone, user_text, reply, history)
    return reply


# ---------------------------------------------------------------- agent loop
_AZURE_DOWN_UNTIL = 0.0


def respond(phone: str, user_text: str) -> str:
    """Amazon Bedrock first; Azure OpenAI as secondary model; deterministic router as last resort.
    All three drive the same tools, so clinic state stays consistent whichever engine answers."""
    global _BEDROCK_DOWN_UNTIL, _AZURE_DOWN_UNTIL
    if time.time() >= _BEDROCK_DOWN_UNTIL:
        try:
            r = _respond_llm(phone, user_text)
            LAST_ENGINE["name"] = f"bedrock:{MODEL_ID}"
            return r
        except Exception as e:
            if e.__class__.__name__ not in ("ThrottlingException", "AccessDeniedException", "ResourceNotFoundException",
                                            "ValidationException", "ServiceUnavailableException",
                                            "ModelNotReadyException", "ClientError"):
                c.log.exception("Bedrock turn failed; falling through")
            else:
                _BEDROCK_DOWN_UNTIL = time.time() + 600
            c.log.warning("Bedrock unavailable (%s)", e.__class__.__name__)
    if AZURE_ENDPOINT and AZURE_DEPLOYMENT and time.time() >= _AZURE_DOWN_UNTIL:
        try:
            r = _respond_azure(phone, user_text)
            LAST_ENGINE["name"] = f"azure-openai:{AZURE_DEPLOYMENT}"
            return r
        except Exception:
            _AZURE_DOWN_UNTIL = time.time() + 120
            c.log.exception("Azure OpenAI unavailable")
    LAST_ENGINE["name"] = "fallback-router"
    return fallback(phone, user_text)


def _history_line(phone: str) -> str:
    try:
        b = c.history_brief(c.patient_history(phone))
        if not b["returning"]:
            return "first-time patient (no past visits)"
        return json.dumps({k: b[k] for k in ("visit_no", "since", "last_date", "last_service", "last_concern", "last_rx_date",
                                             "packages", "no_shows") if b.get(k)})
    except Exception:
        c.log.exception("history line failed")
        return "unknown"


def history_text(phone: str, with_rx: bool = False) -> str:
    """WhatsApp-friendly history card (deterministic, also used by the fallback router)."""
    h = c.patient_history(phone)
    cl = c.clinic()
    if not h["visits"] and not h["packages"]:
        return (f"You haven't visited {cl.get('name', 'the clinic')} with this number yet 🙂 "
                "After your first visit, your visits and prescriptions will be saved here. Reply *token* to book.")
    def d(x):
        try:
            return c.dt.date.fromisoformat(x).strftime("%d %b")
        except Exception:
            return x or "–"
    lines = [f"🗂️ *Your visits at {cl.get('name', 'the clinic')}*"]
    if h["visit_count"]:
        lines[0] += f" ({h['visit_count']} since {d(h['first_visit'])})"
    for v in h["visits"][:5]:
        what = (v.get("intake") or {}).get("complaint") or v.get("service") or ("Laser" if v.get("queue") == "LASER" else "Consultation")
        tag = " ❌ missed" if v.get("status") == "no_show" else (" 💊" if v.get("rx_text") else "")
        who = f" ({v['who']})" if v.get("family") else ""
        lines.append(f"• {d(v['date'])}: {what[:48] + ('…' if len(what) > 48 else '')}{who}{tag}")
    for p in h["packages"]:
        bar = "▓" * p["used"] + "░" * max(0, p["total"] - p["used"])
        lines.append(f"📦 {p['name']}: {bar} {p['used']}/{p['total']}" + (f", next due {d(p['next_due'])}" if p["used"] < p["total"] else " ✅"))
    if h["next_followup"]:
        lines.append(f"🗓️ Next follow-up: {d(h['next_followup']['due'])}")
    if with_rx and h["last_rx"]:
        lines.append(f"\n💊 *Last prescription ({d(h['last_rx']['date'])})*\n{h['last_rx']['text'][:900]}")
    elif h["last_rx"]:
        lines.append("Reply *prescription* to see your last prescription again.")
    return "\n".join(lines)


def _system_and_history(phone: str):
    cl = c.clinic()
    prof = c.patient(phone)
    system = SYSTEM.format(
        name=cl.get("name", "the clinic"), address=cl.get("address", ""), hours=cl.get("hours", ""),
        opd=c.queue_cfg("OPD", cl)["service_min"], laser=c.queue_cfg("LASER", cl)["service_min"],
        services=", ".join(cl.get("services", [])),
        today=c.today(), now=c.now_ist().strftime("%H:%M"),
        profile=json.dumps({k: prof.get(k) for k in ("name", "email", "travel_min") if prof.get(k)}) or "new patient",
        history=_history_line(phone),
    )
    return system, c.load_conv(phone)


def _respond_llm(phone: str, user_text: str) -> str:
    system, history = _system_and_history(phone)
    messages = [{"role": m["role"], "content": [{"text": m["text"]}]} for m in history]
    messages.append({"role": "user", "content": [{"text": user_text}]})

    reply = "Sorry, I had trouble there. Please try again 🙏"
    for _ in range(6):
        out = _bedrock().converse(modelId=MODEL_ID, system=[{"text": system}], messages=messages,
                           toolConfig=TOOL_CONFIG, inferenceConfig={"maxTokens": 600, "temperature": 0.3})
        msg = out["output"]["message"]
        messages.append(msg)
        if out["stopReason"] != "tool_use":
            reply = "".join(b.get("text", "") for b in msg["content"]).strip() or reply
            break
        results = []
        for b in msg["content"]:
            if "toolUse" in b:
                tu = b["toolUse"]
                try:
                    res = run_tool(phone, tu["name"], tu.get("input") or {})
                except Exception as e:  # surface tool errors to the model
                    c.log.exception("tool %s failed", tu["name"])
                    res = {"error": str(e)}
                c.log.info("tool %s(%s) -> %s", tu["name"], tu.get("input"), res)
                results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": res}]}})
        messages.append({"role": "user", "content": results})

    # Nova sometimes wraps reasoning in <thinking> tags; strip it
    if "<thinking>" in reply:
        reply = reply.split("</thinking>")[-1].strip() or reply
    c.save_turn(phone, user_text, reply, history)
    return reply
