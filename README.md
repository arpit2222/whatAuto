# ClinicFlow – the AI front desk that lives in WhatsApp

**An agentic WhatsApp front desk for small skin & laser clinics in India.** Patients book a token, wait at home until a
traffic-aware *"leave now"* ping, send photos and voice notes, and get their prescription as a PDF, all inside WhatsApp.
The doctor and receptionist run the clinic from one button, with every patient's history and an AI pre-read on screen.

Built on **AWS Communication Developer Services** (AWS End User Messaging Social for WhatsApp + Amazon SES), with the agent
hosted on **Amazon Bedrock AgentCore Runtime**.

> Built for the **AWS CDS Agentic AI Partner Hackathon** (Devpost) by **Dehix Technologies Pvt. Ltd.** (AWS Partner, India).
> Testing guide for judges: **[TESTING.md](TESTING.md)**

---

## 1. Try it in 2 minutes

| What | How |
|---|---|
| **WhatsApp the clinic** | Send `hi` to **+91 72178 11862**. (The sender shows *Secureyourhealthdata* while Meta's display-name review is pending; it's the same number.) |
| **Receptionist / doctor dashboard** | URL + access key are in the Devpost judges-only field (kept out of this public repo). |
| **Waiting-room TV** | Dashboard → **📺 Waiting-room TV** |
| Full script | See [TESTING.md](TESTING.md) |

---

## 2. The problem

Lakhs of small clinics in India run walk-in queues on paper tokens. Waiting rooms are packed by 10 AM because nobody knows
when their turn will come, the receptionist answers *"kitna time lagega?"* calls all day, prescriptions get lost, and
patients on 6-session laser packages quietly drop off after session 2 or 3 because nobody reminds them.
Every one of those patients already uses WhatsApp. Nobody wants another app.

---

## 3. Features

### For patients, on WhatsApp (zero install)

