#!/bin/bash
source /etc/nut/nutcontroller.conf

FLAG_FILE="/var/lib/nut/blackout.flag"
LOG_FILE="/var/lib/nut/blackout.log"

send_message() {
  local msg="$1"
  for i in $(seq 1 30); do
    if curl -s --max-time 5 https://api.telegram.org > /dev/null 2>&1; then
      curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" \
        -d text="${msg}" \
        -d parse_mode="Markdown"
      return 0
    fi
    sleep 10
  done
  return 1
}

case "$NOTIFYTYPE" in
  ONLINE)   PREFIX="✅" ;;
  ONBATT)   PREFIX="⚠️" ;;
  LOWBATT)  PREFIX="🔴" ;;
  FSD)      PREFIX="💀" ;;
  SHUTDOWN) PREFIX="💀" ;;
  REPLBATT) PREFIX="🔧" ;;
  COMMBAD)  PREFIX="📵" ;;
  COMMOK)   PREFIX="📶" ;;
  *)        PREFIX="ℹ️" ;;
esac

if [ "$NOTIFYTYPE" = "ONBATT" ]; then
  echo "$(date +%s)" > "$FLAG_FILE"
  echo "$(date '+%Y-%m-%d %H:%M:%S') | INIZIO blackout" >> "$LOG_FILE"

elif [ "$NOTIFYTYPE" = "ONLINE" ]; then
  if [ -f "$FLAG_FILE" ]; then
    START=$(cat "$FLAG_FILE")
    END=$(date +%s)
    TOTAL_SEC=$(( END - START ))
    DURATION_H=$(( TOTAL_SEC / 3600 ))
    DURATION_M=$(( (TOTAL_SEC % 3600) / 60 ))
    DURATION_S=$(( TOTAL_SEC % 60 ))
    if [ "$DURATION_H" -gt 0 ]; then
      DURATION_STR="${DURATION_H}h ${DURATION_M}m ${DURATION_S}s"
    elif [ "$DURATION_M" -gt 0 ]; then
      DURATION_STR="${DURATION_M}m ${DURATION_S}s"
    else
      DURATION_STR="${DURATION_S}s"
    fi
    DATE_STR=$(date -d @"$START" '+%d/%m/%Y')
    START_STR=$(date -d @"$START" '+%H:%M')
    END_STR=$(date -d @"$END" '+%H:%M')

    if send_message "✅ *Corrente ripristinata!*

📅 *Data:* ${DATE_STR}
🕐 *Interruzione:* dalle ${START_STR} alle ${END_STR}
⏱ *Durata:* ${DURATION_STR}

🖥 ${WEB_URL}"; then
      rm -f "$FLAG_FILE"
      echo "$(date '+%Y-%m-%d %H:%M:%S') | FINE blackout | durata: ${DURATION_STR}" >> "$LOG_FILE"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') | FINE blackout | durata: ${DURATION_STR} (invio Telegram fallito, flag mantenuto)" >> "$LOG_FILE"
    fi
  else
    send_message "${PREFIX} *NUT UPS Alert*
📅 $(date '+%d/%m/%Y %H:%M')
Evento: *ONLINE*
${1}"
  fi

else
  send_message "${PREFIX} *NUT UPS Alert*
📅 $(date '+%d/%m/%Y %H:%M')
Evento: *${NOTIFYTYPE}*
${1}"
fi
