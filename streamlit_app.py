import json
import base64
import hashlib
import hmac
import io
import mimetypes
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

import streamlit as st
import streamlit.components.v1 as components
from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as google_api_build
from googleapiclient.http import MediaIoBaseUpload


BASE = pathlib.Path("/tmp/manimflow_workspace")
BASE.mkdir(parents=True, exist_ok=True)
APP_DIR = pathlib.Path(__file__).resolve().parent
LOCAL_PI = APP_DIR / "node_modules" / ".bin" / "pi"
RUNTIME_DIR = pathlib.Path("/tmp/manimflow_pi_runtime")
NODE_DIR = pathlib.Path("/tmp/manimflow_node")
PI_VERSION = "0.80.6"
_PI_INSTALL_LOCK = threading.Lock()
MANIM_DOCS_REPO = pathlib.Path("/tmp/manim_community_docs")
MANIM_DOCS_URL = "https://github.com/ManimCommunity/manim.git"
_MANIM_DOCS_LOCK = threading.Lock()
_RENDER_SEMAPHORE = threading.Semaphore(1)

GITHUB_CLIENT_ID = "8b76dd0df855d8bc7db1"
COPILOT_BASE = "https://api.individual.githubcopilot.com"
COPILOT_MODEL = os.environ.get("COPILOT_MODEL", "gpt-4o")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
CHATGPT_MODEL = os.environ.get("CHATGPT_MODEL", "gpt-5.4")
EXA_ENDPOINT = os.environ.get(
    "EXA_ENDPOINT", "https://demos.exa.ai/chatbot-demo/api/chat/stream"
)
EXA_MODEL = os.environ.get("EXA_MODEL", "google/gemini-2.5-flash")
EXA_EXTENSION = APP_DIR / "pi_extensions" / "exa_direct.ts"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_STORAGE_KEY = "manimflow.youtube.credentials.v1"
_browser_storage = components.declare_component(
    "manimflow_browser_storage", path=str(APP_DIR / "browser_storage_component")
)
_code_browser = components.declare_component(
    "manimflow_code_browser", path=str(APP_DIR / "code_browser_component")
)
PI_PROVIDER = "manimflow-copilot"
PI_TOKEN_ENV = "MANIMFLOW_COPILOT_TOKEN"
ANTHROPIC_TOKEN_ENV = "ANTHROPIC_OAUTH_TOKEN"
CHATGPT_TOKEN_ENV = "MANIMFLOW_CHATGPT_OAUTH_TOKEN"

def request_device_code():
    data = urllib.parse.urlencode(
        {"client_id": GITHUB_CLIENT_ID, "scope": "read:user"}
    ).encode()
    req = urllib.request.Request(
        "https://github.com/login/device/code",
        data=data,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read())


def secret_value(name):
    try:
        value = st.secrets.get(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(name, "").strip()


def deployment_token():
    return secret_value("COPILOT_TOKEN")


def youtube_config():
    return {
        "client_id": secret_value("YOUTUBE_CLIENT_ID"),
        "client_secret": secret_value("YOUTUBE_CLIENT_SECRET"),
        "redirect_uri": secret_value("YOUTUBE_REDIRECT_URI"),
        "encryption_key": secret_value("YOUTUBE_TOKEN_ENCRYPTION_KEY"),
    }


def browser_credential(action="get", value=None):
    return _browser_storage(
        action=action,
        storageKey=YOUTUBE_STORAGE_KEY,
        value=value or "",
        key="youtube_browser_credentials",
        default=None,
    )


def workspace_code_snapshot(
    workspace,
    max_file_bytes=100_000,
    max_total_bytes=600_000,
    max_media_file_bytes=50_000_000,
    max_media_total_bytes=100_000_000,
):
    allowed_suffixes = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"}
    media_suffixes = {".mp4", ".webm", ".mov", ".gif", ".png", ".jpg", ".jpeg", ".wav", ".mp3"}
    excluded_parts = {"manim-docs", "partial_movie_files", "Tex", "texts", "__pycache__", ".git"}
    files = {}
    total = 0
    media_total = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if any(part in excluded_parts or part.startswith(".") for part in relative.parts):
            continue
        suffix = path.suffix.lower()
        try:
            size = path.stat().st_size
            if suffix in media_suffixes:
                if size <= 0 or size > max_media_file_bytes or media_total + size > max_media_total_bytes:
                    continue
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files[relative.as_posix()] = {
                    "kind": "download",
                    "mime": mime,
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    "size": size,
                }
                media_total += size
                continue
            if suffix not in allowed_suffixes or size > max_file_bytes or total + size > max_total_bytes:
                continue
            content = path.read_text(errors="replace")
        except OSError:
            continue
        files[relative.as_posix()] = content
        total += size
    return files


def code_snapshot_signature(files):
    digest = hashlib.sha256()
    for path, value in files.items():
        digest.update(path.encode())
        digest.update(b"\0")
        if isinstance(value, str):
            digest.update(value.encode())
        else:
            digest.update(str(value.get("kind", "")).encode())
            digest.update(str(value.get("size", 0)).encode())
            digest.update(value.get("data", "").encode())
        digest.update(b"\0")
    return digest.hexdigest()


def credential_cipher(secret):
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_youtube_credentials(credentials, secret):
    payload = json.dumps(
        {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes or [YOUTUBE_SCOPE]),
        }
    ).encode()
    return credential_cipher(secret).encrypt(payload).decode()


def decrypt_youtube_credentials(encrypted, secret):
    try:
        payload = json.loads(credential_cipher(secret).decrypt(encrypted.encode()))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return None
    return Credentials(**payload)


def youtube_oauth_flow(config, state=None):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [config["redirect_uri"]],
            }
        },
        scopes=[YOUTUBE_SCOPE],
        state=state,
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = config["redirect_uri"]
    return flow


def youtube_login_url(config):
    timestamp = str(int(time.time()))
    nonce = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
    payload = f"{timestamp}.{nonce}"
    signature = hmac.new(
        config["encryption_key"].encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    state = f"{payload}.{signature}"
    flow = youtube_oauth_flow(config, state=state)
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    return url


def valid_youtube_oauth_state(state, secret, max_age_seconds=15 * 60):
    try:
        timestamp, nonce, supplied = state.split(".", 2)
        payload = f"{timestamp}.{nonce}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        age = int(time.time()) - int(timestamp)
        return hmac.compare_digest(supplied, expected) and 0 <= age <= max_age_seconds
    except (AttributeError, TypeError, ValueError):
        return False


def restore_youtube_credentials(encrypted, config):
    if not encrypted or not config["encryption_key"]:
        return None
    credentials = decrypt_youtube_credentials(encrypted, config["encryption_key"])
    if not credentials:
        return None
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
    return credentials


def poll_token(device_code):
    data = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
    ).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=data,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read())


