import streamlit as st
import subprocess, pathlib, shutil, os, json, re, threading, queue
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

BASE = pathlib.Path("/tmp/manimflow_workspace")
BASE.mkdir(parents=True, exist_ok=True)
MANIM_OUTPUT = BASE / "manim_output"

EXA_URL = "https://demos.exa.ai/chatbot-demo/api/chat/stream"
LOCAL_PORT = 18642

# ── Exa proxy ────────────────────────────────────────────────────────────────

def _strip_followups(text):
    i = text.find("\n\n```followups")
    if i >= 0:
        text = text[:i]
    return re.sub(r'\n\[(["\']).*?\1(?:,\s*(["\']).*?\2)*\s*\]\s*$', '', text, flags=re.DOTALL).rstrip()

def _collect_exa_stream(messages):
    system_msg = next((m for m in messages if m['role'] == 'system'), None)
    non_system = [m for m in messages if m['role'] != 'system']
    last = non_system[-1]
    sys_content = system_msg['content'] if system_msg else ''
    user_content = "IMPORTANT: use plain ``` for ALL code blocks, never ```python or ```bash.\n\n" + sys_content + "\n\n" + last['content']
    payload = json.dumps({
        'message': user_content,
        'history': [{'role': m['role'], 'content': m['content']} for m in non_system[:-1]],
        'exaEnabled': False,
        'model': 'google/gemini-2.5-flash',
        'searchType': 'instant',
    }).encode()
    req = urllib.request.Request(EXA_URL, data=payload,
        headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
    full = ''; evt = None
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = b''
        for raw in resp:
            buf += raw
            lines = buf.split(b'\n'); buf = lines.pop()
            for line in lines:
                t = line.decode('utf-8', errors='replace').strip()
                if not t: continue
                if t.startswith('event:'): evt = t[6:].strip()
                elif t.startswith('data:') and evt == 'content':
                    try: full += json.loads(t[5:].strip()).get('content', '')
                    except: pass
    return _strip_followups(full)

class _TS(ThreadingMixIn, HTTPServer): daemon_threads = True
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == '/v1/models':
            b = json.dumps({'object': 'list', 'data': [{'id': 'manimator', 'object': 'model', 'created': 0, 'owned_by': 'exa'}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
        else: self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path != '/v1/chat/completions': self.send_response(404); self.end_headers(); return
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n))
        try: content = _collect_exa_stream(body.get('messages', []))
        except Exception as e:
            self.send_response(500); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode()); return
        rb = json.dumps({'id': 'chatcmpl-local', 'object': 'chat.completion', 'model': 'manimator',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(rb))); self.end_headers(); self.wfile.write(rb)

@st.cache_resource
def start_proxy():
    try:
        srv = _TS(('127.0.0.1', LOCAL_PORT), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return True
    except OSError:
        return True

start_proxy()

# ── LLM helpers ──────────────────────────────────────────────────────────────

def call_llm(messages):
    payload = json.dumps({'model': 'manimator', 'messages': messages, 'max_tokens': 2048}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{LOCAL_PORT}/v1/chat/completions',
        data=payload, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer dummy'})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())['choices'][0]['message']['content'].strip()

def plan_animation(user_prompt):
    raw = call_llm([
        {'role': 'system', 'content': (
            'You are a Manim animation planner. Given a topic, output ONLY a numbered scene plan. '
            'No preamble, no closing remarks, no code blocks. '
            'Scale scenes: short -> 3-4, default -> 5-6, detailed -> 8-12. '
            'Each scene: plain English description of visuals, shapes, motion, equations. '
            'No markdown, no backticks, just a numbered list.'
        )},
        {'role': 'user', 'content': user_prompt},
    ])
    raw = re.sub(r'```.*?```', '', raw, flags=re.DOTALL).strip()
    return raw.strip()

# ── Render pipeline ──────────────────────────────────────────────────────────

def run_render(user_prompt, log_queue):
    try:
        log_queue.put(('log', 'Planning scenes...'))
        plan = plan_animation(user_prompt)
        log_queue.put(('plan', plan))

        if MANIM_OUTPUT.exists():
            shutil.rmtree(MANIM_OUTPUT)
        MANIM_OUTPUT.mkdir(parents=True)

        (MANIM_OUTPUT / 'plan.md').write_text(
            f"# Animation Plan\n\n## Original request\n{user_prompt}\n\n## Scenes\n{plan}\n"
        )
        subprocess.run(['git', 'init'], cwd=str(MANIM_OUTPUT), capture_output=True)
        subprocess.run(['git', 'add', 'plan.md'], cwd=str(MANIM_OUTPUT), capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=str(MANIM_OUTPUT), capture_output=True)

        task = (
            "Read plan.md and implement the animation as a Manim project.\n\n"
            "Write files in this order:\n"
            "1. All helper modules first (objects.py, helpers.py, etc.) with all reusable classes/functions\n"
            "2. scene.py last — imports from helpers, defines AnimScene(Scene) with construct()\n\n"
            "Do not leave any file empty. scene.py runs with:\n"
            "  python3 -m manim -pql --disable_caching scene.py AnimScene\n\n"
            "Rules:\n"
            "- AnimScene(Scene) in scene.py\n"
            "- MathTex(r'...') for all equations and math symbols\n"
            "- Text() only for plain prose labels\n"
            "- Arrow(start=..., end=...) — never left=/right= kwargs\n"
            "- Make it visually complete and polished"
        )

        aider_env = {
            **os.environ,
            'OPENAI_API_KEY': 'dummy',
            'GIT_AUTHOR_NAME': 'aider', 'GIT_AUTHOR_EMAIL': 'aider@manimflow',
            'GIT_COMMITTER_NAME': 'aider', 'GIT_COMMITTER_EMAIL': 'aider@manimflow',
        }

        log_queue.put(('log', 'Running aider (this takes 10-20 min)...'))
        proc = subprocess.Popen(
            ['aider',
             '--model', 'openai/manimator',
             '--openai-api-base', f'http://127.0.0.1:{LOCAL_PORT}/v1',
             '--openai-api-key', 'dummy',
             '--yes-always', '--no-auto-commits', '--no-pretty',
             '--no-show-model-warnings', '--no-check-update',
             '--test-cmd', 'python3 -m manim -pql --disable_caching scene.py AnimScene 2>&1',
             '--auto-test', '--message', task, 'plan.md'],
            cwd=str(MANIM_OUTPUT), text=True, env=aider_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for line in proc.stdout:
            log_queue.put(('log', line.rstrip()))
        proc.wait()
        log_queue.put(('log', f'aider exit: {proc.returncode}'))

        videos = [v for v in MANIM_OUTPUT.rglob('*.mp4') if v.stat().st_size > 50_000]
        if not videos:
            log_queue.put(('error', 'No valid video rendered.'))
            return

        dest = BASE / 'animation.mp4'
        shutil.copy(sorted(videos, key=lambda p: p.stat().st_mtime)[-1], dest)
        log_queue.put(('done', str(dest)))

    except Exception as e:
        log_queue.put(('error', str(e)))

# ── UI ───────────────────────────────────────────────────────────────────────

st.set_page_config(page_title='ManimFlow', layout='wide')
st.title('ManimFlow')
st.caption('Describe an animation topic → get a rendered MP4')

prompt = st.text_area('Animation prompt', placeholder='e.g. Explain how a Fourier series builds up a square wave', height=100)
generate = st.button('Generate', type='primary')

if generate and prompt.strip():
    log_box = st.empty()
    plan_box = st.empty()
    status = st.empty()

    logs = []
    q = queue.Queue()

    thread = threading.Thread(target=run_render, args=(prompt.strip(), q), daemon=True)
    thread.start()

    video_path = None
    while thread.is_alive() or not q.empty():
        try:
            kind, val = q.get(timeout=0.5)
        except queue.Empty:
            continue

        if kind == 'log':
            logs.append(val)
            log_box.code('\n'.join(logs[-60:]), language=None)
        elif kind == 'plan':
            plan_box.info(f'**Scene plan:**\n\n{val}')
        elif kind == 'done':
            video_path = val
            status.success('Render complete!')
        elif kind == 'error':
            status.error(val)

    if video_path and pathlib.Path(video_path).exists():
        video_bytes = pathlib.Path(video_path).read_bytes()
        st.video(video_bytes)
        st.download_button('Download animation.mp4', video_bytes, file_name='animation.mp4', mime='video/mp4')
