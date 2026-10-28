"""ClinicFlow – WhatsApp gateway Lambda.

patient -> WhatsApp -> AWS End User Messaging Social -> SNS -> this Lambda
  - text / buttons / list picks -> ClinicFlow agent on Amazon Bedrock AgentCore Runtime
  - photos                      -> GetWhatsAppMessageMedia -> S3, attached to today's token
  - shared location             -> Amazon Location Service drive time -> "leave now" timing
Every reply goes back with SendWhatsAppMessage, wrapped in context-aware interactive UI
(reply buttons when the patient holds a token, a list menu otherwise).
"""
import json
import re
import os

import common as c

PHASE1_ECHO = os.environ.get("ECHO_ONLY") == "1"
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
_ac = None

# interactive ids -> what the patient "said"
INTENTS = {
    "m_opd": "token opd", "m_laser": "token laser", "m_status": "status", "m_pkg": "package",
    "m_slot": "slot", "m_hist": "my history", "h_rx": "my prescription",
    "m_privacy": "__privacy__", "m_clinic": "__clinic__", "c_yes": "__consent_yes__", "c_no": "__consent_no__",
    "d_ask": "delete my data", "d_yes": "__delete_yes__", "d_no": "__delete_no__", "r_stop": "stop reminders", "r_start": "start reminders", "m_cancel": "cancel", "m_late": "running late", "m_here": "I'm here", "m_loc": "__share_location__",
    "b_status": "status", "b_late": "running late", "b_cancel": "cancel", "b_here": "I'm here",
    "b_loc": "__share_location__",
    "book_pkg": "book session", "later": "remind me later", "fu_book": "token",
    "fb_good": "__fb_good__", "fb_ok": "__fb_ok__", "fb_bad": "__fb_bad__",
}

MENU_ROWS = [
    ("m_opd", "Doctor consultation", "OPD token: skin, acne, hair, follow-up"),
    ("m_laser", "Laser / procedure", "Laser, peel, PRP: book today's slot"),
    ("m_status", "My wait time", "Live position and ETA"),
    ("m_pkg", "My packages", "Sessions done, next due, book next"),
    ("m_hist", "🗂️ My visits & Rx", "Past visits, last prescription, follow-ups"),
    ("m_slot", "🕔 Book laser slot", "Pick a fixed time for laser / procedure"),
    ("m_loc", "📍 Share my location", "Live-traffic drive time: know exactly when to leave"),
    ("m_clinic", "🗺️ Clinic address", "Map pin and one-tap directions"),
    ("m_cancel", "Cancel my token", "Free your slot for someone else"),
    ("m_privacy", "🔒 Privacy & my data", "Stop reminders or delete your data"),
]
LOC_ASK = ("📍 Tap *Send location* below. ClinicFlow checks live traffic with Amazon Location Service "
           "and tells you the exact minute to leave, so you wait at home, not in the clinic.")


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
        rid = r.get("id") or ""
        if rid.startswith("slot_"):
            return f"__slot_{rid[5:]}__"
        return INTENTS.get(rid) or r.get("title")
    if t == "button":  # template quick reply
        return msg["button"].get("text")
    return None


def handle_photo(pid: str, msg: dict, phone: str) -> str:
    media = msg.get("image") or msg.get("document") or {}
    ext = "jpg" if msg.get("type") == "image" else "bin"
    key = f"photos/{c.today()}/{phone}/{msg['id']}.{ext}"
    c.wa_fetch_media(media["id"], key, pid)
    c.bump("photos")
    c.wa_reaction(phone, msg["id"], "👍", pid)
    hist = c.patient(phone).get("photo_history") or []
    c.upsert_patient(phone, photo_history=(hist + [{"date": c.today(), "key": key}])[-24:])
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
    now = c.iso_now()
    c.upsert_patient(phone, travel_min=minutes, travel_source=source, lat=lat, lon=lon,
                     loc_at=now, travel_at=now, pending="none")
    c.bump("locations")
    how = "by car with live traffic (Amazon Location Service)" if source == "amazon-location" else "by road (estimate)"
    tok = c.active_token(phone)
    if tok and tok["status"] == "waiting":
        ahead, eta = c.eta_minutes(tok)
        if eta <= minutes + 5:
            if not tok.get("nudged"):
                c.set_token(tok, nudged=True, nudged_at=now)
                c.bump("nudges")
            return (f"📍 You're ~{minutes} min away {how}. Your wait is ~{eta} min, so "
                    f"*please leave now* 🚶 for token {tok['label']}.")
        return (f"📍 You're ~{minutes} min away {how}. Token {tok['label']} has ~{eta} min to go. "
                f"Relax where you are. I'll re-check traffic as the queue moves and ping you when it's time to leave "
                f"(about {max(1, eta - minutes - 5)} min from now).")
    return f"📍 Saved: you're ~{minutes} min away {how}. I'll use this to tell you exactly when to leave."


