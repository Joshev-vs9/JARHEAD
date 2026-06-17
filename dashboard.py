#!/usr/bin/env python3
"""
dashboard.py
============
Local control panel for the exploded-view pipeline. Standard-library only.

Lives next to:
    onshape_build_exploded.py      (fetch + bake the exploded model locally)
    exploded_view_annotator.html   (the viewer / annotator)

Run it:
    python dashboard.py

…then in the browser:
    1. Build exploded model   (from a source assembly URL — a few read calls,
                               constant regardless of part count)
    2. Open in annotator      (model + parts list pre-loaded)

Credentials are read by the build script from your .onshape file; the server
binds to 127.0.0.1 only and never sees your keys.
"""

import json
import os
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from subprocess import Popen, PIPE, STDOUT
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "ev_work")
os.makedirs(WORK, exist_ok=True)

SCRIPT_BUILD = os.path.join(BASE, "onshape_build_exploded.py")
ANNOTATOR    = os.path.join(BASE, "exploded_view_annotator.html")

GLB_NAME   = "exploded_view.glb"
PARTS_NAME = "parts.json"

PORT = int(os.environ.get("EV_DASHBOARD_PORT", "8765"))

JOBS = {}
JOBS_LOCK = threading.Lock()


def _new_job():
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"state": "running", "log": [], "result": {}}
    return jid

def _append(jid, line):
    with JOBS_LOCK:
        JOBS[jid]["log"].append(line.rstrip("\n"))

