"""ClinicFlow v2 deploy: OPD/LASER queues, interactive WhatsApp UI, Amazon Location, impact metrics,
doctor summary. Updates the AgentCore runtime + 3 Lambdas in place.

Run in CloudShell from the unzipped bundle:  python3 -u deploy/deploy_v2.py
"""
import io
import json
import os
import subprocess
import sys
import time
import zipfile

import boto3

REGION = "us-east-1"
os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
ACCT = boto3.client("sts").get_caller_identity()["Account"]
BUCKET = f"clinicflow-media-{ACCT}"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
iam, s3, lam, ev = boto3.client("iam"), boto3.client("s3"), boto3.client("lambda"), boto3.client("events")
ctl, dp = boto3.client("bedrock-agentcore-control"), boto3.client("bedrock-agentcore")
ddb = boto3.resource("dynamodb").Table("clinicflow")


def step(m):
    print(f"\n== {m}", flush=True)


step("1. Clinic config (queues, location, doctor email)")
ddb.update_item(
    Key={"pk": "CLINIC#glowskin", "sk": "META"},
    UpdateExpression="SET #n=:n, address=:a, hours=:h, lat=:lat, lon=:lon, doctor_email=:de, queues=:q, services=:s",
    ExpressionAttributeNames={"#n": "name"},
    ExpressionAttributeValues={
        ":n": "GlowSkin Clinic", ":a": "Indirapuram, Ghaziabad", ":h": "10:00-14:00 and 17:00-20:00, Mon-Sat",
        ":lat": "28.6460", ":lon": "77.3695", ":de": os.environ.get("DOCTOR_EMAIL", "arpit@dehix.co"),
        ":q": {"OPD": {"label": "Doctor consultation (OPD)", "service_min": 6, "prefix": "O"},
               "LASER": {"label": "Laser / procedure", "service_min": 20, "prefix": "L"}},
        ":s": ["Dermatology consultation", "Acne & pigmentation", "Hair fall", "Laser hair removal (package)",
               "Chemical peel (package)", "PRP for hair (package)"]})
from boto3.dynamodb.conditions import Attr
for p in ddb.scan(FilterExpression=Attr("sk").begins_with("PACKAGE#"))["Items"]:
    ddb.update_item(Key={"pk": p["pk"], "sk": p["sk"]}, UpdateExpression="SET #q = if_not_exists(#q, :l)",
                    ExpressionAttributeNames={"#q": "queue"}, ExpressionAttributeValues={":l": "LASER"})
print("   ok")

step("2. IAM: Amazon Location routes for the WhatsApp Lambda")
iam.put_role_policy(RoleName="clinicflow-lambda-role", PolicyName="clinicflow-location", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["geo-routes:CalculateRoutes"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{BUCKET}/*"}]}))
time.sleep(5)
try:
    r = boto3.client("geo-routes").calculate_routes(Origin=[77.4538, 28.6692], Destination=[77.3695, 28.6460],
                                                     TravelMode="Car", DepartNow=True, LegAdditionalFeatures=["Summary"])
    print("   Amazon Location test (Ghaziabad -> Indirapuram):",
          round(r["Routes"][0].get("Summary", {}).get("Duration", 0) / 60), "min")
except Exception as e:
    print("   !! Amazon Location unavailable, distance estimate will be used:", str(e)[:160])

step("3. Package code + vendored boto3")
build = os.path.expanduser("~/cf_build")
subprocess.run(["rm", "-rf", build], check=True)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "boto3", "-t", build], check=True)
for f in os.listdir(SRC):
    if f.endswith(".py"):
        subprocess.run(["cp", os.path.join(SRC, f), build], check=True)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(build):
        if "__pycache__" in root:
            continue
        for f in files:
            if not f.endswith(".pyc"):
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, build))
code = buf.getvalue()
key = f"agentcore/clinicflow-agent-{int(time.time())}.zip"
s3.put_object(Bucket=BUCKET, Key=key, Body=code)
print(f"   s3://{BUCKET}/{key} ({len(code) // 1024} KB)")

step("4. Update AgentCore Runtime")
rt = next(r for r in ctl.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"] == "clinicflow_agent")
cur = ctl.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
env = cur.get("environmentVariables", {})
ctl.update_agent_runtime(
    agentRuntimeId=rt["agentRuntimeId"], roleArn=cur["roleArn"],
    agentRuntimeArtifact={"codeConfiguration": {"code": {"s3": {"bucket": BUCKET, "prefix": key}},
                                                "runtime": "PYTHON_3_12", "entryPoint": ["agentcore_main.py"]}},
    networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
    environmentVariables=env, description=cur.get("description", "ClinicFlow agent"))
for _ in range(90):
    st = ctl.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
    if st["status"] in ("READY", "UPDATE_FAILED"):
        break
    print("   status:", st["status"], flush=True)
    time.sleep(6)
RUNTIME_ARN = st["agentRuntimeArn"]
print("   runtime:", st["status"], st.get("failureReason", ""))

step("5. Update Lambdas")
for fn in ("clinicflow-whatsapp-handler", "clinicflow-dashboard", "clinicflow-reminders"):
    lam.update_function_code(FunctionName=fn, ZipFile=code)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    print("   updated", fn)

step("6. Daily doctor summary at 20:30 IST (EventBridge -> dashboard Lambda)")
ev.put_rule(Name="clinicflow-daily-summary", ScheduleExpression="cron(0 15 * * ? *)")
try:
    lam.add_permission(FunctionName="clinicflow-dashboard", StatementId="events-summary",
                       Action="lambda:InvokeFunction", Principal="events.amazonaws.com",
                       SourceArn=f"arn:aws:events:{REGION}:{ACCT}:rule/clinicflow-daily-summary")
except lam.exceptions.ResourceConflictException:
    pass
ev.put_targets(Rule="clinicflow-daily-summary", Targets=[{
    "Id": "1", "Arn": f"arn:aws:lambda:{REGION}:{ACCT}:function:clinicflow-dashboard",
    "Input": json.dumps({"summary": True})}])
print("   ok")

step("7. Smoke test via AgentCore")
time.sleep(5)
r = dp.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId="clinicflow-smoke-test-session-v2-0000001",
                            payload=json.dumps({"phone": "910000000001", "text": "laser chahiye"}).encode(),
                            contentType="application/json", accept="application/json")
print("   ", r["response"].read().decode()[:240])
print("\nCLINICFLOW_V2_OK")
