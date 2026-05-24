#!/usr/bin/env python3
"""
FBA Audit — Report Generator
------------------------------
Читает findings CSV → генерирует PDF + HTML отчёт для клиента.

Запуск:
    venv/bin/python report_generator.py \
        --findings output/findings_TestClient.csv \
        --client   "TestClient" \
        --market   "US"

Результат:
    output/report_TestClient.pdf
    output/report_TestClient.html
"""

import argparse
import os
import sys
from datetime import datetime, date

import pandas as pd
from jinja2 import Environment, FileSystemLoader


TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "report_templates")


def fmt_number(val, decimals=2):
    try:
        return f"{float(val):,.{decimals}f}"
    except Exception:
        return str(val)


def main():
    parser = argparse.ArgumentParser(description="FBA Audit Report Generator")
    parser.add_argument("--findings", required=True, help="Findings CSV (output от парсера)")
    parser.add_argument("--client",   default="Client", help="Имя клиента")
    parser.add_argument("--market",   default="US", help="Marketplace")
    parser.add_argument("--output",   default="", help="Папка для вывода (default: output/)")
    args = parser.parse_args()

    if not os.path.exists(args.findings):
        print(f"[ERROR] Файл не найден: {args.findings}")
        sys.exit(1)

    # ── Загрузка findings ───────────────────────────────────────────────────
    df = pd.read_csv(args.findings, encoding="utf-8-sig", dtype=str)
    df["delta"]          = pd.to_numeric(df["delta"],          errors="coerce").fillna(0)
    df["amazon_total"]   = pd.to_numeric(df["amazon_total"],   errors="coerce").fillna(0)
    df["expected_total"] = pd.to_numeric(df["expected_total"], errors="coerce").fillna(0)
    df["unit_cost"]      = pd.to_numeric(df.get("unit_cost",   pd.Series(["0"]*len(df))), errors="coerce").fillna(0)
    df["qty"]            = pd.to_numeric(df["qty"],            errors="coerce").fillna(0)
    df["days_left"]      = pd.to_numeric(df["days_left"],      errors="coerce").fillna(-999)

    # Убедиться что все нужные поля есть
    for col in ["invoice_ref", "asin", "product_name"]:
        if col not in df.columns:
            df[col] = ""

    df["invoice_ref"]   = df["invoice_ref"].fillna("")
    df["asin"]          = df["asin"].fillna("")
    df["product_name"]  = df["product_name"].fillna("")

    # Сортировка: сначала Critical/Urgent, потом по delta
    urgency_order = {"Critical": 0, "Urgent": 1, "Open": 2, "Expired": 3, "Unknown": 4}
    df["_urgency_sort"] = df["urgency"].map(urgency_order).fillna(5)
    df = df.sort_values(["_urgency_sort", "delta"], ascending=[True, False])
    df = df.drop(columns=["_urgency_sort"])

    # ── Метрики ──────────────────────────────────────────────────────────────
    total_delta      = df["delta"].sum()
    total_cases      = len(df)
    high_conf        = df[df["confidence"].str.lower() == "high"]
    critical         = df[df["urgency"] == "Critical"]
    urgent           = df[df["urgency"] == "Urgent"]
    open_cases       = df[df["urgency"] == "Open"]
    expired          = df[df["urgency"] == "Expired"]

    date_from = df["date"].min() if "date" in df.columns else "—"
    date_to   = df["date"].max() if "date" in df.columns else "—"

    # ── Рендер шаблона ───────────────────────────────────────────────────────
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("report.html")

    html = template.render(
        client         = args.client,
        audit_date     = datetime.now().strftime("%B %d, %Y"),
        date_from      = date_from,
        date_to        = date_to,
        marketplace    = args.market,
        total_reviewed = total_cases,
        total_delta    = fmt_number(total_delta),
        total_cases    = total_cases,
        high_conf_cases= len(high_conf),
        critical_count = len(critical),
        urgent_count   = len(urgent),
        open_count     = len(open_cases),
        expired_count  = len(expired),
        findings       = df.to_dict(orient="records"),
    )

    # ── Сохранить HTML ───────────────────────────────────────────────────────
    out_dir = args.output or os.path.join(os.path.dirname(args.findings))
    os.makedirs(out_dir, exist_ok=True)

    client_safe = args.client.replace(" ", "_")
    html_path = os.path.join(out_dir, f"report_{client_safe}.html")
    pdf_path  = os.path.join(out_dir, f"report_{client_safe}.pdf")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] HTML → {html_path}")

    # ── Сохранить PDF ────────────────────────────────────────────────────────
    try:
        from weasyprint import HTML
        HTML(string=html, base_url=TEMPLATE_DIR).write_pdf(pdf_path)
        print(f"[OK] PDF  → {pdf_path}")
    except Exception as e:
        print(f"[WARN] PDF не создан ({e}). Используй HTML версию.")
        pdf_path = None

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  REPORT SUMMARY — {args.client}")
    print(f"{'='*55}")
    print(f"  Cases in report:       {total_cases}")
    print(f"  Total underpayment:    ${fmt_number(total_delta)}")
    print(f"  High-confidence:       {len(high_conf)}")
    print(f"  Critical / Urgent:     {len(critical)} / {len(urgent)}")
    print(f"  Expired (missed):      {len(expired)}")
    print(f"{'='*55}")
    print(f"  HTML: {html_path}")
    if pdf_path:
        print(f"  PDF:  {pdf_path}")
    print(f"{'='*55}\n")

    return pdf_path or html_path


if __name__ == "__main__":
    main()