def _with_intake(phone: str, reply: str) -> str:
    try:
        import agent
        nxt = agent.intake_start(phone)
        return reply + (f"\n\n{nxt}" if nxt else "")
    except Exception:
        c.log.exception("intake start failed")
        return reply


def handle_feedback(phone: str, kind: str) -> str:
    from boto3.dynamodb.conditions import Key
    done = [t for t in c.tokens() if t["phone"] == phone and t["status"] == "done"]
    if done:
        c.set_token(done[-1], feedback=kind, feedback_at=c.iso_now())
    try:
        c.set_visit_feedback(phone, kind)
    except Exception:
        c.log.exception("visit feedback failed")
    c.bump(f"fb_{kind}")
    cl = c.clinic()
    if kind == "good":
        url = cl.get("google_review_url")
        if url:
            c.bump("reviews_requested")
            return (f"Thank you! 😊 It would mean a lot to {cl.get('name')} if you shared that on Google, "
                    f"it takes 20 seconds: {url}")
        return "Thank you! 😊 We're glad it went well. See you next time."
    if kind == "ok":
        return "Thanks for the honest feedback 🙏 What could we do better? Just reply here."
    return ("We're really sorry 😞 The clinic manager has been notified and will call you today. "
            "If you'd like, tell us here what went wrong.")


def send_with_ui(phone: str, reply: str, pid: str, asked: str = ""):
    """Wrap the reply in interactive UI based on the patient's state."""
    tok = c.active_token(phone)
    prof = c.patient(phone)
    has_gps = prof.get("lat") is not None
    try:
        if tok and tok["status"] == "in_consult":
            c.wa_text(phone, reply, pid)
        elif tok:
            if tok.get("arrived"):
                buttons = [("b_status", "🔄 Refresh wait"), ("b_cancel", "❌ Cancel")]
            elif not has_gps:
                buttons = [("b_status", "🔄 Refresh wait"), ("b_loc", "📍 Share location"), ("b_late", "⏰ Running late")]
            else:
                buttons = [("b_status", "🔄 Refresh wait"), ("b_late", "⏰ Running late"), ("b_here", "✅ I've arrived")]
            c.wa_buttons(phone, reply, buttons, pid)
            if not has_gps and not tok.get("arrived") and prof.get("loc_asked") != c.today():
                c.upsert_patient(phone, loc_asked=c.today())
                c.wa_location_request(phone, LOC_ASK, pid)
        else:
            c.wa_list(phone, reply, "Menu", MENU_ROWS, pid)
    except Exception:  # interactive failed (e.g. body too long) -> plain text
        c.log.exception("interactive send failed; sending text")
        c.wa_text(phone, reply, pid)


AUDIO_FMT = {"ogg": "ogg", "opus": "ogg", "mpeg": "mp3", "mp3": "mp3", "mp4": "mp4", "m4a": "m4a", "amr": "amr", "aac": "mp4"}
DELETE_RE = re.compile(r"delete (all )?(my )?data|data delete|mera data|meri details|forget me|erase my|remove my data|data hata|data mita", re.I)
CLINIC_RE = re.compile(r"\b(address|directions?|clinic (location|kaha|kahan|ka pata)|kaha(n)? (hai )?clinic|rasta|google maps?|map)\b", re.I)
GREETING_RE = re.compile(r"^(hi+|hello|hey|namaste|hii+|start|menu)\W*$", re.I)


