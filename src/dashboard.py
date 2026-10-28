"""ClinicFlow front desk (Lambda Function URL) + daily doctor summary.

GET  /?key=K                      HTML front-desk board (OPD + LASER queues, impact tiles)
GET  /api/queue?key=K             JSON state
POST /api/next?key=K&q=OPD        finish current, call next in that queue, WhatsApp "your turn" + leave-now nudges
POST /api/skip?key=K&q=OPD        mark current as no-show, call next
POST /api/done?key=K&q=OPD        finish current without calling next
POST /api/walkin?key=K&q=OPD&name=Ravi   add a walk-in patient (no WhatsApp)
POST /api/summary?key=K           email today's summary to the doctor (Amazon SES)
summary_handler                   EventBridge daily 20:30 IST -> same summary email
"""
import html
import json
import os
import time

import boto3

import common as c

KEY = os.environ.get("DASHBOARD_KEY", "")
_s3 = None


def _resp(code, body, ctype="application/json"):
    return {"statusCode": code, "headers": {"content-type": ctype, "cache-control": "no-store"},
            "body": body if isinstance(body, str) else json.dumps(body)}


def _is_wa(phone: str) -> bool:
    return not str(phone).startswith("walkin")


def _photo_urls(keys: list) -> list:
    global _s3
    if not (keys and c.MEDIA_BUCKET):
        return []
    try:
        _s3 = _s3 or boto3.client("s3")
        return [_s3.generate_presigned_url("get_object", Params={"Bucket": c.MEDIA_BUCKET, "Key": k}, ExpiresIn=3600)
                for k in keys[-3:]]
    except Exception:
        c.log.exception("presign failed")
        return []


# ---------------------------------------------------------------- queue actions
def finish_current(q: str, status: str = "done", fu_days: int = 0):
    cl = c.clinic()
    for t in c.tokens(q=q):
        if t["status"] != "in_consult":
            continue
        now = c.iso_now()
        c.set_token(t, status=status, done_at=now)
        t = {**t, "status": status, "done_at": now}
        if status == "no_show":
            c.bump("no_shows")
            try:
                c.save_visit(t, "no_show")
            except Exception:
                c.log.exception("save_visit failed")
            if _is_wa(t["phone"]):
                c.wa_buttons(t["phone"], f"We called token {t['label']} but couldn't find you 😕 "
                                         "Your token was marked missed. Want a new one?",
                             [("m_laser" if q == "LASER" else "m_opd", "🎟️ New token"), ("m_status", "Queue status")])
            continue
        c.bump("served")
        if t.get("package"):  # count the session and schedule the next one
            pk = {"pk": f"PATIENT#{t['phone']}", "sk": f"PACKAGE#{t['package']}"}
            gap = int(c.clean(c.ddb.get_item(Key=pk).get("Item", {})).get("interval_days", 28))
            nxt = (c.now_ist() + c.dt.timedelta(days=gap)).strftime("%Y-%m-%d")
            c.ddb.update_item(Key=pk, UpdateExpression="ADD used :one SET next_due = :d",
                              ExpressionAttributeValues={":one": 1, ":d": nxt})
        if not _is_wa(t["phone"]):
            continue
        prof = c.patient(t["phone"])
        fu_line = ""
        if fu_days:
            due = (c.now_ist() + c.dt.timedelta(days=fu_days)).strftime("%Y-%m-%d")
            c.ddb.put_item(Item={"pk": f"PATIENT#{t['phone']}", "sk": f"FOLLOWUP#{due}", "due": due,
                                 "queue": q, "reason": t.get("intake_summary") or t.get("service", ""),
                                 "from_token": t["label"], "status": "pending"})
            c.set_token(t, followup_due=due)
            t["followup_due"] = due
            c.bump("followups")
            fu_line = f"\n🗓️ The doctor wants to see you again around *{c.dt.date.fromisoformat(due).strftime('%d %b')}*. I'll remind you."
        try:
            c.save_visit(t, "done")
        except Exception:
            c.log.exception("save_visit failed")
        c.set_token(t, feedback_asked=True)
        try:
            c.wa_buttons(t["phone"], f"Thanks for visiting {cl.get('name')}, {t['name']}! 🙏{fu_line}\n\nHow was your visit today?",
                         [("fb_good", "😊 Great"), ("fb_ok", "😐 Okay"), ("fb_bad", "😞 Not good")])
        except Exception:
            c.log.exception("thanks message failed")
        if prof.get("email"):
            link = c.wa_link("token")
            c.email(prof["email"], f"Visit summary – {cl.get('name')}",
                    f"Hi {t['name']},\n\nThanks for visiting today (token {t['label']}, {t['service']}).\n"
                    f"Photos shared: {len(t.get('photos', []))}.\n"
                    "Your doctor's notes and next steps will be shared at the front desk.\n\n"
                    f"Book your next visit on WhatsApp: {link}\n{cl.get('name')}")


def nudge_waiting(q: str) -> list:
    """Leave-now engine, run on every queue move.
    1. Re-compute each waiting patient's drive time from their last location with live traffic (Amazon Location).
    2. remaining wait <= drive time + 5 min  -> "Time to leave" (once).
    3. about two patients before that point and location older than 30 min -> ask for a fresh location."""
    sent = []
    svc = int(c.queue_cfg(q).get("service_min", 10))
    for t in c.waiting(q=q):
        if t.get("nudged") or t.get("arrived") or not _is_wa(t["phone"]):
            continue
        ahead, eta = c.eta_minutes(t)
        prof = c.patient(t["phone"])
        travel, source, loc_age = c.refresh_travel(t["phone"], prof)
        travel = travel if travel is not None else 15
        if eta <= travel + 5:
            how = " with live traffic (Amazon Location)" if source == "amazon-location" else ""
            try:
                c.wa_buttons(t["phone"], f"🚶 Time to leave, {t['name']}! Token {t['label']}: {ahead} ahead of you, "
                                         f"~{eta} min. You're ~{travel} min away{how}.",
                             [("b_here", "✅ I've arrived"), ("b_late", "⏰ Running late"), ("b_status", "🔄 Refresh")])
            except Exception:
                c.log.exception("nudge failed")
            c.set_token(t, nudged=True, nudged_at=c.iso_now(), nudge_travel_min=travel)
            c.bump("nudges")
            sent.append(t["label"])
        elif eta <= travel + 5 + 2 * svc and not t.get("loc_refresh_asked") and (loc_age is None or loc_age > 30):
            try:
                c.wa_location_request(t["phone"], f"⏳ {t['name']}, token {t['label']} is getting close ({ahead} ahead, "
                                                  f"~{eta} min). Tap below to share where you are *now*. I'll check live "
                                                  "traffic and tell you the exact minute to leave.")
                c.set_token(t, loc_refresh_asked=True)
                c.bump("location_requests")
            except Exception:
                c.log.exception("location request failed")
    return sent