| # | Feature | What happens |
|---|---|---|
| 1 | **Tap-first UX** | A 10-row WhatsApp list **Menu** and context-aware reply buttons (🔄 Refresh wait · 📍 Share location · ⏰ Running late · ✅ I've arrived · ❌ Cancel). Typing works in English, Hindi, Hinglish and Devanagari. |
| 2 | **Two parallel queues** | OPD (doctor, ~6 min) and Laser/procedure (~20 min), tokens like **O7** / **L2**, separate live ETAs. |
| 3 | **📍 Traffic-aware "leave now"** | Patient shares location once → **Amazon Location Service** live-traffic drive time, re-checked on every queue move → *"🚶 Time to leave"* + map pin + one-tap directions. They wait at home, not in the clinic. |
| 4 | **Running late / I've arrived** | Late moves the token back 2 places instead of losing it; arrival checks them in. |
| 5 | **"It's your turn"** | Sent the moment the receptionist calls them. |
| 6 | **📝 Pre-visit intake** | 4 quick questions (problem, since when, tried, allergies) → intake card for the doctor. |
| 7 | **🚨 Red-flag triage** (EN + HI) | Emergencies (chest pain, *"saans nahi aa rahi"*, severe swelling) → instant *call 112*, even before consent. Urgent symptoms jump the queue and are flagged red. Runs *before* any AI model. |
| 8 | **👨‍👩‍👧 Family booking** | *"token for my mother"* → separate token from the same phone. |
| 9 | **🕔 Laser time slots + UPI deposit** | Pick a fixed slot from a WhatsApp list; optional UPI deposit link to cut no-shows. |
| 10 | **📷 Photos for the doctor** | Stored privately in S3, attached to the token, acknowledged with a 👍 reaction. |
| 11 | **🎙️ Voice-note booking** | Send a voice note in Hindi or English → **Amazon Transcribe** (automatic language ID) → *"🎙️ I heard: …"* → the agent acts. Recording deleted after transcription. |
| 12 | **📦 Treatment packages** | *"Laser hair removal ▓▓░░░░ 2/6, next due 12 Nov"* + one-tap booking of the next session. |
| 13 | **🗂️ My visits & prescriptions** | Past visits, missed visits, package progress, next follow-up, last prescription (+ PDF). |
| 14 | **🧾 Prescription PDF** | Printable Rx generated in Lambda, pushed with `PostWhatsAppMessageMedia`, delivered as a WhatsApp document. |
| 15 | **🗺️ Clinic address** | Location pin + CTA button with Google Maps directions. |
| 16 | **🔁 Follow-up recall** | Reminder on the due date with *"Book today"*. |
| 17 | **😊 / 😐 / 😞 feedback** | Happy → Google review link; unhappy → private to the manager, not a public 1-star. |
| 18 | **🔒 Privacy (DPDP Act 2023)** | Consent before anything is stored · *stop reminders* · *delete my data* (full erasure). |
| 19 | **📧 Email** | Confirmations, visit summaries and Rx via Amazon SES, with a "Check my queue on WhatsApp" button. |

### For the doctor & receptionist (dashboard on a Lambda Function URL)

| # | Feature | What happens |
|---|---|---|
| 20 | **One-button front desk** | Per queue: ✅ Call next · ⏭️ No-show · Finish · + Walk-in (patients without WhatsApp). |
| 21 | **⏳ Doctor running late** | One tap (15/30/45 min) broadcasts the delay on WhatsApp and shifts every ETA. |
| 22 | **🧠 Self-learning ETAs** | Each queue learns its real pace from today's called→done times. |
| 23 | **Signals at a glance** | arrived · nudged · late · "14 min away · AWS Location" · family · package · slot · deposit · 🚩 urgent · intake card · Rx sent. |
| 24 | **👤 Patient profile** | Each row: *"🔁 Visit #5 · last seen 15 Oct · Last Rx: Adapalene…"* + ⚠️ allergy / 📦 package flags. One click: visit timeline, packages, photo timeline, follow-ups, feedback. |
| 25 | **✨ AI pre-read** | 20-second summary for the doctor before the patient walks in (facts only, no diagnosis). |
| 26 | **📷 Before/after compare** | Today's photo next to the previous visit's. |
| 27 | **🎙️ Dictated prescription** | Doctor speaks → **Amazon Transcribe** (en-IN) with browser speech as live preview/fallback → AI formats → WhatsApp text + PDF + email; *"review in 2 weeks"* schedules the follow-up. |
| 28 | **📺 Waiting-room TV** | `/tv`: now serving + next up per queue (first names only). |
| 29 | **Impact tiles** | Waiting-room minutes saved · reception calls avoided · nudges · no-shows · packages · follow-ups · red flags · Rx · slots · feedback. |
| 30 | **📧 Doctor's daily summary** | Amazon SES at 20:30 IST (or on demand): numbers, red flags, packages due. |
| 31 | **Package revenue protection** | Sessions auto-counted, next due date scheduled, daily 10:00 IST reminders. |

---

## 4. How it aligns with the hackathon

| Hackathon theme | ClinicFlow |
|---|---|
| **AWS CDS as the backbone** | **AWS End User Messaging Social (WhatsApp)** is the entire patient interface: 3 APIs (`SendWhatsAppMessage`, `GetWhatsAppMessageMedia`, `PostWhatsAppMessageMedia`) and 10 message types (text, reply buttons, lists, location request, location pin, CTA URL, reaction, document, template, read receipts). **Amazon SES** carries confirmations, summaries, prescriptions and the doctor's report, with an email → WhatsApp hand-off. |
| **Agentic AI** | An agent with **13 tools** that change real clinic state (book, cancel, move, check in, intake, slots, packages, history), not a FAQ bot. |
| **Amazon Bedrock AgentCore** | The agent is deployed on **AgentCore Runtime**: one isolated session per patient via `InvokeAgentRuntime`. |
| **Production-minded** | Model fail-over chain, triage before AI, consent and erasure, least-privilege IAM, budgets, logs of which engine answered. |
| **Real customer** | Designed with a real dermatology clinic in Meerut; customer engagement registered in AWS Partner Central (ACE). |

---

## 5. Architecture

```
 Patient (WhatsApp: text · buttons · list · location · photo · voice note)
      │
      ▼
 AWS End User Messaging Social ──► Amazon SNS ──► Lambda: clinicflow-whatsapp-handler
      ▲                                              │ 1. read receipt + reaction
      │ SendWhatsAppMessage                          │ 2. red-flag triage (before any AI)
      │ PostWhatsAppMessageMedia (Rx PDF)            │ 3. consent gate (DPDP)
      │                                              │ 4. media → S3 (GetWhatsAppMessageMedia)
      │                                              │    voice → Amazon Transcribe (hi-IN / en-IN)
      │                                              │    location → Amazon Location Service
      │                                              │ 5. InvokeAgentRuntime (session per patient)
      │                                              ▼
      │                      Amazon Bedrock AgentCore Runtime: clinicflow_agent
      │                      ├─ model chain: Amazon Bedrock (Nova) → Azure OpenAI → deterministic router
      │                      └─ 13 tools → DynamoDB · SES · Amazon Location
      │
      ├── Lambda: clinicflow-dashboard (Function URL) ◄── receptionist / doctor
      │      call next · no-show · delay broadcast · dictated Rx (Transcribe + AI + PDF) ·
      │      patient history + AI pre-read · TV screen · impact metrics
      │
      └── Lambda: clinicflow-reminders ◄── Amazon EventBridge (10:00 IST)
             package + follow-up reminders (template / buttons + SES)   · doctor summary 20:30 IST
```

Diagram: `docs/architecture.png` (source `docs/architecture.mmd`).

### Tech stack

| Service / tech | How ClinicFlow uses it |
|---|---|
| **AWS End User Messaging Social (WhatsApp)** | All patient messaging; inbound via SNS event destination; outbound with `SendWhatsAppMessage`; media in with `GetWhatsAppMessageMedia`, out with `PostWhatsAppMessageMedia` |
| **Amazon SES (v2)** | Confirmations, visit summaries, prescriptions, reminders, doctor's daily HTML summary |
| **Amazon SNS** | Event destination for inbound WhatsApp events → gateway Lambda |
| **Amazon Bedrock AgentCore Runtime** | Hosts the agent (Python 3.12 direct code deploy, `/ping` + `/invocations`) |
| **Amazon Bedrock (Converse API + tool use)** | Primary model (`us.amazon.nova-pro-v1:0`, configurable) |
| Azure OpenAI gpt-6-astra (Responses API) | Secondary model today (key in AWS Secrets Manager) |
| **Amazon Transcribe** | Patient voice notes (auto language ID hi-IN/en-IN) and doctor dictation (en-IN) |
| **Amazon Location Service (Routes)** | `CalculateRoutes` with `DepartNow` live traffic for leave-now timing |
| **Amazon DynamoDB** | Single-table data model (below) |
| **Amazon S3** | Private photos, Rx PDFs, temporary audio (deleted after transcription) |
| **AWS Lambda + Function URL** | WhatsApp gateway, dashboard + API + TV, reminders |
| **Amazon EventBridge** | Daily reminders (10:00 IST) and doctor summary (20:30 IST) |
| **AWS Secrets Manager** | Azure OpenAI key |
| **AWS IAM · Amazon CloudWatch · AWS Budgets** | Least privilege, logs (engine per turn), cost alerts |
| Python 3.12 · boto3 · vanilla HTML/JS | Code; no front-end framework, dependency-free PDF generator |

---

## 6. The AI: Amazon Bedrock first, with a fail-safe chain

```
respond(phone, text)
  1. Amazon Bedrock  ── Converse API + toolConfig (13 tools), model MODEL_ID (default us.amazon.nova-pro-v1:0)
                        optional BEDROCK_ROLE_ARN → STS AssumeRole to call Bedrock in another AWS account
                        boto3 retries=1, read timeout 25 s
                        circuit breaker: Throttling / AccessDenied / ResourceNotFound / Validation /
                        ServiceUnavailable / ModelNotReady → skip Bedrock for 10 min
  2. Azure OpenAI    ── Responses API (/openai/v1/responses) with function tools, previous_response_id +
                        function_call_output loop, reasoning effort "low"
                        key read from AWS Secrets Manager (clinicflow/azure-openai-key), never in code
                        circuit breaker: any error → skip Azure for 2 min
  3. Deterministic router ── regex intents (EN / HI / Hinglish / Devanagari) calling the SAME tools
```

* **Same tools everywhere**, so clinic state is identical whichever engine answers.
* Every AgentCore response returns and logs `engine` (`bedrock:<model>`, `azure-openai:<deployment>` or `fallback-router`).
* **Safety never depends on a model**: red-flag triage, consent and privacy commands run in the gateway *before* the agent.
* The agent's system prompt carries the patient's profile and visit history (visit number, last concern, packages), so it
  greets returning patients and asks about their follow-up.

**Honest status at submission:** the agent runs on AgentCore Runtime and the live model is **Azure OpenAI gpt-6-astra**.
Our new partner account's Bedrock daily token quota is still pending AWS account verification (support case open). The
Bedrock path is first in the chain and takes over automatically when the quota is granted, with no redeploy.
AgentCore Memory failed to provision on the new account, so conversation history uses DynamoDB (`MEMORY_ID` switches it on).

---

## 7. Data model (DynamoDB single table `clinicflow`)

| pk | sk | attributes |
|---|---|---|
| `CLINIC#<id>` | `META` | name, address, hours, lat/lon, queues, services, upi_vpa, google_review_url, doctor_email |
| `CLINIC#<id>#DAY#<date>` | `COUNTER#<OPD\|LASER>` | last_token, now_serving, delay_min |
| `CLINIC#<id>#DAY#<date>` | `STATS` | status_checks, nudges, no_shows, photos, voice_notes, prescriptions, redflags, … |
| `CLINIC#<id>#DAY#<date>` | `TOKEN#<Q>#<nnn>` | label, pos, phone, name, service, status, intake, urgent, family, slot, deposit, photos, rx_text, rx_pdf, timestamps |
| `CLINIC#<id>#DAY#<date>` | `SLOT#<HH:MM>` | laser slot booking |
| `PATIENT#<phone>` | `PROFILE` | name, consent, opt_out, email, travel_min, lat/lon, family, photo_history |
| `PATIENT#<phone>` | `VISIT#<date>#<label>` | permanent visit record: queue, who, service, intake, rx_text, rx_pdf, follow-up, photos, wait/consult min, feedback, status |
| `PATIENT#<phone>` | `PACKAGE#<name>` | total, used, interval_days, next_due |
| `PATIENT#<phone>` | `FOLLOWUP#<date>` | due, reason, status |
| `PATIENT#<phone>` | `CONV` | recent chat turns |

---

## 8. Deploy it yourself (AWS CloudShell, us-east-1)

Prerequisites: a WhatsApp Business Account linked in AWS End User Messaging Social with an SNS event destination,
SES-verified sender, Bedrock model access (optional Azure OpenAI deployment).

```bash
bash deploy/deploy_phase1.sh                              # SNS subscription + WhatsApp Lambda (echo test)
SES_FROM=you@yourdomain.com bash deploy/deploy_phase2.sh  # DynamoDB, S3, IAM, 3 Lambdas, dashboard URL, schedules, seed data
python3 deploy/deploy_agentcore.py                        # AgentCore Runtime (+ Memory), wires the Lambda to it
python3 deploy/deploy_v2.py                               # queues, Amazon Location, metrics, doctor summary schedule
python3 deploy/deploy_v3.py                               # model chain, Secrets Manager, Transcribe, media + privacy IAM, code update
python3 deploy/seed_history.py                            # optional demo history (python3 deploy/seed_history.py --clear removes it)
```

## 9. Project structure

```
src/
  whatsapp_handler.py  SNS → triage → consent → media/voice/location → AgentCore → interactive reply
  agentcore_main.py    AgentCore Runtime HTTP entry point (/ping, /invocations)
  agent.py             model chain (Bedrock → Azure OpenAI → router), 13 tools, intake, history card
  common.py            WhatsApp / SES / DynamoDB / Location / Transcribe / PDF / privacy helpers
  dashboard.py         front desk, patient profile + AI pre-read, dictated Rx, TV, metrics, doctor summary
  reminders.py         package + follow-up reminders (respects opt-out)
  triage.py            red-flag screening (emergency → 112, urgent → priority)
deploy/                CloudShell deploy + seed scripts
docs/                  architecture diagram
```

## 10. Privacy & safety

* Consent before any data is stored; *stop reminders*; *delete my data* erases profile, visits, Rx, photos and chat (DPDP Act 2023 principles)
* Voice notes and dictation audio deleted right after transcription
* Private S3 bucket, time-limited presigned previews, masked phone numbers on the board, first names only on the TV
* The agent never diagnoses or prescribes; emergencies → 112 before any AI runs
* Secrets in AWS Secrets Manager; least-privilege IAM per component; AWS Budgets alerts

## 11. Roadmap

Bedrock (Nova) live once the quota is granted · AgentCore Memory long-term strategies · multi-doctor rooms ·
clinic-branded WhatsApp sender (Meta display-name approval) · AgentCore Gateway (clinic tools over MCP) ·
WhatsApp Flows for intake · payment-gateway webhooks for deposits · RCS/SMS fallback after DLT registration ·
multi-clinic onboarding and AWS Marketplace listing.

## License

MIT, see `LICENSE`.

### Pre-existing work disclosure

All code was written during the hackathon submission period with the help of AI coding assistants. No pre-existing code was used.
