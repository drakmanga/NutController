#!/bin/bash
source /etc/nut/nutcontroller.conf

STATE_FILE="/var/lib/nut/emergency.state"

send_message() {
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d text="${1}" \
    -d parse_mode="Markdown"
}

get_ct_list() {
  ssh root@${PROXMOX_HOST} "pct list" | awk 'NR>1 {print $1}' | grep -v "^${NUT_CT_ID}$"
}

shutdown_all_ct() {
  echo "shutdown" > "$STATE_FILE"
  send_message "🔴 *Emergenza UPS!*
Autonomia stimata: ${1} minuti.
Spegnimento di tutti i CT in corso..."
  for CTID in $(get_ct_list); do
    STATUS=$(ssh root@${PROXMOX_HOST} "pct status ${CTID}" | awk '{print $2}')
    if [ "$STATUS" = "running" ]; then
      ssh root@${PROXMOX_HOST} "pct shutdown ${CTID}"
    fi
  done
  send_message "✅ Tutti i CT spenti. NutController attivo in modalità emergenza."
}

restore_all_ct() {
  rm -f "$STATE_FILE"
  ssh root@${PROXMOX_HOST} "bash /usr/local/bin/restore-cts.sh > /dev/null 2>&1 &"
  send_message "🟢 *Corrente ripristinata, autonomia ${1} minuti!*
Tutti i CT sono stati riavviati."
}

UPS_STATUS=$(upsc myups ups.status 2>/dev/null)
RUNTIME=$(upsc myups battery.runtime 2>/dev/null)
RUNTIME=${RUNTIME%.*}
[[ "$RUNTIME" =~ ^[0-9]+$ ]] || RUNTIME=99999
RUNTIME_MIN=$(( RUNTIME / 60 ))

EMERGENCY_ACTIVE=false
[ -f "$STATE_FILE" ] && EMERGENCY_ACTIVE=true

# Scatta al superamento della soglia di runtime (con margine rispetto al taglio
# hardware battery.runtime.low, cosi' i CT vengono spenti PRIMA che l'UPS forzi
# lo shutdown di NutController) oppure subito se l'UPS segnala gia' "LB" (batteria
# scarica) per non dipendere unicamente da una stima di runtime che puo' saltare
# bruscamente da un valore alto a uno basso tra due letture.
IS_LB=false
[[ "$UPS_STATUS" == *"LB"* ]] && IS_LB=true

if [[ "$UPS_STATUS" == *"OB"* ]] && { [ "$IS_LB" = true ] || [ "$RUNTIME" -le "$THRESHOLD_RUNTIME_LOW" ]; }; then
  if [ "$EMERGENCY_ACTIVE" = false ]; then
    shutdown_all_ct "$RUNTIME_MIN"
  fi
elif [[ "$UPS_STATUS" == *"OL"* ]] && [ "$RUNTIME" -ge "$THRESHOLD_RUNTIME_RESTORE" ]; then
  if [ "$EMERGENCY_ACTIVE" = true ]; then
    restore_all_ct "$RUNTIME_MIN"
  fi
fi