def announce_delay(q: str, minutes: int) -> dict:
    """Doctor running late: shift every ETA in the queue, tell waiting patients, re-time leave-now alerts."""
    minutes = max(0, min(180, int(minutes)))
    c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"COUNTER#{q}"},
                      UpdateExpression="SET delay_min = :d, delay_at = :t",
                      ExpressionAttributeValues={":d": minutes, ":t": c.iso_now()})
    told = []
    if minutes == 0:
        return {"delay_min": 0, "told": told}
    c.bump("delay_broadcasts")
    for t in c.waiting(q=q):
        if not _is_wa(t["phone"]):
            continue
        ahead, eta = c.eta_minutes(t)
        extra = (" You were told to leave earlier, so no need to rush. I'll ping you again at the right time."
                 if t.get("nudged") and not t.get("arrived") else " I'll re-time your leave-now alert.")
        try:
            c.wa_buttons(t["phone"], f"⏱️ Update from the clinic: the doctor is running ~{minutes} min late "
                                     f"(emergency). Token {t['label']}: new wait ~{eta} min.{extra}",
                         [("b_status", "🔄 Refresh wait"), ("b_late", "⏰ Running late"), ("b_cancel", "❌ Cancel")])
            told.append(t["label"])
        except Exception:
            c.log.exception("delay message failed")
        if t.get("nudged") and not t.get("arrived"):
            c.set_token(t, nudged=False)      # re-arm the leave-now alert with the new timing
    return {"delay_min": minutes, "told": told}


def call_next(q: str, skip: bool = False, fu_days: int = 0) -> dict:
    if int(c.counter(q).get("delay_min", 0) or 0):   # doctor is back -> delay over
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"COUNTER#{q}"},
                          UpdateExpression="SET delay_min = :z", ExpressionAttributeValues={":z": 0})
    finish_current(q, "no_show" if skip else "done", fu_days)
    w = c.waiting(q=q)
    called = None
    if w:
        nxt = w[0]
        c.set_token(nxt, status="in_consult", called_at=c.iso_now())
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"COUNTER#{q}"},
                          UpdateExpression="SET now_serving = :n, now_serving_label = :l",
                          ExpressionAttributeValues={":n": nxt["token"], ":l": nxt["label"]})
        if _is_wa(nxt["phone"]):
            room = "laser room" if q == "LASER" else "doctor's cabin"
            try:
                c.wa_buttons(nxt["phone"], f"✅ It's your turn, {nxt['name']}! Token {nxt['label']}. "
                                           f"Please come to the {room}.", [("b_here", "✅ I've arrived")])
            except Exception:
                c.log.exception("turn message failed")
        called = nxt["label"]
    return {"called": called, "nudged": nudge_waiting(q)}


def walkin(q: str, name: str) -> dict:
    import agent
    r = agent._book(f"walkin-{int(time.time() * 1000)}", name or "Walk-in", queue=q)
    c.bump("walkins")
    return r


# ---------------------------------------------------------------- metrics
def metrics(toks: list, st: dict) -> dict:
    served = [t for t in toks if t["status"] == "done"]
    waits = [m for t in toks if (m := c.minutes_between(t.get("created"), t.get("called_at"))) is not None]
    home = [m for t in toks if t.get("nudged_at") and (m := c.minutes_between(t.get("created"), t.get("nudged_at")))]
    in_room = [m for t in toks if t.get("arrived_at") and t.get("called_at")
               and (m := c.minutes_between(t.get("arrived_at"), t.get("called_at"))) is not None]
    return {
        "served": len(served),
        "waiting": sum(1 for t in toks if t["status"] == "waiting"),
        "avg_wait_min": round(sum(waits) / len(waits)) if waits else 0,
        "avg_in_clinic_min": round(sum(in_room) / len(in_room)) if in_room else 0,
        "home_wait_min_saved": round(sum(home)),
        "calls_avoided": int(st.get("status_checks", 0)),
        "nudges": int(st.get("nudges", 0)),
        "no_shows": int(st.get("no_shows", 0)),
        "late": int(st.get("late", 0)),
        "photos": int(st.get("photos", 0)),
        "package_bookings": int(st.get("package_bookings", 0)),
        "locations": int(st.get("locations", 0)),
        "location_requests": int(st.get("location_requests", 0)),
        "bookings": int(st.get("bookings", 0)),
        "walkins": int(st.get("walkins", 0)),
        "intakes": int(st.get("intakes", 0)),
        "followups": int(st.get("followups", 0)),
        "redflags": int(st.get("redflag_urgent", 0)) + int(st.get("redflag_emergency", 0)),
        "prescriptions": int(st.get("prescriptions", 0)),
        "slot_bookings": int(st.get("slot_bookings", 0)), "deposits_paid": int(st.get("deposits_paid", 0)),
        "fb_good": int(st.get("fb_good", 0)), "fb_ok": int(st.get("fb_ok", 0)), "fb_bad": int(st.get("fb_bad", 0)),
        "reviews_requested": int(st.get("reviews_requested", 0)),
    }


def state() -> dict:
    cl = c.clinic()
    toks = c.tokens()
    queues = {}
    for q in c.QUEUES:
        cfg = c.queue_cfg(q, cl)
        rows = []
        for t in [x for x in toks if x["queue"] == q]:
            row = {k: t.get(k) for k in ("label", "name", "service", "status", "nudged", "package", "arrived", "late",
                                         "intake_summary", "feedback", "followup_due", "urgent", "urgent_reason", "family", "rx_sent", "slot", "deposit_status", "deposit_amount")}
            row["phone"] = "walk-in" if not _is_wa(t["phone"]) else "xxxxxx" + t["phone"][-4:]
            row["photos"] = _photo_urls(t.get("photos", [])) if t["status"] in ("waiting", "in_consult") else []
            row["prev_photos"] = []
            if t["status"] in ("waiting", "in_consult") and _is_wa(t["phone"]):
                hist = [h for h in (c.patient(t["phone"]).get("photo_history") or []) if h.get("date") < c.today()]
                if hist:
                    last_date = hist[-1]["date"]
                    urls = _photo_urls([h["key"] for h in hist if h["date"] == last_date])
                    row["prev_photos"] = [{"date": last_date, "url": u} for u in urls[-2:]]
            row["photo_count"] = len(t.get("photos", []))
            row["wa"] = _is_wa(t["phone"])
            if row["wa"] and t["status"] in ("waiting", "in_consult"):
                try:
                    row["hist"] = c.history_brief(c.patient_history(t["phone"], t.get("name"), bool(t.get("family"))))
                except Exception:
                    c.log.exception("history brief failed")
            if t["status"] == "waiting":
                row["people_ahead"], row["eta_min"] = c.eta_minutes(t)
                prof = c.patient(t["phone"]) if _is_wa(t["phone"]) else {}
                row["travel_min"] = prof.get("travel_min")
                row["travel_source"] = prof.get("travel_source")
                row["travel_age_min"] = round(c.minutes_between(prof.get("travel_at"), c.iso_now()) or 0) \
                    if prof.get("travel_at") else None
                row["loc_refresh_asked"] = t.get("loc_refresh_asked", False)
            rows.append(row)
        svc, svc_cfg, samples = c.service_minutes(q, [x for x in toks if x["queue"] == q], cl)
        cnt = c.counter(q)
        queues[q] = {"label": cfg["label"], "service_min": svc, "service_min_cfg": svc_cfg, "learned_from": samples,
                     "delay_min": int(cnt.get("delay_min", 0) or 0),
                     "now_serving": cnt.get("now_serving_label", ""), "tokens": rows}
    return {"clinic": cl.get("name"), "date": c.today(), "time": c.now_ist().strftime("%H:%M"),
            "queues": queues, "metrics": metrics(toks, c.stats())}


# ---------------------------------------------------------------- doctor summary (SES)
def due_tomorrow() -> list:
    from boto3.dynamodb.conditions import Attr
    tmr = (c.now_ist() + c.dt.timedelta(days=1)).strftime("%Y-%m-%d")
    r = c.ddb.scan(FilterExpression=Attr("sk").begins_with("PACKAGE#"))
    out = []
    for p in c.clean(r["Items"]):
        if p.get("next_due") and p["next_due"] <= tmr and p["used"] < p["total"]:
            prof = c.patient(p["pk"].split("#", 1)[1])
            out.append({"name": prof.get("name", "Patient"), "package": p["sk"].split("#", 1)[1],
                        "session": f"{p['used'] + 1}/{p['total']}", "due": p["next_due"]})
    return out


