# ClinicFlow – WhatsApp front desk for Indian clinics

**An agentic WhatsApp assistant for small skin & laser clinics.** Patients take a token, see their live
queue position, get a "leave now" nudge timed to their travel time, share photos for the doctor and never
miss a package session. The receptionist runs the queue from one button. All of it runs on AWS
Communication Developer Services, with the agent hosted on **Amazon Bedrock AgentCore**.

> Built for the **AWS CDS Agentic AI Partner Hackathon** (Devpost) by Dehix Technologies Pvt. Ltd.

---

## Try it (judges)

| What | How |
|---|---|
| **WhatsApp the clinic bot** | Send `hi` to **+91 72178 11862** (display name currently shows *Secureyourhealthdata*: Meta display-name review is pending) |
| Suggested script | `hi` → tap **Menu → Doctor consultation** → your name → tap **📍 Send location** → **🔄 Refresh wait** → send a photo → **⏰ Running late** → **📍 I'm here** → `laser` / `package` |
| **Receptionist dashboard** | Link + key are in the Devpost submission (judges-only field), kept out of the public repo |
| See the queue move | Press **✅ Call next** on the OPD or Laser queue: you get *"It's your turn"* on WhatsApp, and patients whose wait ≤ travel time + 5 min get *"Time to leave"* with buttons. Try **No-show**, **+ Walk-in**, **📧 Send doctor summary**. |

Hindi / Hinglish works too (*"token chahiye"*, *"kitna time lagega"*).

---

## The problem

Lakhs of small dermatology, dental and physio clinics in India run walk-in queues on paper tokens and phone
calls. Patients wait 1–2 hours in crowded waiting rooms, receptionists spend most of their day answering
*"how long?"* calls, and multi-session packages (laser, peels, PRP) leak revenue when patients forget to come
back. Everyone already uses WhatsApp; nobody wants another app.

## What ClinicFlow does

