#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


PI_VERSION = "0.80.6"
COPILOT_BASE = "https://api.individual.githubcopilot.com"
COPILOT_PROVIDER = "manimflow-copilot"
COPILOT_MODEL = "gpt-4o"
COPILOT_TOKEN_ENV = "MANIMFLOW_COPILOT_TOKEN"
EXA_ENDPOINT = "https://demos.exa.ai/chatbot-demo/api/chat/stream"
EXA_MODEL = "google/gemini-2.5-flash"
MANIM_DOCS_URL = "https://github.com/ManimCommunity/manim.git"


def request_json(url, payload, headers, timeout=180):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def copilot_chat(token, messages):
    payload = {"model": COPILOT_MODEL, "messages": messages}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Editor-Version": "vscode/1.85.0",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Intent": "conversation-completions",
    }
    for attempt in range(5):
        try:
            body = request_json(f"{COPILOT_BASE}/chat/completions", payload, headers)
            return body["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"Copilot HTTP {exc.code}: {detail}") from exc
            delay = min(5 * (2**attempt), 120)
            print(f"Copilot rate limited the planner; retrying in {delay}s...", flush=True)
            time.sleep(delay)
    raise RuntimeError("Copilot planner exhausted its retries.")


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
            if isinstance(event.get("content"), str):
                text += event["content"]
        except json.JSONDecodeError:
            pass
    return re.sub(r"```followups[\s\S]*$", "", text, flags=re.IGNORECASE).strip()


def exa_chat(messages):
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    conversation = [m for m in messages if m["role"] != "system"]
    latest = conversation[-1]["content"]
    payload = {
        "message": f"{system}\n\n{latest}",
        "history": conversation[:-1],
        "exaEnabled": False,
        "model": EXA_MODEL,
        "searchType": "instant",
    }
    request = urllib.request.Request(
        EXA_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = parse_exa_stream(response.read().decode("utf-8", errors="replace"))
    if not result:
        raise RuntimeError("Exa returned no planner text.")
    return result


def clean_plan(plan):
    plan = re.sub(r"```[\s\S]*?```", "", plan).strip()
    markers = (
        "Would you like", "Do you want", "Should I", "Could you clarify",
        "Can you provide", "What specific", "How long", "Any specific",
        "Please let me know", "Let me know",
    )
    for marker in markers:
        for separator in ("\n\n", "\n"):
            index = plan.find(separator + marker)
            if index >= 0:
                plan = plan[:index]
    return plan.strip()


def plan_animation(provider, token, prompt):
    messages = [
        {
            "role": "system",
            "content": (
                "You are a Manim animation planner. Output a plain numbered list of visual "
                "scenes with no markdown or commentary. Use 3-4 scenes for a short request, "
                "5-6 by default, and 8-12 when detailed. Describe shapes, colors, motion, "
                "labels, and properly typeset LaTeX equations where useful. Never ask questions."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    plan = exa_chat(messages) if provider == "exa" else copilot_chat(token, messages)
    plan = clean_plan(plan)
    if not plan:
        raise RuntimeError("The planner returned an empty scene plan.")
    return plan


def clone_docs(workspace):
    repository = workspace / ".manim-docs-repo"
    print("Cloning Manim documentation (shallow, docs only)...", flush=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", MANIM_DOCS_URL, str(repository)],
        check=True,
        timeout=180,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "docs"], cwd=repository, check=True, timeout=120
    )
    docs = repository / "docs"
    shutil.rmtree(docs / "source" / "changelog", ignore_errors=True)
    try:
        (docs / "source" / "changelog.rst").unlink()
    except FileNotFoundError:
        pass
    shutil.move(str(docs), str(workspace / "manim-docs"))
    shutil.rmtree(repository, ignore_errors=True)


def write_project_files(workspace, prompt, plan, require_docs):
    (workspace / "plan.md").write_text(
        f"# Animation Plan\n\n## Original request\n{prompt}\n\n## Scenes\n{plan}\n"
    )
    docs = (
        "MANDATORY DOCUMENTATION GATE, complete before writing Python:\n"
        "1. Identify the Manim APIs planned for use.\n"
        "2. Search only manim-docs/source/ with focused commands. Never search i18n or the web.\n"
        "3. Use Pi's read tool to open at least three distinct relevant documentation files.\n"
        "4. Create docs_consulted.md before any .py file. Record each exact file path read and what was learned.\n"
        if require_docs
        else "Documentation reading is optional. Start implementation immediately.\n"
    )
    (workspace / "AGENTS.md").write_text(
        "You are an autonomous coding agent building a Manim animation.\n"
        "Read plan.md fully before editing and work without asking questions.\n"
        "The current working directory is already the project root. Write docs_consulted.md, helper modules, scene.py, and media directly here. Never create or write into a manim_output/ or project/ subdirectory.\n"
        + docs
        + "Create helper modules first and scene.py last.\n"
        "Every Python file must start with: from manim import *\n"
        "scene.py must define exactly one direct Scene subclass: class AnimScene(Scene).\n"
        "Implement every planned scene sequentially inside AnimScene.construct(). Helper modules may define Mobject/VGroup classes and functions, but must not define Scene subclasses. Never alias AnimScene, never use multiple inheritance between Scene classes, and never render separate scene classes.\n"
        "A full LaTeX toolchain is installed. Use MathTex for mathematics, Tex for mixed LaTeX prose and math, and Text only for ordinary labels. Never avoid LaTeX to bypass an error.\n"
        "Use Arrow(start=..., end=...) for arrows. Keep objects inside the frame and avoid overlaps.\n"
        f'Repeatedly run and repair: "{sys.executable}" -m manim -pql --disable_caching scene.py AnimScene\n'
        "Stop only after that command succeeds and produces a valid MP4.\n"
    )


def write_pi_config(agent_dir, provider, extension):
    agent_dir.mkdir(parents=True, exist_ok=True)
    providers = {}
    if provider == "copilot":
        providers[COPILOT_PROVIDER] = {
            "baseUrl": COPILOT_BASE,
            "api": "openai-completions",
            "apiKey": f"${COPILOT_TOKEN_ENV}",
            "authHeader": True,
            "headers": {
                "Editor-Version": "vscode/1.85.0",
                "Copilot-Integration-Id": "vscode-chat",
                "Openai-Intent": "conversation-completions",
            },
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [{
                "id": COPILOT_MODEL,
                "name": f"GitHub Copilot {COPILOT_MODEL}",
                "reasoning": False,
                "input": ["text"],
                "contextWindow": 128000,
                "maxTokens": 16384,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            }],
        }
    (agent_dir / "models.json").write_text(json.dumps({"providers": providers}, indent=2))
    (agent_dir / "settings.json").write_text(json.dumps({"telemetry": False, "quietStartup": True}, indent=2))
    if provider == "exa":
        if not extension or not extension.is_file():
            raise RuntimeError("The Exa Pi extension is required for Exa runs.")
        destination = agent_dir / "extensions"
        destination.mkdir()
        shutil.copy2(extension, destination / "exa_direct.ts")


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
        p
        for p in workspace.rglob("AnimScene.mp4")
        if p.is_file()
        and "partial_movie_files" not in p.parts
        and p.stat().st_size > 50_000
        and video_duration(p) >= minimum_duration
    ]
    return max(videos, key=lambda p: p.stat().st_mtime) if videos else None


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
    print("Normalized accidental nested manim_output/ workspace.", flush=True)
    return True


def canonical_doc_path(value, workspace):
    normalized = str(value).replace("\\", "/")
    if "manim-docs/" not in normalized:
        return None
    relative = "manim-docs/" + normalized.split("manim-docs/", 1)[1]
    relative = relative.rstrip(".,;:)]}`'").replace("//", "/")
    candidate = workspace / relative
    docs = (workspace / "manim-docs").resolve()
    try:
        return relative if candidate.is_file() and candidate.resolve().is_relative_to(docs) else None
    except (OSError, ValueError):
        return None


def observe_doc_read(line, workspace, reads):
    if '"type":"tool_execution_start"' not in line or '"toolName":"read"' not in line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    path = (event.get("args") or {}).get("path", "")
    canonical = canonical_doc_path(path, workspace)
    if canonical:
        reads.add(canonical)


def validate_docs(workspace, reads):
    evidence = workspace / "docs_consulted.md"
    if not evidence.is_file() or evidence.stat().st_size < 200:
        raise RuntimeError("Pi skipped or incompletely recorded the documentation gate.")
    citations = re.findall(
        r"manim-docs/[A-Za-z0-9_./-]+\.(?:rst|md)\b",
        evidence.read_text(errors="replace"),
        flags=re.IGNORECASE,
    )
    canonical = {canonical_doc_path(path, workspace) for path in citations}
    missing = [path for path in citations if not canonical_doc_path(path, workspace)]
    canonical.discard(None)
    if missing:
        raise RuntimeError("Nonexistent documentation cited: " + ", ".join(sorted(set(missing))))
    if len(canonical) < 3:
        raise RuntimeError(f"Only {len(canonical)} distinct documentation files were cited.")
    unread = canonical - reads
    if unread:
        raise RuntimeError("Documentation cited but not read: " + ", ".join(sorted(unread)))
    python_files = list(workspace.glob("*.py"))
    if python_files and evidence.stat().st_mtime > min(path.stat().st_mtime for path in python_files):
        raise RuntimeError("Python was written before docs_consulted.md.")


def concise_event(line, details):
    stripped = line.strip()
    if not stripped or stripped.startswith('{"type":"message_update"'):
        return None
    if stripped.startswith('{"type":"agent_start"'):
        return "Pi is analyzing the plan."
    if stripped.startswith('{"type":"agent_end"'):
        return "Pi finished its work."
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped[:1000]
    kind = event.get("type")
    if kind == "tool_execution_start":
        name = event.get("toolName", "tool")
        args = event.get("args") or {}
        detail = args.get("path", "") if name in {"read", "write", "edit"} else args.get("command", "") if name == "bash" else json.dumps(args)
        if event.get("toolCallId"):
            details[event["toolCallId"]] = (name, str(detail))
        return f"{ {'read': 'Reading file', 'write': 'Writing file', 'edit': 'Editing file', 'bash': 'Running command'}.get(name, 'Running ' + name) } -> {str(detail).replace(chr(10), ' ')[:500]}"
    if kind == "tool_execution_end":
        name = event.get("toolName", "tool")
        details.pop(event.get("toolCallId"), None)
        result = event.get("result")
        text = ""
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            text = "\n".join(str(p.get("text", "")) for p in result["content"] if isinstance(p, dict))
        return f"{name} {'failed' if event.get('isError') else 'completed'}" + (f":\n{text.strip()[:1500]}" if text.strip() else "")
    if kind == "message_end":
        message = event.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, str):
            return f"Pi: {content[:1500]}"
        texts = [str(part.get("text", "")).strip() for part in content if isinstance(part, dict) and part.get("type") == "text"]
        return "\n".join(f"Pi: {text[:1500]}" for text in texts if text) or None
    if kind == "auto_retry_start":
        return f"Provider retry in {event.get('delayMs', 0) / 1000:g}s: {event.get('errorMessage', '')}"
    return None


def run_pi(args):
    workspace = args.workspace.resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    agent_dir = workspace.parent / "pi-agent"
    token = os.environ.get(args.token_env, "") if args.provider == "copilot" else ""
    if args.provider == "copilot" and not token:
        raise RuntimeError(f"{args.token_env} is required.")

    if args.provider == "copilot":
        clone_docs(workspace)
    print("Planning scenes...", flush=True)
    plan = plan_animation(args.provider, token, args.prompt)
    print(f"\nScene plan:\n{plan}\n", flush=True)
    write_project_files(workspace, args.prompt, plan, args.provider == "copilot")
    write_pi_config(agent_dir, args.provider, args.exa_extension)

    provider = "exa-direct" if args.provider == "exa" else COPILOT_PROVIDER
    model = EXA_MODEL if args.provider == "exa" else COPILOT_MODEL
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_TELEMETRY"] = "0"
    if args.provider == "copilot":
        env[COPILOT_TOKEN_ENV] = token

    subprocess.run([args.pi, "--version"], cwd=workspace, env=env, check=True, timeout=20)
    check = subprocess.run(
        [args.pi, "--list-models", provider], cwd=workspace, env=env,
        check=True, capture_output=True, text=True, timeout=30,
    )
    if model not in check.stdout:
        raise RuntimeError(f"Pi cannot see {provider}/{model}: {check.stdout.strip()}")
    print(f"Pi model ready -> {provider}/{model}", flush=True)

    command = [
        args.pi, "--mode", "json", "--verbose", "--approve", "--no-session",
        "--no-skills", "--provider", provider, "--model", model,
        "--thinking", "off", "@plan.md",
    ]
    if args.provider == "copilot":
        command.insert(7, "--no-extensions")
    task = (
        "Implement the complete animation described in plan.md immediately. Use file and bash "
        "tools autonomously, repeatedly render, diagnose failures, and repair until a valid MP4 exists. "
        "Do not ask questions."
        if args.provider == "exa"
        else "First satisfy the documentation gate in AGENTS.md using local manim-docs/. Then implement "
        "all of plan.md autonomously, repeatedly render, diagnose failures, and repair until a valid MP4 exists."
    )
    reads = set()
    video = None
    required_duration = minimum_video_duration(plan)
    details = {}
    for attempt in range(1, args.attempts + 1):
        print(f"Starting Pi agent attempt {attempt}/{args.attempts}...", flush=True)
        process = subprocess.Popen(
            [*command, task], cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in process.stdout or []:
            if args.provider == "copilot":
                observe_doc_read(line, workspace, reads)
            update = concise_event(line, details)
            if update:
                print(update, flush=True)
        code = process.wait()
        print(f"Pi attempt {attempt} exit code: {code}", flush=True)
        normalize_nested_workspace(workspace)
        video = newest_video(workspace, required_duration)
        if video:
            break
        state = (
            "scene.py is missing"
            if not (workspace / "scene.py").is_file()
            else f"no assembled AnimScene.mp4 of at least {required_duration:g} seconds was rendered"
        )
        task = (
            f"Continue from the existing workspace. The previous attempt ended with {state}. Inspect all "
            "files, finish or repair the implementation, run the exact render command in AGENTS.md, and "
            "do not stop until a valid MP4 exists."
        )
    if not video:
        raise RuntimeError(f"Pi did not produce a valid MP4 after {args.attempts} attempts.")
    if args.provider == "copilot":
        validate_docs(workspace, reads)
        print(f"Documentation gate passed -> {len(reads)} distinct files read", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, args.output)
    print(f"SUCCESS -> {args.output} ({args.output.stat().st_size:,} bytes)", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Autonomous ManimFlow Pi runner")
    parser.add_argument("--provider", choices=("exa", "copilot"), required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workspace", type=pathlib.Path, default=pathlib.Path("manim_output"))
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("animation.mp4"))
    parser.add_argument("--pi", default=shutil.which("pi") or "pi")
    parser.add_argument("--exa-extension", type=pathlib.Path)
    parser.add_argument("--token-env", default="COPILOT_TOKEN")
    parser.add_argument("--attempts", type=int, default=6)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        run_pi(parse_args())
    except Exception as error:
        print(f"FAILED: {error}", file=sys.stderr, flush=True)
        raise
