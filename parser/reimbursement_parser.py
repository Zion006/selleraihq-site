#!/usr/bin/env python3
"""
FBA Underpaid Reimbursement Parser
-----------------------------------
Читает Amazon FBA Reimbursement Report + Cost Registry,
считает underpayment delta, флагует дедлайны, экспортирует findings.

Запуск:
    pip install pandas openpyxl
    python reimbursement_parser.py \
        --reimb   data/reimbursements.csv \
        --costs   data/cost_registry.csv \
        --output  output/findings.csv

Опционально:
    --days 60        # окно re-evaluation (по умолчанию 60)
    --min-delta 10   # минимальная дельта для включения в findings (по умолчанию 10)
    --client "Acme"  # имя клиента для отчёта
"""

import argparse
import sys
import os
from datetime import datetime, timedelta
import pandas as pd


# ─── COLUMN ALIASES ──────────────────────────────────────────────────────────
# Amazon иногда меняет названия колонок в разных регионах/версиях отчётов.

REIMB_COL_MAP = {
    "approval-date":                   "date",
    "approval_date":                   "date",
    "date":                            "date",
    "reimbursement-id":                "reimb_id",
    "reimbursement_id":                "reimb_id",
    "reimbursementid":                 "reimb_id",
    "reason":                          "reason",
    "sku":                             "sku",
    "fnsku":                           "fnsku",
    "asin":                            "asin",
    "product-name":                    "product_name",
    "product_name":                    "product_name",
    "title":                           "product_name",
    "currency-unit":                   "currency",
    "currency_unit":                   "currency",
    "currency":                        "currency",
    "amount-per-unit":                 "amount_per_unit",
    "amount_per_unit":                 "amount_per_unit",
    "amountperunit":                   "amount_per_unit",
    "amount-total":                    "amount_total",
    "amount_total":                    "amount_total",
    "amounttotal":                     "amount_total",
    "quantity-reimbursed-cash":        "qty_cash",
    "quantity_reimbursed_cash":        "qty_cash",
    "quantity-reimbursed-inventory":   "qty_inventory",
    "quantity_reimbursed_inventory":   "qty_inventory",
    "quantity-reimbursed-total":       "qty_total",
    "quantity_reimbursed_total":       "qty_total",
    "quantityreimbursedtotal":         "qty_total",
}

COST_COL_MAP = {
    "sku":                  "sku",
    "fnsku":                "fnsku",
    "asin":                 "asin",
    "unit_sourcing_cost":   "unit_cost",
    "unit-sourcing-cost":   "unit_cost",
    "unit_cost":            "unit_cost",
    "cost":                 "unit_cost",
    "sourcing_cost":        "unit_cost",
    "currency":             "cost_currency",
    "cost_currency":        "cost_currency",
    "confidence":           "confidence",
    "invoice_ref":          "invoice_ref",
    "invoice-ref":          "invoice_ref",
    "notes":                "notes",
}


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def normalize_columns(df, col_map):
    """Приводит названия колонок к стандартным через alias-таблицу."""
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rename = {c: col_map[c] for c in df.columns if c in col_map}
    return df.rename(columns=rename)


def load_csv(path, col_map, label):
    """Загружает CSV или TSV, нормализует колонки."""
    if not os.path.exists(path):
        print(f"[ERROR] Файл не найден: {path}")
        sys.exit(1)

    # Amazon иногда выгружает TSV
    sep = "\t" if path.endswith(".txt") or path.endswith(".tsv") else None
    try:
        if sep:
            df = pd.read_csv(path, sep=sep, encoding="utf-8-sig", dtype=str)
        else:
            # пробуем запятую, потом точку с запятой
            df = pd.read_csv(path, sep=",", encoding="utf-8-sig", dtype=str)
            if len(df.columns) == 1:
                df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    except Exception as e:
        print(f"[ERROR] Не удалось прочитать {label}: {e}")
        sys.exit(1)

    df = normalize_columns(df, col_map)
    print(f"[OK] {label}: {len(df)} строк, колонки: {list(df.columns)}")
    return df


def parse_amount(series):
    """Чистит строки вида '1,234.56' или '1.234,56' → float."""
    return (
        series.astype(str)
        .str.replace(r"[^\d.,\-]", "", regex=True)
        .str.replace(",", ".", regex=False)
        .pipe(lambda s: pd.to_numeric(s, errors="coerce"))
        .fillna(0.0)
    )


