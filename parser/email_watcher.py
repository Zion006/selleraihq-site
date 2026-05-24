#!/usr/bin/env python3
"""
FBA Audit — Email Watcher (AgentMail API)
------------------------------------------
Мониторит inbox zionbot@agentmail.to через AgentMail API,
скачивает CSV/Excel вложения в drop/, запускает аудит,
отправляет PDF результаты клиенту.

Разовый запуск:
    venv/bin/python email_watcher.py

Демон (бесконечный цикл, для systemd):
    venv/bin/python email_watcher.py --daemon
    venv/bin/python email_watcher.py --daemon --interval 120
"""

import os
import sys
import json
import time
import base64
import argparse
import subprocess
from datetime import datetime

CONFIG_FILE = "config.json"
DROP_DIR    = "drop"
OUTPUT_DIR  = "output"
LOG_FILE    = "email_watcher.log"
SEEN_FILE   = "seen_messages.json"

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".txt", ".tsv"}


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── CONFIG ───────────────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[!] Файл {CONFIG_FILE} не найден. Создай: cp config.example.json config.json")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── SEEN MESSAGES ────────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def extract_client_name(from_addr, subject):
    subject_lower = (subject or "").lower()
    for keyword in ["audit:", "client:", "seller:", "store:"]:
        if keyword in subject_lower:
            idx = subject_lower.index(keyword) + len(keyword)
            name = (subject or "")[idx:].strip().split()[0]
            if name:
                return name.replace(",", "").replace(";", "")[:30]
    if from_addr and "<" in from_addr:
        name_part = from_addr.split("<")[0].strip().strip('"')
        if name_part:
            return name_part.replace(" ", "")[:20]
    if from_addr:
        return from_addr.split("<")[-1].strip(">").split("@")[0][:20]
    return "Unknown"


def extract_client_email(body):
    for line in (body or "").splitlines():
        if line.strip().lower().startswith("email:"):
            return line.split(":", 1)[1].strip()
    return ""


def read_findings_summary(client_safe):
    """Читает findings CSV и возвращает текст summary для письма."""
    import csv
    path = os.path.join(OUTPUT_DIR, f"findings_{client_safe}.csv")
    if not os.path.exists(path):
        return "", 0, 0.0

    rows = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return "", 0, 0.0

    total_cases = len(rows)
    total_delta = sum(float(r.get("delta", 0)) for r in rows)
    high_conf   = sum(1 for r in rows if r.get("confidence") == "High")
    critical    = sum(1 for r in rows if r.get("urgency") == "Critical")
    urgent      = sum(1 for r in rows if r.get("urgency") == "Urgent")
    expired     = sum(1 for r in rows if r.get("urgency") == "Expired")

    lines = [
        f"AUDIT COMPLETE — {client_safe}",
        "",
        f"Underpaid cases found: {total_cases}",
        f"Total potential underpayment: ${total_delta:,.2f}",
        f"High-confidence cases: {high_conf}",
        "",
        f"URGENT (deadline < 7 days):    {critical}",
        f"EXPIRING SOON (< 14 days):     {urgent}",
        f"Already expired (> 60 days):   {expired}",
    ]

    if rows:
        lines += ["", "Top findings:"]
        top = sorted(rows, key=lambda r: float(r.get("delta", 0)), reverse=True)[:5]
        for r in top:
            lines.append(
                f"  • {r.get('case_id','')} — {r.get('sku','')} — "
                f"${float(r.get('delta',0)):,.2f} underpaid — "
                f"deadline {r.get('deadline','')} ({r.get('urgency','')})"
            )

    if total_cases == 0:
        lines += [
            "",
            "No underpayment found in your reimbursements.",
            "Either Amazon paid correctly, or sourcing costs were not matched.",
            "Check that your invoice file contains the correct FNSKUs.",
        ]

    lines += [
        "",
        "Full evidence pack attached as PDF.",
        "Submit re-evaluation claims through Amazon Seller Central within the deadline.",
        "",
        "— SellerAIHQ FBA Audit",
        "selleraihq.com",
    ]

    return "\n".join(lines), total_cases, total_delta


