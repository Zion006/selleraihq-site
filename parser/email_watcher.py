#!/usr/bin/env python3
"""
FBA Audit — Email Watcher (AgentMail API)
------------------------------------------
Мониторит inbox zionbot@agentmail.to через AgentMail API,
скачивает CSV/Excel вложения в drop/ и запускает аудит.

Настройка:
    cp config.example.json config.json
    # вписать api_key из dashboard.agentmail.to

Запуск:
    venv/bin/python email_watcher.py
"""

import os
import sys
import json
import subprocess
from datetime import datetime

CONFIG_FILE = "config.json"
DROP_DIR    = "drop"
LOG_FILE    = "email_watcher.log"
SEEN_FILE   = "seen_messages.json"   # чтобы не обрабатывать одно письмо дважды

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".txt", ".tsv"}


# ─── LOGGING ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ─── CONFIG ───────────────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"\n[!] Файл {CONFIG_FILE} не найден.")
        print(f"    Создай его командой:")
        print(f"    cp config.example.json config.json")
        print(f"    И вставь api_key из dashboard.agentmail.to → API Keys\n")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── SEEN MESSAGES (чтобы не дублировать) ────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ─── CLIENT NAME ─────────────────────────────────────────────────────────────

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
        email_part = from_addr.split("<")[-1].strip(">").split("@")[0]
        return email_part[:20]

    return "Unknown"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    config    = load_config()
    api_key   = config.get("api_key")
    inbox_id  = config.get("inbox_id", "zionbot@agentmail.to")
    auto_run  = config.get("auto_run", True)
    min_delta = config.get("min_delta", 10.0)
    days      = config.get("days", 60)

    if not api_key:
        print("[!] api_key не заполнен в config.json")
        print("    Зайди на dashboard.agentmail.to → API Keys → создай ключ")
        sys.exit(1)

    # Импорт SDK
    try:
        from agentmail import AgentMail
    except ImportError:
        print("[!] AgentMail SDK не установлен.")
        print("    Запусти: venv/bin/pip install agentmail")
        sys.exit(1)

    client = AgentMail(api_key=api_key)

    log(f"Подключаюсь к AgentMail inbox: {inbox_id}")

    # Загрузить список уже обработанных писем
    seen = load_seen()

    # Получить список сообщений
    try:
        response = client.inboxes.messages.list(inbox_id=inbox_id)
        messages = response.messages if hasattr(response, "messages") else list(response)
    except Exception as e:
        log(f"[ERROR] Не удалось получить сообщения: {e}")
        sys.exit(1)

    if not messages:
        log("Нет сообщений в inbox.")
        return

    new_messages = [m for m in messages if m.message_id not in seen]
    log(f"Сообщений всего: {len(messages)}, новых: {len(new_messages)}")

    if not new_messages:
        log("Нет новых сообщений.")
        return

    os.makedirs(DROP_DIR, exist_ok=True)
    processed = 0

    for msg in new_messages:
        msg_id    = msg.message_id
        from_addr = getattr(msg, "from_", "") or ""
        subject   = getattr(msg, "subject", "") or ""

        log(f"\n─── Письмо: {msg_id}")
        log(f"    От: {from_addr}")
        log(f"    Тема: {subject}")

        # Фильтр: только audit-заявки (тема начинается с "Audit:")
        if not subject.strip().lower().startswith("audit:"):
            log("  Тема не начинается с 'Audit:' — пропускаю (спам/нерелевантное).")
            seen.add(msg_id)
            continue

        attachments = getattr(msg, "attachments", []) or []

        # Фильтр: только CSV/Excel
        valid_attachments = [
            a for a in attachments
            if os.path.splitext(getattr(a, "filename", ""))[1].lower() in ALLOWED_EXTENSIONS
        ]

        if not valid_attachments:
            log("  Нет CSV/Excel вложений. Пропускаю.")
            seen.add(msg_id)
            continue

        log(f"  Вложений CSV/Excel: {len(valid_attachments)}")

        client_name = extract_client_name(from_addr, subject)
        log(f"  Клиент: {client_name}")

        # Извлечь email клиента из тела письма
        client_email = ""
        try:
            full_msg = client.inboxes.messages.get(inbox_id=inbox_id, message_id=msg_id)
            body = getattr(full_msg, "text", "") or getattr(full_msg, "body", "") or ""
            for line in body.splitlines():
                if line.strip().lower().startswith("email:"):
                    client_email = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        if client_email:
            log(f"  Email клиента: {client_email}")

        # Очистить drop/
        for f in os.listdir(DROP_DIR):
            os.remove(os.path.join(DROP_DIR, f))

        # Скачать вложения
        saved = []
        for att in valid_attachments:
            att_id    = att.attachment_id
            filename  = att.filename or f"attachment_{att_id}"
            safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
            out_path  = os.path.join(DROP_DIR, safe_name)

            try:
                import urllib.request
                att_response = client.inboxes.messages.get_attachment(
                    inbox_id=inbox_id,
                    message_id=msg_id,
                    attachment_id=att_id
                )
                download_url = att_response.download_url
                urllib.request.urlretrieve(download_url, out_path)
                log(f"  Сохранено: {out_path} ({os.path.getsize(out_path)} bytes)")
                saved.append(out_path)
            except Exception as e:
                log(f"  [ERROR] Не удалось скачать {filename}: {e}")

        seen.add(msg_id)

        # Запустить аудит
        if auto_run and saved:
            log(f"\n  Запускаю аудит для: {client_name}...")
            cmd = [
                sys.executable, "auto_prepare.py",
                "--client",         client_name,
                "--days",           str(days),
                "--min-delta",      str(min_delta),
                "--non-interactive",
            ]
            if client_email:
                cmd += ["--email", client_email]
            result = subprocess.run(cmd)
            if result.returncode == 0:
                log(f"  [OK] Готово → output/findings_{client_name}.csv")
            else:
                log(f"  [ERROR] Аудит завершился с ошибкой (код {result.returncode})")

        processed += 1

    save_seen(seen)
    log(f"\nОбработано новых писем: {processed}")


if __name__ == "__main__":
    main()