| For patients (WhatsApp) | For the clinic |
|---|---|
| Interactive WhatsApp: list **Menu**, reply buttons (🔄 Refresh wait · ⏰ Running late · 📍 I'm here · ❌ Cancel) | **Two parallel queues**: OPD (doctor, ~6 min) and Laser/procedure (~20 min), tokens `O7` / `L2` |
| Take a token in one message, in English, Hindi or Hinglish | Per-queue **✅ Call next**, **⏭️ No-show**, **Finish**, **+ Walk-in** (patients without WhatsApp) |
| **📍 Share location → Amazon Location Service** computes real drive time → **"Leave now"** nudge at the right moment | Live board: ETA, ✓ arrived, 🚶 nudged, ⏰ late, "14 min away · AWS Location", 📦 package, 📷 photo previews |
| **Running late** moves the token back 2 places instead of losing it; **I'm here** checks in | **Impact tiles**: waiting-room minutes saved, reception calls avoided, nudges, no-shows, package sessions booked |
| Send photos: stored privately in S3, previewed by the doctor on the board | **📧 Daily doctor summary** via Amazon SES (20:30 IST, or on demand) incl. packages due |
| Package status & one-tap booking of the next session (laser queue) | Package sessions auto-counted + next due date scheduled; daily reminders (WhatsApp template + email) |
| Emails with a **"Check my queue on WhatsApp"** button (SES → WhatsApp handoff) | **↺ Reset today (demo)** for clean demos |

---

## Architecture

```
 Patient (WhatsApp)
      │  inbound message / photo
      ▼
 AWS End User Messaging Social ──► Amazon SNS ──► Lambda: clinicflow-whatsapp-handler
      ▲                                              │   (gateway: parse, media, route)
      │ SendWhatsAppMessage                          │ InvokeAgentRuntime (session per patient)
      │                                              ▼
      │                               Amazon Bedrock AgentCore Runtime: clinicflow_agent
      │                               ├─ Bedrock Converse + tool use (Amazon Nova)
      │                               ├─ tools: book_token(OPD|LASER) · queue_status · running_late ·
      │                               │         im_here · cancel_token · my_packages ·
      │                               │         book_package_session · set_travel_time · save_email
      │                               └─ AgentCore Memory (code-ready; history in DynamoDB today)
      │                                              │
      │                       ┌──────────────────────┼─────────────────────┐
      │                       ▼                      ▼                     ▼
      │                 Amazon DynamoDB         Amazon SES            Amazon S3
      │                 (clinic, tokens,        (confirmations,       (patient photos,
      │                  patients, packages)     visit summaries)      private)
      │
      ├──── Lambda: clinicflow-dashboard (Function URL) ◄── Receptionist "Call next"
      │        → "your turn" + "leave now" nudges, package session counting
      │
      └──── Lambda: clinicflow-reminders ◄── Amazon EventBridge (daily 10:00 IST)
               → session_reminder template (WhatsApp) + SES email
```

See `docs/architecture.png` for the diagram.

### AWS services used

| Service | Role |
|---|---|
| **AWS End User Messaging Social (WhatsApp)** | Inbound + outbound WhatsApp: text, interactive buttons, templates, media download (`SendWhatsAppMessage`, `GetWhatsAppMessageMedia`) |
| **Amazon SES** (sesv2) | Booking confirmations, visit summaries, package reminders |
| **Amazon Bedrock AgentCore Runtime** | Hosts the ClinicFlow agent; one isolated session per patient |
| Amazon Bedrock AgentCore Memory | Implemented (`common.load_conv/save_turn`, enabled by `MEMORY_ID`); provisioning failed on our new account ("Failed to provision Vector Store"), so conversation history currently uses DynamoDB |
| **Amazon Bedrock** (Converse API, tool use, Amazon Nova) | Reasoning + tool calling |
| AWS Lambda | WhatsApp gateway, receptionist dashboard, reminders |
| Amazon SNS | Event destination for inbound WhatsApp events |
| Amazon DynamoDB | Single-table data model (clinic, daily queue, patients, packages) |
| Amazon S3 | Private storage for patient photos |
| Amazon EventBridge | Daily package reminders (10:00 IST) + doctor summary (20:30 IST) |
| **Amazon Location Service** (Routes) | Drive time from the patient's shared WhatsApp location to the clinic for leave-now timing |
| AWS IAM, Amazon CloudWatch, AWS Budgets | Least-privilege roles, logs, cost alerts |

### Where AWS CDS is called in code

* `src/common.py`: `boto3.client("socialmessaging")` → `send_whatsapp_message`, `get_whatsapp_message_media`
* `src/common.py`: `boto3.client("sesv2")` → `send_email`
* `src/whatsapp_handler.py`: SNS → WhatsApp event parsing, routing to AgentCore
* `src/agentcore_main.py`: AgentCore Runtime entry point (`/ping`, `/invocations`)
* `src/agent.py`: Bedrock Converse tool-use loop + tool implementations

### Resilience: deterministic fallback

If Bedrock model invocation is unavailable (throttling, quota, account limits), the agent automatically
switches for 10 minutes to a **deterministic intent router** that calls the *same tools*. Clinic state stays
consistent and patients always get an answer. Each AgentCore response reports which engine answered
(`bedrock:<model>` or `fallback-router`) in CloudWatch logs.

> **Honest status at submission time:** the agent is deployed and serving live traffic on **AgentCore
> Runtime**. Our new AWS account's Bedrock model quota and AgentCore Memory are still pending account
> verification with AWS Support, so the live bot currently answers through the fallback router (same tools,
> same data). The Bedrock tool-use path and Memory integration are implemented and switch on automatically
> once the account is verified. No redeploy needed.

---

## Data model (DynamoDB, single table `clinicflow`)

| pk | sk | attributes |
|---|---|---|
| `CLINIC#<id>` | `META` | name, address, hours, avg_consult_min, services |
| `CLINIC#<id>#DAY#<date>` | `COUNTER#<OPD\|LASER>` | last_token, now_serving, now_serving_label |
| `CLINIC#<id>#DAY#<date>` | `STATS` | status_checks, nudges, no_shows, late, photos, package_bookings, locations |
| `CLINIC#<id>#DAY#<date>` | `TOKEN#<Q>#<nnn>` | queue, label (O7/L2), pos, phone, name, service, status, nudged, arrived, late, photos, package, created/nudged_at/called_at/arrived_at/done_at |
| `PATIENT#<phone>` | `PROFILE` | name, email, travel_min, travel_source (amazon-location / patient), lat, lon |
| `PATIENT#<phone>` | `PACKAGE#<name>` | total, used, interval_days, next_due |

---

## Deploy it yourself

Prerequisites: an AWS account with a WhatsApp Business Account linked in AWS End User Messaging Social,
an SNS topic set as its event destination, and Bedrock model access.

```bash
# in AWS CloudShell (us-east-1), from the repo root
bash deploy/deploy_phase1.sh           # SNS subscription + WhatsApp Lambda (echo test)
SES_FROM=you@yourdomain.com bash deploy/deploy_phase2.sh   # DynamoDB, S3, IAM, 3 Lambdas, dashboard URL, schedule, seed data
python3 deploy/deploy_agentcore.py     # AgentCore Memory + Runtime, wires the Lambda to it
python3 deploy/deploy_v2.py            # OPD/Laser queues, Amazon Location, impact metrics, doctor summary schedule
```

Edit `PHONE_ID` in `deploy/deploy_phase2.sh` to your `phone-number-id-…`. The deploy prints the dashboard URL.

## Project structure

```
src/
  whatsapp_handler.py   SNS → parse → media → AgentCore → reply
  agentcore_main.py     AgentCore Runtime HTTP entry point
  agent.py              Bedrock tool-use agent + fallback router + tools
  common.py             WhatsApp/SES/DynamoDB/AgentCore Memory helpers
  dashboard.py          Receptionist board + "call next" + leave-now nudges
  reminders.py          Daily package reminders (template + email)
deploy/                 CloudShell deploy scripts
```

## Privacy & safety

* Photos stored in a private S3 bucket (public access blocked); dashboard shows masked phone numbers
* The agent never diagnoses or prescribes; emergencies are directed to 112 / nearest hospital
* Least-privilege IAM roles per component

## Roadmap

AgentCore Memory long-term strategies (patient preferences across visits) · AgentCore Gateway to expose
clinic tools via MCP · multi-clinic onboarding & AWS Marketplace listing · RCS / SMS fallback after DLT
registration · doctor-side visit notes → SES summary.

## License

MIT, see `LICENSE`.

### Pre-existing work disclosure

All code was written during the hackathon submission period with the help of AI coding assistants.
No pre-existing code was used.
