"""Simple local web UI for running serviceability checks.

Upload a CSV, pick a provider, run, and download the results and fresh-leads
CSVs. This is an operator convenience that wraps the same pipeline the CLI uses,
not the client dashboard. Runs are done in a background thread so the page can
show live progress.

Start it with:  python webapp/app.py    then open http://localhost:5000
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, abort, jsonify, render_template_string, request, send_file, url_for

from run_poc import build_checkers, load_addresses
from serviceability.compare import find_fresh_leads
from serviceability.config import load_config
from serviceability.importers import load_dealmachine
from serviceability.output.csv_writer import write_fresh_leads, write_results

app = Flask(__name__)

UPLOAD_DIR = "data/uploads"
JOBS: dict[str, dict] = {}


def load_any(path: str) -> list:
    """Accept either a DealMachine export or the simple POC address format."""
    with open(path, "r", encoding="utf-8-sig") as handle:
        header = handle.readline().lower()
    if "associated_property_address_full" in header or "contact_id" in header:
        return load_dealmachine(path)
    return load_addresses(path)


def run_job(job_id: str, source_path: str, provider: str, mock: bool,
            show_browser: bool = False) -> None:
    job = JOBS[job_id]
    try:
        if mock:
            os.environ["MOCK_AVAILABLE"] = "half"
        config = load_config("config.yaml")
        if show_browser:
            config.headless = False  # headful scores better against reCAPTCHA
        addresses = load_any(source_path)
        job["total_addresses"] = len(addresses)

        from serviceability.storage.db import ResultStore
        store = ResultStore(config.db_path)
        checkers = build_checkers(provider, mock, config, addresses)
        job["total"] = sum(len([a for a in addresses if c.serves(a)]) for c in checkers)

        results = []
        for checker in checkers:
            served = [a for a in addresses if checker.serves(a)]
            job["log"].append(f"{checker.name}: {len(served)} in footprint, "
                              f"{len(addresses) - len(served)} skipped")
            try:
                for address in served:
                    result = checker._safe_check(address)
                    results.append(result)
                    job["done"] += 1
                    key = result.category.value
                    job["counts"][key] = job["counts"].get(key, 0) + 1
                    job["rows"].append({
                        "address": address.single_line(),
                        "provider": checker.name,
                        "category": key,
                        "reason": (result.notes or result.raw_status or "")[:160],
                    })
            finally:
                checker.close()

        store.save(results)
        out_dir = os.path.join("output", job_id)
        os.makedirs(out_dir, exist_ok=True)
        results_path = os.path.join(out_dir, "results.csv")
        fresh_path = os.path.join(out_dir, "fresh_leads.csv")
        write_results(results_path, results)
        write_fresh_leads(fresh_path, find_fresh_leads(store, results))
        job["results_path"] = results_path
        job["fresh_path"] = fresh_path
        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[:400]


@app.route("/")
def index():
    return render_template_string(INDEX_HTML, jobs=JOBS)


@app.route("/run", methods=["POST"])
def run():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        abort(400, "no file uploaded")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    job_id = uuid.uuid4().hex[:8]
    source_path = os.path.join(UPLOAD_DIR, f"{job_id}_{upload.filename}")
    upload.save(source_path)

    provider = request.form.get("provider", "kinetic")
    mock = request.form.get("mock") == "on"
    show_browser = request.form.get("show_browser") == "on"
    JOBS[job_id] = {
        "status": "running", "provider": provider, "mock": mock,
        "filename": upload.filename, "done": 0, "total": 0, "total_addresses": 0,
        "counts": {}, "rows": [], "log": [], "results_path": "", "fresh_path": "", "error": "",
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    threading.Thread(target=run_job,
                     args=(job_id, source_path, provider, mock, show_browser),
                     daemon=True).start()
    return app.redirect(url_for("status_page", job_id=job_id))


@app.route("/status/<job_id>")
def status_page(job_id: str):
    if job_id not in JOBS:
        abort(404)
    return render_template_string(STATUS_HTML, job_id=job_id, job=JOBS[job_id])


@app.route("/status/<job_id>.json")
def status_json(job_id: str):
    if job_id not in JOBS:
        abort(404)
    return jsonify(JOBS[job_id])


@app.route("/download/<job_id>/<kind>")
def download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    path = job.get("results_path") if kind == "results" else job.get("fresh_path")
    if not path or not os.path.exists(path):
        abort(404)
    name = f"{kind}_{job_id}.csv"
    return send_file(os.path.abspath(path), as_attachment=True, download_name=name)


INDEX_HTML = """
<!doctype html><html><head><title>Serviceability Checker</title>
<style>
body{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#1a1a1a}
h1{font-size:22px} label{display:block;margin:14px 0 4px;font-weight:600}
select,input[type=file]{padding:8px;width:100%;box-sizing:border-box}
.row{margin:10px 0} .btn{background:#0b6;color:#fff;border:0;padding:11px 18px;border-radius:6px;font-size:15px;cursor:pointer;margin-top:18px}
.note{color:#666;font-size:13px} table{border-collapse:collapse;width:100%;margin-top:24px;font-size:14px}
td,th{border-bottom:1px solid #eee;padding:7px 6px;text-align:left} a{color:#0a6}
.check{margin-top:10px}
</style></head><body>
<h1>ISP fiber serviceability checker</h1>
<p class="note">Upload an address CSV (a DealMachine export or the simple address format both work),
pick a provider, and run. Results and fresh leads download as CSV.</p>
<form action="/run" method="post" enctype="multipart/form-data">
  <label>Address CSV</label>
  <input type="file" name="file" accept=".csv" required>
  <label>Provider</label>
  <select name="provider">
    <option value="kinetic">Kinetic (Windstream)</option>
    <option value="frontier">Frontier</option>
    <option value="att">AT&amp;T (needs unblocker)</option>
    <option value="all">All in footprint</option>
  </select>
  <div class="check"><label style="display:inline;font-weight:400">
    <input type="checkbox" name="show_browser" checked> Show browser (recommended for Kinetic, beats reCAPTCHA)
  </label></div>
  <div class="check"><label style="display:inline;font-weight:400">
    <input type="checkbox" name="mock"> Test mode (no live checks, simulated output)
  </label></div>
  <button class="btn" type="submit">Run checks</button>
</form>
{% if jobs %}
<table><tr><th>Job</th><th>File</th><th>Provider</th><th>Status</th><th></th></tr>
{% for jid, j in jobs.items() %}
<tr><td>{{jid}}</td><td>{{j.filename}}</td><td>{{j.provider}}{% if j.mock %} (test){% endif %}</td>
<td>{{j.status}}</td><td><a href="/status/{{jid}}">view</a></td></tr>
{% endfor %}</table>
{% endif %}
</body></html>
"""

STATUS_HTML = """
<!doctype html><html><head><title>Job {{job_id}}</title>
<style>
body{font-family:system-ui,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#1a1a1a}
h1{font-size:20px} .bar{background:#eee;border-radius:6px;height:22px;overflow:hidden;margin:14px 0}
.fill{background:#0b6;height:100%;width:0;transition:width .3s} .muted{color:#666}
.dl{display:inline-block;background:#0b6;color:#fff;padding:9px 16px;border-radius:6px;text-decoration:none;margin:8px 8px 0 0}
table{border-collapse:collapse;margin-top:14px} td,th{border-bottom:1px solid #eee;padding:5px 14px 5px 0;text-align:left}
.err{color:#c00} a.back{color:#0a6}
</style></head><body>
<p><a class="back" href="/">&larr; back</a></p>
<h1>Job {{job_id}} <span class="muted">({{job.provider}}{% if job.mock %}, test mode{% endif %})</span></h1>
<p class="muted">{{job.filename}} &middot; started {{job.started}}</p>
<div id="body"></div>
<script>
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function catColor(c){ return c=='Fiber Available'?'#0a7':(c=='Not Available'?'#555':(c=='Existing Customer'?'#06c':'#c60')); }
async function tick(){
  const r = await fetch("/status/{{job_id}}.json"); const j = await r.json();
  let html = "";
  const pct = j.total ? Math.round(100*j.done/j.total) : (j.status=='done'?100:0);
  html += '<div class="bar"><div class="fill" style="width:'+pct+'%"></div></div>';
  html += '<p>'+j.done+' / '+(j.total||'?')+' checks done'+(j.total_addresses?(' &middot; '+j.total_addresses+' addresses uploaded'):'')+'</p>';
  if(j.log && j.log.length){ html += '<p class="muted">'+j.log.join('<br>')+'</p>'; }
  if(Object.keys(j.counts||{}).length){
    html += '<table><tr><th>Category</th><th>Count</th></tr>';
    for(const k in j.counts){ html += '<tr><td>'+k+'</td><td>'+j.counts[k]+'</td></tr>'; }
    html += '</table>';
  }
  if(j.rows && j.rows.length){
    html += '<h3 style="font-size:15px;margin:22px 0 6px">Per-address log ('+j.rows.length+')</h3>';
    html += '<div style="max-height:360px;overflow:auto;border:1px solid #eee;border-radius:6px">';
    html += '<table style="margin:0;font-size:13px"><tr>'+
            '<th>#</th><th>Address</th><th>Category</th><th>Reason</th></tr>';
    j.rows.forEach((r,i)=>{
      html += '<tr><td class="muted">'+(i+1)+'</td><td>'+esc(r.address)+'</td>'+
              '<td style="color:'+catColor(r.category)+'">'+esc(r.category)+'</td>'+
              '<td class="muted">'+esc(r.reason)+'</td></tr>';
    });
    html += '</table></div>';
  }
  if(j.status=='done'){
    html += '<p><a class="dl" href="/download/{{job_id}}/results">Download results.csv</a>'+
            '<a class="dl" href="/download/{{job_id}}/fresh">Download fresh_leads.csv</a></p>';
  } else if(j.status=='error'){
    html += '<p class="err">Error: '+j.error+'</p>';
  }
  document.getElementById("body").innerHTML = html;
  if(j.status=='running'){ setTimeout(tick, 2000); }
}
tick();
</script>
</body></html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
