#!/usr/bin/env python3
"""
FBA Audit — Auto Prepare
-------------------------
Кидаешь любые файлы клиента в папку drop/
Скрипт сам определяет что есть что, готовит data/ и запускает парсер.

Запуск:
    venv/bin/python auto_prepare.py --client "Имя клиента"

Поддерживает:
    - Amazon FBA Reimbursement Report (.csv / .txt / .xlsx)
    - Amazon Manage Your Sourcing Cost export (.csv / .xlsx)
    - Инвойсы поставщика в CSV/Excel (интерактивный маппинг колонок)
"""

import os
import sys
import argparse
import subprocess
import pandas as pd


DROP_DIR   = "drop"
DATA_DIR   = "data"
OUTPUT_DIR = "output"


# ─── СИГНАТУРЫ ФАЙЛОВ ────────────────────────────────────────────────────────

# Если в файле есть ≥2 колонки из этого списка — это reimbursement report
REIMB_SIGNATURES = {
    "reimbursement-id", "reimbursementid", "reimbursement_id",
    "approval-date", "approval_date",
    "quantity-reimbursed-total", "quantity_reimbursed_total",
    "amount-total", "amount_total",
}

# Если в файле есть ≥2 колонки из этого списка — это Amazon sourcing cost export
SOURCING_SIGNATURES = {
    "sourcing-cost", "sourcing_cost", "sourcingcost",
    "manufacturing-cost", "manufacturing_cost",
    "manage your sourcing cost",
    "cost-per-unit", "cost_per_unit",
}

