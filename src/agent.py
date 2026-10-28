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
brt = boto3.client("bedrock-runtime", config=Config(retries={"max_attempts": 1}, read_timeout=20))
_BEDROCK_DOWN_UNTIL = 0.0  # skip Bedrock for 10 min after an availability error

SYSTEM = """You are ClinicFlow, the WhatsApp front desk for {name}, a skin & laser clinic in {address}.
Clinic hours: {hours}. Today is {today}, time now {now} IST.
Two queues run in parallel: OPD (doctor consultation, ~{opd} min each) and LASER (laser/procedures, ~{laser} min each).
Services: {services}.

You help patients:
- take a token for today in the right queue (book_token: queue OPD for consults/acne/skin problems, LASER for laser/peel/PRP/procedures)
- see live position / ETA (queue_status), say they are running late (running_late) or have arrived (im_here), cancel (cancel_token)
- check treatment packages and book the next session (my_packages, book_package_session)
- save travel time so we can ping them "leave now" (set_travel_time); they can also share their WhatsApp location
- save an email for confirmations and visit summaries (save_email)

Rules:
- Reply in the patient's language and script (English, Hindi or Hinglish), warm and short: max 3-4 lines, WhatsApp style, at most one emoji.
- ALWAYS use tools for facts about tokens, queue, packages. Never invent token numbers or times. Tokens look like O7 or L2.
- Ask for the patient's name before booking if you don't know it. After booking, ask how far they are (minutes) or to share location if unknown.
- You are not a doctor: no diagnosis or medicine advice. For medical questions say the doctor will review at the visit; for emergencies call 112 / go to the nearest hospital.
- If a patient sends a photo, it is attached to their token for the doctor; acknowledge it.
Known patient profile: {profile}"""

TOOLS = [
    {"name": "book_token", "description": "Book a token for today for this patient in the OPD or LASER queue.",
     "schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Patient name"},
         "queue": {"type": "string", "enum": ["OPD", "LASER"], "description": "OPD = doctor consult, LASER = laser/procedure"},
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
    {"name": "save_email", "description": "Save the patient's email for confirmations.",
     "schema": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}},
]
TOOL_CONFIG = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                       "inputSchema": {"json": t["schema"]}}} for t in TOOLS]}


# ---------------------------------------------------------------- tool impls
def _book(phone: str, name: str, service: str = "", queue: str = "OPD", package: str | None = None) -> dict:
    existing = c.active_token(phone)
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
    item = {"pk": c.day_pk(), "sk": f"TOKEN#{q}#{num:03d}", "queue": q, "token": num, "label": label, "pos": num,
            "phone": phone, "name": name, "service": service or cfg["label"], "status": "waiting", "nudged": False,
            "created": c.iso_now(), "photos": []}
    if package:
        item["package"] = package
    c.ddb.put_item(Item=item)
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
                     args.get("service", ""), args.get("queue", "OPD"))
    if name == "queue_status":
        c.bump("status_checks")   # each one is a phone call the receptionist didn't take
        t = c.active_token(phone)
        if not t:
            return {"has_token": False, "queues": {q: {"waiting": len(c.waiting(q=q)),
                                                       "now_serving": c.counter(q).get("now_serving_label", "")}
                                                   for q in c.QUEUES}}
        ahead, eta = c.eta_minutes(t)
        return {"has_token": True, "token": t["label"], "queue": t["queue"], "status": t["status"],
                "people_ahead": ahead, "eta_min": eta, "now_serving": c.counter(t["queue"]).get("now_serving_label", "")}
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
        c.upsert_patient(phone, travel_min=int(args["minutes"]), travel_source="patient")
        return {"saved": True, "travel_min": int(args["minutes"])}
    if name == "save_email":
        c.upsert_patient(phone, email=args["email"].strip())
        return {"saved": True}
    return {"error": f"unknown tool {name}"}


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


def _fallback(phone: str, text: str) -> str:
    """Deterministic intent router used when Bedrock is unavailable.
    Calls the same tools as the agent, so clinic state stays consistent."""
    t = text.lower().strip()
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
        return f"Got it – {m} min away. I'll ping you when it's time to leave 🚶"
    if pending.startswith("name") and len(t) < 40 and not re.search(r"token|queue|status|cancel|package|menu|hi$|hello", t):
        c.upsert_patient(phone, name=text.strip().title(), pending="none")
        prof["name"] = text.strip().title()
        t = "token laser" if pending == "name:LASER" else "token opd"
    if re.search(r"cancel|रद्द", t):
        r = run_tool(phone, "cancel_token", {})
        return f"Token {r['token']} cancelled." if r["cancelled"] else "You don't have an active token today."
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
        return f"Token {r['token']} ({'Laser' if r['queue'] == 'LASER' else 'OPD'}): {state} Now serving {r['now_serving'] or '–'}."
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
        return msg
    cl = c.clinic()
    return (f"Hi{(' ' + prof['name']) if prof.get('name') else ''}! 👋 Welcome to {cl.get('name', 'the clinic')}.\n"
            "I can book your token, tell you your live wait time, and ping you when it's time to leave. "
            "Tap *Menu* below or just type what you need.")


# ---------------------------------------------------------------- agent loop
def respond(phone: str, user_text: str) -> str:
    global _BEDROCK_DOWN_UNTIL
    if time.time() < _BEDROCK_DOWN_UNTIL:
        return fallback(phone, user_text)
    try:
        return _respond_llm(phone, user_text)
    except Exception as e:
        if e.__class__.__name__ in ("ThrottlingException", "AccessDeniedException", "ResourceNotFoundException",
                                    "ValidationException", "ServiceUnavailableException", "ModelNotReadyException"):
            _BEDROCK_DOWN_UNTIL = time.time() + 600
            c.log.warning("Bedrock unavailable (%s) – using fallback router", e.__class__.__name__)
            return fallback(phone, user_text)
        raise


def _respond_llm(phone: str, user_text: str) -> str:
    cl = c.clinic()
    prof = c.patient(phone)
    system = SYSTEM.format(
        name=cl.get("name", "the clinic"), address=cl.get("address", ""), hours=cl.get("hours", ""),
        opd=c.queue_cfg("OPD", cl)["service_min"], laser=c.queue_cfg("LASER", cl)["service_min"],
        services=", ".join(cl.get("services", [])),
        today=c.today(), now=c.now_ist().strftime("%H:%M"),
        profile=json.dumps({k: prof.get(k) for k in ("name", "email", "travel_min") if prof.get(k)}) or "new patient",
    )
    history = c.load_conv(phone)
    messages = [{"role": m["role"], "content": [{"text": m["text"]}]} for m in history]
    messages.append({"role": "user", "content": [{"text": user_text}]})

    reply = "Sorry, I had trouble there. Please try again 🙏"
    for _ in range(6):
        out = brt.converse(modelId=MODEL_ID, system=[{"text": system}], messages=messages,
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
