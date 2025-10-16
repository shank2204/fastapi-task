import os
import base64
import json
import tempfile
import subprocess
import time
import uuid
from http import HTTPStatus
from pathlib import Path
from fastapi import FastAPI, Request, Response, BackgroundTasks
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
API_SECRET = os.environ.get("API_SECRET")
GH_API = "https://api.github.com"

app = FastAPI()

def run(cmd, cwd=None):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout.strip()

def create_minimal_site(tmpdir, task, attachments):
    p = Path(tmpdir)
    p.mkdir(parents=True, exist_ok=True)
    sample = None
    for a in attachments:
        name = a.get("name")
        url = a.get("url")
        if not name or not url: continue
        if url.startswith("data:"):
            header, b64 = url.split(",",1)
            data = base64.b64decode(b64)
            (p / name).write_bytes(data)
            sample = name
    index = p / "index.html"
    content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{task}</title></head><body>
<h1 id="title">{task}</h1>
<div id="demo"></div>
<script>
const params = new URLSearchParams(location.search);
const url = params.get('url') || '{sample or ""}';
if (url) {{
  document.getElementById('demo').innerHTML = `<img id="img" src="${{url}}" alt="captcha image">`;
}}
setTimeout(() => {{
  const s = document.createElement('div');
  s.id = 'solved';
  s.textContent = 'SAMPLE_SOLUTION';
  document.body.appendChild(s);
}}, 1000);
</script>
</body></html>"""
    index.write_text(content)
    (p / "README.md").write_text(f"# {task}\n\nAuto-generated site.\n")
    (p / "LICENSE").write_text("MIT License\n")
    (p / ".nojekyll").write_text("")
    return tmpdir

def create_github_repo(repo_name, tmpdir):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    data = {"name": repo_name, "private": False, "auto_init": False}
    r = requests.post(f"{GH_API}/user/repos", json=data, headers=headers)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"create repo failed: {r.status_code} {r.text}")
    repo = r.json()
    clone_url = repo["clone_url"]
    run("git init", cwd=tmpdir)
    run("git add .", cwd=tmpdir)
    run('git -c user.name="auto" -c user.email="auto@example.com" commit -m "initial"', cwd=tmpdir)
    run(f"git remote add origin {clone_url}", cwd=tmpdir)
    run("git branch -M main", cwd=tmpdir)
    run("git push -u origin main", cwd=tmpdir)
    return repo["html_url"]

def enable_pages(owner, repo):
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    data = {"source": {"branch": "main", "path": "/"}}
    requests.post(f"{GH_API}/repos/{owner}/{repo}/pages", json=data, headers=headers)
    time.sleep(2)
    pages = requests.get(f"{GH_API}/repos/{owner}/{repo}", headers=headers).json().get("html_url")
    if pages:
        return f"https://{owner}.github.io/{repo}/"
    return None

def post_evaluation(evaluation_url, payload):
    delay = 1
    for _ in range(8):
        r = requests.post(evaluation_url, json=payload, headers={"Content-Type":"application/json"})
        if r.status_code == 200:
            return True
        time.sleep(delay)
        delay *= 2
    return False

def process_task(body):
    try:
        email = body.get("email")
        task = body.get("task") or f"task-{uuid.uuid4().hex[:6]}"
        nonce = body.get("nonce")
        round_index = body.get("round", 1)
        attachments = body.get("attachments", [])
        tmpdir = tempfile.mkdtemp(prefix="genrepo_")
        create_minimal_site(tmpdir, task, attachments)
        repo_name = f"{task}-{uuid.uuid4().hex[:5]}"
        repo_url = create_github_repo(repo_name, tmpdir)
        owner = requests.get(f"{GH_API}/user", headers={"Authorization": f"token {GITHUB_TOKEN}"}).json()["login"]
        pages_url = enable_pages(owner, repo_name)
        commit_sha = run("git rev-parse HEAD", cwd=tmpdir)
        payload = {
            "email": email, "task": task, "round": round_index, "nonce": nonce,
            "repo_url": repo_url, "commit_sha": commit_sha, "pages_url": pages_url
        }
        eval_url = body.get("evaluation_url")
        if eval_url:
            post_evaluation(eval_url, payload)
    except Exception as e:
        print("Error in process_task:", e)

@app.post("/api/task")
async def task_endpoint(req: Request, background_tasks: BackgroundTasks):
    body = await req.json()
    secret = body.get("secret")
    if secret != API_SECRET:
        return Response(status_code=HTTPStatus.UNAUTHORIZED.value, content=json.dumps({"error":"invalid secret"}))
    ack = {"status":"ok"}
    background_tasks.add_task(process_task, body)
    return ack
