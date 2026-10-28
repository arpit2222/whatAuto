# ClinicFlow – testing guide for judges (≈10 minutes)

**WhatsApp number:** +91 72178 11862 (sender name currently shows *Secureyourhealthdata* while Meta's display-name review
is pending; it's the same ClinicFlow number).
**Dashboard:** link + access key are in the Devpost judges-only field. Open it on a laptop next to your phone.

Everything below is live on AWS. The clinic is a demo clinic open 24x7, so you can test at any hour.

## A. Patient on WhatsApp

| Step | Do this | You should see |
|---|---|---|
| 1 | Send `hi` | 🔒 privacy note with **✅ I agree / ❌ No thanks** (nothing is stored before you agree). Tap **I agree**. |
| 2 | Tap **Menu → Doctor consultation**, send your name | Token **O#** with live position + ETA, then intake question 1/4 |
| 3 | Answer the 4 intake questions | "Thanks, the doctor will see this" (the intake card appears on the dashboard) |
| 4 | Tap **📍 Share location** and send your location | Drive time from **Amazon Location Service** with live traffic + when you'll be told to leave |
| 5 | Type `kitna time lagega?` | Hinglish status reply |
| 6 | Send a **voice note**: "mujhe laser ke liye token chahiye" | 🎧 reaction, then *"🎙️ I heard: …"* and a laser token (Amazon Transcribe, Hindi/English auto-detect) |
| 7 | Send a **photo** | 👍 reaction, "attached to token …" (preview appears on the dashboard) |
| 8 | Tap **⏰ Running late**, then **✅ I've arrived** | Token moved back 2 places; checked in |
| 9 | **Menu → 🕔 Book laser slot**, pick a time | Fixed slot booked (UPI deposit link if the clinic has UPI configured) |
| 10 | Type `token for my mother`, give a name | Separate family token from the same phone |
| 11 | **Menu → 🗂️ My visits & Rx** | Visit history, package progress bar, next follow-up; **💊 Last prescription** resends the Rx + PDF |
| 12 | **Menu → 🗺️ Clinic address** | Map pin + **🗺️ Directions** button |
| 13 | Type `I have chest pain and can't breathe` | Instant 112 emergency message (triage runs before any AI) |
| 14 | **Menu → 🔒 Privacy & my data** | Stop reminders / Delete my data (tap *Keep my data* if you want to keep testing) |

## B. Doctor / receptionist on the dashboard

| Step | Do this | You should see |
|---|---|---|
| 1 | Find your token row | Intake card, ✓ arrived, "N min away · AWS Location", photo, and *"🆕 First visit"* or *"🔁 Visit #N"* |
| 2 | Click **👤 Patient history** | Profile: past visits timeline, packages, photo timeline, follow-ups, **✨ AI pre-read** |
| 3 | Press **⏳ Doctor delayed** (15 min) | Every waiting patient gets the delay on WhatsApp, ETAs shift |
| 4 | Press **✅ Call next** | You get *"It's your turn"*; patients further back get *"🚶 Time to leave"* + directions when wait ≤ drive time + 5 min |
| 5 | On the in-consult row click **🎙️ Rx**, **Dictate** (Chrome, allow mic), say e.g. "Adapalene gel at night for 8 weeks, sunscreen every morning, review in 2 weeks", **Stop** | Live text, then *"✅ Transcribed by Amazon Transcribe"* (~15 s). **Send** → formatted Rx on WhatsApp + 🧾 **PDF document** + follow-up scheduled |
| 6 | Press **✅ Call next** again | Visit saved to history; you get 😊/😐/😞 feedback buttons (😊 → Google review link) |
| 7 | **📺 Waiting-room TV** | Now serving / next up per queue |
| 8 | **📧 Send doctor summary** | Daily summary email via Amazon SES |
| 9 | Impact tiles | Waiting-room minutes saved, calls avoided, nudges, red flags, Rx sent, … |

**↺ Reset today (demo)** clears today's queue (patients, history and packages are kept).

## C. What to look for in logs (optional)

CloudWatch log group `/aws/lambda/clinicflow-whatsapp-handler` shows `agentcore reply via azure-openai:gpt-6-astra`
(or `bedrock:…` / `fallback-router`) for every turn, plus Transcribe language/latency for voice notes.

## Known limitations (honest)

* The live model is Azure OpenAI; Amazon Bedrock is first in the chain and activates automatically once our new account's
  daily token quota is approved.
* WhatsApp templates for reminders outside the 24-hour window are pending Meta business verification; inside the window
  reminders use interactive buttons.
* The PDF prescription contains the English text; the WhatsApp message also carries the Hindi tips.