def handle_voice(pid: str, msg: dict, phone: str) -> dict:
    """WhatsApp voice note -> S3 (GetWhatsAppMessageMedia) -> Amazon Transcribe (Hindi / Indian English auto-detect)."""
    audio = msg.get("audio") or {}
    mime = (audio.get("mime_type") or "audio/ogg").lower()
    fmt = next((v for k, v in AUDIO_FMT.items() if k in mime), "ogg")
    key = f"voice/{c.today()}/{phone}/{msg['id']}.{fmt}"
    c.wa_reaction(phone, msg["id"], "🎧", pid)
    c.wa_fetch_media(audio["id"], key, pid)
    r = c.transcribe_s3(key, fmt)
    c.bump("voice_notes")
    c.log.info("voice note transcribed: %s", {k: r.get(k) for k in ("ok", "language", "secs", "error")})
    return r


def privacy_menu(phone: str, pid: str):
    cl = c.clinic()
    opted = c.patient(phone).get("opt_out")
    c.wa_buttons(phone, c.PRIVACY_TEXT.format(name=cl.get("name", "The clinic")) +
                 ("\n\n🔕 Reminders are currently *off*." if opted else ""),
                 [("d_ask", "🗑️ Delete my data"), ("r_start" if opted else "r_stop", "🔔 Start reminders" if opted else "🔕 Stop reminders")], pid)


def consent_prompt(phone: str, pid: str):
    cl = c.clinic()
    c.wa_buttons(phone, f"👋 Welcome to {cl.get('name', 'the clinic')}!\n\n" + c.PRIVACY_TEXT.format(name=cl.get("name", "The clinic")) +
                 "\n\nTap *I agree* to continue.", [("c_yes", "✅ I agree"), ("c_no", "❌ No thanks")], pid)


def route_text(phone: str, text: str, pid: str, profile_name: str | None) -> str | None:
    """Text (typed, tapped or spoken) -> reply. Returns None when the reply was already sent."""
    low = text.strip().lower()
    if low.startswith("__slot_") or low in ("slot", "laser slot", "slots", "book slot"):
        import agent
        return agent.fallback(phone, text)
    if text in ("my history", "my prescription"):
        import agent
        rx = text == "my prescription"
        c.bump("history_views")
        card = agent.history_text(phone, with_rx=rx)
        try:
            if len(card) < 1000:
                btns = [("m_opd", "🎟️ Book a visit")] if rx else [("h_rx", "💊 Last prescription"), ("m_opd", "🎟️ Book a visit")]
                c.wa_buttons(phone, card, btns, pid)
            else:
                c.wa_text(phone, card, pid)
            if rx:
                v = next((v for v in c.visits(phone) if v.get("rx_pdf")), None)
                if v:
                    c.wa_document(phone, v["rx_pdf"], f"Prescription-{v['date']}.pdf", "🧾 Printable copy", pid)
        except Exception:
            c.log.exception("history card send failed")
            c.wa_text(phone, card[:4000], pid)
        return None
    if text in ("__fb_good__", "__fb_ok__", "__fb_bad__"):
        c.wa_text(phone, handle_feedback(phone, text.strip("_").split("_")[1]), pid)
        return None
    if text == "__privacy__":
        privacy_menu(phone, pid)
        return None
    if text == "__clinic__" or CLINIC_RE.search(text):
        c.send_clinic_location(phone, pid)
        return None
    if text == "__share_location__" or low in ("location", "share location", "my location", "📍"):
        c.wa_location_request(phone, LOC_ASK, pid)
        return None
    if low in ("stop", "stop reminders", "unsubscribe", "reminders off"):
        c.upsert_patient(phone, opt_out=True)
        c.wa_text(phone, "🔕 Done. No more reminders from us. Your visits and tokens still work as usual. "
                         "Type *start reminders* anytime to turn them back on.", pid)
        return None
    if low in ("start reminders", "r_start", "reminders on"):
        c.upsert_patient(phone, opt_out=False)
        c.wa_text(phone, "🔔 Reminders are back on. We'll remind you about follow-ups and package sessions.", pid)
        return None
    if DELETE_RE.search(text):
        c.wa_buttons(phone, "🗑️ This permanently deletes your profile, visit history, prescriptions, photos and chat "
                            "history from ClinicFlow. Today's token (if any) will be cancelled. Are you sure?",
                     [("d_yes", "🗑️ Yes, delete all"), ("d_no", "Keep my data")], pid)
        return None
    if text == "__delete_yes__":
        r = c.delete_patient_data(phone)
        c.wa_text(phone, f"✅ Deleted. {r['records']} records and {r['files']} files were erased"
                         f"{', today’s token cancelled' if r['tokens_anonymised'] else ''}. "
                         "We've kept nothing that identifies you. Message us anytime if you want to come back. 🙏", pid)
        return None
    if text == "__delete_no__":
        return "👍 Nothing deleted. Your data stays safe with the clinic."
    rf = __import__("triage").handle(phone, text)
    if rf:
        return rf
    if PHASE1_ECHO:
        return f"ClinicFlow is live ✅\nYou said: {text}"
    if profile_name and not c.patient(phone).get("wa_name"):
        c.upsert_patient(phone, wa_name=profile_name)
    return ask_agent(phone, text)


