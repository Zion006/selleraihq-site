#!/bin/bash
# Одна команда для запуска полного аудита
# Использование: ./audit.sh "Имя клиента"

CLIENT="${1:-Client}"

echo ""
echo "========================================"
echo "  FBA Underpaid Reimbursement Audit"
echo "  Клиент: $CLIENT"
echo "========================================"
echo ""
echo "Положи файлы клиента в папку drop/ и нажми Enter..."
read

venv/bin/python auto_prepare.py --client "$CLIENT"
