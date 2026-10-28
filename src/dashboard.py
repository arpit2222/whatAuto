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
def finish_current(q: str, status: str = "done"):
    cl = c.clinic()
    for t in c.tokens(q=q):
        if t["status"] != "in_consult":
            continue
        c.set_token(t, status=status, done_at=c.iso_now())
        if status == "no_show":
            c.bump("no_shows")
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
        c.wa_text(t["phone"], f"Thanks for visiting {cl.get('name')}, {t['name']}! 🙏 "
                              "Reply here anytime to book your next visit.")
        if prof.get("email"):
            link = c.wa_link("token")
            c.email(prof["email"], f"Visit summary – {cl.get('name')}",
                    f"Hi {t['name']},\n\nThanks for visiting today (token {t['label']}, {t['service']}).\n"
                    f"Photos shared: {len(t.get('photos', []))}.\n"
                    "Your doctor's notes and next steps will be shared at the front desk.\n\n"
                    f"Book your next visit on WhatsApp: {link}\n{cl.get('name')}")


def nudge_waiting(q: str) -> list:
    """Leave-now nudges: if remaining wait <= travel time + 5 min buffer, ping once."""
    sent = []
    for t in c.waiting(q=q):
        if t.get("nudged") or t.get("arrived") or not _is_wa(t["phone"]):
            continue
        ahead, eta = c.eta_minutes(t)
        prof = c.patient(t["phone"])
        travel = int(prof.get("travel_min", 15))
        if eta <= travel + 5:
            src = " (Amazon Location)" if prof.get("travel_source") == "amazon-location" else ""
            try:
                c.wa_buttons(t["phone"], f"🚶 Time to leave, {t['name']}! Token {t['label']}: {ahead} ahead of you, "
                                         f"~{eta} min. You're ~{travel} min away{src}.",
                             [("b_here", "📍 I'm here"), ("b_late", "⏰ Running late"), ("b_status", "🔄 Refresh")])
            except Exception:
                c.log.exception("nudge failed")
            c.set_token(t, nudged=True, nudged_at=c.iso_now())
            c.bump("nudges")
            sent.append(t["label"])
    return sent


def call_next(q: str, skip: bool = False) -> dict:
    finish_current(q, "no_show" if skip else "done")
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
                                           f"Please come to the {room}.", [("b_here", "📍 I'm here")])
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
        "bookings": int(st.get("bookings", 0)),
        "walkins": int(st.get("walkins", 0)),
    }