def urgency_label(days_left):
    if pd.isna(days_left):
        return "Unknown"
    if days_left < 0:
        return "Expired"
    if days_left <= 7:
        return "Critical"
    if days_left <= 14:
        return "Urgent"
    return "Open"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FBA Underpaid Reimbursement Parser")
    parser.add_argument("--reimb",      required=True, help="Amazon Reimbursement Report CSV/TSV")
    parser.add_argument("--costs",      required=True, help="Cost Registry CSV")
    parser.add_argument("--output",     default="output/findings.csv", help="Куда сохранить findings")
    parser.add_argument("--days",       type=int, default=60, help="Окно re-evaluation в днях (default: 60)")
    parser.add_argument("--min-delta",  type=float, default=10.0, help="Минимальная дельта для findings (default: 10)")
    parser.add_argument("--client",     default="", help="Имя клиента")
    args = parser.parse_args()

    today = datetime.today().date()
    print(f"\n{'='*60}")
    print(f"  FBA Underpaid Reimbursement Parser")
    print(f"  Дата аудита: {today}")
    if args.client:
        print(f"  Клиент: {args.client}")
    print(f"{'='*60}\n")

    # ── 1. Загрузка ──────────────────────────────────────────────────────────
    reimb = load_csv(args.reimb, REIMB_COL_MAP, "Reimbursement Report")
    costs = load_csv(args.costs, COST_COL_MAP, "Cost Registry")

    # ── 2. Проверка обязательных колонок ─────────────────────────────────────
    required_reimb = {"date", "reimb_id", "reason", "sku", "fnsku"}
    required_costs = {"unit_cost"}
    missing_r = required_reimb - set(reimb.columns)
    missing_c = required_costs - set(costs.columns)

    if missing_r:
        print(f"[WARN] Reimbursement report: не найдены колонки {missing_r}")
        print(f"       Найдены: {list(reimb.columns)}")
    if missing_c:
        print(f"[ERROR] Cost Registry: обязательная колонка unit_cost не найдена")
        print(f"        Найдены: {list(costs.columns)}")
        sys.exit(1)

    # ── 3. Нормализация reimbursements ────────────────────────────────────────
    if "date" not in reimb.columns:
        print(f"[WARN] Колонка date/approval-date не найдена — используется сегодняшняя дата для всех строк")
        reimb["date"] = today
    else:
        reimb["date"] = pd.to_datetime(reimb["date"], errors="coerce").dt.date

    for col in ["amount_per_unit", "amount_total"]:
        if col in reimb.columns:
            reimb[col] = parse_amount(reimb[col])
        else:
            reimb[col] = 0.0

    # Qty: берём qty_total, если нет — qty_cash
    if "qty_total" in reimb.columns:
        reimb["qty"] = parse_amount(reimb["qty_total"])
    elif "qty_cash" in reimb.columns:
        reimb["qty"] = parse_amount(reimb["qty_cash"])
    else:
        reimb["qty"] = 1.0
        print("[WARN] Колонка qty не найдена, использую qty=1 для всех строк")

    # Reimbursed amount per unit
    reimb["reimb_per_unit"] = reimb.apply(
        lambda r: r["amount_per_unit"] if r["amount_per_unit"] > 0
                  else (r["amount_total"] / r["qty"] if r["qty"] > 0 else 0),
        axis=1
    )

    # Дедлайн
    reimb["deadline"] = reimb["date"].apply(
        lambda d: d + timedelta(days=args.days) if pd.notna(d) else None
    )

    # Фильтр: только lost/damaged
    lost_damaged_keywords = ["lost", "damaged", "warehouse", "inbound", "outbound", "carrier"]
    if "reason" in reimb.columns:
        mask = reimb["reason"].astype(str).str.lower().str.contains(
            "|".join(lost_damaged_keywords), na=False
        )
        filtered = reimb[mask].copy()
        excluded = len(reimb) - len(filtered)
        print(f"\n[INFO] Reimbursements всего: {len(reimb)}")
        print(f"[INFO] Lost/Damaged (аудит): {len(filtered)}")
        print(f"[INFO] Пропущено (другие причины): {excluded}")
    else:
        filtered = reimb.copy()
        print("[WARN] Колонка reason не найдена, обрабатываем все строки")

    if len(filtered) == 0:
        print("\n[WARN] Нет строк с lost/damaged. Проверь файл или reason-колонку.")
        sys.exit(0)

    # ── 4. Нормализация cost registry ────────────────────────────────────────
    costs["unit_cost"] = parse_amount(costs["unit_cost"])

    # Убрать нулевые costs
    costs = costs[costs["unit_cost"] > 0].copy()

    # ── 5. Join: сначала по FNSKU, потом по SKU ───────────────────────────────
    join_key = None
    if "fnsku" in costs.columns and "fnsku" in filtered.columns:
        merged = filtered.merge(
            costs[["fnsku", "unit_cost"] + [c for c in ["confidence", "invoice_ref", "cost_currency"] if c in costs.columns]],
            on="fnsku", how="left"
        )
        join_key = "fnsku"
    elif "sku" in costs.columns and "sku" in filtered.columns:
        merged = filtered.merge(
            costs[["sku", "unit_cost"] + [c for c in ["confidence", "invoice_ref", "cost_currency"] if c in costs.columns]],
            on="sku", how="left"
        )
        join_key = "sku"
    else:
        print("[ERROR] Нет общей колонки для join (fnsku или sku) между reimbursements и cost registry")
        sys.exit(1)

    print(f"[INFO] Join по: {join_key}")
    matched = merged[merged["unit_cost"].notna()].copy()
    unmatched = merged[merged["unit_cost"].isna()].copy()
    print(f"[INFO] Matched (есть sourcing cost): {len(matched)}")
    print(f"[INFO] Unmatched (нет sourcing cost): {len(unmatched)}")

    # ── 6. Расчёт delta ───────────────────────────────────────────────────────
    matched["expected_total"] = matched["unit_cost"] * matched["qty"]
    matched["amazon_total"]   = matched["amount_total"].fillna(
        matched["reimb_per_unit"] * matched["qty"]
    )
    matched["delta"] = matched["expected_total"] - matched["amazon_total"]

    # Только случаи с underpayment
    underpaid = matched[matched["delta"] >= args.min_delta].copy()
    print(f"\n[INFO] Underpaid cases (delta ≥ {args.min_delta}): {len(underpaid)}")

    if len(underpaid) == 0:
        print("[INFO] Underpayment не обнаружен. Возможно, нужно пересмотреть cost registry или threshold.")

    # ── 7. Дедлайны и urgency ─────────────────────────────────────────────────
    underpaid["days_left"] = underpaid["deadline"].apply(
        lambda d: (d - today).days if pd.notna(d) else None
    )
    underpaid["urgency"] = underpaid["days_left"].apply(urgency_label)

    # ── 8. Confidence ─────────────────────────────────────────────────────────
    if "confidence" not in underpaid.columns:
        underpaid["confidence"] = "Medium"

    # High confidence: есть invoice_ref + confidence явно High
    if "invoice_ref" in underpaid.columns:
        underpaid.loc[
            underpaid["invoice_ref"].notna() & (underpaid["invoice_ref"] != ""),
            "confidence"
        ] = underpaid.loc[
            underpaid["invoice_ref"].notna() & (underpaid["invoice_ref"] != ""),
            "confidence"
        ].fillna("High")

    # ── 9. Case ID ────────────────────────────────────────────────────────────
    underpaid = underpaid.reset_index(drop=True)
    underpaid["case_id"] = ["UR-" + str(i+1).zfill(3) for i in underpaid.index]
    if args.client:
        prefix = args.client[:3].upper()
        underpaid["case_id"] = [f"UR-{prefix}-" + str(i+1).zfill(3) for i in underpaid.index]

    # ── 10. Формирование findings ─────────────────────────────────────────────
    cols_out = [
        "case_id", "reimb_id", "sku", "fnsku", "asin", "product_name",
        "reason", "date", "qty",
        "amazon_total", "unit_cost", "expected_total", "delta",
        "deadline", "days_left", "urgency", "confidence",
    ]
    if "invoice_ref" in underpaid.columns:
        cols_out.append("invoice_ref")
    if "currency" in underpaid.columns:
        cols_out.append("currency")

    cols_out = [c for c in cols_out if c in underpaid.columns]
    findings = underpaid[cols_out].copy()
    findings = findings.sort_values("days_left", ascending=True, na_position="last")

    # ── 11. Сохранение ────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    findings.to_csv(args.output, index=False, encoding="utf-8-sig")

    # ── 12. Summary ───────────────────────────────────────────────────────────
    total_delta   = findings["delta"].sum()
    high_conf     = findings[findings["confidence"] == "High"]
    critical      = findings[findings["urgency"] == "Critical"]
    urgent        = findings[findings["urgency"] == "Urgent"]
    expired       = findings[findings["urgency"] == "Expired"]

    print(f"\n{'='*60}")
    print(f"  AUDIT SUMMARY")
    print(f"{'='*60}")
    print(f"  Reimbursements reviewed:    {len(filtered)}")
    print(f"  Matched to sourcing cost:   {len(matched)}")
    print(f"  Underpaid cases found:      {len(findings)}")
    print(f"  Total potential underpaid:  ${total_delta:,.2f}")
    print(f"  High-confidence cases:      {len(high_conf)}")
    print(f"  Critical deadline (<7d):    {len(critical)}")
    print(f"  Urgent deadline (<14d):     {len(urgent)}")
    print(f"  Expired (>60d):             {len(expired)}")
    print(f"{'='*60}")
    print(f"  Findings saved to: {args.output}")
    print(f"{'='*60}\n")

    # Топ-5 по дельте
    if len(findings) > 0:
        print("  TOP FINDINGS BY DELTA:")
        top = findings.nlargest(min(5, len(findings)), "delta")[
            ["case_id", "sku", "fnsku", "delta", "days_left", "urgency", "confidence"]
        ]
        print(top.to_string(index=False))
        print()

    # Unmatched SKUs
    if len(unmatched) > 0:
        unmatched_skus = unmatched[join_key].dropna().unique()
        print(f"  UNMATCHED SKUs (нет sourcing cost в registry):")
        for s in unmatched_skus[:20]:
            print(f"    - {s}")
        if len(unmatched_skus) > 20:
            print(f"    ... и ещё {len(unmatched_skus) - 20}")
        print()


if __name__ == "__main__":
    main()