def send_results(agentmail_client, inbox_id, client_email, client_name, client_safe):
    """Отправляет PDF отчёт клиенту через AgentMail."""
    pdf_path  = os.path.join(OUTPUT_DIR, f"report_{client_safe}.pdf")
    html_path = os.path.join(OUTPUT_DIR, f"report_{client_safe}.html")

    body, total_cases, total_delta = read_findings_summary(client_safe)

    attachments = []

    # Прикрепить PDF если есть
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            attachments.append({
                "filename":     f"FBA_Audit_{client_safe}.pdf",
                "content":      base64.b64encode(f.read()).decode(),
                "content_type": "application/pdf",
            })
    # Fallback: HTML если нет PDF
    elif os.path.exists(html_path):
        with open(html_path, "rb") as f:
            attachments.append({
                "filename":     f"FBA_Audit_{client_safe}.html",
                "content":      base64.b64encode(f.read()).decode(),
                "content_type": "text/html",
            })

    subject = (
        f"FBA Audit Results — {client_name} — "
        f"{total_cases} cases — ${total_delta:,.2f} potential underpayment"
        if total_cases > 0
        else f"FBA Audit Complete — {client_name} — No underpayment found"
    )

    try:
        agentmail_client.inboxes.messages.send(
            inbox_id=inbox_id,
            to=[client_email],
            subject=subject,
            text=body,
            attachments=attachments if attachments else None,
        )
        log(f"  [SENT] Результаты отправлены → {client_email}")
        return True
    except Exception as e:
        log(f"  [ERROR] Не удалось отправить email: {e}")
        return False


# ─── ONE PASS ─────────────────────────────────────────────────────────────────

