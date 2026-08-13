#!/bin/bash
source /etc/nut/nutcontroller.conf

FLAG_FILE="/var/lib/nut/blackout.flag"
LOG_FILE="/var/lib/nut/blackout.log"

NET_OK=false
for i in $(seq 1 30); do
  if curl -s --max-time 5 https://api.telegram.org > /dev/null 2>&1; then
    NET_OK=true
    break
  fi
  sleep 10
done

# Senza rete non possiamo notificare: usciamo senza toccare il flag,
# cosi' resta intatto per ups-notify.sh (blackout eventualmente ancora in corso).
if [ "$NET_OK" = false ]; then
  exit 1
fi

UPS_STATUS=$(upsc myups ups.status 2>/dev/null)

if [ -f "$FLAG_FILE" ] && [[ "$UPS_STATUS" == *"OL"* ]]; then
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
  START_STR=$(date -d @"$START" '+%H:%M')
  END_STR=$(date -d @"$END" '+%H:%M')
  if curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="⚡ *Sistema riavviato dopo blackout!*

🕐 *Interruzione:* dalle ${START_STR} alle ${END_STR}
⏱ *Durata:* ${DURATION_STR}
🖥 Il CT NutController è tornato online.

🖥 ${WEB_URL}" \
    -d parse_mode="Markdown" > /dev/null; then
    rm -f "$FLAG_FILE"
    echo "$(date -d @"$END" '+%Y-%m-%d %H:%M:%S') | FINE blackout | durata: ${DURATION_STR}" >> "$LOG_FILE"
  else
    echo "$(date -d @"$END" '+%Y-%m-%d %H:%M:%S') | FINE blackout | durata: ${DURATION_STR} (invio Telegram fallito, flag mantenuto)" >> "$LOG_FILE"
  fi
elif [ -f "$FLAG_FILE" ]; then
  # Il blackout che ha causato il riavvio non e' ancora finito (UPS ancora OB):
  # non tocchiamo il flag, sara' ups-notify.sh a chiuderlo al vero ONLINE.
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="⚡ *Sistema riavviato*, ma il blackout risulta ancora in corso (UPS: ${UPS_STATUS})." \
    -d parse_mode="Markdown" > /dev/null
else
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="🟢 *NutController avviato*
Il monitoraggio UPS è attivo." \
    -d parse_mode="Markdown" > /dev/null
fi
