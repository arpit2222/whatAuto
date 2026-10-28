"""Deploy the ClinicFlow agent to Amazon Bedrock AgentCore (Runtime + Memory).

Run in CloudShell from the unzipped bundle folder:
    pip install --user -q -U boto3 && python3 deploy/deploy_agentcore.py
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
sts = boto3.client("sts")
ACCT = sts.get_caller_identity()["Account"]
BUCKET = f"clinicflow-media-{ACCT}"
ROLE = "clinicflow-agentcore-role"
LAMBDA_ROLE = "clinicflow-lambda-role"
RUNTIME_NAME = "clinicflow_agent"
MEMORY_NAME = "clinicflow_patient_memory"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")

iam = boto3.client("iam")
s3 = boto3.client("s3")
ctl = boto3.client("bedrock-agentcore-control")
dp = boto3.client("bedrock-agentcore")
lam = boto3.client("lambda")


def step(msg):
    print(f"\n== {msg}", flush=True)


# ------------------------------------------------------------------ memory
step("1. AgentCore Memory (short-term conversation events, 30-day expiry)")
MEMORY_ID = ""
if os.environ.get("SKIP_MEMORY"):
    print("   skipped (SKIP_MEMORY set) – conversation history stays in DynamoDB")
else:
    mem = next((m for m in ctl.list_memories().get("memories", [])
                if m["id"].startswith(MEMORY_NAME) and m["status"] != "FAILED"), None)
    if not mem:
        mem = ctl.create_memory(name=MEMORY_NAME, eventExpiryDuration=30,
                                description="ClinicFlow per-patient WhatsApp conversation memory")["memory"]
    st = "CREATING"
    for _ in range(40):
        m = ctl.get_memory(memoryId=mem["id"])["memory"]
        st = m["status"]
        if st in ("ACTIVE", "FAILED"):
            break
        print("   memory status:", st, flush=True)
        time.sleep(5)
    if st == "ACTIVE":
        MEMORY_ID = mem["id"]
        print("   memory:", MEMORY_ID)
    else:
        print("   !! memory", st, m.get("failureReason"), "– continuing with DynamoDB history")

# ------------------------------------------------------------------ roles
step("2. IAM role for AgentCore Runtime")
trust = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": ACCT}}}]}
try:
    iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))
    print("   created role; waiting for IAM"); time.sleep(12)
except iam.exceptions.EntityAlreadyExistsException:
    pass
common_stmts = [
    {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                   "dynamodb:Query", "dynamodb:Scan"],
     "Resource": f"arn:aws:dynamodb:{REGION}:{ACCT}:table/clinicflow"},
    {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Converse",
                                   "bedrock:InvokeModelWithResponseStream"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["ses:SendEmail"], "Resource": "*"},
] + ([{"Effect": "Allow", "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents",
                                     "bedrock-agentcore:GetEvent"],
        "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:memory/{MEMORY_ID}"}] if MEMORY_ID else [])
iam.put_role_policy(RoleName=ROLE, PolicyName="clinicflow-agent", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": common_stmts + [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                                       "logs:DescribeLogStreams", "logs:DescribeLogGroups"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                                       "cloudwatch:PutMetricData"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{BUCKET}/agentcore/*"},
    ]}))
ROLE_ARN = f"arn:aws:iam::{ACCT}:role/{ROLE}"

# ------------------------------------------------------------------ package
step("3. Package code + vendored boto3 (pure Python, works on ARM64)")
build = os.path.expanduser("~/cf_build")
subprocess.run(["rm", "-rf", build], check=True)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "boto3", "-t", build], check=True)
for f in os.listdir(SRC):
    if f.endswith(".py"):
        subprocess.run(["cp", os.path.join(SRC, f), build], check=True)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(build):
        for f in files:
            if "__pycache__" in root or f.endswith(".pyc"):
                continue
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, build))
code = buf.getvalue()
key = f"agentcore/clinicflow-agent-{int(time.time())}.zip"
s3.put_object(Bucket=BUCKET, Key=key, Body=code)
print(f"   uploaded s3://{BUCKET}/{key} ({len(code)//1024} KB)")

# ------------------------------------------------------------------ runtime
step("4. AgentCore Runtime (direct code deploy, Python 3.12)")
env = {"TABLE_NAME": "clinicflow", "CLINIC_ID": "glowskin", "MEMORY_ID": MEMORY_ID,
       "MODEL_ID": os.environ.get("MODEL_ID", "us.amazon.nova-pro-v1:0"),
       "SES_FROM": "info@dehix.co", "AWS_REGION_NAME": REGION}
artifact = {"codeConfiguration": {"code": {"s3": {"bucket": BUCKET, "prefix": key}},
                                  "runtime": "PYTHON_3_12", "entryPoint": ["agentcore_main.py"]}}
existing = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                 if r["agentRuntimeName"] == RUNTIME_NAME), None)
kw = dict(agentRuntimeArtifact=artifact, roleArn=ROLE_ARN, networkConfiguration={"networkMode": "PUBLIC"},
          protocolConfiguration={"serverProtocol": "HTTP"}, environmentVariables=env,
          description="ClinicFlow WhatsApp front-desk agent (tools: tokens, queue, packages)")
if existing:
    rid = existing["agentRuntimeId"]
    ctl.update_agent_runtime(agentRuntimeId=rid, **kw)
else:
    rid = ctl.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, **kw)["agentRuntimeId"]
for _ in range(90):
    rt = ctl.get_agent_runtime(agentRuntimeId=rid)
    if rt["status"] in ("READY", "CREATE_FAILED", "UPDATE_FAILED"):
        break
    print("   runtime status:", rt["status"], flush=True)
    time.sleep(6)
RUNTIME_ARN = rt["agentRuntimeArn"]
print("   runtime:", RUNTIME_ARN, rt["status"], rt.get("failureReason", ""))
if rt["status"] != "READY":
    sys.exit("!! runtime not ready")

# ------------------------------------------------------------------ smoke test
step("5. Invoke the agent")
time.sleep(5)
r = dp.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN,
                            runtimeSessionId="clinicflow-smoke-test-session-000000001",
                            payload=json.dumps({"phone": "910000000000", "text": "hi"}).encode(),
                            contentType="application/json", accept="application/json")
print("   ", r["response"].read().decode()[:300])

# ------------------------------------------------------------------ lambda wiring
step("6. Point the WhatsApp Lambda at AgentCore")
iam.put_role_policy(RoleName=LAMBDA_ROLE, PolicyName="clinicflow-agentcore", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
         "Resource": [RUNTIME_ARN, RUNTIME_ARN + "/*"]},
    ] + ([{"Effect": "Allow", "Action": ["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents"],
           "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:memory/{MEMORY_ID}"}] if MEMORY_ID else [])}))
for fn in ("clinicflow-whatsapp-handler", "clinicflow-dashboard", "clinicflow-reminders"):
    lam.update_function_code(FunctionName=fn, ZipFile=code)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    cur = lam.get_function_configuration(FunctionName=fn).get("Environment", {}).get("Variables", {})
    cur.update({"MEMORY_ID": MEMORY_ID})
    if fn == "clinicflow-whatsapp-handler":
        cur["AGENT_RUNTIME_ARN"] = RUNTIME_ARN
    lam.update_function_configuration(FunctionName=fn, Environment={"Variables": cur})
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    print("   updated", fn)

print("\nCLINICFLOW_AGENTCORE_OK")
print("RUNTIME_ARN =", RUNTIME_ARN)
print("MEMORY_ID   =", MEMORY_ID)
