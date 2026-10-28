"""ClinicFlow v3 deploy: Azure OpenAI secondary model.
(based on) ClinicFlow v2 deploy: OPD/LASER queues, interactive WhatsApp UI, Amazon Location, impact metrics,
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


AZ = {"AZURE_OPENAI_ENDPOINT": "https://dehixai.openai.azure.com/openai/v1",
      "AZURE_OPENAI_DEPLOYMENT": "gpt-6-astra", "AZURE_OPENAI_SECRET_ID": "clinicflow/azure-openai-key"}
step("1. IAM: read the Azure OpenAI key from Secrets Manager")
pol = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
       "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:clinicflow/azure-openai-key*"}]})
for role in ("clinicflow-agentcore-role", "clinicflow-lambda-role"):
    iam.put_role_policy(RoleName=role, PolicyName="clinicflow-azure-secret", PolicyDocument=pol)
iam.put_role_policy(RoleName="clinicflow-lambda-role", PolicyName="clinicflow-transcribe", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Resource": "*",
    "Action": ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob"]}]}))
print("   transcribe policy attached to clinicflow-lambda-role")
_mb = lam.get_function_configuration(FunctionName="clinicflow-whatsapp-handler")["Environment"]["Variables"].get("MEDIA_BUCKET", BUCKET)
iam.put_role_policy(RoleName="clinicflow-lambda-role", PolicyName="clinicflow-v4-media-privacy", PolicyDocument=json.dumps({
    "Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["social-messaging:PostWhatsAppMessageMedia"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:DeleteObject", "s3:PutObject", "s3:GetObject"], "Resource": f"arn:aws:s3:::{_mb}/*"}]}))
print("   media upload + erasure policy attached (bucket", _mb + ")")
try:
    boto3.client("secretsmanager").describe_secret(SecretId="clinicflow/azure-openai-key")
    print("   secret clinicflow/azure-openai-key found")
except Exception:
    print("   !! secret clinicflow/azure-openai-key NOT found – create it, then re-run")

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
env = {**cur.get("environmentVariables", {}), **AZ}
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
    ev_ = lam.get_function_configuration(FunctionName=fn).get("Environment", {}).get("Variables", {})
    extra = {"Timeout": 120} if fn == "clinicflow-whatsapp-handler" else {}
    lam.update_function_configuration(FunctionName=fn, Environment={"Variables": {**ev_, **AZ}}, **extra)
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    print("   updated", fn)

step("7. Smoke test via AgentCore")
time.sleep(5)
r = dp.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId="clinicflow-smoke-test-session-v2-0000001",
                            payload=json.dumps({"phone": "910000000001", "text": "Hi, mujhe laser ke liye token chahiye, naam Demo"}).encode(),
                            contentType="application/json", accept="application/json")
print("   ", r["response"].read().decode()[:240])
print("\nCLINICFLOW_V3_OK")
