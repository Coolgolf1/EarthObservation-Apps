
import base64
import hashlib
import http.server
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

API_BASE = "https://imagecube-toucan-api.jollyground-fbd4d1eb.eastus.azurecontainerapps.io".rstrip("/")
REDIRECT_URI = "http://localhost:8001/docs/oauth2-redirect" 

p = urllib.parse.urlparse(REDIRECT_URI)
ORIGIN = f"{p.scheme}://{p.hostname}:{p.port}"

# --- client_id, scope and endpoints from the API itself ---
flow = next(iter(requests.get(f"{API_BASE}/openapi.json").json()
                 ["components"]["securitySchemes"].values()))["flows"]["authorizationCode"]
client_id = re.search(r'"clientId":\s*"([0-9a-f-]+)"', requests.get(f"{API_BASE}/docs").text).group(1)
scope = next(iter(flow["scopes"]))

# --- PKCE ---
verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)

got = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        got.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
        self.send_response(200)
        self.end_headers()
        self.wfile.write("You can close this tab.".encode())

    def log_message(self, *a):
        pass


try:
    server = http.server.HTTPServer(("localhost", p.port), Handler)
except OSError as e:
    sys.exit(f"Port {p.port} is busy ({e}). Use another registered redirect URI with a free port.")
threading.Thread(target=server.handle_request, daemon=True).start()

url = flow["authorizationUrl"] + "?" + urllib.parse.urlencode({
    "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
    "scope": f"{scope} openid profile", "state": state,
    "code_challenge": challenge, "code_challenge_method": "S256",
})
print("Open this URL in Chrome and complete the login:\n" + url + "\n")
for cmd in (["open", "-a", "Google Chrome", url], ["open", url]):
    try:
        subprocess.run(cmd, check=True)
        break
    except Exception:
        continue

waited = 0.0
while "code" not in got and "error" not in got and waited < 240:
    time.sleep(0.5)
    waited += 0.5
if "code" not in got:
    sys.exit(f"No code received: {got or 'timeout'}")
if got.get("state", [None])[0] != state:
    sys.exit("state mismatch")

resp = requests.post(flow["tokenUrl"], data={
    "grant_type": "authorization_code", "client_id": client_id,
    "code": got["code"][0], "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
}, headers={"Origin": ORIGIN})

tok = resp.json()
if "access_token" not in tok:
    sys.exit(f"Token endpoint failed (HTTP {resp.status_code}):\n{tok}")


TOKEN = tok["access_token"]

# Save token to .env file
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    content = env_path.read_text()
    if "IMAGECUBES_TOKEN=" in content:
        # Replace existing token
        import re as _re
        content = _re.sub(r"IMAGECUBES_TOKEN=.*", f"IMAGECUBES_TOKEN={TOKEN}", content)
    else:
        # Append new token
        if not content.endswith("\n"):
            content += "\n"
        content += f"\nIMAGECUBES_TOKEN={TOKEN}\n"
    env_path.write_text(content)
    print(f"\n✓ Token saved to {env_path}")
else:
    env_path.write_text(f"IMAGECUBES_TOKEN={TOKEN}\n")
    print(f"\n✓ Token saved to new {env_path}")

r = requests.get(f"{API_BASE}/api/v1/imagecubes",
                 headers={"Authorization": f"Bearer {TOKEN}"})
r.raise_for_status()
print(r.status_code, r.json())  