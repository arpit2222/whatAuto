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
Clinic hours: {hours}. Average consultation: {avg} minutes. Today is {today}, time now {now} IST.
Services: {services}.

You help patients:
- take a walk-in token for today (book_token) and see their live queue position / ETA (queue_status)
- cancel their token (cancel_token)
- check their treatment packages (e.g. laser sessions left, next due date) and book the next session (my_packages, book_package_session)
- save how many minutes they live from the clinic so we can ping them "leave now" at the right time (set_travel_time)
- save an email to receive confirmations and visit summaries (save_email)

Rules:
- Reply in the patient's language and script (English, Hindi or Hinglish), warm and short: max 3-4 lines, WhatsApp style, at most one emoji.
- ALWAYS use tools for facts about tokens, queue, packages. Never invent token numbers or times.
- Ask for the patient's name before booking if you don't know it. After booking, ask travel time if unknown.
- You are not a doctor: no diagnosis or medicine advice. For medical questions or emergencies, tell them the doctor will review at the visit, and for emergencies to call 112 / go to the nearest hospital.
- If a patient sends a photo, it is attached to their token for the doctor; acknowledge it.
Known patient profile: {profile}"""

TOOLS = [
    {"name": "book_token", "description": "Book a walk-in token for today for this patient.",
     "schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Patient name"},
         "service": {"type": "string", "description": "Reason / service, e.g. acne consult, laser hair removal"}},
         "required": ["name"]}},
    {"name": "queue_status", "description": "Get the patient's current token, people ahead and ETA.",
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
def _book(phone: str, name: str, service: str = "consultation", package: str | None = None) -> dict:
    existing = c.active_token(phone)
    if existing:
        ahead, eta = c.eta_minutes(existing)
        return {"already_booked": True, "token": existing["token"], "people_ahead": ahead, "eta_min": eta}
    cnt = c.ddb.update_item(
        Key={"pk": c.day_pk(), "sk": "COUNTER"},
        UpdateExpression="ADD last_token :one SET now_serving = if_not_exists(now_serving, :zero)",
        ExpressionAttributeValues={":one": 1, ":zero": 0},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    num = int(cnt["last_token"])
    item = {"pk": c.day_pk(), "sk": f"TOKEN#{num:03d}", "token": num, "phone": phone, "name": name,
            "service": service or "consultation", "status": "waiting", "nudged": False,
            "created": c.now_ist().isoformat(), "photos": []}
    if package:
        item["package"] = package
    c.ddb.put_item(Item=item)
    c.upsert_patient(phone, name=name)
    ahead, eta = c.eta_minutes(item)
    prof = c.patient(phone)
    if prof.get("email"):
        cl = c.clinic()
        c.email(prof["email"], f"Your token #{num} at {cl.get('name')}",
                f"Hi {name},\n\nYour token for today is #{num} ({item['service']}).\n"
                f"People ahead: {ahead}. Estimated wait: ~{eta} min.\n"
                f"We'll WhatsApp you when it's time to leave.\n\n{cl.get('name')}, {cl.get('address')}")
    return {"token": num, "people_ahead": ahead, "eta_min": eta,
            "travel_min_known": bool(prof.get("travel_min")), "email_sent": bool(prof.get("email"))}


def run_tool(phone: str, name: str, args: dict) -> dict:
    if name == "book_token":
        return _book(phone, args.get("name") or c.patient(phone).get("name", "Patient"), args.get("service"))
    if name == "queue_status":
        t = c.active_token(phone)
        cnt = c.counter()
        if not t:
            return {"has_token": False, "now_serving": cnt.get("now_serving", 0), "waiting": len(c.waiting())}
        ahead, eta = c.eta_minutes(t)
        return {"has_token": True, "token": t["token"], "status": t["status"], "people_ahead": ahead,
                "eta_min": eta, "now_serving": cnt.get("now_serving", 0)}
    if name == "cancel_token":
        t = c.active_token(phone)
        if not t:
            return {"cancelled": False, "reason": "no active token"}
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"TOKEN#{t['token']:03d}"},
                          UpdateExpression="SET #s = :x", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":x": "cancelled"})
        return {"cancelled": True, "token": t["token"]}
    if name == "my_packages":
        return {"packages": [{"package": p["sk"].split("#", 1)[1], "total": p["total"], "used": p["used"],
                              "left": p["total"] - p["used"], "next_due": p.get("next_due")}
                             for p in c.packages(phone)]}
    if name == "book_package_session":
        pk = next((p for p in c.packages(phone) if args["package"].lower() in p["sk"].lower()), None)
        if not pk:
            return {"booked": False, "reason": "package not found", "packages": [p["sk"] for p in c.packages(phone)]}
        pname = pk["sk"].split("#", 1)[1]
        res = _book(phone, c.patient(phone).get("name", "Patient"), f"{pname} session", package=pname)
        return {"booked": True, "package": pname, "session_number": pk["used"] + 1, **res}
    if name == "set_travel_time":
        c.upsert_patient(phone, travel_min=int(args["minutes"]))
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


def _fallback(phone: str, text: str) -> str:
    """Deterministic intent router used when Bedrock is unavailable.
    Calls the same tools as the agent, so clinic state stays consistent."""
    t = text.lower().strip()
    prof = c.patient(phone)
    pending = prof.get("pending")
    em = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    mins = re.search(r"(\d{1,3})\s*(min|minute|mins|मिनट)", t)
    if em:
        run_tool(phone, "save_email", {"email": em.group(0)})
        return f"Saved ✅ We'll email confirmations to {em.group(0)}."
    if mins or (pending == "travel" and t.isdigit()):
        m = int(mins.group(1) if mins else t)
        run_tool(phone, "set_travel_time", {"minutes": m})
        c.upsert_patient(phone, pending="none")
        return f"Got it – {m} min away. I'll ping you when it's time to leave 🚶"
    if pending == "name" and len(t) < 40 and not re.search(r"token|queue|status|cancel|package", t):
        c.upsert_patient(phone, name=text.strip().title(), pending="none")
        prof["name"] = text.strip().title()
        t = "token"
    if re.search(r"cancel|रद्द", t):
        r = run_tool(phone, "cancel_token", {})
        return f"Token #{r['token']} cancelled." if r["cancelled"] else "You don't have an active token today."
    if t in ("book", "book today", "yes") and c.packages(phone):  # reply to session_reminder template
        t = "book session"
    if re.search(r"package|session|laser|peel|prp|पैकेज", t):
        if re.search(r"book|today|token|yes", t):
            pk = c.packages(phone)
            if pk:
                r = run_tool(phone, "book_package_session", {"package": pk[0]["sk"].split("#", 1)[1]})
                if r.get("booked"):
                    return (f"Booked ✅ {r['package']} session {r['session_number']} – token #{r['token']}. "
                            f"{r['people_ahead']} ahead, ~{r['eta_min']} min.")
        r = run_tool(phone, "my_packages", {})["packages"]
        if not r:
            return "You don't have any active packages. Ask at the front desk about laser, peel or PRP packages."
        lines = [f"• {p['package']}: {p['used']}/{p['total']} done, next due {p['next_due']}" for p in r]
        return "Your packages:\n" + "\n".join(lines) + "\nReply 'book session' to take a token for today."
    if re.search(r"status|queue|kitna|kab|wait|eta|position|number|turn", t):
        r = run_tool(phone, "queue_status", {})
        if not r["has_token"]:
            return f"No token yet. Now serving #{r['now_serving'] or '–'}, {r['waiting']} waiting. Reply 'token' to join."
        return (f"Token #{r['token']}: {r['people_ahead']} ahead of you, ~{r['eta_min']} min. "
                f"Now serving #{r['now_serving'] or '–'}.")
    if re.search(r"token|book|appointment|appoint|visit|doctor|chahiye|lena", t):
        if not prof.get("name"):
            c.upsert_patient(phone, pending="name")
            return "Sure! What's the patient's name?"
        r = run_tool(phone, "book_token", {"name": prof["name"], "service": "consultation"})
        msg = (f"You already have token #{r['token']}." if r.get("already_booked")
               else f"Booked ✅ Token #{r['token']} for {prof['name']}.")
        msg += f" {r['people_ahead']} ahead, ~{r['eta_min']} min wait."
        if not prof.get("travel_min"):
            c.upsert_patient(phone, pending="travel")
            msg += "\nHow many minutes are you from the clinic? I'll tell you when to leave."
        return msg
    cl = c.clinic()
    return (f"Hi{(' ' + prof['name']) if prof.get('name') else ''}! 👋 Welcome to {cl.get('name', 'the clinic')}.\n"
            "Reply:\n• *token* – join today's queue\n• *status* – your wait time\n"
            "• *package* – your sessions\n• *cancel* – cancel token\nOr send a photo for the doctor.")


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
        avg=cl.get("avg_consult_min", 10), services=", ".join(cl.get("services", [])),
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