def lambda_handler(event, context):
    for record in event.get("Records", []):
        for pid, profile_name, msg in iter_inbound(record):
            phone = msg["from"]
            c.wa_mark_read(msg["id"], pid)
            text, heard, reply = None, None, None
            try:
                mtype = msg.get("type")
                tapped = text_of(msg) if mtype in ("interactive", "button") else None
                typed = text_of(msg) if mtype == "text" else None
                # 1) safety first: red flags are answered even before consent
                if typed and (rf := __import__("triage").handle(phone, typed)):
                    c.wa_text(phone, rf, pid)
                    continue
                # 2) consent gate (DPDP): nothing is stored until the patient agrees
                prof = c.patient(phone)
                if not prof.get("consent"):
                    if tapped == "__consent_yes__":
                        c.upsert_patient(phone, consent=c.iso_now(), consent_version="v1")
                        c.bump("consents")
                        pending = prof.get("pending_text")
                        if pending:
                            c.upsert_patient(phone, pending_text="")
                        text = pending or "hi"
                    elif tapped == "__consent_no__":
                        if prof:
                            c.delete_patient_data(phone)
                        c.wa_text(phone, "No problem 🙏 We haven't stored anything. You're welcome to walk in to the clinic, "
                                         "or message us anytime if you change your mind.", pid)
                        continue
                    else:
                        if typed and not GREETING_RE.match(typed.strip()):
                            c.upsert_patient(phone, pending_text=typed[:500])
                        consent_prompt(phone, pid)
                        continue
                # 3) media
                if text is None and mtype in ("image", "document"):
                    reply = handle_photo(pid, msg, phone)
                    caption = (msg.get("image") or msg.get("document") or {}).get("caption")
                    if caption:
                        rf = __import__("triage").handle(phone, caption)
                        reply += "\n" + (rf or ask_agent(phone, caption))
                elif text is None and mtype == "location":
                    reply = _with_intake(phone, handle_location(msg, phone))
                    c.maybe_send_directions(phone, pid) if "leave now" in reply.lower() else None
                elif text is None and mtype == "audio":
                    r = handle_voice(pid, msg, phone)
                    if not r.get("ok"):
                        c.wa_text(phone, "🎧 Sorry, I couldn't catch that voice note clearly. Could you try again or type it? 🙏", pid)
                        continue
                    text = heard = r["text"]
                    if (rf := __import__("triage").handle(phone, text)):
                        c.wa_text(phone, f"🎙️ _I heard:_ “{heard}”\n\n{rf}", pid)
                        continue
                elif text is None:
                    text = tapped or typed
                    if not text:
                        reply = "Sorry, I can read text, voice notes, buttons, photos and locations for now 🙏"
                if reply is None and text:
                    reply = route_text(phone, text, pid, profile_name)
                    if reply is None:
                        continue
                    if heard:
                        reply = f"🎙️ _I heard:_ “{heard}”\n\n{reply}"
            except Exception:
                c.log.exception("handling failed")
                reply = "Sorry, something went wrong on our side. Please try again in a minute 🙏"
            if not reply:
                continue
            if "__SLOT_LIST__" in reply:
                free = c.free_slots()
                if not free:
                    c.wa_text(phone, "No laser slots left today 😕 Type *laser* to join the walk-in laser queue instead.", pid)
                else:
                    c.wa_list(phone, "🕔 Pick a laser / procedure time for today:", "See slots",
                              [(f"slot_{x}", x, "30 min · laser / procedure") for x in free], pid)
                continue
            send_with_ui(phone, reply, pid, text or "")
    return {"ok": True}
