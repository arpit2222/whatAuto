#!/bin/bash
set -e
REGION=us-east-1
ACCT=$(aws sts get-caller-identity --query Account --output text)
TOPIC=arn:aws:sns:$REGION:$ACCT:clinicflow-whatsapp-inbound
FN=clinicflow-whatsapp-handler
ROLE=clinicflow-lambda-role
mkdir -p ~/clinicflow && cd ~/clinicflow
cat > whatsapp_handler.py <<'PYEOF'
"""ClinicFlow – WhatsApp inbound handler (Phase 1: round-trip test).

Flow: patient -> WhatsApp -> AWS End User Messaging Social -> SNS -> this Lambda
      this Lambda -> socialmessaging.SendWhatsAppMessage -> patient

Phase 1 just marks the message read and echoes it back, so we can prove the
pipe works end to end. Phase 2 swaps `reply_for()` for the Bedrock agent.
"""
import json
import logging
import os

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

sm = boto3.client("socialmessaging")
META_API_VERSION = os.environ.get("META_API_VERSION", "v20.0")


def send(phone_number_id: str, payload: dict) -> dict:
    """Send any Meta Cloud API message payload through AWS EUM Social."""
    body = {"messaging_product": "whatsapp", **payload}
    resp = sm.send_whatsapp_message(
        originationPhoneNumberId=phone_number_id,
        message=json.dumps(body).encode("utf-8"),
        metaApiVersion=META_API_VERSION,
    )
    log.info("sent messageId=%s", resp.get("messageId"))
    return resp


def send_text(phone_number_id: str, to: str, text: str) -> dict:
    return send(phone_number_id, {"to": to, "type": "text", "text": {"body": text}})


def mark_read(phone_number_id: str, message_id: str) -> None:
    try:
        send(phone_number_id, {"status": "read", "message_id": message_id})
    except Exception:  # read receipts are best-effort
        log.exception("mark_read failed")


def reply_for(msg: dict) -> str:
    """Phase 1 reply. Replaced by the agent in Phase 2."""
    mtype = msg.get("type")
    if mtype == "text":
        return f"ClinicFlow is live ✅\nYou said: {msg['text']['body']}"
    if mtype == "image":
        return "Got your photo 📷 (media handling arrives in Phase 2)."
    return f"Received a '{mtype}' message."


def iter_inbound(sns_record: dict):
    """Yield (phone_number_id, message) for every inbound user message."""
    envelope = json.loads(sns_record["Sns"]["Message"])
    entry = json.loads(envelope["whatsAppWebhookEntry"])

    # AWS phone number id (e.g. "phone-number-id-abc...") used for sending
    ids = envelope.get("context", {}).get("MetaPhoneNumberIds", [])
    meta_to_aws = {i["metaPhoneNumberId"]: i["arn"].split("/")[-1] for i in ids}

    for change in entry.get("changes", []):
        value = change.get("value", {})
        meta_pid = value.get("metadata", {}).get("phone_number_id")
        aws_pid = meta_to_aws.get(meta_pid) or os.environ.get("ORIGINATION_PHONE_NUMBER_ID")
        for msg in value.get("messages", []):
            yield aws_pid, msg
        for st in value.get("statuses", []):
            log.info("status %s for %s", st.get("status"), st.get("id"))


def lambda_handler(event, context):
    log.info("event: %s", json.dumps(event)[:4000])
    for record in event.get("Records", []):
        for phone_number_id, msg in iter_inbound(record):
            mark_read(phone_number_id, msg["id"])
            send_text(phone_number_id, msg["from"], reply_for(msg))
    return {"ok": True}
PYEOF
rm -f fn.zip && zip -q fn.zip whatsapp_handler.py
if ! aws iam get-role --role-name $ROLE >/dev/null 2>&1; then
  aws iam create-role --role-name $ROLE --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name $ROLE --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "role created, waiting for IAM..."; sleep 12
fi
aws iam put-role-policy --role-name $ROLE --policy-name clinicflow-messaging --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["social-messaging:SendWhatsAppMessage","social-messaging:GetWhatsAppMessageMedia"],"Resource":"*"}]}'
ROLE_ARN=arn:aws:iam::$ACCT:role/$ROLE
if aws lambda get-function --function-name $FN --region $REGION >/dev/null 2>&1; then
  aws lambda update-function-code --function-name $FN --zip-file fileb://fn.zip --region $REGION >/dev/null
else
  aws lambda create-function --function-name $FN --runtime python3.12 --handler whatsapp_handler.lambda_handler \
    --role $ROLE_ARN --zip-file fileb://fn.zip --timeout 30 --memory-size 256 --region $REGION \
    --environment "Variables={ORIGINATION_PHONE_NUMBER_ID=phone-number-id-a41bc917e9cb49bf8b6aed106afd9386,META_API_VERSION=v20.0}" >/dev/null
fi
aws lambda wait function-active-v2 --function-name $FN --region $REGION
aws lambda add-permission --function-name $FN --statement-id sns-invoke --action lambda:InvokeFunction \
  --principal sns.amazonaws.com --source-arn $TOPIC --region $REGION >/dev/null 2>&1 || true
aws sns subscribe --topic-arn $TOPIC --protocol lambda --notification-endpoint arn:aws:lambda:$REGION:$ACCT:function:$FN --region $REGION >/dev/null
echo "=== Topic policy ==="; aws sns get-topic-attributes --topic-arn $TOPIC --query Attributes.Policy --output text --region $REGION
echo "=== Subscriptions ==="; aws sns list-subscriptions-by-topic --topic-arn $TOPIC --query 'Subscriptions[].Endpoint' --output text --region $REGION
echo "CLINICFLOW_DEPLOY_OK"