def run_once(agentmail_client, inbox_id, auto_run, min_delta, days):
    seen = load_seen()

    try:
        response = agentmail_client.inboxes.messages.list(inbox_id=inbox_id)
        messages = response.messages if hasattr(response, "messages") else list(response)
    except Exception as e:
        # Логируем только краткое описание ошибки, без HTML тела
        err_str = str(e)
        status = ""
        if "status_code" in err_str:
            try:
                status = "HTTP " + err_str.split("status_code:")[1].split(",")[0].strip()
            except Exception:
                pass
        log(f"[ERROR] AgentMail недоступен ({status or err_str[:80]}). Повтор через {60}s.")
        return

    new_messages = [m for m in messages if m.message_id not in seen]

    if not new_messages:
        log(f"Inbox: {len(messages)} сообщений, новых нет.")
        return

    log(f"Inbox: {len(messages)} сообщений, новых: {len(new_messages)}")
    os.makedirs(DROP_DIR, exist_ok=True)
    processed = 0

    for msg in new_messages:
        msg_id    = msg.message_id
        from_addr = getattr(msg, "from_", "") or ""
        subject   = getattr(msg, "subject", "") or ""

        log(f"\n─── {subject[:60]}")

        # Только audit-заявки
        if not subject.strip().lower().startswith("audit:"):
            seen.add(msg_id)
            continue

        log(f"  От: {from_addr}")

        attachments = getattr(msg, "attachments", []) or []
        valid_attachments = [
            a for a in attachments
            if os.path.splitext(getattr(a, "filename", ""))[1].lower() in ALLOWED_EXTENSIONS
        ]

        if not valid_attachments:
            log("  Нет CSV/Excel вложений — пропускаю.")
            seen.add(msg_id)
            continue

        client_name = extract_client_name(from_addr, subject)
        log(f"  Клиент: {client_name}")

        # Email клиента из тела письма
        client_email = ""
        try:
            full_msg = agentmail_client.inboxes.messages.get(
                inbox_id=inbox_id, message_id=msg_id
            )
            body = getattr(full_msg, "text", "") or getattr(full_msg, "body", "") or ""
            client_email = extract_client_email(body)
        except Exception:
            pass
        if client_email:
            log(f"  Email клиента: {client_email}")

        # Очистить drop/
        for f in os.listdir(DROP_DIR):
            os.remove(os.path.join(DROP_DIR, f))

        # Скачать вложения
        import urllib.request
        saved = []
        for att in valid_attachments:
            att_id   = att.attachment_id
            filename = att.filename or f"attachment_{att_id}"
            safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
            out_path  = os.path.join(DROP_DIR, safe_name)
            try:
                att_resp = agentmail_client.inboxes.messages.get_attachment(
                    inbox_id=inbox_id, message_id=msg_id, attachment_id=att_id
                )
                urllib.request.urlretrieve(att_resp.download_url, out_path)
                log(f"  Скачано: {safe_name} ({os.path.getsize(out_path):,} bytes)")
                saved.append(out_path)
            except Exception as e:
                log(f"  [ERROR] {filename}: {e}")

        seen.add(msg_id)

        # Запустить аудит
        client_safe = client_name.replace(" ", "_")
        if auto_run and saved:
            log(f"  Запускаю аудит...")
            cmd = [
                sys.executable, "auto_prepare.py",
                "--client",          client_name,
                "--days",            str(days),
                "--min-delta",       str(min_delta),
                "--non-interactive",
            ]
            if client_email:
                cmd += ["--email", client_email]
            result = subprocess.run(cmd, capture_output=False)

            if result.returncode == 0:
                log(f"  [OK] Аудит завершён → output/findings_{client_safe}.csv")

                # Отправить результаты клиенту
                if client_email:
                    send_results(agentmail_client, inbox_id, client_email, client_name, client_safe)
                else:
                    log("  [WARN] Email клиента не найден — результаты не отправлены.")
                    log(f"         Отчёт: output/report_{client_safe}.pdf")
            else:
                log(f"  [ERROR] Аудит завершился с ошибкой (код {result.returncode})")
                if client_email:
                    try:
                        agentmail_client.inboxes.messages.send(
                            inbox_id=inbox_id,
                            to=[client_email],
                            subject=f"FBA Audit — Issue with your files ({client_name})",
                            text=(
                                f"Hi,\n\nWe received your files but encountered an issue "
                                f"processing them.\n\nPlease ensure:\n"
                                f"1. Reimbursement Report is downloaded from Seller Central → "
                                f"Reports → Fulfillment → Reimbursements\n"
                                f"2. Invoice file contains FNSKU/SKU and unit cost columns\n\n"
                                f"Reply to this email and we'll sort it out.\n\n"
                                f"— SellerAIHQ FBA Audit"
                            ),
                        )
                        log(f"  Уведомление об ошибке отправлено → {client_email}")
                    except Exception:
                        pass

        processed += 1

    save_seen(seen)
    if processed:
        log(f"\nОбработано: {processed} новых audit-заявок")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FBA Audit Email Watcher")
    parser.add_argument("--daemon",   action="store_true", help="Бесконечный цикл (для systemd)")
    parser.add_argument("--interval", type=int, default=60, help="Интервал проверки в секундах (default: 60)")
    args = parser.parse_args()

    config   = load_config()
    api_key  = config.get("api_key")
    inbox_id = config.get("inbox_id", "zionbot@agentmail.to")
    auto_run = config.get("auto_run", True)
    min_delta= config.get("min_delta", 10.0)
    days     = config.get("days", 60)

    if not api_key:
        print("[!] api_key не заполнен в config.json")
        sys.exit(1)

    try:
        from agentmail import AgentMail
    except ImportError:
        print("[!] AgentMail SDK не установлен. Запусти: venv/bin/pip install agentmail")
        sys.exit(1)

    agentmail_client = AgentMail(api_key=api_key)

    if args.daemon:
        log(f"=== Демон запущен. Интервал проверки: {args.interval}s ===")
        while True:
            try:
                run_once(agentmail_client, inbox_id, auto_run, min_delta, days)
            except Exception as e:
                log(f"[ERROR] Необработанная ошибка: {e}")
            time.sleep(args.interval)
    else:
        log(f"Подключаюсь к AgentMail inbox: {inbox_id}")
        run_once(agentmail_client, inbox_id, auto_run, min_delta, days)


if __name__ == "__main__":
    main()