def send_summary() -> dict:
    s = state()
    cl = c.clinic()
    to = cl.get("doctor_email") or c.SES_FROM
    m = s["metrics"]
    due = due_tomorrow()
    tiles = [("Patients served", m["served"]), ("Avg wait (booking→called)", f"{m['avg_wait_min']} min"),
             ("Waiting-room minutes saved", m["home_wait_min_saved"]), ("Reception calls avoided", m["calls_avoided"]),
             ("Leave-now nudges", m["nudges"]), ("No-shows", m["no_shows"]),
             ("Package sessions booked", m["package_bookings"]), ("Photos received", m["photos"])]
    tile_html = "".join(f"<td style='padding:10px;border:1px solid #e3e8e5;text-align:center'>"
                        f"<div style='font-size:22px;font-weight:700;color:#0f5132'>{v}</div>"
                        f"<div style='font-size:12px;color:#555'>{html.escape(k)}</div></td>"
                        + ("</tr><tr>" if i == 3 else "") for i, (k, v) in enumerate(tiles))
    urgent = [t for Q in s["queues"].values() for t in Q["tokens"] if t.get("urgent")]
    urgent_html = ("<h3 style='color:#8a1c00'>🚩 Red flags today</h3><ul>" + "".join(
        f"<li><b>{html.escape(t['label'])} {html.escape(t['name'] or '')}</b>: {html.escape(t.get('urgent_reason') or '')}</li>"
        for t in urgent) + "</ul>") if urgent else ""
    q_lines = "".join(
        f"<li><b>{html.escape(q['label'])}</b>: {sum(1 for t in q['tokens'] if t['status'] == 'done')} served, "
        f"{sum(1 for t in q['tokens'] if t['status'] == 'waiting')} still waiting</li>" for q in s["queues"].values())
    due_html = ("".join(f"<tr><td>{html.escape(d['name'])}</td><td>{html.escape(d['package'])}</td>"
                        f"<td>{d['session']}</td><td>{d['due']}</td></tr>" for d in due)
                or "<tr><td colspan=4>No package sessions due.</td></tr>")
    body = (f"<div style='font-family:Arial,sans-serif;max-width:640px'>"
            f"<h2 style='color:#0f5132'>{html.escape(cl.get('name', 'Clinic'))}: daily summary, {s['date']}</h2>"
            f"<table style='border-collapse:collapse;width:100%'><tr>{tile_html}</tr></table>"
            f"{urgent_html}<h3>Queues</h3><ul>{q_lines}</ul>"
            f"<h3>Package sessions due (follow up)</h3>"
            f"<table border=1 cellpadding=6 style='border-collapse:collapse'>"
            f"<tr><th>Patient</th><th>Package</th><th>Session</th><th>Due</th></tr>{due_html}</table>"
            f"<p style='color:#888;font-size:12px'>Sent by ClinicFlow via Amazon SES. Patients who waited at home "
            f"until nudged count toward 'waiting-room minutes saved'.</p></div>")
    text = (f"{cl.get('name')} – {s['date']}\n" + "\n".join(f"{k}: {v}" for k, v in tiles) +
            "\n\nPackage sessions due:\n" + ("\n".join(f"- {d['name']}: {d['package']} {d['session']} ({d['due']})"
                                                     for d in due) or "none"))
    ok = c.email(to, f"ClinicFlow daily summary – {s['date']}", text, body)
    return {"sent": ok, "to": to, "metrics": m, "packages_due": len(due)}


def reset_today() -> dict:
    """Demo helper: clear today's queue partition (tokens, counters, stats). Patients/packages are kept."""
    from boto3.dynamodb.conditions import Key
    r = c.ddb.query(KeyConditionExpression=Key("pk").eq(c.day_pk()))
    n = 0
    with c.ddb.batch_writer() as bw:
        for it in r["Items"]:
            bw.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
            n += 1
    return {"cleared": n}


def summary_handler(event, context):
    return send_summary()