def _run(cmd):
    proc = Popen(cmd, cwd=BASE, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        yield line
    proc.wait()
    yield f"\x00EXIT {proc.returncode}"

def _py():
    return [sys.executable, "-u"]


def job_build(jid, source_url, indented):
    cmd = _py() + [SCRIPT_BUILD, source_url, "--outdir", WORK, "--emit"]
    if indented:
        cmd.append("--bom-indented")

    result = {}
    code = None
    for line in _run(cmd):
        if line.startswith("\x00EXIT "):
            code = int(line.split()[1]); continue
        if line.startswith("EVRESULT "):
            try: result.update(json.loads(line[len("EVRESULT "):]))
            except Exception: pass
            continue
        _append(jid, line)

    out = {}
    glb_path = result.get("glb")
    parts_path = result.get("parts")
    if glb_path and os.path.exists(glb_path):
        out["model"] = "/files/" + os.path.basename(glb_path)
    if parts_path and os.path.exists(parts_path):
        out["parts"] = "/files/" + os.path.basename(parts_path)
    out["assembly"] = result.get("assembly") or ""
    out["matched"] = result.get("matched")
    out["leaves"] = result.get("leaves")
    out["gaps"] = (result.get("unmatched_occ", 0) or 0) + (result.get("leftover_nodes", 0) or 0)

    with JOBS_LOCK:
        JOBS[jid]["result"] = out
        JOBS[jid]["state"] = "done" if out.get("model") else "error"
        if not out.get("model"):
            _append(jid, "No model was produced — see log above.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        if path == "/annotator":
            try:
                with open(ANNOTATOR, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, "annotator file not found", "text/plain")
        if path.startswith("/files/"):
            name = os.path.basename(path[len("/files/"):])
            fp = os.path.join(WORK, name)
            if not os.path.isfile(fp):
                return self._send(404, "not found", "text/plain")
            ctype = ("model/gltf-binary" if name.endswith(".glb")
                     else "model/gltf+json" if name.endswith(".gltf")
                     else "application/json" if name.endswith(".json")
                     else "application/octet-stream")
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/status":
            jid = (parse_qs(parsed.query).get("id") or [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if not job:
                    return self._send(404, {"error": "no such job"})
                return self._send(200, {"state": job["state"], "log": job["log"], "result": job["result"]})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/api/build":
            url = (body.get("sourceUrl") or "").strip()
            if not url:
                return self._send(400, {"error": "sourceUrl required"})
            jid = _new_job()
            threading.Thread(target=job_build, args=(jid, url, bool(body.get("indented"))),
                             daemon=True).start()
            return self._send(200, {"jobId": jid})
        return self._send(404, {"error": "not found"})


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exploded view · control panel</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
  :root{--ink:#0E1116;--ink-soft:#586070;--paper:#ECEEEA;--panel:#FFFFFF;--hairline:#D7DAD2;
    --blue:#1E3AE6;--blue-deep:#1227B4;--blue-tint:#E9ECFE;--ok:#1B7A3D;--warn:#9A6A00;--danger:#C8351B;
    --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;--sans:"IBM Plex Sans",system-ui,sans-serif;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:var(--sans);color:var(--ink);background:var(--paper);min-height:100vh;display:flex;justify-content:center;padding:32px 20px}
  .wrap{width:100%;max-width:720px}
  header{border-bottom:1.5px solid var(--ink);padding-bottom:14px;margin-bottom:22px}
  h1{font-size:19px;font-weight:600}
  .brand{font-family:var(--mono);font-size:11px;letter-spacing:.12em;font-weight:600;color:var(--blue)}
  .sub{font-size:13px;color:var(--ink-soft);margin-top:3px}
  label{display:block;font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:var(--ink-soft);margin:0 0 5px 2px}
  input[type=text]{width:100%;padding:9px 11px;border:1.5px solid var(--ink);border-radius:2px;font-size:13px;font-family:var(--mono);background:var(--panel)}
  input[type=text]:focus{outline:2px solid var(--blue);outline-offset:1px}
  .row{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink-soft);margin-top:10px}
  .step{background:var(--panel);border:1.5px solid var(--ink);border-radius:3px;padding:16px 18px;margin-bottom:14px}
  .step-head{display:flex;align-items:center;gap:11px;margin-bottom:13px}
  .step-n{flex:0 0 26px;height:26px;width:26px;border:2px solid var(--ink);border-radius:50%;font-family:var(--mono);font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:center}
  .step.done .step-n{background:var(--ok);border-color:var(--ok);color:#fff}
  .step.busy .step-n{background:var(--blue);border-color:var(--blue);color:#fff}
  .step-title{font-size:15px;font-weight:600}
  .step-desc{font-size:12.5px;color:var(--ink-soft);margin-top:1px}
  .field{margin-bottom:12px}
  .btn{padding:9px 16px;border:1.5px solid var(--ink);border-radius:2px;font:inherit;font-size:13px;font-weight:600;background:var(--panel);cursor:pointer}
  .btn:hover:not(:disabled){background:var(--paper)}
  .btn:disabled{opacity:.35;cursor:default}
  .btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}
  .btn.primary:hover:not(:disabled){background:var(--blue-deep);border-color:var(--blue-deep)}
  .status{font-family:var(--mono);font-size:11.5px;margin-top:9px;min-height:14px}
  .status.ok{color:var(--ok)}.status.warn{color:var(--warn)}.status.err{color:var(--danger)}.status.run{color:var(--blue)}
  .log{margin-top:22px;background:#0E1116;color:#D7DAD2;border-radius:3px;padding:14px 16px;font-family:var(--mono);font-size:11.5px;line-height:1.5;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow-y:auto;display:none}
  .log.show{display:block}
  .log .err{color:#FF8A6E}.log .warn{color:#FFCC66}
  .hint{font-size:12px;color:var(--ink-soft);margin-top:6px}
  .checkbox{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink-soft);margin-top:10px;cursor:pointer}
</style></head>
<body><div class="wrap">
  <header>
    <div class="brand">EXPLODED VIEW</div>
    <h1>Pipeline control panel</h1>
    <div class="sub">Source assembly &rarr; annotated, shareable 3D — built from a handful of read calls.</div>
  </header>

  <div class="field">
    <label for="src">SOURCE ASSEMBLY URL</label>
    <input type="text" id="src" placeholder="https://cad.onshape.com/documents/…/w/…/e/…" autocomplete="off" spellcheck="false">
    <div class="hint">The assembly that has the exploded view. Copy it from the browser while that tab is open.</div>
    <label class="checkbox"><input type="checkbox" id="indented"> Include parts nested in sub-assemblies in the BOM</label>
  </div>

  <div class="step" id="step1">
    <div class="step-head"><div class="step-n">1</div>
      <div><div class="step-title">Build exploded model</div>
        <div class="step-desc">Fetches geometry, transforms and BOM, then bakes the explode locally. No assembly is created in Onshape.</div></div></div>
    <button class="btn primary" id="run1">Build exploded model</button>
    <div class="status" id="st1"></div>
  </div>

  <div class="step" id="step2">
    <div class="step-head"><div class="step-n">2</div>
      <div><div class="step-title">Open in annotator</div>
        <div class="step-desc">Loads the exploded model and parts list, ready to balloon.</div></div></div>
    <button class="btn" id="run2" disabled>Open in annotator</button>
    <div class="status" id="st2"></div>
  </div>

  <div class="log" id="log"></div>
</div>
<script>
const $=s=>document.querySelector(s);
const out={model:null,parts:null};
function setStatus(el,msg,kind){el.className='status'+(kind?(' '+kind):'');el.textContent=msg;}
function setStep(el,s){el.classList.remove('busy','done');if(s)el.classList.add(s);}
const logEl=$('#log');
function renderLog(lines){
  logEl.classList.add('show');
  logEl.innerHTML=lines.map(l=>{
    const e=l.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    if(/error|fail/i.test(l))return '<span class="err">'+e+'</span>';
    if(/warn|gap|unmatched|not assigned/i.test(l))return '<span class="warn">'+e+'</span>';
    return e;
  }).join('\n');
  logEl.scrollTop=logEl.scrollHeight;
}
async function poll(jobId){
  while(true){
    const r=await fetch('/api/status?id='+jobId);const j=await r.json();
    renderLog(j.log);
    if(j.state!=='running')return j;
    await new Promise(res=>setTimeout(res,650));
  }
}
$('#run1').addEventListener('click',async()=>{
  const sourceUrl=$('#src').value.trim();
  if(!sourceUrl){setStatus($('#st1'),'Enter a source assembly URL first.','err');return;}
  $('#run1').disabled=true;setStep($('#step1'),'busy');setStatus($('#st1'),'Building…','run');
  try{
    const r=await fetch('/api/build',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sourceUrl,indented:$('#indented').checked})});
    const {jobId,error}=await r.json();if(error)throw new Error(error);
    const res=await poll(jobId);
    if(res.state==='done'&&res.result.model){
      out.model=res.result.model;out.parts=res.result.parts||null;
      const m=res.result.matched,l=res.result.leaves,gaps=res.result.gaps||0;
      if(gaps>0){
        setStep($('#step1'),'done');
        setStatus($('#st1'),`Built with gaps: ${m}/${l} parts placed. See log — some parts may not be exploded.`,'warn');
      }else{
        setStep($('#step1'),'done');
        setStatus($('#st1'),`Built — ${m}/${l} parts placed.`,'ok');
      }
      $('#run2').disabled=false;
    }else{setStep($('#step1'),'');setStatus($('#st1'),'Failed — see log below.','err');}
  }catch(e){setStep($('#step1'),'');setStatus($('#st1'),'Error: '+e.message,'err');}
  $('#run1').disabled=false;
});
$('#run2').addEventListener('click',()=>{
  if(!out.model){setStatus($('#st2'),'Build something first.','err');return;}
  const q=new URLSearchParams({model:out.model});
  if(out.parts)q.set('parts',out.parts);
  if(out.assembly)q.set('title',out.assembly);
  window.open('/annotator?'+q.toString(),'_blank');
  setStep($('#step2'),'done');setStatus($('#st2'),'Opened in a new tab.','ok');
});
</script>
</body></html>
"""


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 56)
    print("  Exploded view dashboard")
    print("  Open:  " + url)
    print("  (Ctrl+C to stop)")
    print("=" * 56)
    for f, label in [(SCRIPT_BUILD, "build script"), (ANNOTATOR, "annotator")]:
        if not os.path.exists(f):
            print(f"  WARNING: missing {label}: {f}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
