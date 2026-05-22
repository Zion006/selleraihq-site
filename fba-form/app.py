#!/usr/bin/env python3
"""
FBA Audit — Intake Form
Env vars (Railway):
    AGENTMAIL_API_KEY   — AgentMail API key
    AGENTMAIL_INBOX_ID  — inbox address (default: zionbot@agentmail.to)
"""

import os
import json
import shutil
from datetime import datetime
from flask import Flask, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".txt", ".tsv", ".pdf"}


def load_config():
    """Read config from env vars (Railway) or fallback to local config.json."""
    api_key  = os.environ.get("AGENTMAIL_API_KEY")
    inbox_id = os.environ.get("AGENTMAIL_INBOX_ID", "zionbot@agentmail.to")
    if api_key:
        return {"api_key": api_key, "inbox_id": inbox_id}
    config_path = os.path.join(os.path.dirname(__file__), "../parser/config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    raise RuntimeError("No AGENTMAIL_API_KEY in env and no config.json found")


def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def send_to_agentmail(config, client_name, store_name, sender_email, form_data, files):
    try:
        from agentmail import AgentMail
        client = AgentMail(api_key=config["api_key"])

        subject = f"Audit: {store_name}"
        body = f"""New FBA Audit Intake Form Submission

Name:          {form_data.get('name')}
Email:         {sender_email}
Store:         {store_name}
Marketplace:   {form_data.get('marketplace')}
Monthly GMV:   {form_data.get('gmv')}
SKU Count:     {form_data.get('sku_count')}
Selling Model: {form_data.get('model')}

Has reimbursements since March 2025: {form_data.get('has_reimbursements')}
Sourcing cost uploaded:              {form_data.get('sourcing_uploaded')}
Reimbursements looked low:           {form_data.get('looks_low')}
Has supplier invoices:               {form_data.get('has_invoices')}

Notes:
{form_data.get('notes', 'None')}

--- Files attached: {len(files)} ---
"""
        msg_params = {
            "inbox_id": config["inbox_id"],
            "to":       [config["inbox_id"]],
            "subject":  subject,
            "text":     body,
        }

        if files:
            attachments = []
            for filepath, filename in files:
                with open(filepath, "rb") as f:
                    content = f.read()
                attachments.append({
                    "filename":     filename,
                    "content":      content,
                    "content_type": "application/octet-stream",
                })
            msg_params["attachments"] = attachments

        try:
            client.inboxes.messages.send(**msg_params)
            return True, None
        except TypeError:
            del msg_params["attachments"]
            client.inboxes.messages.send(**msg_params)
            return True, "files_local_only"

    except Exception as e:
        return False, str(e)


def save_submission_log(form_data, files, status):
    log_path = os.path.join(os.path.dirname(__file__), "submissions.jsonl")
    entry = {
        "ts":       datetime.now().isoformat(),
        "name":     form_data.get("name"),
        "email":    form_data.get("email"),
        "store":    form_data.get("store_name"),
        "market":   form_data.get("marketplace"),
        "gmv":      form_data.get("gmv"),
        "invoices": form_data.get("has_invoices"),
        "files":    [f[1] for f in files],
        "status":   status,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@app.route("/", methods=["GET"])
def form():
    return render_template("form.html", error=None, form={
        "name": "", "email": "", "store_name": "", "marketplace": ""
    })


@app.route("/", methods=["POST"])
def submit():
    form_data = request.form.to_dict()

    required = ["name", "email", "store_name", "marketplace", "gmv",
                "has_reimbursements", "has_invoices"]
    for field in required:
        if not form_data.get(field):
            return render_template("form.html",
                error="Please fill in all required fields.",
                form=form_data)

    reimb_file = request.files.get("reimb_file")
    if not reimb_file or reimb_file.filename == "":
        return render_template("form.html",
            error="Please upload your FBA Reimbursement Report.",
            form=form_data)

    if not allowed_file(reimb_file.filename):
        return render_template("form.html",
            error="Reimbursement report must be CSV or Excel format.",
            form=form_data)

    saved_files = []
    store_safe = "".join(c for c in form_data["store_name"] if c.isalnum() or c in "_-")[:30]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    reimb_fname = f"{store_safe}_{ts}_reimbursements{os.path.splitext(reimb_file.filename)[1]}"
    reimb_path  = os.path.join(UPLOAD_FOLDER, reimb_fname)
    reimb_file.save(reimb_path)
    saved_files.append((reimb_path, reimb_fname))

    invoice_files = request.files.getlist("invoice_files")
    for i, inv_file in enumerate(invoice_files):
        if inv_file and inv_file.filename and allowed_file(inv_file.filename):
            ext = os.path.splitext(inv_file.filename)[1]
            inv_fname = f"{store_safe}_{ts}_invoice_{i+1}{ext}"
            inv_path  = os.path.join(UPLOAD_FOLDER, inv_fname)
            inv_file.save(inv_path)
            saved_files.append((inv_path, inv_fname))

    config = load_config()
    ok, err = send_to_agentmail(
        config,
        client_name=form_data["name"],
        store_name=form_data["store_name"],
        sender_email=form_data["email"],
        form_data=form_data,
        files=saved_files
    )

    status = "ok" if ok else f"error: {err}"
    save_submission_log(form_data, saved_files, status)

    if not ok and err != "files_local_only":
        app.logger.warning(f"AgentMail send error: {err}")

    return render_template("thank_you.html",
        name=form_data["name"],
        store_name=form_data["store_name"],
        email=form_data["email"]
    )


@app.route("/submissions")
def submissions():
    log_path = os.path.join(os.path.dirname(__file__), "submissions.jsonl")
    entries = []
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    entries.reverse()
    html = "<h2 style='font-family:sans-serif;padding:20px'>Submissions</h2>"
    html += "<table border=1 cellpadding=8 style='font-family:sans-serif;font-size:13px'>"
    html += "<tr><th>Time</th><th>Name</th><th>Store</th><th>Email</th><th>GMV</th><th>Files</th><th>Status</th></tr>"
    for e in entries:
        html += (f"<tr><td>{e['ts'][:16]}</td><td>{e['name']}</td><td>{e['store']}</td>"
                 f"<td>{e['email']}</td><td>{e['gmv']}</td>"
                 f"<td>{', '.join(e['files'])}</td><td>{e['status']}</td></tr>")
    html += "</table>"
    return html


if __name__ == "__main__":
    print("\n" + "="*50)
    print("  FBA Audit Intake Form")
    print("  http://localhost:5000")
    print("="*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000)