# Ключевые слова в названиях колонок инвойса
INVOICE_COST_KEYWORDS   = ["unit price", "unit cost", "price", "cost", "цена", "стоимость", "rate"]
INVOICE_SKU_KEYWORDS    = ["fnsku", "sku", "item", "article", "product", "код", "артикул", "item#", "item no"]
INVOICE_QTY_KEYWORDS    = ["qty", "quantity", "units", "кол-во", "количество"]


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def load_file(path):
    """Читает CSV, TSV или Excel в DataFrame."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in [".xlsx", ".xls"]:
            return pd.read_excel(path, dtype=str)
        elif ext in [".txt", ".tsv"]:
            return pd.read_csv(path, sep="\t", encoding="utf-8-sig", dtype=str)
        else:
            df = pd.read_csv(path, sep=",", encoding="utf-8-sig", dtype=str)
            if len(df.columns) == 1:
                df = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
            return df
    except Exception as e:
        print(f"  [!] Не удалось прочитать {os.path.basename(path)}: {e}")
        return None


def normalize_cols(df):
    """Нормализует имена колонок: lowercase, убирает пробелы."""
    df.columns = [c.strip().lower().replace(" ", "_").replace("-", "_") for c in df.columns]
    return df


def cols_match(df_cols, signatures, threshold=2):
    """Проверяет сколько колонок из signatures есть в файле."""
    normalized = {c.strip().lower().replace("-", "").replace("_", "").replace(" ", "") for c in df_cols}
    sig_norm   = {s.replace("-", "").replace("_", "").replace(" ", "") for s in signatures}
    return len(normalized & sig_norm) >= threshold


def find_col(df_cols, keywords):
    """Ищет колонку по ключевым словам — сначала точные совпадения, потом подстроки."""
    cols_lower = [c.lower() for c in df_cols]
    # 1. Exact match
    for kw in keywords:
        for i, col_l in enumerate(cols_lower):
            if col_l == kw:
                return df_cols[i]
    # 2. Substring match
    for kw in keywords:
        for i, col_l in enumerate(cols_lower):
            if kw in col_l:
                return df_cols[i]
    return None


NON_INTERACTIVE = False  # set to True by --non-interactive flag


def pick_column(df, keywords, label):
    """
    Пытается найти колонку автоматически.
    Если не находит — показывает список и спрашивает у пользователя.
    В non-interactive режиме возвращает None если не нашёл.
    """
    auto = find_col(list(df.columns), keywords)
    if auto:
        print(f"    Авто-определена '{label}': {auto}")
        return auto

    print(f"\n    Не удалось авто-определить колонку '{label}'.")
    print(f"    Доступные колонки:")
    for i, col in enumerate(df.columns):
        sample = df[col].dropna().iloc[0] if len(df[col].dropna()) > 0 else "—"
        print(f"      {i+1}. {col}  (пример: {sample})")

    if NON_INTERACTIVE:
        print(f"    [non-interactive] Колонка '{label}' не определена автоматически — пропускаю.")
        return None

    while True:
        try:
            idx = int(input(f"    Введи номер колонки для '{label}': ")) - 1
            if 0 <= idx < len(df.columns):
                return df.columns[idx]
        except (ValueError, KeyboardInterrupt):
            pass
        print("    Неверный ввод, попробуй снова.")


# ─── ДЕТЕКТОРЫ ───────────────────────────────────────────────────────────────

def detect_file_type(path, df):
    """Определяет тип файла по сигнатурам колонок."""
    cols = list(df.columns)

    if cols_match(cols, REIMB_SIGNATURES, threshold=2):
        return "reimbursement"

    # Sourcing cost: достаточно 1 явной колонки
    if cols_match(cols, SOURCING_SIGNATURES, threshold=1):
        return "sourcing_cost"

    # Если есть что-то похожее на цену + SKU — инвойс
    has_cost = find_col(cols, INVOICE_COST_KEYWORDS) is not None
    has_sku  = find_col(cols, INVOICE_SKU_KEYWORDS) is not None
    if has_cost and has_sku:
        return "invoice"

    return "unknown"


# ─── ОБРАБОТЧИКИ ─────────────────────────────────────────────────────────────

def process_reimbursement(path, df):
    """Сохраняет reimbursement report как data/reimbursements.csv."""
    out = os.path.join(DATA_DIR, "reimbursements.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"    ✓ Reimbursement report → {out}  ({len(df)} строк)")
    return out


def process_sourcing_cost(path, df):
    """
    Парсит Amazon Sourcing Cost export.
    Ищет колонки FNSKU/SKU и unit cost, сохраняет как cost_registry.csv.
    """
    df = normalize_cols(df)
    print(f"    Колонки: {list(df.columns)}")

    fnsku_col = pick_column(df, INVOICE_SKU_KEYWORDS, "FNSKU или SKU")
    cost_col  = pick_column(df, INVOICE_COST_KEYWORDS, "unit cost (закупочная цена)")

    # Опционально: confidence, invoice_ref
    registry = pd.DataFrame()
    registry["fnsku"]     = df[fnsku_col].str.strip()
    registry["unit_cost"] = pd.to_numeric(
        df[cost_col].astype(str)
        .str.replace(r"[^\d.,]", "", regex=True)
        .str.replace(",", "."),
        errors="coerce"
    )
    registry["currency"]   = "USD"
    registry["confidence"] = "Medium"
    registry["invoice_ref"] = ""
    registry = registry[registry["unit_cost"] > 0].dropna(subset=["fnsku"])

    out = os.path.join(DATA_DIR, "cost_registry.csv")
    registry.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"    ✓ Sourcing cost export → {out}  ({len(registry)} записей)")
    return out


def process_invoice(path, df, existing_registry=None):
    """
    Парсит инвойс поставщика (CSV/Excel).
    Интерактивно маппит колонки.
    Добавляет к существующему cost_registry если он уже есть.
    """
    df = normalize_cols(df)
    fname = os.path.basename(path)
    print(f"\n    Файл: {fname}")
    print(f"    Колонки: {list(df.columns)}")
    print(f"    Строк: {len(df)}")
    print(f"    Первые 3 строки:")
    print(df.head(3).to_string(index=False))
    print()

    fnsku_col = pick_column(df, INVOICE_SKU_KEYWORDS, "FNSKU или SKU")
    cost_col  = pick_column(df, INVOICE_COST_KEYWORDS, "unit cost (цена за единицу)")

    if fnsku_col is None or cost_col is None:
        print(f"    [!] Не удалось определить колонки SKU/cost — файл пропущен.")
        return existing_registry

    # Спросим про invoice_ref
    if NON_INTERACTIVE:
        inv_num = ""
    else:
        inv_num = input(f"    Номер инвойса (для invoice_ref, Enter = пропустить): ").strip()

    registry = pd.DataFrame()
    registry["fnsku"]      = df[fnsku_col].astype(str).str.strip()
    registry["unit_cost"]  = pd.to_numeric(
        df[cost_col].astype(str)
        .str.replace(r"[^\d.,]", "", regex=True)
        .str.replace(",", "."),
        errors="coerce"
    )
    registry["currency"]    = "USD"
    registry["confidence"]  = "High"
    registry["invoice_ref"] = inv_num

    registry = registry[registry["unit_cost"] > 0].dropna(subset=["fnsku"])
    registry = registry[registry["fnsku"] != "nan"]

    if existing_registry is not None:
        # Обновляем: новые записи добавляем, существующие перезаписываем
        existing_registry = existing_registry[~existing_registry["fnsku"].isin(registry["fnsku"])]
        registry = pd.concat([existing_registry, registry], ignore_index=True)

    out = os.path.join(DATA_DIR, "cost_registry.csv")
    registry.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"    ✓ Инвойс обработан → {out}  ({len(registry)} записей суммарно)")
    return registry


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FBA Audit — Auto Prepare")
    parser.add_argument("--client",    default="Client", help="Имя клиента")
    parser.add_argument("--days",      type=int, default=60)
    parser.add_argument("--min-delta", type=float, default=10.0)
    parser.add_argument("--no-run",         action="store_true", help="Только подготовить файлы, не запускать парсер")
    parser.add_argument("--email",          default="", help="Email клиента для логирования")
    parser.add_argument("--non-interactive", action="store_true", help="Без интерактивных вопросов (для email pipeline)")
    args = parser.parse_args()

    global NON_INTERACTIVE
    NON_INTERACTIVE = args.non_interactive

    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  FBA Audit — Auto Prepare")
    print(f"  Клиент: {args.client}")
    if args.email:
        print(f"  Email:   {args.email}")
    print(f"{'='*60}")

    # Найти все файлы в drop/
    files = [
        os.path.join(DROP_DIR, f) for f in os.listdir(DROP_DIR)
        if f.lower().endswith((".csv", ".xlsx", ".xls", ".txt", ".tsv"))
        and not f.startswith("~")  # Excel временные файлы
    ]

    if not files:
        print(f"\n[!] Папка drop/ пуста.")
        print(f"    Положи туда файлы клиента и запусти снова.")
        sys.exit(0)

    print(f"\nФайлы в drop/: {len(files)}")

    reimb_path   = None
    registry_df  = None

    # Загрузить существующий cost_registry если есть
    existing_registry_path = os.path.join(DATA_DIR, "cost_registry.csv")
    if os.path.exists(existing_registry_path):
        try:
            registry_df = pd.read_csv(existing_registry_path, encoding="utf-8-sig", dtype=str)
            registry_df["unit_cost"] = pd.to_numeric(registry_df["unit_cost"], errors="coerce")
            print(f"  Найден существующий cost_registry: {len(registry_df)} записей")
        except Exception:
            registry_df = None

    # Обработать каждый файл
    for path in files:
        fname = os.path.basename(path)
        print(f"\n─── {fname}")
        df = load_file(path)
        if df is None or len(df) == 0:
            print(f"  [!] Пустой или нечитаемый файл, пропускаю")
            continue

        ftype = detect_file_type(path, df)
        print(f"  Тип определён: {ftype}")

        if ftype == "reimbursement":
            reimb_path = process_reimbursement(path, df)

        elif ftype == "sourcing_cost":
            process_sourcing_cost(path, df)

        elif ftype == "invoice":
            registry_df = process_invoice(path, df, existing_registry=registry_df)

        else:
            print(f"  [?] Не удалось определить тип файла.")
            print(f"  Колонки: {list(df.columns)}")
            print(f"  Переименуй файл или обработай вручную.")

    # Проверить что есть все нужные файлы
    cost_path = os.path.join(DATA_DIR, "cost_registry.csv")
    print(f"\n{'='*60}")
    print(f"  Итог подготовки:")
    print(f"  Reimbursements: {'✓ ' + reimb_path if reimb_path else '✗ не найден'}")
    print(f"  Cost Registry:  {'✓ ' + cost_path if os.path.exists(cost_path) else '✗ не найден'}")
    print(f"{'='*60}")

    if not reimb_path:
        print("\n[!] Нет reimbursement report. Парсер не запущен.")
        print("    Положи Amazon reimbursement report в drop/ и повтори.")
        sys.exit(1)

    if not os.path.exists(cost_path):
        print("\n[!] Нет cost registry. Парсер не запущен.")
        print("    Положи инвойс поставщика или sourcing cost export в drop/ и повтори.")
        sys.exit(1)

    if args.no_run:
        print("\nФлаг --no-run: парсер не запускается.")
        sys.exit(0)

    # Запустить парсер
    client_safe = args.client.replace(' ', '_')
    output_file = os.path.join(OUTPUT_DIR, f"findings_{client_safe}.csv")
    print(f"\nЗапускаю парсер...")
    cmd = [
        sys.executable, "reimbursement_parser.py",
        "--reimb",      reimb_path,
        "--costs",      cost_path,
        "--output",     output_file,
        "--client",     args.client,
        "--days",       str(args.days),
        "--min-delta",  str(args.min_delta),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Сгенерировать отчёт PDF + HTML
    if os.path.exists(output_file):
        print(f"\nГенерирую отчёт...")
        report_cmd = [
            sys.executable, "report_generator.py",
            "--findings", output_file,
            "--client",   args.client,
            "--market",   "US",
        ]
        subprocess.run(report_cmd)
    sys.exit(0)


if __name__ == "__main__":
    main()
