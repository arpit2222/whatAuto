"""Red-flag triage: deterministic safety screen that runs on every inbound message, before the AI.

Never diagnoses. Two levels:
  EMERGENCY -> tell the patient to call 112 / go to the nearest emergency immediately.
  URGENT    -> flag for the doctor and move the patient to the front of their queue.
"""
import re

import common as c

EMERGENCY = [
    (r"(saans|breath|breathing).{0,25}(nahi|problem|difficult|taklif|dikkat|tough|short|trouble)|can'?t breathe|saans.{0,10}(phool|ruk)", "breathing difficulty"),
    (r"(throat|gala|gale|tongue|jeebh|lips?|honth|face|chehra|chehre).{0,25}(swell|sujan|soojan|swollen|phool)", "face/throat swelling"),
    (r"(anaphyla|faint|behosh|unconscious|chakkar.{0,10}gir)", "fainting / severe reaction"),
]
URGENT = [
    (r"(rash|daane|dane|spots|chakatte).{0,40}(fever|bukhar|bukhaar)|(fever|bukhar|bukhaar).{0,40}(rash|daane|dane|spots|chakatte)", "rash with fever"),
    (r"(spread|fail rah|badh rah|growing).{0,25}(fast|tezi|jaldi|quickly)|(fast|tezi se).{0,15}(spread|fail)", "rapidly spreading"),
    (r"(blister|chhale|chale|burn|jal gay|jala|jalan).{0,40}(laser|peel|treatment|session)|(laser|peel).{0,40}(blister|chhale|burn|jal|jalan)", "blister/burn after procedure"),
    (r"\bpus\b|mavaad|bleed|khoon", "pus or bleeding"),
    (r"(severe|bahut|bohot|unbearable|tez).{0,15}(pain|dard)", "severe pain"),
    (r"(eye|aankh|aankhon).{0,30}(swell|sujan|red|laal|pain|dard|rash)", "eye involvement"),
    (r"(swell|sujan|soojan).{0,30}(after|baad).{0,20}(laser|peel|treatment|injection)", "swelling after procedure"),
]


def screen(text: str) -> tuple[str | None, str | None]:
    t = (text or "").lower()
    for pat, why in EMERGENCY:
        if re.search(pat, t):
            return "emergency", why
    for pat, why in URGENT:
        if re.search(pat, t):
            return "urgent", why
    return None, None


def handle(phone: str, text: str) -> str | None:
    """Returns a reply if the message is a red flag (and acts on it), else None."""
    level, why = screen(text)
    if not level:
        return None
    c.bump(f"redflag_{level}")
    tok = c.active_token(phone)
    if level == "emergency":
        if tok:
            c.set_token(tok, urgent=True, urgent_reason=f"EMERGENCY: {why}", urgent_text=text[:200])
        return ("🚨 This could be an emergency. *Please call 112 or go to the nearest hospital emergency now.* "
                "Don't wait for your clinic turn. I've informed the clinic too.")
    if not tok:
        import agent
        prof = c.patient(phone)
        agent._book(phone, prof.get("name") or prof.get("wa_name") or "Patient", "Urgent review", queue="OPD")
        tok = c.active_token(phone)
    ahead_before = c.eta_minutes(tok)[0]
    others = [w for w in c.waiting(q=tok["queue"]) if w["sk"] != tok["sk"]]
    new_pos = (min(w["pos"] for w in others) - 0.5) if others else tok["pos"]
    if tok["status"] == "waiting":
        c.set_token(tok, urgent=True, urgent_reason=why, urgent_text=text[:200], pos=float(new_pos))
    else:
        c.set_token(tok, urgent=True, urgent_reason=why, urgent_text=text[:200])
    return (f"🚩 I've flagged this for the doctor as a priority ({why}). Token {tok['label']} has been moved up "
            f"(was {ahead_before} ahead). Please come to the clinic now. If it gets worse, e.g. trouble breathing "
            "or face swelling, call 112 immediately.")