def copilot_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Editor-Version": "vscode/1.85.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Intent": "conversation-completions",
    }


def copilot_chat(token, messages, log_queue=None):
    payload = json.dumps({"model": COPILOT_MODEL, "messages": messages}).encode()
    for attempt in range(5):
        req = urllib.request.Request(
            f"{COPILOT_BASE}/chat/completions",
            data=payload,
            headers=copilot_headers(token),
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                body = json.loads(response.read())
            return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            try:
                delay = max(int(retry_after), 5 * (2**attempt))
            except ValueError:
                delay = 5 * (2**attempt)
            delay = min(delay, 120)
            if log_queue:
                log_queue.put(
                    ("log", f"Copilot rate limited the planner; retrying in {delay}s...")
                )
            time.sleep(delay)
    raise RuntimeError("Copilot planner exhausted its retry attempts.")


def gemini_chat(api_key, messages, log_queue=None):
    system_text = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    user_text = "\n".join(
        message["content"] for message in messages if message["role"] == "user"
    )
    payload = json.dumps(
        {
            "systemInstruction": {"parts": [{"text": system_text}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        }
    ).encode()
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={urllib.parse.quote(api_key)}"
    )
    for attempt in range(5):
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.loads(response.read())
            parts = body["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts).strip()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            try:
                delay = max(int(retry_after), 5 * (2**attempt))
            except ValueError:
                delay = 5 * (2**attempt)
            delay = min(delay, 120)
            if log_queue:
                log_queue.put(
                    ("log", f"Gemini rate limited the planner; retrying in {delay}s...")
                )
            time.sleep(delay)
    raise RuntimeError("Gemini planner exhausted its retry attempts.")


def anthropic_chat(access_token, messages, log_queue=None):
    system_text = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    conversation = [
        {"role": message["role"], "content": message["content"]}
        for message in messages
        if message["role"] in {"user", "assistant"}
    ]
    payload = json.dumps(
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 4096,
            "system": system_text,
            "messages": conversation,
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        "anthropic-dangerous-direct-browser-access": "true",
        "User-Agent": "claude-cli/2.1.0",
        "x-app": "cli",
    }
    for attempt in range(4):
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.loads(response.read())
            text = "".join(
                block.get("text", "")
                for block in body.get("content", [])
                if block.get("type") == "text"
            ).strip()
            if not text:
                raise RuntimeError("Anthropic returned no planner text.")
            return text
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Anthropic HTTP {exc.code}: {detail}") from exc
            delay = min(10 * (2**attempt), 60)
            if log_queue:
                log_queue.put(
                    ("log", f"Anthropic rate limited the planner; retrying in {delay}s...")
                )
            time.sleep(delay)
    raise RuntimeError("Anthropic planner exhausted its retry attempts.")


def jwt_payload(token):
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        return json.loads(base64.urlsafe_b64decode(encoded))
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("The ChatGPT OAuth access token is not a valid JWT.") from exc


def chatgpt_account_id(access_token):
    account_id = jwt_payload(access_token).get("https://api.openai.com/auth", {}).get(
        "chatgpt_account_id"
    )
    if not account_id:
        raise RuntimeError("The ChatGPT OAuth token does not contain an account ID.")
    return account_id


def chatgpt_codex_chat(access_token, messages, log_queue=None):
    system_text = "\n".join(
        message["content"] for message in messages if message["role"] == "system"
    )
    inputs = [
        {
            "role": message["role"],
            "content": [{"type": "input_text", "text": message["content"]}],
        }
        for message in messages
        if message["role"] in {"user", "assistant"}
    ]
    payload = json.dumps(
        {
            "model": CHATGPT_MODEL,
            "store": False,
            "stream": True,
            "instructions": system_text or "You are a helpful assistant.",
            "input": inputs,
            "text": {"verbosity": "low"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "low", "summary": "auto"},
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": chatgpt_account_id(access_token),
        "originator": "pi",
        "User-Agent": "pi (streamlit; cloud)",
        "OpenAI-Beta": "responses=experimental",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    for attempt in range(4):
        request = urllib.request.Request(
            "https://chatgpt.com/backend-api/codex/responses",
            data=payload,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read().decode("utf-8", errors="replace")
            text = ""
            for line in raw.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "response.output_text.delta":
                    text += event.get("delta", "")
            if not text.strip():
                raise RuntimeError("ChatGPT Codex returned no planner text.")
            return text.strip()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"ChatGPT Codex HTTP {exc.code}: {detail}") from exc
            delay = min(10 * (2**attempt), 60)
            if log_queue:
                log_queue.put(
                    ("log", f"ChatGPT Codex rate limited the planner; retrying in {delay}s...")
                )
            time.sleep(delay)
    raise RuntimeError("ChatGPT Codex planner exhausted its retry attempts.")


def messages_to_exa_prompt(messages):
    return "\n\n".join(
        [
            "You are called by ManimFlow.",
            "Follow the latest SYSTEM and USER instructions exactly.",
            "When asked for JSON, output exactly one strict JSON object and nothing else.",
            "Do not wrap JSON in markdown fences.",
            *[
                f"{str(message.get('role', 'user')).upper()}:\n{message.get('content', '')}"
                for message in messages
            ],
        ]
    )


def parse_exa_stream(raw):
    text = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event.get("content"), str):
            text += event["content"]
    if not text and raw.strip() and "data:" not in raw:
        try:
            response = json.loads(raw)
            text = (
                response.get("text")
                or response.get("response")
                or response.get("content")
                or response.get("choices", [{}])[0].get("message", {}).get("content")
                or ""
            )
        except json.JSONDecodeError:
            text = raw.strip()
    return text


def clean_exa_text(text):
    cleaned = re.sub(r"```followups[\s\S]*?```", "", str(text), flags=re.I)
    cleaned = re.sub(r"```followups[\s\S]*$", "", cleaned, flags=re.I).strip()
    if not cleaned.startswith("{"):
        return cleaned
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return cleaned[: index + 1]
    return cleaned


def exa_chat(messages, log_queue=None):
    payload = json.dumps(
        {
            "message": messages_to_exa_prompt(messages),
            "history": [],
            "exaEnabled": False,
            "model": EXA_MODEL,
            "searchType": "instant",
        }
    ).encode()
    request = urllib.request.Request(
        EXA_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        raw = response.read().decode("utf-8", errors="replace")
    result = clean_exa_text(parse_exa_stream(raw))
    if not result:
        raise RuntimeError("Exa returned no planner content.")
    return result


def parse_metadata_json(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip(), flags=re.I)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        raise ValueError("The model did not return metadata JSON.")
    data = json.loads(match.group(0))
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
    return {
        "title": str(data.get("title") or "Manim animation")[:100],
        "description": str(data.get("description") or "Generated with ManimFlow"),
        "tags": [str(tag)[:30] for tag in tags[:20]],
    }


def suggest_youtube_metadata(provider, credential, user_prompt):
    messages = [
        {
            "role": "system",
            "content": (
                "Create accurate YouTube metadata for a short educational Manim animation. "
                "Return only strict JSON with title, description, and tags. Title must be under "
                "100 characters. tags must be an array of at most 12 concise strings. Do not "
                "invent claims about animation content beyond the user's prompt."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]
    if provider == "gemini":
        response = gemini_chat(credential, messages)
    elif provider == "chatgpt":
        response = chatgpt_codex_chat(credential, messages)
    elif provider == "anthropic":
        response = anthropic_chat(credential, messages)
    elif provider == "exa":
        response = exa_chat(messages)
    else:
        response = copilot_chat(credential, messages)
    return parse_metadata_json(response)


def upload_to_youtube(video_bytes, credentials, title, description, tags, privacy, progress):
    youtube = google_api_build("youtube", "v3", credentials=credentials, cache_discovery=False)
    media = MediaIoBaseUpload(
        io.BytesIO(video_bytes),
        mimetype="video/mp4",
        chunksize=4 * 1024 * 1024,
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "27",
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        },
        media_body=media,
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress.progress(min(float(status.progress()), 1.0))
    progress.progress(1.0)
    return response["id"]


def node_major(node_command):
    try:
        version = subprocess.check_output(
            [node_command, "--version"], text=True, timeout=10
        ).strip()
        return int(version.removeprefix("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def resolve_pi_command(log_queue):
    configured = os.environ.get("PI_COMMAND", "").strip()
    if configured:
        return configured
    system_node = shutil.which("node")
    if LOCAL_PI.exists() and system_node and node_major(system_node) >= 22:
        return str(LOCAL_PI)
    installed = shutil.which("pi")
    if installed and system_node and node_major(system_node) >= 22:
        return installed

    runtime_pi = RUNTIME_DIR / "node_modules" / ".bin" / "pi"
    isolated_node = NODE_DIR / "bin" / "node"
    runtime_node = str(isolated_node) if isolated_node.exists() else system_node
    if runtime_pi.exists() and runtime_node and node_major(runtime_node) >= 22:
        return str(runtime_pi)

    with _PI_INSTALL_LOCK:
        isolated_node = NODE_DIR / "bin" / "node"
        runtime_node = str(isolated_node) if isolated_node.exists() else shutil.which("node")
        if runtime_pi.exists() and runtime_node and node_major(runtime_node) >= 22:
            return str(runtime_pi)

        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or node_major(node) < 22 or not npm:
            log_queue.put(("log", "Installing an isolated Node 22 runtime..."))
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nodeenv",
                    "--node=22.19.0",
                    "--prebuilt",
                    str(NODE_DIR),
                ],
                check=True,
                timeout=300,
            )
            node = str(NODE_DIR / "bin" / "node")
            npm = str(NODE_DIR / "bin" / "npm")

        log_queue.put(("log", f"Installing Pi {PI_VERSION}..."))
        install_env = os.environ.copy()
        install_env["PATH"] = f"{pathlib.Path(node).parent}:{install_env.get('PATH', '')}"
        subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(RUNTIME_DIR),
                "--no-audit",
                "--no-fund",
                f"@earendil-works/pi-coding-agent@{PI_VERSION}",
            ],
            check=True,
            env=install_env,
            timeout=300,
        )
        if not runtime_pi.exists():
            raise RuntimeError("Pi installation completed but its executable was not found.")
        return str(runtime_pi)


def write_pi_config(agent_dir, provider):
    agent_dir.mkdir(parents=True, exist_ok=True)
    models = {
        "providers": {
            PI_PROVIDER: {
                "baseUrl": COPILOT_BASE,
                "api": "openai-completions",
                "apiKey": f"${PI_TOKEN_ENV}",
                "authHeader": True,
                "headers": {
                    "Editor-Version": "vscode/1.85.0",
                    "Copilot-Integration-Id": "vscode-chat",
                    "Openai-Intent": "conversation-completions",
                },
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": COPILOT_MODEL,
                        "name": f"GitHub Copilot {COPILOT_MODEL}",
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": 128000,
                        "maxTokens": 16384,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }
    if provider == "chatgpt":
        models["providers"]["openai-codex"] = {
            "baseUrl": "https://chatgpt.com/backend-api",
            "api": "openai-codex-responses",
            "apiKey": f"${CHATGPT_TOKEN_ENV}",
            "models": [
                {
                    "id": CHATGPT_MODEL,
                    "name": f"ChatGPT Codex {CHATGPT_MODEL}",
                    "reasoning": True,
                    "thinkingLevelMap": {"minimal": "low", "xhigh": "xhigh"},
                    "input": ["text", "image"],
                    "contextWindow": 272000,
                    "maxTokens": 128000,
                    "cost": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                }
            ],
        }
    (agent_dir / "models.json").write_text(json.dumps(models, indent=2))
    (agent_dir / "settings.json").write_text(
        json.dumps({"telemetry": False, "quietStartup": True}, indent=2)
    )
    if provider == "exa":
        if not EXA_EXTENSION.is_file():
            raise RuntimeError(f"Exa Pi extension is missing: {EXA_EXTENSION}")
        extensions_dir = agent_dir / "extensions"
        extensions_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(EXA_EXTENSION, extensions_dir / "exa_direct.ts")


def prune_manim_docs(docs_dir):
    shutil.rmtree(docs_dir / "source" / "changelog", ignore_errors=True)
    changelog_index = docs_dir / "source" / "changelog.rst"
    try:
        changelog_index.unlink()
    except FileNotFoundError:
        pass


def ensure_manim_docs(log_queue):
    docs_dir = MANIM_DOCS_REPO / "docs"
    if docs_dir.is_dir():
        prune_manim_docs(docs_dir)
        return docs_dir

    with _MANIM_DOCS_LOCK:
        if docs_dir.is_dir():
            prune_manim_docs(docs_dir)
            return docs_dir

        log_queue.put(("log", "Cloning Manim documentation (shallow, docs only)..."))
        staging_root = pathlib.Path(tempfile.mkdtemp(prefix="manim-docs-", dir="/tmp"))
        staging_repo = staging_root / "repo"
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--filter=blob:none",
                    "--sparse",
                    MANIM_DOCS_URL,
                    str(staging_repo),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            subprocess.run(
                ["git", "sparse-checkout", "set", "docs"],
                cwd=str(staging_repo),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if not (staging_repo / "docs").is_dir():
                raise RuntimeError("The Manim repository did not contain docs/.")
            if MANIM_DOCS_REPO.exists():
                shutil.rmtree(MANIM_DOCS_REPO)
            staging_repo.rename(MANIM_DOCS_REPO)
            prune_manim_docs(docs_dir)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Unable to clone Manim documentation: {detail}") from exc
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        log_queue.put(("log", f"Manim documentation ready → {docs_dir}"))
        return docs_dir


def write_project_files(workspace, user_prompt, plan, require_docs=True):
    render_command = f'"{sys.executable}" -m manim -pql --disable_caching scene.py AnimScene'
    (workspace / "plan.md").write_text(
        f"# Animation Plan\n\n## Original request\n{user_prompt}\n\n## Scenes\n{plan}\n"
    )
    documentation_gate = (
        "MANDATORY DOCUMENTATION GATE — complete this before writing any Python:\n"
        "1. Identify every Manim class, animation, object, layout helper, and renderer feature you plan to use.\n"
        "2. Search only manim-docs/source/ with focused commands such as `rg -l 'Pattern' manim-docs/source`. Do not search manim-docs/i18n, do not dump broad match output, and do not use the web.\n"
        "3. Use Pi's read tool (not only grep/cat) to read the relevant local documentation for every planned part. Read at least three distinct relevant documentation files.\n"
        "4. Create docs_consulted.md before any .py file. For each planned API, record the exact manim-docs/ path actually read and the key constraint or usage learned. Cite no file you did not read with the read tool.\n"
        "Do not start implementation until docs_consulted.md is complete.\n"
        if require_docs
        else "Documentation reading is optional. Start implementation immediately and do not create docs_consulted.md unless it is genuinely useful.\n"
    )
    (workspace / "AGENTS.md").write_text(
        "You are an autonomous coding agent building a Manim animation.\n"
        "Read plan.md before editing. Work without asking questions.\n"
        "The current working directory is already the project root. Write docs_consulted.md, helper modules, scene.py, and media directly here. Never create or write into a manim_output/ or project/ subdirectory.\n"
        + documentation_gate
        + "Create helper modules first and scene.py last.\n"
        "Every Python file must start with: from manim import *\n"
        "scene.py must define exactly one direct Scene subclass: class AnimScene(Scene).\n"
        "Implement every planned scene sequentially inside AnimScene.construct(). Helper modules may define Mobject/VGroup classes and functions, but must not define Scene subclasses. Never alias AnimScene, never use multiple inheritance between Scene classes, and never render separate scene classes.\n"
        "A full LaTeX toolchain is installed and available. Use MathTex for equations, variables, units, operators, and all mathematical notation. Use Tex for mixed LaTeX prose and mathematics, and Text only for ordinary non-mathematical labels.\n"
        "Do not replace mathematical notation with Text to avoid LaTeX. If TeX compilation fails, diagnose and repair the expression or template, then render again.\n"
        "Use Arrow(start=..., end=...) for arrows.\n"
        "Keep every object inside the camera frame and avoid overlaps.\n"
        "Repeatedly run this test and repair every error:\n"
        f"{render_command}\n"
        "Stop only after the command succeeds and produces an MP4.\n"
    )


def video_duration(path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def minimum_video_duration(plan):
    scene_count = len(re.findall(r"(?m)^\s*\d+[.)]\s+", plan))
    return max(6.0, scene_count * 2.0)


def newest_video(workspace, minimum_duration=0):
    videos = [
        path
        for path in workspace.rglob("AnimScene.mp4")
        if path.is_file()
        and "partial_movie_files" not in path.parts
        and path.stat().st_size > 50_000
        and video_duration(path) >= minimum_duration
    ]
    return max(videos, key=lambda path: path.stat().st_mtime) if videos else None


def normalize_nested_workspace(workspace):
    """Recover when an agent mistakenly creates workspace/manim_output/*."""
    nested = workspace / "manim_output"
    if (workspace / "scene.py").is_file() or not (nested / "scene.py").is_file():
        return False
    for child in nested.iterdir():
        destination = workspace / child.name
        if destination.exists():
            continue
        shutil.move(str(child), str(destination))
    try:
        nested.rmdir()
    except OSError:
        pass
    return True


def canonical_doc_path(value, workspace):
    normalized = str(value).replace("\\", "/")
    marker = "manim-docs/"
    if marker not in normalized:
        return None
    relative = marker + normalized.split(marker, 1)[1]
    relative = relative.rstrip(".,;:)]}`'").replace("//", "/")
    candidate = workspace / relative
    workspace_docs = (workspace / "manim-docs").resolve()
    try:
        if candidate.is_file() and candidate.resolve().is_relative_to(workspace_docs):
            return relative
    except (OSError, ValueError):
        return None
    return None


def observe_documentation_read(line, workspace, observed_reads):
    if '"type":"tool_execution_start"' not in line or '"toolName":"read"' not in line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if event.get("type") != "tool_execution_start" or event.get("toolName") != "read":
        return
    path = (event.get("args") or {}).get("path", "")
    canonical = canonical_doc_path(path, workspace)
    if canonical:
        observed_reads.add(canonical)


def validate_documentation_gate(workspace, observed_reads):
    evidence = workspace / "docs_consulted.md"
    if not evidence.is_file() or evidence.stat().st_size < 200:
        raise RuntimeError(
            "Pi skipped the documentation gate: docs_consulted.md is missing or incomplete."
        )
    content = evidence.read_text(errors="replace")
    # Match actual documentation files, not prose such as "search under
    # manim-docs/source/". Treating that directory prefix as a citation caused
    # successful renders to fail the gate even when three real files were read.
    cited_values = re.findall(
        r"manim-docs/[A-Za-z0-9_./-]+\.(?:rst|md)\b", content, flags=re.IGNORECASE
    )
    if not cited_values:
        raise RuntimeError(
            "Pi's docs_consulted.md does not cite exact paths from the local Manim docs."
        )
    cited_paths = set()
    missing_paths = []
    for value in cited_values:
        canonical = canonical_doc_path(value, workspace)
        if canonical:
            cited_paths.add(canonical)
        else:
            missing_paths.append(value.rstrip(".,;:"))
    if missing_paths:
        raise RuntimeError(
            "Pi cited documentation paths that do not exist: "
            + ", ".join(sorted(set(missing_paths)))
        )
    if len(cited_paths) < 3:
        raise RuntimeError(
            f"Pi consulted only {len(cited_paths)} distinct documentation file(s); at least 3 are required."
        )
    unread_citations = cited_paths - observed_reads
    if unread_citations:
        raise RuntimeError(
            "Pi cited documentation it did not actually open with the read tool: "
            + ", ".join(sorted(unread_citations))
        )
    python_files = list(workspace.glob("*.py"))
    if python_files and evidence.stat().st_mtime > min(path.stat().st_mtime for path in python_files):
        raise RuntimeError(
            "Pi wrote Python before completing docs_consulted.md; documentation gate failed."
        )


def clean_old_runs(max_age_seconds=6 * 60 * 60):
    cutoff = time.time() - max_age_seconds
    for path in BASE.glob("run-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            pass


def concise_pi_event(line, tool_details=None):
    line = line.strip()
    if not line:
        return None
    # These token-level events contain an increasingly large copy of the whole
    # partial message. Parsing or rendering them can freeze a Streamlit session.
    if line.startswith('{"type":"message_update"'):
        return None
    if line.startswith('{"type":"agent_start"'):
        return "Pi is analyzing the plan."
    if line.startswith('{"type":"agent_end"'):
        return "Pi finished its work."
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line[:1000]

    event_type = event.get("type")
    if event_type == "auto_retry_start":
        return (
            f"Provider retry {event.get('attempt')}/{event.get('maxAttempts')} in "
            f"{event.get('delayMs', 0) / 1000:g}s: {event.get('errorMessage', 'unknown error')}"
        )
    if event_type == "auto_retry_end":
        if event.get("success"):
            return "Provider retry succeeded."
        return f"Provider retry failed: {event.get('finalError', 'unknown error')}"
    if event_type == "tool_execution_start":
        name = event.get("toolName", "tool")
        arguments = event.get("args") or {}
        if name in {"write", "edit", "read"}:
            detail = arguments.get("path", "")
        elif name == "bash":
            detail = arguments.get("command", "")
        else:
            detail = json.dumps(arguments)
        if tool_details is not None and event.get("toolCallId"):
            tool_details[event["toolCallId"]] = {
                "name": name,
                "detail": str(detail),
            }
        detail = str(detail).replace("\n", " ")[:500]
        labels = {
            "write": "Writing file",
            "edit": "Editing file",
            "read": "Reading file",
            "bash": "Running command",
        }
        return f"{labels.get(name, f'Running {name}')} → {detail}".rstrip()
    if event_type == "tool_execution_end":
        name = event.get("toolName", "tool")
        tool_detail = {}
        if tool_details is not None and event.get("toolCallId"):
            tool_detail = tool_details.pop(event["toolCallId"], {})
        result = event.get("result")
        result_text = ""
        if isinstance(result, dict):
            parts = result.get("content", [])
            if isinstance(parts, list):
                result_text = "\n".join(
                    str(part.get("text", ""))
                    for part in parts
                    if isinstance(part, dict) and part.get("text")
                )
        if not result_text and result is not None:
            result_text = str(result)
        status = "failed" if event.get("isError") else "completed"
        result_text = result_text.strip()
        is_plan_read = (
            name == "read"
            and tool_detail.get("name") == "read"
            and pathlib.PurePath(tool_detail.get("detail", "")).name == "plan.md"
        )
        if not is_plan_read:
            result_text = result_text[:1500]
        return f"{name} {status}" + (f":\n{result_text}" if result_text else "")
    if event_type != "message_end":
        return None

    message = event.get("message", {})
    role = message.get("role")
    content = message.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]

    updates = []
    for block in content:
        block_type = block.get("type")
        if block_type == "text" and role == "assistant":
            text = str(block.get("text", "")).strip()
            if text:
                updates.append(f"Pi: {text[:1500]}")
    return "\n".join(updates) or None


def is_rate_limit_event(line):
    if line.startswith('{"type":"message_update"'):
        return False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        lowered = line.lower()
        return "http error 429" in lowered or "too many requests" in lowered
    if event.get("type") not in {"auto_retry_start", "auto_retry_end"}:
        return False
    error_text = f"{event.get('errorMessage', '')} {event.get('finalError', '')}".lower()
    return "429" in error_text or "too many requests" in error_text


def _run_render(provider, credential, user_prompt, log_queue):
    clean_old_runs()
    run_dir = pathlib.Path(tempfile.mkdtemp(prefix="run-", dir=BASE))
    workspace = run_dir / "project"
    agent_dir = run_dir / "pi-agent"
    workspace.mkdir()
    log_queue.put(("workspace", str(workspace)))
    provider_display = {
        "gemini": "Gemini",
        "chatgpt": "ChatGPT Codex",
        "anthropic": "Anthropic",
        "exa": "Exa",
        "copilot": "Copilot",
    }[provider]

    try:
        pi_command = resolve_pi_command(log_queue)
        if provider != "exa":
            docs_dir = ensure_manim_docs(log_queue)
            shutil.copytree(
                docs_dir,
                workspace / "manim-docs",
                copy_function=os.link,
            )

        log_queue.put(("log", "Planning scenes..."))
        planner_messages = [
            {
                "role": "system",
                "content": (
                    "You are a Manim animation planner. Output a plain numbered "
                    "list of visual scenes with no markdown or commentary. Use 3-4 "
                    "scenes for a short request, 5-6 by default, and 8-12 when detailed. "
                    "When the topic is mathematical or scientific, plan properly typeset "
                    "LaTeX equations, symbols, and derivations wherever they improve the explanation."
                ),
            },
            {"role": "user", "content": user_prompt},
        ]
        if provider == "gemini":
            plan = gemini_chat(credential, planner_messages, log_queue)
            pi_provider = "google"
            pi_model = GEMINI_MODEL
        elif provider == "chatgpt":
            plan = chatgpt_codex_chat(credential, planner_messages, log_queue)
            pi_provider = "openai-codex"
            pi_model = CHATGPT_MODEL
        elif provider == "anthropic":
            plan = anthropic_chat(credential, planner_messages, log_queue)
            pi_provider = "anthropic"
            pi_model = ANTHROPIC_MODEL
        elif provider == "exa":
            plan = exa_chat(planner_messages, log_queue)
            pi_provider = "exa-direct"
            pi_model = EXA_MODEL
        else:
            plan = copilot_chat(credential, planner_messages, log_queue)
            pi_provider = PI_PROVIDER
            pi_model = COPILOT_MODEL
        log_queue.put(("plan", plan))

        write_project_files(workspace, user_prompt, plan, require_docs=provider != "exa")
        write_pi_config(agent_dir, provider)

        env = os.environ.copy()
        if provider == "gemini":
            env["GEMINI_API_KEY"] = credential
        elif provider == "chatgpt":
            env[CHATGPT_TOKEN_ENV] = credential
        elif provider == "anthropic":
            env[ANTHROPIC_TOKEN_ENV] = credential
        elif provider == "copilot":
            env[PI_TOKEN_ENV] = credential
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        env["PI_TELEMETRY"] = "0"
        env["PATH"] = f"{pathlib.Path(sys.executable).parent}:{env.get('PATH', '')}"
        if (NODE_DIR / "bin" / "node").exists():
            env["PATH"] = f"{NODE_DIR / 'bin'}:{env.get('PATH', '')}"

        log_queue.put(("log", f"Pi executable → {pi_command}"))
        try:
            pi_version = subprocess.run(
                [pi_command, "--version"],
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Pi preflight timed out while checking its version.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Pi version preflight failed: {detail}") from exc
        log_queue.put(("log", f"Pi version → {pi_version.stdout.strip()}"))

        try:
            model_check = subprocess.run(
                [pi_command, "--list-models", pi_provider],
                cwd=str(workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Pi preflight timed out while loading the Copilot model.") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Pi model preflight failed: {detail}") from exc
        if pi_model not in model_check.stdout:
            raise RuntimeError(
                f"Pi cannot see {pi_provider}/{pi_model}. Output: "
                f"{model_check.stdout.strip() or '(empty)'}"
            )
        log_queue.put(("log", f"Pi model ready → {pi_provider}/{pi_model}"))

        if provider == "exa":
            task = (
                "Implement the complete animation described in plan.md immediately. Use your "
                "file and bash tools autonomously. Run the Manim test after edits, diagnose "
                "failures, and keep repairing until it renders successfully. Do not ask questions."
            )
        else:
            task = (
                "First satisfy the mandatory documentation gate in AGENTS.md using only the "
                "local manim-docs/ checkout. Then implement the complete animation described "
                "in plan.md. Use your file and bash tools autonomously. Run the Manim test after "
                "edits, diagnose failures, and keep repairing until it renders successfully. "
                "Do not ask questions."
            )
        command_prefix = [
            pi_command,
            "--mode",
            "json",
            "--verbose",
            "--approve",
            "--no-session",
            "--no-skills",
            "--provider",
            pi_provider,
            "--model",
            pi_model,
            "--thinking",
            "medium" if provider in {"gemini", "chatgpt", "anthropic"} else "off",
            "@plan.md",
        ]
        if provider != "exa":
            command_prefix.insert(7, "--no-extensions")
        observed_doc_reads = set()
        pi_tool_details = {}
        video = None
        required_duration = minimum_video_duration(plan)
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            log_queue.put(("log", f"Starting Pi agent attempt {attempt}/{max_attempts}..."))
            process = subprocess.Popen(
                [*command_prefix, task],
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            log_queue.put(("log", f"Pi process started → PID {process.pid}"))
            if process.stdout is None:
                raise RuntimeError("Pi output stream is unavailable.")

            raw_lines = queue.Queue()

            def read_pi_output():
                for output_line in process.stdout:
                    raw_lines.put(output_line)

            reader = threading.Thread(target=read_pi_output, daemon=True)
            reader.start()
            output_started = False
            rate_limited = False
            launched_at = time.monotonic()
            while reader.is_alive() or not raw_lines.empty():
                try:
                    output_line = raw_lines.get(timeout=1)
                except queue.Empty:
                    output_line = None
                if output_line is not None:
                    output_started = True
                    if is_rate_limit_event(output_line):
                        rate_limited = True
                    if provider != "exa":
                        observe_documentation_read(output_line, workspace, observed_doc_reads)
                    update = concise_pi_event(output_line, pi_tool_details)
                    if update:
                        log_queue.put(("log", update))
                elif not output_started and time.monotonic() - launched_at >= 60:
                    process.terminate()
                    raise RuntimeError(
                        "Pi passed version/model preflight but emitted zero agent output for 60 seconds."
                    )
            reader.join(timeout=1)
            return_code = process.wait()
            log_queue.put(("log", f"Pi attempt {attempt} exit code: {return_code}"))

            if normalize_nested_workspace(workspace):
                log_queue.put(("log", "Normalized accidental nested manim_output/ workspace."))
            video = newest_video(workspace, required_duration)
            if video is not None:
                break

            missing_scene = not (workspace / "scene.py").is_file()
            state = (
                "scene.py is missing"
                if missing_scene
                else f"scene.py exists but no assembled AnimScene.mp4 of at least {required_duration:g} seconds was rendered"
            )
            if attempt < max_attempts:
                if rate_limited:
                    delay = min(30 * attempt, 120)
                    log_queue.put(
                        (
                            "log",
                            f"{provider_display} rate limited Pi; waiting {delay}s before continuation...",
                        )
                    )
                    time.sleep(delay)
                log_queue.put(
                    ("log", f"Attempt {attempt} was incomplete ({state}); continuing autonomously...")
                )
                task = (
                    f"The previous attempt exited prematurely: {state}. Continue from the existing "
                    "workspace; do not restart or merely explain. Inspect every current file, finish "
                    "scene.py and all required helpers, run the exact Manim test from AGENTS.md, fix "
                    "all failures, and verify that a non-empty MP4 exists before stopping."
                )

        if provider != "exa":
            validate_documentation_gate(workspace, observed_doc_reads)
            log_queue.put(
                ("log", f"Documentation gate passed → {len(observed_doc_reads)} distinct files read")
            )
        if video is None:
            raise RuntimeError(
                f"Pi did not produce a valid MP4 after {max_attempts} autonomous attempts."
            )

        destination = run_dir / "animation.mp4"
        shutil.copy2(video, destination)
        log_queue.put(("done", str(destination)))
    except Exception as exc:
        log_queue.put(("error", str(exc)))


def run_render(provider, credential, user_prompt, log_queue):
    if not _RENDER_SEMAPHORE.acquire(blocking=False):
        log_queue.put(
            ("log", "Another animation is currently rendering; this request is queued.")
        )
        _RENDER_SEMAPHORE.acquire()
        log_queue.put(("log", "Render slot available; starting this request."))
    try:
        _run_render(provider, credential, user_prompt, log_queue)
    finally:
        _RENDER_SEMAPHORE.release()


st.set_page_config(page_title="ManimFlow", layout="wide")
st.title("ManimFlow")
st.caption("Describe an animation topic → get a rendered MP4")

youtube_settings = youtube_config()
youtube_ready = all(youtube_settings.values())
oauth_code = st.query_params.get("code")
oauth_error = st.query_params.get("error")
if oauth_error:
    st.session_state.youtube_oauth_error = f"YouTube login failed: {oauth_error}"
    st.query_params.clear()
    st.rerun()
elif oauth_code and youtube_ready:
    returned_state = st.query_params.get("state", "")
    if not valid_youtube_oauth_state(
        returned_state, youtube_settings["encryption_key"]
    ):
        st.session_state.youtube_oauth_error = (
            "YouTube login state did not match or expired. Please start login again."
        )
    else:
        try:
            flow = youtube_oauth_flow(youtube_settings, state=returned_state)
            flow.fetch_token(code=oauth_code)
            encrypted = encrypt_youtube_credentials(
                flow.credentials, youtube_settings["encryption_key"]
            )
            st.session_state.youtube_storage_action = ("set", encrypted)
            st.session_state.youtube_login_complete = True
            st.session_state.pop("youtube_oauth_error", None)
        except Exception as exc:
            st.session_state.youtube_oauth_error = (
                f"Unable to complete YouTube login: {exc}"
            )
    st.query_params.clear()
    st.rerun()
elif oauth_code and not youtube_ready:
    st.session_state.youtube_oauth_error = (
        "YouTube OAuth returned to the app, but its Streamlit secrets are incomplete."
    )
    st.query_params.clear()
    st.rerun()

if st.session_state.get("youtube_oauth_error"):
    st.error(st.session_state.youtube_oauth_error)

storage_action, storage_value = st.session_state.get(
    "youtube_storage_action", ("get", None)
)
stored_youtube_credential = browser_credential(storage_action, storage_value)
if storage_action == "set" and stored_youtube_credential == storage_value:
    del st.session_state.youtube_storage_action
elif storage_action == "delete" and stored_youtube_credential is None:
    del st.session_state.youtube_storage_action

youtube_credentials = None
if youtube_ready and stored_youtube_credential:
    try:
        youtube_credentials = restore_youtube_credentials(
            stored_youtube_credential, youtube_settings
        )
    except Exception as exc:
        st.warning(f"Stored YouTube login could not be refreshed: {exc}")

provider_label = st.radio(
    "Model provider",
    [
        "GitHub Copilot",
        "Google Gemini",
        "ChatGPT OAuth Token",
        "Anthropic OAuth Token",
        "Exa",
    ],
    index=4,
    horizontal=True,
)
selected_provider = {
    "GitHub Copilot": "copilot",
    "Google Gemini": "gemini",
    "ChatGPT OAuth Token": "chatgpt",
    "Anthropic OAuth Token": "anthropic",
    "Exa": "exa",
}[provider_label]

configured_copilot_token = deployment_token()
if configured_copilot_token:
    st.session_state.copilot_token = configured_copilot_token
elif "copilot_token" not in st.session_state:
    st.session_state.copilot_token = None
if "device_flow" not in st.session_state:
    st.session_state.device_flow = None

gemini_key = secret_value("GEMINI_API_KEY")
chatgpt_token = ""
if selected_provider == "chatgpt":
    chatgpt_token = st.text_input(
        "ChatGPT OAuth access token",
        type="password",
        help=(
            "Paste the short-lived ChatGPT/Codex OAuth access token. It remains only "
            "in this browser session and is not saved by ManimFlow."
        ),
    ).strip()
anthropic_token = ""
if selected_provider == "anthropic":
    anthropic_token = st.text_input(
        "Anthropic OAuth access token",
        type="password",
        placeholder="sk-ant-oat...",
        help=(
            "Paste the short-lived access token from your Anthropic OAuth credential. "
            "It remains only in this browser session and is not saved by ManimFlow."
        ),
    ).strip()

if selected_provider == "gemini" and not gemini_key:
    st.error(
        "Google Gemini is selected, but GEMINI_API_KEY is missing. Add it under "
        "Streamlit App settings → Secrets before generating."
    )
    st.code('GEMINI_API_KEY = "your-key"', language="toml")
    st.stop()

if selected_provider == "anthropic" and not anthropic_token:
    st.info("Paste an Anthropic OAuth access token to use Claude for this render.")
    st.stop()

if selected_provider == "chatgpt" and not chatgpt_token:
    st.info("Paste a ChatGPT OAuth access token to use Codex for this render.")
    st.stop()

if selected_provider == "copilot" and not st.session_state.copilot_token:
    st.info("Login with GitHub to use your Copilot subscription for rendering.")
    if st.session_state.device_flow is None:
        if st.button("Login with GitHub Copilot", type="primary"):
            with st.spinner("Requesting device code..."):
                st.session_state.device_flow = request_device_code()
            st.rerun()
    else:
        flow = st.session_state.device_flow
        st.markdown(
            f"""
**Step 1:** Go to **[github.com/login/device](https://github.com/login/device)**

**Step 2:** Enter this code:

### `{flow['user_code']}`

Then continue after authorizing ManimFlow.
"""
        )
        if st.button("I've authorized — continue", type="primary"):
            with st.spinner("Verifying..."):
                result = poll_token(flow["device_code"])
            if "access_token" in result:
                st.session_state.copilot_token = result["access_token"]
                st.session_state.device_flow = None
                st.rerun()
            else:
                st.error(
                    f"Not authorized yet: {result.get('error', 'unknown error')}. Try again."
                )
    st.stop()

with st.sidebar:
    if selected_provider == "gemini":
        st.success("Connected to Google Gemini")
        st.caption(f"Agent: Pi · Model: {GEMINI_MODEL}")
        st.caption("Using GEMINI_API_KEY from Streamlit Secrets.")
    elif selected_provider == "chatgpt":
        st.success("Connected to ChatGPT OAuth")
        st.caption(f"Agent: Pi · Model: {CHATGPT_MODEL}")
        st.caption("The pasted access token is session-only and is not persisted.")
    elif selected_provider == "anthropic":
        st.success("Connected to Anthropic OAuth")
        st.caption(f"Agent: Pi · Model: {ANTHROPIC_MODEL}")
        st.caption("The pasted access token is session-only and is not persisted.")
    elif selected_provider == "exa":
        st.success("Connected to Exa")
        st.caption(f"Agent: Pi · Model: {EXA_MODEL}")
        st.caption("Using Exa's public endpoint; no API key required.")
    else:
        st.success("Connected to GitHub Copilot")
        st.caption(f"Agent: Pi · Model: {COPILOT_MODEL}")
        if deployment_token():
            st.caption("Using the deployment's Streamlit secret.")
        elif st.button("Logout"):
            st.session_state.copilot_token = None
            st.session_state.device_flow = None
            st.rerun()

    st.divider()
    st.subheader("YouTube")
    if not youtube_ready:
        st.caption("YouTube upload secrets are incomplete.")
    elif youtube_credentials:
        st.success("Account connected")
        if st.button("Disconnect YouTube", key="sidebar_youtube_disconnect"):
            st.session_state.youtube_storage_action = ("delete", None)
            st.session_state.pop("youtube_login_complete", None)
            st.rerun()
    else:
        st.link_button(
            "Connect YouTube account",
            youtube_login_url(youtube_settings),
            type="primary",
        )
        st.caption("Connect before generating so the render remains in this session.")

prompt = st.text_area(
    "Animation prompt",
    placeholder="e.g. Explain how a Fourier series builds up a square wave",
    height=100,
)
generate = st.button("Generate", type="primary")

if generate and prompt.strip():
    log_box = st.empty()
    plan_box = st.empty()
    status = st.empty()
    code_box = st.empty()
    logs = []
    last_log_render = 0.0
    events = queue.Queue()
    credential = (
        gemini_key
        if selected_provider == "gemini"
        else chatgpt_token
        if selected_provider == "chatgpt"
        else anthropic_token
        if selected_provider == "anthropic"
        else st.session_state.copilot_token
        if selected_provider == "copilot"
        else None
    )
    thread = threading.Thread(
        target=run_render,
        args=(selected_provider, credential, prompt.strip(), events),
        daemon=True,
    )
    thread.start()

    video_path = None
    live_workspace = None
    last_code_refresh = 0.0
    last_code_signature = ""
    while thread.is_alive() or not events.empty():
        try:
            kind, value = events.get(timeout=0.5)
        except queue.Empty:
            kind, value = None, None
        if kind == "workspace":
            live_workspace = pathlib.Path(value)
        elif kind == "log":
            value = str(value)
            logs.append(value if value.endswith("\n") else f"{value}\n")
            now = time.monotonic()
            if now - last_log_render >= 0.25 or events.empty():
                log_box.code("".join(logs[-80:]), language=None)
                last_log_render = now
        elif kind == "plan":
            plan_box.info(f"**Scene plan:**\n\n{value}")
        elif kind == "done":
            video_path = pathlib.Path(value)
            status.success("Render complete!")
        elif kind == "error":
            status.error(value)

        now = time.monotonic()
        if live_workspace and now - last_code_refresh >= 0.75:
            files = workspace_code_snapshot(live_workspace)
            signature = code_snapshot_signature(files)
            if files and signature != last_code_signature:
                code_box.empty()
                with code_box.container():
                    st.subheader("Live generated code")
                    _code_browser(files=files, revision=signature, default=None)
                st.session_state.generated_code_files = files
                st.session_state.generated_code_revision = signature
                last_code_signature = signature
            last_code_refresh = now

    if video_path and video_path.exists():
        video_bytes = video_path.read_bytes()
        st.session_state.rendered_video = video_bytes
        st.session_state.rendered_prompt = prompt.strip()
        st.session_state.rendered_provider = selected_provider
        try:
            with st.spinner("Suggesting YouTube metadata..."):
                st.session_state.youtube_metadata = suggest_youtube_metadata(
                    selected_provider, credential, prompt.strip()
                )
        except Exception:
            st.session_state.youtube_metadata = {
                "title": prompt.strip()[:100] or "Manim animation",
                "description": "An educational animation generated with ManimFlow.",
                "tags": ["manim", "animation", "education"],
            }

if not generate and st.session_state.get("generated_code_files"):
    st.subheader("Generated code")
    _code_browser(
        files=st.session_state.generated_code_files,
        revision=st.session_state.get("generated_code_revision", ""),
        default=None,
    )

if st.session_state.get("rendered_video"):
    video_bytes = st.session_state.rendered_video
    st.video(video_bytes)
    st.download_button(
        "Download animation.mp4",
        video_bytes,
        file_name="animation.mp4",
        mime="video/mp4",
    )

    st.divider()
    st.subheader("Upload to YouTube")
    metadata = st.session_state.get(
        "youtube_metadata",
        {
            "title": "Manim animation",
            "description": "Generated with ManimFlow.",
            "tags": ["manim", "animation"],
        },
    )

    if not youtube_ready:
        st.warning(
            "YouTube upload is not configured. Add YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET, YOUTUBE_REDIRECT_URI and "
            "YOUTUBE_TOKEN_ENCRYPTION_KEY to Streamlit Secrets."
        )
    elif not youtube_credentials:
        if st.session_state.pop("youtube_login_complete", False):
            st.info("Saving your YouTube login in this browser...")
        login_url = youtube_login_url(youtube_settings)
        st.link_button("Connect YouTube account", login_url, type="primary")
        st.caption(
            "Your encrypted login is stored only in this browser. Connecting may reload "
            "the app, so connecting before generation is recommended."
        )
    else:
        st.success("YouTube account connected")

        with st.form("youtube_upload_form"):
            title = st.text_input("Title", value=metadata["title"], max_chars=100)
            description = st.text_area(
                "Description", value=metadata["description"], height=180
            )
            tags_text = st.text_input("Tags", value=", ".join(metadata["tags"]))
            privacy = st.selectbox(
                "Privacy", ["private", "unlisted", "public"], index=0
            )
            upload = st.form_submit_button("Upload to YouTube", type="primary")

        if upload:
            tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()][:20]
            progress = st.progress(0.0, text="Uploading to YouTube...")
            try:
                video_id = upload_to_youtube(
                    video_bytes,
                    youtube_credentials,
                    title.strip(),
                    description,
                    tags,
                    privacy,
                    progress,
                )
                st.success(f"Uploaded successfully: https://youtu.be/{video_id}")
                st.link_button("Open on YouTube", f"https://youtu.be/{video_id}")
            except Exception as exc:
                st.error(f"YouTube upload failed: {exc}")
