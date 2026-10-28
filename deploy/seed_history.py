"""Demo data: give existing WhatsApp patients a realistic past at the clinic (visits, Rx, a missed follow-up,
a laser package). Only touches patients with no visit history yet. All rows are tagged seed=True.
    python3 deploy/seed_history.py            # seed
    python3 deploy/seed_history.py --clear    # remove seeded rows
"""
import datetime as dt
import sys

import boto3
from boto3.dynamodb.conditions import Attr, Key

t = boto3.resource("dynamodb").Table("clinicflow")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
today = dt.datetime.now(IST).date()
D = lambda n: (today - dt.timedelta(days=n)).isoformat()

RX1 = ("💊 *Prescription*\n1. Adapalene 0.1% gel: pea-sized amount on the whole face at night, 8 weeks\n"
       "2. Clindamycin 1% gel: on active pimples in the morning, 6 weeks\n3. Gentle face wash twice daily\n\n"
       "*How to use*\n• Apply on dry skin, avoid eyes and lips\n• Mild dryness in week 1-2 is normal\n"
       "• रात को सूखी त्वचा पर लगाएँ, आँखों से दूर रखें\n\n🗓️ Review after 4 weeks\n"
       "_As advised by your doctor. Call the clinic if you have side effects._")
RX2 = ("💊 *Prescription*\n1. Adapalene 0.1% gel: continue at night, 8 weeks\n"
       "2. Sunscreen SPF 50 gel: every morning, reapply after 3 hours outdoors\n3. Stop clindamycin gel\n\n"
       "*How to use*\n• Use sunscreen even indoors\n• धूप में निकलने से पहले सनस्क्रीन लगाएँ\n\n"
       "🗓️ Review after 6 weeks. Laser hair removal package started.\n"
       "_As advised by your doctor. Call the clinic if you have side effects._")


def visit(phone, days, label, q, status, who, service, intake=None, rx="", fu="", fb="", pkg="", wait=0, consult=0):
    i = intake or {}
    summ = " · ".join(x for x in [i.get("complaint"), i.get("duration"), ("tried " + i["tried"]) if i.get("tried") else "",
                                  ("allergies: " + i["allergies"]) if i.get("allergies") else ""] if x)
    item = {"pk": f"PATIENT#{phone}", "sk": f"VISIT#{D(days)}#{label}", "date": D(days), "label": label, "queue": q,
            "status": status, "who": who, "family": False, "service": service, "intake": i, "intake_summary": summ,
            "rx_text": rx, "followup_due": fu, "feedback": fb, "package": pkg, "wait_min": wait, "consult_min": consult,
            "seed": True}
    t.put_item(Item={k: v for k, v in item.items() if v not in ("", {}, None)} | {"family": False})


def profiles():
    r = t.scan(FilterExpression=Attr("pk").begins_with("PATIENT#") & Attr("sk").eq("PROFILE"))
    return r["Items"]


if "--clear" in sys.argv:
    n = 0
    for p in profiles():
        for it in t.query(KeyConditionExpression=Key("pk").eq(p["pk"]))["Items"]:
            if it.get("seed"):
                t.delete_item(Key={"pk": it["pk"], "sk": it["sk"]}); n += 1
    print("removed", n, "seeded rows"); sys.exit()

for p in profiles():
    phone = p["pk"].split("#", 1)[1]
    name = p.get("name") or p.get("wa_name")
    if not name or not phone.isdigit():
        continue
    have = t.query(KeyConditionExpression=Key("pk").eq(p["pk"]) & Key("sk").begins_with("VISIT#"))["Items"]
    if have:
        print(f"skip ..{phone[-4:]} ({name}): already has {len(have)} visits"); continue
    visit(phone, 76, "O4", "OPD", "done", name, "Acne consult",
          {"complaint": "acne on cheeks and forehead", "duration": "3 months", "tried": "face wash only", "allergies": "sulfa drugs"},
          RX1, fu=D(48), fb="good", wait=14, consult=8)
    visit(phone, 48, "O9", "OPD", "no_show", name, "Acne follow-up")
    visit(phone, 41, "O2", "OPD", "done", name, "Acne follow-up",
          {"complaint": "acne follow-up, about 60% better, some dark marks", "duration": "since last visit",
           "tried": "adapalene + clindamycin as prescribed", "allergies": "sulfa drugs"},
          RX2, fu=D(-1), fb="good", pkg="", wait=9, consult=6)
    visit(phone, 41, "L3", "LASER", "done", name, "Laser hair removal session 1", pkg="Laser hair removal", wait=11, consult=22)
    visit(phone, 13, "L1", "LASER", "done", name, "Laser hair removal session 2", pkg="Laser hair removal", fb="ok", wait=6, consult=20)
    pk = {"pk": p["pk"], "sk": "PACKAGE#Laser hair removal"}
    if "Item" not in t.get_item(Key=pk):
        t.put_item(Item={**pk, "total": 6, "used": 2, "interval_days": 28, "next_due": D(-15), "queue": "LASER", "seed": True})
    t.put_item(Item={"pk": p["pk"], "sk": f"FOLLOWUP#{D(-1)}", "due": D(-1), "queue": "OPD", "status": "pending",
                     "reason": "acne follow-up, dark marks", "from_token": "O2", "seed": True})
    print(f"seeded ..{phone[-4:]} ({name}): 5 past visits, laser package 2/6, follow-up {D(-1)}")
