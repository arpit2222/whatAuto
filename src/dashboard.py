"""Receptionist dashboard (Lambda Function URL).

GET  /?key=K            -> HTML queue board
GET  /api/queue?key=K   -> JSON
POST /api/next?key=K    -> finish current patient, call next token, send WhatsApp
                           "your turn" + "leave now" nudges based on travel time
POST /api/done?key=K    -> finish current patient without calling next
"""
import json
import os

import common as c

KEY = os.environ.get("DASHBOARD_KEY", "")


def _resp(code, body, ctype="application/json"):
    return {"statusCode": code, "headers": {"content-type": ctype},
            "body": body if isinstance(body, str) else json.dumps(body)}


def _set_status(tok: dict, status: str):
    c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"TOKEN#{tok['token']:03d}"},
                      UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":s": status})


def finish_current(send_summary=True):
    for t in c.tokens():
        if t["status"] == "in_consult":
            _set_status(t, "done")
            if t.get("package"):  # count the session and schedule the next one
                pk = {"pk": f"PATIENT#{t['phone']}", "sk": f"PACKAGE#{t['package']}"}
                gap = int(c.clean(c.ddb.get_item(Key=pk).get("Item", {})).get("interval_days", 28))
                nxt = (c.now_ist() + c.dt.timedelta(days=gap)).strftime("%Y-%m-%d")
                c.ddb.update_item(Key=pk, UpdateExpression="ADD used :one SET next_due = :d",
                                  ExpressionAttributeValues={":one": 1, ":d": nxt})
            prof = c.patient(t["phone"])
            cl = c.clinic()
            msg = f"Thanks for visiting {cl.get('name')}, {t['name']}! 🙏 Reply here anytime to book your next visit."
            c.wa_text(t["phone"], msg)
            if send_summary and prof.get("email"):
                c.email(prof["email"], f"Visit summary – {cl.get('name')}",
                        f"Hi {t['name']},\n\nThanks for visiting today (token #{t['token']}, {t['service']}).\n"
                        f"Photos shared: {len(t.get('photos', []))}.\n"
                        "Your doctor's notes and next steps will be shared at the front desk.\n\n"
                        f"Book your next visit anytime on WhatsApp.\n{cl.get('name')}")


def nudge_waiting():
    """Leave-now nudges: if ETA <= travel time + 5 min buffer, ping once."""
    sent = []
    for t in c.waiting():
        if t.get("nudged"):
            continue
        ahead, eta = c.eta_minutes(t)
        travel = int(c.patient(t["phone"]).get("travel_min", 15))
        if eta <= travel + 5:
            c.wa_text(t["phone"],
                      f"🚶 Time to leave, {t['name']}! Token #{t['token']}: {ahead} ahead of you, "
                      f"~{eta} min wait. You're ~{travel} min away.")
            c.ddb.update_item(Key={"pk": c.day_pk(), "sk": f"TOKEN#{t['token']:03d}"},
                              UpdateExpression="SET nudged = :t", ExpressionAttributeValues={":t": True})
            sent.append(t["token"])
    return sent


def call_next():
    finish_current()
    w = c.waiting()
    called = None
    if w:
        nxt = w[0]
        _set_status(nxt, "in_consult")
        c.ddb.update_item(Key={"pk": c.day_pk(), "sk": "COUNTER"}, UpdateExpression="SET now_serving = :n",
                          ExpressionAttributeValues={":n": nxt["token"]})
        c.wa_text(nxt["phone"], f"✅ It's your turn, {nxt['name']}! Token #{nxt['token']} – please come to the doctor's cabin.")
        called = nxt["token"]
    return {"called": called, "nudged": nudge_waiting()}


def state():
    cl = c.clinic()
    toks = c.tokens()
    out = []
    for t in toks:
        row = {k: t.get(k) for k in ("token", "name", "service", "status", "nudged", "package")}
        row["phone"] = "xxxxxx" + t["phone"][-4:]
        row["photos"] = len(t.get("photos", []))
        if t["status"] == "waiting":
            row["people_ahead"], row["eta_min"] = c.eta_minutes(t)
        out.append(row)
    return {"clinic": cl.get("name"), "date": c.today(), "now_serving": c.counter().get("now_serving", 0),
            "avg_consult_min": cl.get("avg_consult_min"), "tokens": out}


HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ClinicFlow – Front desk</title><style>
body{font-family:system-ui,sans-serif;margin:0;background:#f5f7f6;color:#14211c}
header{background:#0f5132;color:#fff;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
h1{font-size:20px;margin:0}.big{font-size:42px;font-weight:700}main{padding:16px 20px;max-width:900px;margin:auto}
button{background:#198754;color:#fff;border:0;border-radius:10px;padding:14px 22px;font-size:17px;cursor:pointer;margin-right:8px}
button.sec{background:#6c757d}table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;margin-top:16px}
td,th{padding:10px;border-bottom:1px solid #e3e8e5;text-align:left;font-size:15px}
.waiting{color:#a15c00}.in_consult{color:#0f5132;font-weight:700}.done,.cancelled{color:#999}
#log{margin-top:10px;color:#555;min-height:20px}</style></head><body>
<header><div><h1 id=cn>ClinicFlow</h1><div id=dt></div></div><div>Now serving<div class=big id=ns>–</div></div></header>
<main><button onclick="act('next')">✅ Call next patient</button><button class=sec onclick="act('done')">Finish current</button>
<div id=log></div><table><thead><tr><th>#</th><th>Patient</th><th>Service</th><th>Status</th><th>ETA</th><th>Nudged</th><th>📷</th></tr></thead><tbody id=tb></tbody></table></main>
<script>
const K=new URLSearchParams(location.search).get('key')||'';
async function load(){const r=await fetch('api/queue?key='+K);const d=await r.json();
cn.textContent=d.clinic+' – Front desk';dt.textContent=d.date+' · avg '+d.avg_consult_min+' min/patient';ns.textContent=d.now_serving||'–';
tb.innerHTML=d.tokens.map(t=>`<tr><td>${t.token}</td><td>${t.name}<br><small>${t.phone}</small></td><td>${t.service}${t.package?' (pkg)':''}</td><td class=${t.status}>${t.status.replace('_',' ')}</td><td>${t.eta_min!=null?t.eta_min+' min':''}</td><td>${t.nudged?'✓':''}</td><td>${t.photos||''}</td></tr>`).join('')}
async function act(a){log.textContent='…';const r=await fetch('api/'+a+'?key='+K,{method:'POST'});const d=await r.json();
log.textContent=a=='next'?(d.called?'Called token #'+d.called:'Queue empty')+(d.nudged&&d.nudged.length?' · leave-now sent to #'+d.nudged.join(', #'):''):'Done';load()}
load();setInterval(load,5000);</script></body></html>"""


def lambda_handler(event, context):
    qs = event.get("queryStringParameters") or {}
    if KEY and qs.get("key") != KEY:
        return _resp(403, {"error": "forbidden"})
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if path.endswith("/api/queue"):
        return _resp(200, state())
    if path.endswith("/api/next") and method == "POST":
        return _resp(200, call_next())
    if path.endswith("/api/done") and method == "POST":
        finish_current()
        return _resp(200, {"ok": True, "nudged": nudge_waiting()})
    return _resp(200, HTML, "text/html; charset=utf-8")