# ---------------------------------------------------------------- HTML
HTML = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ClinicFlow – Front desk</title><style>
:root{--g:#0f5132;--g2:#198754;--bg:#f4f6f5;--card:#fff;--line:#e3e8e5;--muted:#6b7a72}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--bg);color:#14211c}
header{background:var(--g);color:#fff;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
h1{font-size:19px;margin:0}.sub{opacity:.8;font-size:13px}main{padding:16px;max-width:1280px;margin:auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.tile{background:var(--card);border-radius:12px;padding:12px 14px;border:1px solid var(--line)}
.tile b{display:block;font-size:24px;color:var(--g)}.tile span{font-size:12px;color:var(--muted)}
.tile.hi{background:#e8f5ee;border-color:#b7dfc8}
.queues{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:16px}
@media(max-width:600px){.queues{grid-template-columns:1fr}}
.q{background:var(--card);border-radius:14px;border:1px solid var(--line);overflow:hidden}
.qh{display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid var(--line);gap:8px;flex-wrap:wrap}
.qh h2{font-size:16px;margin:0}.now{font-size:13px;color:var(--muted)}.now b{font-size:28px;color:var(--g);display:block;line-height:1}
.acts{display:flex;gap:6px;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line)}
button{background:var(--g2);color:#fff;border:0;border-radius:9px;padding:10px 14px;font-size:14px;cursor:pointer}
button.sec{background:#6c757d}button.warn{background:#b54708}button:disabled{opacity:.5}
input,select{padding:9px;border:1px solid var(--line);border-radius:8px;font-size:14px;width:130px;background:#fff}
select{width:auto}
table{width:100%;border-collapse:collapse}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}.waiting{color:#a15c00}.in_consult{color:var(--g);font-weight:700}
.done,.cancelled,.no_show{color:#9aa}.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;margin:1px;background:#eef2f0;color:#335}
.pill.ok{background:#e1f3e8;color:var(--g)}.pill.late{background:#fde8d7;color:#9a3d00}.pill.loc{background:#e6effc;color:#1849a9}
.ph img{width:40px;height:40px;object-fit:cover;border-radius:6px;margin-right:3px;cursor:zoom-in}
.prev{font-size:10px;color:#889;margin-top:3px}.prev img{width:30px;height:30px;opacity:.85}.cmp{font-size:11px;display:inline-block;margin-top:3px}
.rxb{padding:6px 10px;font-size:12px;margin-top:4px;background:#1849a9}
#modal{position:fixed;inset:0;background:#000a;display:none;align-items:center;justify-content:center;z-index:9}#modal .box{background:#fff;border-radius:14px;padding:16px;max-width:900px;width:94%;max-height:90vh;overflow:auto}
.cmpgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.cmpgrid img{width:100%;border-radius:10px}textarea{width:100%;min-height:110px;font-size:15px;padding:10px;border-radius:8px;border:1px solid var(--line)}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#14211c;color:#fff;padding:10px 16px;border-radius:10px;display:none}
.top-acts button{background:#ffffff22;border:1px solid #ffffff55}
tr.urg td{background:#fff1ee}.urgline{margin-top:4px;padding:5px 7px;background:#fde2dc;border:1px solid #f5b5a7;border-radius:7px;font-size:12px;color:#8a1c00;font-weight:600;max-width:260px}
.hist{margin-top:4px;padding:5px 7px;background:#eef4ff;border:1px solid #c9dafc;border-radius:7px;font-size:12px;color:#1d3a75;max-width:280px}
a.hl{display:inline-block;margin-top:4px;font-size:12px;color:#1849a9;text-decoration:none;font-weight:600}
.hh{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start}.hh h3{margin:0}
.hsum{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:12px 0}.hsum div{background:#f4f6f5;border-radius:10px;padding:8px 10px}.hsum b{display:block;font-size:20px}.hsum span{font-size:11px;color:var(--muted)}
.ai{background:linear-gradient(135deg,#f3f0ff,#eef7ff);border:1px solid #d8d0fb;border-radius:10px;padding:10px 12px;white-space:pre-wrap;font-size:13px;margin:10px 0}
.pk{margin:6px 0;font-size:13px}.bar{height:8px;background:#e6ece9;border-radius:6px;overflow:hidden;margin-top:3px}.bar i{display:block;height:100%;background:var(--g2)}
.tl{border-left:3px solid #cfe0d7;margin:8px 0 0 6px;padding-left:14px}.tv{position:relative;margin-bottom:14px}.tv:before{content:'';position:absolute;left:-21px;top:4px;width:11px;height:11px;border-radius:50%;background:var(--g2);border:2px solid #fff}
.tv.ns:before{background:#c0392b}.tv .d{font-weight:700;font-size:13px}.tv .m{font-size:12px;color:var(--muted)}.tv img,.strip img{height:64px;border-radius:7px;margin:3px 3px 0 0}
.tv details{margin-top:4px;font-size:12px}.tv pre{white-space:pre-wrap;font-family:inherit;background:#f7faf8;border-radius:8px;padding:8px;margin:4px 0}
.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:4px}.strip div{font-size:11px;color:var(--muted);flex:none}
.intake{margin-top:4px;padding:5px 7px;background:#fff8e6;border:1px solid #f3dfa8;border-radius:7px;font-size:12px;color:#5c4400;max-width:260px}
</style></head><body>
<header><div><h1 id=cn>ClinicFlow</h1><div class=sub id=dt></div></div>
<div class=top-acts><button onclick="window.open('tv?key='+K,'_blank')">📺 Waiting-room TV</button> <button onclick="summary()">📧 Send doctor summary</button> <button onclick="reset()">↺ Reset today (demo)</button></div></header>
<main><div class=tiles id=tiles></div><div class=queues id=qs></div></main><div id=toast></div><div id=modal onclick="if(event.target.id=='modal')this.style.display='none'"><div class=box id=mbox></div></div>
<script>
const K=new URLSearchParams(location.search).get('key')||'';
const T=[['served','Patients served'],['waiting','Waiting now'],['avg_wait_min','Avg wait (min)'],['home_wait_min_saved','Waiting-room min saved',1],
['calls_avoided','Reception calls avoided',1],['nudges','Leave-now nudges'],['no_shows','No-shows'],['package_bookings','Package sessions booked',1],['redflags','🚩 Red flags'],['followups','Follow-ups booked',1],['fb_good','😊 Happy patients'],['fb_bad','😞 Needs attention'],['intakes','Intakes ready'],['prescriptions','💊 Rx sent'],['slot_bookings','🕔 Laser slots'],['locations','Locations shared']];
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.style.display='block';clearTimeout(window._t);window._t=setTimeout(()=>e.style.display='none',3500)}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){const r=await fetch('api/queue?key='+K);if(!r.ok){document.body.innerHTML='<p style=padding:20px>Access denied</p>';return}const d=await r.json();
cn.textContent=d.clinic+' · Front desk';dt.textContent=d.date+' · '+d.time+' IST';
tiles.innerHTML=T.map(([k,l,h])=>`<div class="tile${h?' hi':''}"><b>${d.metrics[k]}</b><span>${l}</span></div>`).join('');
qs.innerHTML=Object.entries(d.queues).map(([q,Q])=>`<div class=q><div class=qh><div><h2>${esc(Q.label)}</h2><div class=now>~${Q.service_min} min per patient${Q.learned_from?` <span class="pill ok">learned from ${Q.learned_from} visit${Q.learned_from>1?'s':''} today</span>`:''}${Q.delay_min?` <span class="pill late">⏱️ doctor +${Q.delay_min} min</span>`:''}</div></div>
<div class=now>Now serving<b>${Q.now_serving||'–'}</b></div></div>
<div class=acts><select id=fu_${q} title="Follow-up for the patient being finished"><option value=0>No follow-up</option><option value=7>Follow-up 1 wk</option><option value=14>Follow-up 2 wk</option><option value=28>Follow-up 4 wk</option><option value=42>Follow-up 6 wk</option></select><button onclick="act('next','${q}')">✅ Call next</button><button class=warn onclick="act('skip','${q}')">⏭️ No-show, call next</button>
<button class=sec onclick="act('done','${q}')">Finish current</button><input id=wn_${q} placeholder="Walk-in name"><button class=sec onclick="walk('${q}')">+ Walk-in</button>
<select id=dl_${q}><option value=15>15 min</option><option value=30 selected>30 min</option><option value=45>45 min</option><option value=60>60 min</option><option value=0>clear delay</option></select><button class=warn onclick="delay('${q}')">⏱️ Doctor delayed</button></div>
<table><thead><tr><th>Token</th><th>Patient</th><th>Status</th><th>ETA</th><th>Signals</th><th>📷</th></tr></thead><tbody>
${Q.tokens.length?Q.tokens.map(t=>`<tr${t.urgent&&t.status!='done'?' class=urg':''}><td><b>${t.label}</b>${t.urgent?'<br><span class="pill late">🚩</span>':''}</td><td>${esc(t.name)}<br><small>${esc(t.service)}</small><br><small style=color:#99a>${t.phone}</small>${t.family?'<span class=pill>👨‍👩‍👧 family</span>':''}${t.rx_sent?'<span class="pill ok">💊 Rx sent</span>':''}${t.slot?'<span class=pill>🕔 '+t.slot+'</span>':''}${t.deposit_status=='requested'?`<span class="pill late">💳 ₹${t.deposit_amount} due</span> <a href=# onclick="paid('${q}','${t.label}');return false" style=font-size:11px>mark paid</a>`:''}${t.deposit_status=='paid'?'<span class="pill ok">💳 paid</span>':''}${t.urgent?`<div class=urgline>🚩 Urgent: ${esc(t.urgent_reason)}</div>`:''}${t.intake_summary?`<div class=intake>📝 ${esc(t.intake_summary)}</div>`:''}${t.hist?`<div class=hist>${t.hist.returning?`🔁 <b>Visit #${t.hist.visit_no}</b> · last seen ${esc(fmtd(t.hist.last_date))}<br>Concern: ${esc(t.hist.last_concern||'–')}${t.hist.last_rx?'<br>Last Rx: '+esc(t.hist.last_rx):''}`:'🆕 <b>First visit</b>'}${t.hist.allergies?` <span class="pill late">⚠️ ${esc(t.hist.allergies)}</span>`:''}${(t.hist.packages||[]).map(p=>`<span class=pill>📦 ${esc(p)}</span>`).join('')}${t.hist.no_shows?`<span class=pill>${t.hist.no_shows} no-show</span>`:''}</div>`:''}${t.wa?`<a href=# class=hl onclick="hist('${q}','${t.label}');return false">👤 Patient history</a>`:''}</td>
<td class=${t.status}>${t.status.replace('_',' ')}</td><td>${t.eta_min!=null?t.eta_min+' min<br><small>'+t.people_ahead+' ahead</small>':''}</td>
<td>${t.arrived?'<span class="pill ok">✓ arrived</span>':''}${t.nudged?'<span class=pill>🚶 nudged</span>':''}${t.late?'<span class="pill late">⏰ late</span>':''}${t.travel_min?`<span class="pill loc">${t.travel_min} min away${t.travel_source=='amazon-location'?' · live traffic'+(t.travel_age_min!=null?' · '+(t.travel_age_min<1?'just now':t.travel_age_min+'m ago'):''):''}</span>`:''}${t.loc_refresh_asked?'<span class=pill>📍 asked for location</span>':''}${t.package?'<span class=pill>📦 '+esc(t.package)+'</span>':''}${t.followup_due?'<span class=pill>🗓️ '+t.followup_due.slice(5)+'</span>':''}${t.feedback?`<span class="pill ${t.feedback=='bad'?'late':'ok'}">${{good:'😊',ok:'😐',bad:'😞 call back'}[t.feedback]}</span>`:''}</td>
<td class=ph>${(t.photos||[]).map(u=>`<a href="${u}" target=_blank><img src="${u}"></a>`).join('')||(t.photo_count||'')}${(t.prev_photos||[]).length?`<div class=prev>prev ${t.prev_photos[0].date.slice(5)}: ${t.prev_photos.map(p=>`<a href="${p.url}" target=_blank><img src="${p.url}"></a>`).join('')}</div>`:''}${(t.photos||[]).length&&(t.prev_photos||[]).length?`<a class=cmp href="#" onclick="cmp('${t.photos[t.photos.length-1]}','${t.prev_photos[t.prev_photos.length-1].url}','${t.prev_photos[0].date}');return false">⇄ Compare</a>`:''}${t.status=='in_consult'&&t.phone!='walk-in'?`<br><button class=rxb onclick="rx('${q}','${t.label}')">🎙️ Rx</button>`:''}</td></tr>`).join(''):'<tr><td colspan=6 style="color:#99a;padding:16px">No tokens yet today</td></tr>'}
</tbody></table></div>`).join('')}
async function act(a,q){const fu=(document.getElementById('fu_'+q)||{}).value||0;const r=await fetch(`api/${a}?key=${K}&q=${q}&fu=${a=='skip'?0:fu}`,{method:'POST'});const d=await r.json();
if(document.getElementById('fu_'+q))document.getElementById('fu_'+q).value=0;
toast(a=='done'?'Finished':(d.called?'Called '+d.called:'Queue empty')+(d.nudged&&d.nudged.length?' · leave-now sent to '+d.nudged.join(', '):''));load()}
async function walk(q){const n=document.getElementById('wn_'+q).value.trim();if(!n)return toast('Enter a name');
const r=await fetch(`api/walkin?key=${K}&q=${q}&name=${encodeURIComponent(n)}`,{method:'POST'});const d=await r.json();toast('Walk-in token '+d.token);document.getElementById('wn_'+q).value='';load()}
async function delay(q){const m=document.getElementById('dl_'+q).value;const r=await fetch(`api/delay?key=${K}&q=${q}&min=${m}`,{method:'POST'});const d=await r.json();
toast(d.delay_min?`Delay +${d.delay_min} min sent to ${d.told.length} patient(s)`:'Delay cleared');load()}
function cmp(now,prev,pd){mbox.innerHTML=`<h3 style=margin-top:0>Progress comparison</h3><div class=cmpgrid><div><b>Previous visit (${pd})</b><img src="${prev}"></div><div><b>Today</b><img src="${now}"></div></div><p><button class=sec onclick="modal.style.display='none'">Close</button></p>`;modal.style.display='flex'}
const FB={good:'😊',ok:'😐',bad:'😞'};
function fmtd(d){if(!d)return '–';try{return new Date(d+'T00:00:00').toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'numeric'})}catch(e){return d}}
async function hist(q,label){mbox.innerHTML='<p>Loading patient history…</p>';modal.style.display='flex';
const h=await (await fetch(`api/history?key=${K}&q=${q}&t=${encodeURIComponent(label)}`)).json();
if(!h.ok){mbox.innerHTML=`<p>${esc(h.error||'No history')}</p><button class=sec onclick="modal.style.display='none'">Close</button>`;return}
const b=h.brief,fbs=Object.entries(h.feedback).filter(([k,v])=>v).map(([k,v])=>FB[k]+'×'+v).join(' ')||'–';
mbox.innerHTML=`<div class=hh><div><h3>👤 ${esc(h.name)}</h3><div style="color:var(--muted);font-size:13px">${h.returning?`Returning patient · visit #${b.visit_no} today · since ${fmtd(h.first_visit)}`:'🆕 First visit to the clinic'} · today ${esc(h.today.label)} (${esc(h.today.queue)})</div>
${h.allergies?`<span class="pill late">⚠️ Allergies: ${esc(h.allergies)}</span>`:''}${h.today.urgent_reason?`<span class="pill late">🚩 ${esc(h.today.urgent_reason)}</span>`:''}${h.family.length?`<span class=pill>👨‍👩‍👧 family on this phone: ${h.family.map(esc).join(', ')}</span>`:''}</div>
<button class=sec onclick="modal.style.display='none'">Close</button></div>
<div class=hsum><div><b>${h.visit_count}</b><span>past visits</span></div><div><b>${h.no_shows}</b><span>no-shows</span></div><div><b>${h.avg_wait_min??'–'}${h.avg_wait_min!=null?'<small> min</small>':''}</b><span>avg wait</span></div><div><b>${h.next_followup?fmtd(h.next_followup.due):'–'}</b><span>next follow-up</span></div><div><b style=font-size:16px>${fbs}</b><span>feedback</span></div></div>
<div class=ai id=preread>✨ AI pre-read: preparing…</div>
${h.today.intake_summary?`<div class=intake style=max-width:none>📝 <b>Today:</b> ${esc(h.today.intake_summary)}</div>`:''}
${h.concerns.length?`<p style=font-size:13px><b>Concerns over time:</b> ${h.concerns.map(x=>`<span class=pill>${esc(x)}</span>`).join('')}</p>`:''}
${h.packages.length?`<h4 style=margin-bottom:4px>📦 Treatment packages</h4>${h.packages.map(p=>`<div class=pk><b>${esc(p.name)}</b> · ${p.used}/${p.total} sessions${p.used<p.total?` · next due ${fmtd(p.next_due)}`:' · ✅ completed'}<div class=bar><i style="width:${Math.round(100*p.used/Math.max(1,p.total))}%"></i></div></div>`).join('')}`:''}
${h.photo_timeline.length?`<h4 style=margin-bottom:4px>📷 Photo timeline</h4><div class=strip>${h.photo_timeline.map(p=>`<div>${fmtd(p.date)}<br>${p.urls.map(u=>`<a href="${u}" target=_blank><img src="${u}"></a>`).join('')}</div>`).join('')}</div>`:''}
<h4 style=margin-bottom:4px>🗂️ Visit timeline</h4>${h.visits.length?`<div class=tl>${h.visits.map(v=>`<div class="tv${v.status=='no_show'?' ns':''}"><div class=d>${fmtd(v.date)} · ${v.queue=='LASER'?'Laser':'OPD'} ${esc(v.label||'')}${v.family?` · for ${esc(v.who)}`:''}${v.status=='no_show'?' · <span style=color:#c0392b>missed</span>':''} ${v.feedback?FB[v.feedback]||'':''}</div>
<div class=m>${esc(v.service||'')}${v.package?' · 📦 '+esc(v.package):''}${v.wait_min?` · waited ${v.wait_min} min`:''}${v.consult_min?` · ${v.consult_min} min with doctor`:''}${v.followup_due?` · follow-up ${fmtd(v.followup_due)}`:''}</div>
${v.intake_summary?`<div style=font-size:12px>📝 ${esc(v.intake_summary)}</div>`:''}${v.urgent_reason?`<div style="font-size:12px;color:#c0392b">🚩 ${esc(v.urgent_reason)}</div>`:''}
${v.rx_text?`<details><summary>💊 Prescription</summary><pre>${esc(v.rx_text)}</pre></details>`:''}${(v.photo_urls||[]).map(u=>`<a href="${u}" target=_blank><img src="${u}"></a>`).join('')}</div>`).join('')}</div>`:'<p style="color:var(--muted);font-size:13px">No past visits yet. Today\'s visit will be saved here automatically when the doctor finishes it.</p>'}`;
const pr=await (await fetch(`api/preread?key=${K}&q=${q}&t=${encodeURIComponent(label)}`,{method:'POST'})).json();
const el=document.getElementById('preread');if(el)el.textContent=pr.ok?`✨ AI pre-read${pr.engine=='rules'?'':' (Azure OpenAI)'}\n${pr.text}`:'✨ AI pre-read unavailable'}
let REC=null;
function rx(q,label){mbox.innerHTML=`<h3 style=margin-top:0>🎙️ Prescription for ${label}</h3><p style=color:#667;margin-top:0>Speak or type. e.g. "Adapalene 0.1% gel at night, sunscreen SPF 50 every morning, review after 4 weeks"</p>
<textarea id=rxt></textarea><p><button id=mic onclick="mic()">🎙️ Dictate</button> <button onclick="sendrx('${q}','${label}')">📤 Send to patient on WhatsApp</button> <button class=sec onclick="modal.style.display='none'">Cancel</button></p><div id=micst style="color:#667;font-size:13px"></div><div id=rxout></div>`;modal.style.display='flex'}
let MR=null,CH=[],BASE='';
function micst(t){const e=document.getElementById('micst');if(e)e.textContent=t}
async function mic(){const b=document.getElementById('mic'),ta=document.getElementById('rxt');
if(MR||REC){if(REC){try{REC.stop()}catch(e){}REC=null}if(MR&&MR.state!='inactive')MR.stop();b.textContent='🎙️ Dictate';return}
BASE=ta.value?ta.value.trim()+' ':'';
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
if(SR){REC=new SR();REC.lang='en-IN';REC.continuous=true;REC.interimResults=false;REC.onresult=e=>{for(let i=e.resultIndex;i<e.results.length;i++)ta.value+=e.results[i][0].transcript+' '};REC.onend=()=>{REC=null};try{REC.start()}catch(e){REC=null}}
try{const st=await navigator.mediaDevices.getUserMedia({audio:true});CH=[];
const mt=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/mp4'].find(m=>window.MediaRecorder&&MediaRecorder.isTypeSupported(m))||'';
MR=new MediaRecorder(st,mt?{mimeType:mt}:undefined);MR.ondataavailable=e=>{if(e.data.size)CH.push(e.data)};
MR.onstop=()=>{st.getTracks().forEach(t=>t.stop());const blob=new Blob(CH,{type:MR.mimeType||'audio/webm'});MR=null;transcribe(blob)};MR.start();
micst(REC?'🔴 Listening… live text from browser, Amazon Transcribe refines it when you stop':'🔴 Recording… Amazon Transcribe will convert it when you stop')}
catch(e){MR=null;if(!REC)return toast('Microphone not available. Type instead');micst('🔴 Listening (browser speech only)')}
b.textContent='⏹️ Stop'}
async function transcribe(blob){const ta=document.getElementById('rxt');if(!blob.size){micst('');return}
micst('⏳ Amazon Transcribe is converting your dictation…');
try{const r=await fetch(`api/transcribe?key=${K}&ct=${encodeURIComponent(blob.type)}`,{method:'POST',headers:{'content-type':blob.type},body:blob});const d=await r.json();
if(!d.ok)throw new Error(d.error||'start failed');const snap=ta.value;
for(let i=0;i<30;i++){await new Promise(z=>setTimeout(z,2000));const p=await (await fetch(`api/transcribe?key=${K}&job=${d.job}`)).json();
if(p.status=='done'){if(!p.text)throw new Error('empty');if(ta.value==snap){ta.value=BASE+p.text;micst('✅ Transcribed by Amazon Transcribe (en-IN). Edit if needed, then send.')}else{micst('✅ Amazon Transcribe: '+p.text)}return}
if(p.status=='failed')throw new Error(p.error||'failed')}
throw new Error('timeout')}catch(e){micst(ta.value.trim()?'ℹ️ Amazon Transcribe unavailable ('+e.message+'). Using browser speech text.':'⚠️ Could not transcribe ('+e.message+'). Please type the prescription.')}}
async function sendrx(q,label){const txt=document.getElementById('rxt').value.trim();if(!txt)return toast('Write or dictate the prescription');
document.getElementById('rxout').textContent='Formatting with AI…';
const r=await fetch(`api/rx?key=${K}&q=${q}&t=${encodeURIComponent(label)}`,{method:'POST',body:txt});const d=await r.json();
document.getElementById('rxout').innerHTML=d.ok?`<div class=intake style=max-width:none;white-space:pre-wrap>${esc(d.message)}</div><p>✅ Sent to ${esc(d.name)} on WhatsApp${d.followup?' · follow-up '+d.followup:''}</p>`:'❌ '+esc(d.error||'failed');load()}
async function paid(q,l){await fetch(`api/paid?key=${K}&q=${q}&t=${encodeURIComponent(l)}`,{method:'POST'});toast('Deposit marked paid for '+l);load()}
async function summary(){const r=await fetch('api/summary?key='+K,{method:'POST'});const d=await r.json();toast(d.sent?'Summary emailed to '+d.to:'Email not sent (check SES)')}
async function reset(){if(!confirm("Clear today's queue (tokens, counters, stats)? Patients and packages are kept."))return;
const r=await fetch('api/reset?key='+K,{method:'POST'});const d=await r.json();toast('Cleared '+d.cleared+' rows');load()}
load();setInterval(load,5000);</script></body></html>"""


RX_INSTRUCTIONS = """You format a dermatologist's dictated prescription for a patient's WhatsApp.
Output plain WhatsApp text (use *bold*, no markdown headings), max ~900 characters:
💊 *Prescription* then one numbered line per medicine: name + strength, how to apply/take, when, how long.
Then *How to use* with 2-4 short practical tips in simple English followed by the same tips in simple Hindi (Devanagari).
Then the review date if the doctor mentioned one. Do NOT add medicines, doses or advice the doctor did not say.
If something is unclear, keep the doctor's exact words. End with: _As advised by your doctor. Call the clinic if you have side effects._
Finally, on a separate last line output exactly: FOLLOWUP_DAYS=<number of days until review, or 0>"""


def send_prescription(q: str, label: str, dictated: str) -> dict:
    import agent
    import re as _re
    tok = next((t for t in c.tokens(q=q) if t["label"] == label), None)
    if not tok or not dictated.strip():
        return {"ok": False, "error": "token or text missing"}
    text = None
    try:
        if agent.AZURE_ENDPOINT:
            r = agent._azure_call({"model": agent.AZURE_DEPLOYMENT, "instructions": RX_INSTRUCTIONS,
                                   "input": [{"role": "user", "content": f"Patient: {tok['name']}. Doctor dictated: {dictated}"}],
                                   "reasoning": {"effort": "low"}})
            text = "".join(p_.get("text", "") for o in r.get("output", []) if o.get("type") == "message"
                           for p_ in (o.get("content") or []))
    except Exception:
        c.log.exception("rx formatting failed")
    if not text:   # deterministic fallback: doctor's words as-is
        text = f"💊 *Prescription*\n{dictated.strip()}\n\n_As advised by your doctor._\nFOLLOWUP_DAYS=0"
    m = _re.search(r"FOLLOWUP_DAYS=(\d+)", text)
    fu_days = int(m.group(1)) if m else 0
    if not fu_days:
        w = _re.search(r"(\d+)\s*(week|wk|hafte|hafta)", dictated.lower())
        d = _re.search(r"(\d+)\s*(day|din)", dictated.lower())
        fu_days = int(w.group(1)) * 7 if w else (int(d.group(1)) if d else 0)
    msg = agent._wa_markdown(_re.sub(r"\n?FOLLOWUP_DAYS=\d+\s*$", "", text.strip()))
    cl = c.clinic()
    header = f"{cl.get('name')} · {tok['name']} · {c.now_ist().strftime('%d %b %Y')}\n\n"
    wa_ok = True
    try:
        c.wa_text(tok["phone"], header + msg)
    except Exception:
        c.log.exception("rx whatsapp send failed")
        wa_ok = False
    prof = c.patient(tok["phone"])
    if prof.get("email"):
        c.email(prof["email"], f"Your prescription – {cl.get('name')}", header + msg)
    due = None
    if fu_days:
        due = (c.now_ist() + c.dt.timedelta(days=fu_days)).strftime("%Y-%m-%d")
        c.ddb.put_item(Item={"pk": f"PATIENT#{tok['phone']}", "sk": f"FOLLOWUP#{due}", "due": due, "queue": q,
                             "reason": (tok.get("intake_summary") or tok.get("service", ""))[:120],
                             "from_token": tok["label"], "status": "pending"})
        c.set_token(tok, followup_due=due)
        c.bump("followups")
    c.set_token(tok, rx_sent=True, rx_text=msg[:1500], rx_at=c.iso_now())
    c.bump("prescriptions")
    return {"ok": True, "name": tok["name"], "message": header + msg, "followup": due, "whatsapp_sent": wa_ok}


def tv_state() -> dict:
    """Public-safe waiting-room view: first name + initial only."""
    st = state()
    out = {"clinic": st["clinic"], "time": st["time"], "queues": {}}
    for q, Q in st["queues"].items():
        def short(n):
            p = (n or "Patient").split()
            return p[0] + (f" {p[1][0]}." if len(p) > 1 else "")
        nowt = next((t for t in Q["tokens"] if t["status"] == "in_consult"), None)
        nxt = [t for t in Q["tokens"] if t["status"] == "waiting"][:4]
        out["queues"][q] = {"label": Q["label"], "delay_min": Q["delay_min"], "service_min": Q["service_min"],
                            "now": {"label": nowt["label"], "name": short(nowt["name"])} if nowt else None,
                            "next": [{"label": t["label"], "name": short(t["name"]), "eta": t.get("eta_min"),
                                      "arrived": bool(t.get("arrived"))} for t in nxt],
                            "waiting": sum(1 for t in Q["tokens"] if t["status"] == "waiting")}
    return out


TV_HTML = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Now serving</title><style>
body{margin:0;background:#0b2b1f;color:#fff;font-family:system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
header{display:flex;justify-content:space-between;align-items:center;padding:18px 28px;background:#08231a}
h1{margin:0;font-size:28px}.clock{font-size:26px;opacity:.85}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:22px;padding:22px}
.q{background:#113d2c;border-radius:20px;padding:22px 26px}.q h2{margin:0 0 10px;font-size:24px;opacity:.9}
.now{font-size:20px;opacity:.8}.big{font-size:120px;font-weight:800;line-height:1;margin:6px 0;color:#7CFFB2}
.nm{font-size:30px}.next{margin-top:18px;font-size:24px}.next div{display:flex;justify-content:space-between;padding:10px 0;border-top:1px solid #ffffff22}
.delay{background:#b54708;color:#fff;border-radius:12px;padding:10px 14px;font-size:22px;margin-bottom:10px}
footer{position:fixed;bottom:0;left:0;right:0;background:#25D366;color:#073;padding:14px 28px;font-size:24px;font-weight:700;text-align:center}
</style></head><body><header><h1 id=cn>Clinic</h1><div class=clock id=ck></div></header><div class=grid id=g></div>
<footer>📱 Skip the wait: WhatsApp <span style="color:#fff">"hi"</span> to +91 72178 11862. We'll tell you when to leave home.</footer>
<script>const K=new URLSearchParams(location.search).get('key')||'';
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){const r=await fetch('api/tv?key='+K);if(!r.ok)return;const d=await r.json();cn.textContent=d.clinic;ck.textContent=d.time;
g.innerHTML=Object.values(d.queues).map(Q=>`<div class=q>${Q.delay_min?`<div class=delay>⏱️ Doctor running ~${Q.delay_min} min late. Thank you for your patience</div>`:''}
<h2>${esc(Q.label)}</h2><div class=now>Now serving</div><div class=big>${Q.now?Q.now.label:'–'}</div><div class=nm>${Q.now?esc(Q.now.name):''}</div>
<div class=next><b>Next</b>${Q.next.map(t=>`<div><span>${t.label} · ${esc(t.name)}${t.arrived?' ✓':''}</span><span>~${t.eta??0} min</span></div>`).join('')||'<div><span>No one waiting</span></div>'}</div></div>`).join('')}
load();setInterval(load,5000);</script></body></html>"""


def _find_token(q: str, label: str) -> dict | None:
    return next((t for t in c.tokens(q=q) if t["label"] == label), None)


def history_view(q: str, label: str) -> dict:
    """Full patient profile for the doctor: visits timeline, packages, follow-ups, photo timeline."""
    tok = _find_token(q, label)
    if not tok or not _is_wa(tok["phone"]):
        return {"ok": False, "error": "no history for walk-ins"}
    h = c.patient_history(tok["phone"], tok.get("name"), bool(tok.get("family")))
    for v in h["visits"]:
        v["photo_urls"] = _photo_urls(v.get("photos", []))
        v.pop("pk", None)
        v.pop("sk", None)
    by_date = {}
    for ph in h["photo_history"]:
        by_date.setdefault(ph["date"], []).append(ph["key"])
    h["photo_timeline"] = [{"date": d, "urls": _photo_urls(keys)} for d, keys in sorted(by_date.items())][-8:]
    h.pop("photo_history", None)
    h["today"] = {k: tok.get(k) for k in ("label", "queue", "service", "status", "intake_summary", "urgent_reason", "slot")}
    h["brief"] = c.history_brief(h)
    h["ok"] = True
    return h


PREREAD_INSTRUCTIONS = """You write a dermatologist's 20-second pre-read before a patient walks in, from the clinic's structured records.
Output 3-4 short bullet lines starting with "• ", max 80 words total, plain text (no markdown headings).
Cover: who (today's visit number = todays_visit_number, patient since), the course so far (past concerns, what was prescribed, any follow-up missed or feedback),
today's concern versus last time, and watch-outs (allergies, packages due, no-shows, urgent flags).
Use ONLY facts in the data. Never diagnose, never suggest new medicines. If it's a first visit, say so and summarise today's intake."""


def preread(q: str, label: str) -> dict:
    h = history_view(q, label)
    if not h.get("ok"):
        return h
    slim = {"name": h["name"], "todays_visit_number": h["visit_count"] + 1, "past_visits": h["visit_count"], "first_visit": h["first_visit"], "no_shows": h["no_shows"],
            "allergies": h["allergies"], "packages": h["packages"], "next_followup": h["next_followup"],
            "feedback": h["feedback"], "today": h["today"],
            "visits": [{k: v.get(k) for k in ("date", "queue", "status", "service", "intake_summary", "rx_text",
                                               "followup_due", "feedback", "urgent_reason")} for v in h["visits"][:6]]}
    try:
        import agent
        if agent.AZURE_ENDPOINT:
            r = agent._azure_call({"model": agent.AZURE_DEPLOYMENT, "instructions": PREREAD_INSTRUCTIONS,
                                   "input": [{"role": "user", "content": json.dumps(slim, default=str)[:12000]}],
                                   "reasoning": {"effort": "low"}})
            text = "".join(p_.get("text", "") for o in r.get("output", []) if o.get("type") == "message"
                           for p_ in (o.get("content") or [])).strip()
            if text:
                c.bump("prereads")
                return {"ok": True, "text": text.replace("**", ""), "engine": "azure-openai"}
    except Exception:
        c.log.exception("preread failed")
    b = h["brief"]
    lines = [f"• {'Returning patient, visit #' + str(b['visit_no']) + ' (since ' + str(b['since']) + ')' if b['returning'] else 'First visit'}."]
    if b.get("last_date"):
        lines.append(f"• Last visit {b['last_date']}: {b.get('last_service') or '-'}. Main concern so far: {b.get('last_concern') or '-'}.")
    if b.get("last_rx"):
        lines.append(f"• Last Rx ({b.get('last_rx_date')}): {b['last_rx']}.")
    if h["today"].get("intake_summary"):
        lines.append(f"• Today: {h['today']['intake_summary']}.")
    watch = ([f"allergies: {b['allergies']}"] if b.get("allergies") else []) + b.get("packages", []) + \
            ([f"{b['no_shows']} no-show(s)"] if b.get("no_shows") else [])
    if watch:
        lines.append("• Watch: " + ", ".join(watch) + ".")
    return {"ok": True, "text": "\n".join(lines), "engine": "rules"}


_tr = None
TR_LANG = os.environ.get("TRANSCRIBE_LANG", "en-IN")


def transcribe_start(audio_b64: str, ctype: str) -> dict:
    """Dictated Rx audio -> S3 -> Amazon Transcribe batch job (en-IN). Async: the browser polls."""
    global _s3, _tr
    import base64
    if not c.MEDIA_BUCKET or not audio_b64:
        return {"ok": False, "error": "no audio"}
    ctype = (ctype or "").lower()
    fmt = next((f for f in ("webm", "ogg", "mp4", "m4a", "wav", "mp3", "flac") if f in ctype), "webm")
    job = f"clinicflow-rx-{int(time.time() * 1000)}"
    key = f"rx-audio/{c.today()}/{job}.{fmt}"
    try:
        _s3 = _s3 or boto3.client("s3")
        _tr = _tr or boto3.client("transcribe")
        _s3.put_object(Bucket=c.MEDIA_BUCKET, Key=key, Body=base64.b64decode(audio_b64), ContentType=ctype or "audio/webm")
        _tr.start_transcription_job(TranscriptionJobName=job, LanguageCode=TR_LANG, MediaFormat=fmt,
                                    Media={"MediaFileUri": f"s3://{c.MEDIA_BUCKET}/{key}"},
                                    OutputBucketName=c.MEDIA_BUCKET, OutputKey=f"rx-text/{job}.json")
        c.bump("transcribe_jobs")
        return {"ok": True, "job": job}
    except Exception as e:
        c.log.exception("transcribe start failed")
        return {"ok": False, "error": str(e)[:200]}


def transcribe_poll(job: str) -> dict:
    global _s3, _tr
    try:
        _s3 = _s3 or boto3.client("s3")
        _tr = _tr or boto3.client("transcribe")
        j = _tr.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
        st = j["TranscriptionJobStatus"]
        if st == "COMPLETED":
            obj = _s3.get_object(Bucket=c.MEDIA_BUCKET, Key=f"rx-text/{job}.json")
            data = json.loads(obj["Body"].read())
            text = " ".join(t.get("transcript", "") for t in data.get("results", {}).get("transcripts", [])).strip()
            return {"status": "done", "text": text, "engine": "amazon-transcribe"}
        if st == "FAILED":
            return {"status": "failed", "error": j.get("FailureReason", "")[:200]}
        return {"status": "working"}
    except Exception as e:
        c.log.exception("transcribe poll failed")
        return {"status": "failed", "error": str(e)[:200]}


def lambda_handler(event, context):
    if event.get("source") == "aws.events" or event.get("summary"):
        return send_summary()
    qs = event.get("queryStringParameters") or {}
    if KEY and qs.get("key") != KEY:
        return _resp(403, {"error": "forbidden"})
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    q = c.norm_queue(qs.get("q", "OPD"))
    if path.endswith("/api/queue"):
        return _resp(200, state())
    if path.endswith("/api/history"):
        return _resp(200, history_view(q, qs.get("t", "")))
    if path.endswith("/api/preread"):
        return _resp(200, preread(q, qs.get("t", "")))
    if path.endswith("/api/transcribe") and method == "GET":
        return _resp(200, transcribe_poll(qs.get("job", "")))
    if path.endswith("/api/tv"):
        return _resp(200, tv_state())
    if path.endswith("/tv"):
        return _resp(200, TV_HTML, "text/html; charset=utf-8")
    if method == "POST":
        fu = int(qs.get("fu", "0") or 0)
        if path.endswith("/api/next"):
            return _resp(200, call_next(q, fu_days=fu))
        if path.endswith("/api/skip"):
            return _resp(200, call_next(q, skip=True))
        if path.endswith("/api/done"):
            finish_current(q, fu_days=fu)
            return _resp(200, {"ok": True, "nudged": nudge_waiting(q)})
        if path.endswith("/api/walkin"):
            return _resp(200, walkin(q, qs.get("name", "")))
        if path.endswith("/api/summary"):
            return _resp(200, send_summary())
        if path.endswith("/api/delay"):
            return _resp(200, announce_delay(q, int(qs.get("min", "0") or 0)))
        if path.endswith("/api/rx"):
            body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", "ignore")
            return _resp(200, send_prescription(q, qs.get("t", ""), body))
        if path.endswith("/api/transcribe"):
            body = event.get("body") or ""
            if not event.get("isBase64Encoded"):
                import base64
                body = base64.b64encode(body.encode("latin-1", "ignore")).decode()
            return _resp(200, transcribe_start(body, qs.get("ct", "")))
        if path.endswith("/api/paid"):
            tok = next((t for t in c.tokens(q=q) if t["label"] == qs.get("t")), None)
            if tok:
                c.set_token(tok, deposit_status="paid", deposit_paid_at=c.iso_now())
                c.bump("deposits_paid")
            return _resp(200, {"ok": bool(tok)})
        if path.endswith("/api/reset"):
            return _resp(200, reset_today())
    return _resp(200, HTML, "text/html; charset=utf-8")
