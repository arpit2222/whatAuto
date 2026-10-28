"""ClinicFlow agent on Amazon Bedrock AgentCore Runtime (direct code deployment).

AgentCore Runtime HTTP contract:
  GET  /ping         -> {"status": "Healthy"}
  POST /invocations  -> {"phone": "...", "text": "..."}  =>  {"reply": "...", "engine": "..."}

Each patient conversation runs in its own AgentCore session (isolated microVM),
chat history lives in AgentCore Memory (short-term events), and the agent's
tools act on DynamoDB / SES. The model is Amazon Bedrock (Converse + tool use);
if Bedrock is unavailable the deterministic router answers with the same tools.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
import common as c


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/ping"):
            return self._send(200, {"status": "Healthy", "time_of_last_update": int(time.time())})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/invocations"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            phone, text = str(req["phone"]), str(req["text"])
            started = time.time()
            reply = agent.respond(phone, text)
            engine = "fallback-router" if time.time() < agent._BEDROCK_DOWN_UNTIL else f"bedrock:{agent.MODEL_ID}"
            c.log.info("turn phone=..%s engine=%s ms=%d", phone[-4:], engine, (time.time() - started) * 1000)
            self._send(200, {"reply": reply, "engine": engine})
        except Exception as e:
            c.log.exception("invocation failed")
            self._send(500, {"error": str(e)})

    def log_message(self, *args):  # keep logs clean
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