def state() -> dict:
    cl = c.clinic()
    toks = c.tokens()
    queues = {}
    for q in c.QUEUES:
        cfg = c.queue_cfg(q, cl)
        rows = []
        for t in [x for x in toks if x["queue"] == q]:
            row = {k: t.get(k) for k in ("label", "name", "service", "status", "nudged", "package", "arrived", "late")}
            row["phone"] = "walk-in" if not _is_wa(t["phone"]) else "xxxxxx" + t["phone"][-4:]
            row["photos"] = _photo_urls(t.get("photos", [])) if t["status"] in ("waiting", "in_consult") else []
            row["photo_count"] = len(t.get("photos", []))
            if t["status"] == "waiting":
                row["people_ahead"], row["eta_min"] = c.eta_minutes(t)
                prof = c.patient(t["phone"]) if _is_wa(t["phone"]) else {}
                row["travel_min"] = prof.get("travel_min")
                row["travel_source"] = prof.get("travel_source")
            rows.append(row)
        queues[q] = {"label": cfg["label"], "service_min": cfg["service_min"],
                     "now_serving": c.counter(q).get("now_serving_label", ""), "tokens": rows}
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
    q_lines = "".join(
        f"<li><b>{html.escape(q['label'])}</b>: {sum(1 for t in q['tokens'] if t['status'] == 'done')} served, "
        f"{sum(1 for t in q['tokens'] if t['status'] == 'waiting')} still waiting</li>" for q in s["queues"].values())
    due_html = ("".join(f"<tr><td>{html.escape(d['name'])}</td><td>{html.escape(d['package'])}</td>"
                        f"<td>{d['session']}</td><td>{d['due']}</td></tr>" for d in due)
                or "<tr><td colspan=4>No package sessions due.</td></tr>")
    body = (f"<div style='font-family:Arial,sans-serif;max-width:640px'>"
            f"<h2 style='color:#0f5132'>{html.escape(cl.get('name', 'Clinic'))}: daily summary, {s['date']}</h2>"
            f"<table style='border-collapse:collapse;width:100%'><tr>{tile_html}</tr></table>"
            f"<h3>Queues</h3><ul>{q_lines}</ul>"
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
input{padding:9px;border:1px solid var(--line);border-radius:8px;font-size:14px;width:130px}
table{width:100%;border-collapse:collapse}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}.waiting{color:#a15c00}.in_consult{color:var(--g);font-weight:700}
.done,.cancelled,.no_show{color:#9aa}.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;margin:1px;background:#eef2f0;color:#335}
.pill.ok{background:#e1f3e8;color:var(--g)}.pill.late{background:#fde8d7;color:#9a3d00}.pill.loc{background:#e6effc;color:#1849a9}
.ph img{width:40px;height:40px;object-fit:cover;border-radius:6px;margin-right:3px;cursor:zoom-in}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#14211c;color:#fff;padding:10px 16px;border-radius:10px;display:none}
.top-acts button{background:#ffffff22;border:1px solid #ffffff55}
</style></head><body>
<header><div><h1 id=cn>ClinicFlow</h1><div class=sub id=dt></div></div>
<div class=top-acts><button onclick="summary()">📧 Send doctor summary</button> <button onclick="reset()">↺ Reset today (demo)</button></div></header>
<main><div class=tiles id=tiles></div><div class=queues id=qs></div></main><div id=toast></div>
<script>
const K=new URLSearchParams(location.search).get('key')||'';
const T=[['served','Patients served'],['waiting','Waiting now'],['avg_wait_min','Avg wait (min)'],['home_wait_min_saved','Waiting-room min saved',1],
['calls_avoided','Reception calls avoided',1],['nudges','Leave-now nudges'],['no_shows','No-shows'],['package_bookings','Package sessions booked',1],['photos','Photos received'],['locations','Locations shared']];
function toast(m){const e=document.getElementById('toast');e.textContent=m;e.style.display='block';clearTimeout(window._t);window._t=setTimeout(()=>e.style.display='none',3500)}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){const r=await fetch('api/queue?key='+K);if(!r.ok){document.body.innerHTML='<p style=padding:20px>Access denied</p>';return}const d=await r.json();
cn.textContent=d.clinic+' · Front desk';dt.textContent=d.date+' · '+d.time+' IST';
tiles.innerHTML=T.map(([k,l,h])=>`<div class="tile${h?' hi':''}"><b>${d.metrics[k]}</b><span>${l}</span></div>`).join('');
qs.innerHTML=Object.entries(d.queues).map(([q,Q])=>`<div class=q><div class=qh><div><h2>${esc(Q.label)}</h2><div class=now>~${Q.service_min} min per patient</div></div>
<div class=now>Now serving<b>${Q.now_serving||'–'}</b></div></div>
<div class=acts><button onclick="act('next','${q}')">✅ Call next</button><button class=warn onclick="act('skip','${q}')">⏭️ No-show, call next</button>
<button class=sec onclick="act('done','${q}')">Finish current</button><input id=wn_${q} placeholder="Walk-in name"><button class=sec onclick="walk('${q}')">+ Walk-in</button></div>
<table><thead><tr><th>Token</th><th>Patient</th><th>Status</th><th>ETA</th><th>Signals</th><th>📷</th></tr></thead><tbody>
${Q.tokens.length?Q.tokens.map(t=>`<tr><td><b>${t.label}</b></td><td>${esc(t.name)}<br><small>${esc(t.service)}</small><br><small style=color:#99a>${t.phone}</small></td>
<td class=${t.status}>${t.status.replace('_',' ')}</td><td>${t.eta_min!=null?t.eta_min+' min<br><small>'+t.people_ahead+' ahead</small>':''}</td>
<td>${t.arrived?'<span class="pill ok">✓ arrived</span>':''}${t.nudged?'<span class=pill>🚶 nudged</span>':''}${t.late?'<span class="pill late">⏰ late</span>':''}${t.travel_min?`<span class="pill loc">${t.travel_min} min away${t.travel_source=='amazon-location'?' · AWS Location':''}</span>`:''}${t.package?'<span class=pill>📦 '+esc(t.package)+'</span>':''}</td>
<td class=ph>${(t.photos||[]).map(u=>`<a href="${u}" target=_blank><img src="${u}"></a>`).join('')||(t.photo_count||'')}</td></tr>`).join(''):'<tr><td colspan=6 style="color:#99a;padding:16px">No tokens yet today</td></tr>'}
</tbody></table></div>`).join('')}
async function act(a,q){const r=await fetch(`api/${a}?key=${K}&q=${q}`,{method:'POST'});const d=await r.json();
toast(a=='done'?'Finished':(d.called?'Called '+d.called:'Queue empty')+(d.nudged&&d.nudged.length?' · leave-now sent to '+d.nudged.join(', '):''));load()}
async function walk(q){const n=document.getElementById('wn_'+q).value.trim();if(!n)return toast('Enter a name');
const r=await fetch(`api/walkin?key=${K}&q=${q}&name=${encodeURIComponent(n)}`,{method:'POST'});const d=await r.json();toast('Walk-in token '+d.token);document.getElementById('wn_'+q).value='';load()}
async function summary(){const r=await fetch('api/summary?key='+K,{method:'POST'});const d=await r.json();toast(d.sent?'Summary emailed to '+d.to:'Email not sent (check SES)')}
async function reset(){if(!confirm("Clear today's queue (tokens, counters, stats)? Patients and packages are kept."))return;
const r=await fetch('api/reset?key='+K,{method:'POST'});const d=await r.json();toast('Cleared '+d.cleared+' rows');load()}
load();setInterval(load,5000);</script></body></html>"""


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
    if method == "POST":
        if path.endswith("/api/next"):
            return _resp(200, call_next(q))
        if path.endswith("/api/skip"):
            return _resp(200, call_next(q, skip=True))
        if path.endswith("/api/done"):
            finish_current(q)
            return _resp(200, {"ok": True, "nudged": nudge_waiting(q)})
        if path.endswith("/api/walkin"):
            return _resp(200, walkin(q, qs.get("name", "")))
        if path.endswith("/api/summary"):
            return _resp(200, send_summary())
        if path.endswith("/api/reset"):
            return _resp(200, reset_today())
    return _resp(200, HTML, "text/html; charset=utf-8")
