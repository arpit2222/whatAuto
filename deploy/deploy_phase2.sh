#!/bin/bash
# ClinicFlow Phase 2 deploy – run in AWS CloudShell from the folder holding clinicflow.zip
# Usage: DEMO_PHONE=91XXXXXXXXXX SES_FROM=you@example.com bash deploy_phase2.sh
set -e
REGION=us-east-1
ACCT=$(aws sts get-caller-identity --query Account --output text)
TABLE=clinicflow
BUCKET=clinicflow-media-$ACCT
ROLE=clinicflow-lambda-role
ROLE_ARN=arn:aws:iam::$ACCT:role/$ROLE
PHONE_ID=phone-number-id-a41bc917e9cb49bf8b6aed106afd9386
MODEL_ID=${MODEL_ID:-us.amazon.nova-pro-v1:0}
DASH_KEY=${DASH_KEY:-$(head -c 9 /dev/urandom | base64 | tr -dc a-zA-Z0-9)}
export AWS_DEFAULT_REGION=$REGION

echo "== 0. Bedrock model check ($MODEL_ID)"
[ -n "${SKIP_MODEL_CHECK:-}" ] || aws bedrock-runtime converse --model-id "$MODEL_ID" \
  --messages '[{"role":"user","content":[{"text":"Reply with OK"}]}]' \
  --query 'output.message.content[0].text' --output text || echo "!! Bedrock unavailable – fallback router will be used"

echo "== 0b. SES identities (verification emails go to these inboxes)"
for e in ${SES_FROM:-} ${SES_TEST_TO:-}; do
  aws sesv2 create-email-identity --email-identity $e >/dev/null 2>&1 && echo "verification sent to $e" || echo "$e already exists"
done

if [ -z "${DEMO_PHONE:-}" ]; then  # pick up the number that messaged the bot
  DEMO_PHONE=$(aws logs filter-log-events --log-group-name /aws/lambda/clinicflow-whatsapp-handler \
    --filter-pattern '"from"' --query 'events[-1].message' --output text 2>/dev/null \
    | grep -oE 'from[^0-9]{1,12}[0-9]{10,15}' | grep -oE '[0-9]{10,15}' | tail -1)
  echo "demo phone from logs: ...${DEMO_PHONE: -4}"
fi

echo "== 1. Package code"
rm -rf app && mkdir app && unzip -q -o clinicflow.zip -d app
(cd app/src && rm -f ../../fn.zip && zip -q -r ../../fn.zip .)

echo "== 2. DynamoDB table"
if ! aws dynamodb describe-table --table-name $TABLE >/dev/null 2>&1; then
  aws dynamodb create-table --table-name $TABLE --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE >/dev/null
  aws dynamodb wait table-exists --table-name $TABLE
fi

echo "== 3. S3 media bucket"
aws s3api head-bucket --bucket $BUCKET 2>/dev/null || aws s3api create-bucket --bucket $BUCKET >/dev/null
aws s3api put-public-access-block --bucket $BUCKET --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "== 4. IAM permissions"
cat > policy.json <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["social-messaging:SendWhatsAppMessage","social-messaging:GetWhatsAppMessageMedia"],"Resource":"*"},
 {"Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan"],"Resource":"arn:aws:dynamodb:$REGION:$ACCT:table/$TABLE"},
 {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":"arn:aws:s3:::$BUCKET/*"},
 {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:Converse"],"Resource":"*"},
 {"Effect":"Allow","Action":["ses:SendEmail"],"Resource":"*"}]}
EOF
aws iam put-role-policy --role-name $ROLE --policy-name clinicflow-messaging --policy-document file://policy.json

ENV="Variables={TABLE_NAME=$TABLE,MEDIA_BUCKET=$BUCKET,ORIGINATION_PHONE_NUMBER_ID=$PHONE_ID,META_API_VERSION=v20.0,MODEL_ID=$MODEL_ID,SES_FROM=${SES_FROM:-},CLINIC_ID=glowskin,DASHBOARD_KEY=$DASH_KEY}"

deploy_fn () {  # name handler timeout
  if aws lambda get-function --function-name $1 >/dev/null 2>&1; then
    aws lambda update-function-code --function-name $1 --zip-file fileb://fn.zip >/dev/null
    aws lambda wait function-updated-v2 --function-name $1
    aws lambda update-function-configuration --function-name $1 --handler $2 --timeout $3 \
      --memory-size 512 --environment "$ENV" >/dev/null
  else
    aws lambda create-function --function-name $1 --runtime python3.12 --handler $2 --role $ROLE_ARN \
      --zip-file fileb://fn.zip --timeout $3 --memory-size 512 --environment "$ENV" >/dev/null
  fi
  aws lambda wait function-updated-v2 --function-name $1 2>/dev/null || aws lambda wait function-active-v2 --function-name $1
}

echo "== 5. Lambdas"
deploy_fn clinicflow-whatsapp-handler whatsapp_handler.lambda_handler 60
deploy_fn clinicflow-dashboard dashboard.lambda_handler 30
deploy_fn clinicflow-reminders reminders.lambda_handler 60

echo "== 6. Dashboard URL"
aws lambda create-function-url-config --function-name clinicflow-dashboard --auth-type NONE >/dev/null 2>&1 || true
aws lambda add-permission --function-name clinicflow-dashboard --statement-id url-public \
  --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE >/dev/null 2>&1 || true
aws lambda add-permission --function-name clinicflow-dashboard --statement-id url-invoke \
  --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>&1 || true
URL=$(aws lambda get-function-url-config --function-name clinicflow-dashboard --query FunctionUrl --output text)

echo "== 7. Daily reminder schedule (10:00 IST)"
aws events put-rule --name clinicflow-daily-reminders --schedule-expression "cron(30 4 * * ? *)" >/dev/null
aws lambda add-permission --function-name clinicflow-reminders --statement-id events-invoke \
  --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn arn:aws:events:$REGION:$ACCT:rule/clinicflow-daily-reminders >/dev/null 2>&1 || true
aws events put-targets --rule clinicflow-daily-reminders \
  --targets "Id=1,Arn=arn:aws:lambda:$REGION:$ACCT:function:clinicflow-reminders" >/dev/null

echo "== 8. Seed demo clinic"
python3 - <<PY
import boto3, datetime as dt
t = boto3.resource("dynamodb").Table("$TABLE")
t.put_item(Item={"pk": "CLINIC#glowskin", "sk": "META", "name": "GlowSkin Clinic",
  "address": "Indirapuram, Ghaziabad", "hours": "10:00-14:00 and 17:00-20:00, Mon-Sat",
  "avg_consult_min": 8, "services": ["Dermatology consultation", "Acne treatment",
  "Laser hair removal (package)", "Chemical peel (package)", "PRP for hair (package)"]})
phone = "${DEMO_PHONE:-}"
if phone:
    today = (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    t.put_item(Item={"pk": f"PATIENT#{phone}", "sk": "PACKAGE#Laser hair removal", "total": 6, "used": 3,
                     "interval_days": 28, "next_due": today})
    print("seeded package for", phone[-4:])
print("seeded clinic")
PY

echo
echo "CLINICFLOW_PHASE2_OK"
echo "Dashboard: ${URL}?key=$DASH_KEY"
