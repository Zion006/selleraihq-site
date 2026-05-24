#!/bin/bash
# Проверка статуса FBA Audit Watcher

echo "═══════════════════════════════════════"
echo "  FBA Audit Watcher — статус"
echo "═══════════════════════════════════════"

# Статус сервиса
STATUS=$(systemctl --user is-active fba-watcher 2>/dev/null)
if [ "$STATUS" = "active" ]; then
    echo "  Сервис:    ✅ РАБОТАЕТ ($STATUS)"
else
    echo "  Сервис:    ❌ НЕ РАБОТАЕТ ($STATUS)"
fi

# PID и время работы
PID=$(systemctl --user show fba-watcher --property=MainPID --value 2>/dev/null)
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
    UPTIME=$(ps -o etime= -p $PID 2>/dev/null | tr -d ' ')
    echo "  PID:       $PID (работает $UPTIME)"
fi

# Включён ли автозапуск
ENABLED=$(systemctl --user is-enabled fba-watcher 2>/dev/null)
echo "  Автозапуск: $([ "$ENABLED" = "enabled" ] && echo "✅ включён" || echo "❌ выключен")"

echo ""
echo "─── Последние записи лога ───"
LOG=/home/admin1/fba-audit/parser/email_watcher.log
if [ -f "$LOG" ]; then
    tail -15 "$LOG"
else
    echo "  Лог пуст"
fi

echo ""
echo "─── Отчёты в output/ ───"
ls -lht /home/admin1/fba-audit/parser/output/*.pdf 2>/dev/null | head -5 || echo "  Нет PDF отчётов"

echo ""
echo "─── Управление ───"
echo "  Остановить:    systemctl --user stop fba-watcher"
echo "  Запустить:     systemctl --user start fba-watcher"
echo "  Перезапустить: systemctl --user restart fba-watcher"
echo "  Полный лог:    journalctl --user -u fba-watcher -f"
echo "═══════════════════════════════════════"
