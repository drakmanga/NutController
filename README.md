# NutController

Dashboard web + bot Telegram per monitorare un UPS via [NUT (Network UPS Tools)](https://networkupstools.org/) e proteggere gli altri container di un host Proxmox durante i blackout.

Gira su un container Proxmox (LXC) dedicato con un UPS collegato via USB. Espone una dashboard web in tempo reale, invia notifiche Telegram sugli eventi di rete elettrica e, in caso di autonomia critica, spegne in ordine gli altri CT del nodo Proxmox per evitare shutdown non puliti.

![status](https://img.shields.io/badge/stato-uso%20personale%20%2F%20homelab-blue)

## Indice

- [Architettura](#architettura)
- [Requisiti](#requisiti)
- [Installazione](#installazione)
- [Configurazione](#configurazione)
- [Dashboard web](#dashboard-web)
- [Impostazioni guidate](#impostazioni-guidate)
  - [Collega Telegram](#collega-telegram)
  - [Collega NUT](#collega-nut)
- [Bot Telegram](#bot-telegram)
- [Logica di emergenza](#logica-di-emergenza)
- [File di stato e log](#file-di-stato-e-log)
- [Struttura del repository](#struttura-del-repository)
- [Sicurezza](#sicurezza)
- [Limiti noti](#limiti-noti)

## Architettura

```
UPS (USB) ──► NUT (usbhid-ups driver) ──► upsd ──► upsmon
                                             │           │
                                             │           └─► NOTIFYCMD ──► ups-notify.sh ──► Telegram
                                             │
                                             └─► ups-web.py (Flask, porta 80)
                                                    ├─ dashboard web
                                                    ├─ bot Telegram (/stats, /history)
                                                    └─ API di collegamento guidato (Telegram + NUT)

cron (ogni minuto) ──► ups-emergency.sh ──► SSH verso l'host Proxmox ──► spegne/riaccende gli altri CT
systemd (al boot)  ──► ups-boot-check.sh ──► notifica se il riavvio è avvenuto durante un blackout
```

Tutti i componenti girano sullo stesso CT NUT-dedicato (nell'installazione originale: CT Proxmox con NUT in modalità `standalone`, UPS collegato via USB in passthrough).

## Requisiti

- Un container/host Linux con NUT installato (`nut`, `nut-client` su Debian/Ubuntu) e un UPS supportato collegato via USB (o altro driver NUT).
- Python 3.9+ con `flask`, `requests`, `pyTelegramBotAPI` (`telebot`).
- Accesso SSH senza password (chiave) dal CT verso l'host Proxmox, se si vuole usare `ups-emergency.sh` per spegnere/riaccendere altri CT.
- Un bot Telegram (creato con [@BotFather](https://t.me/BotFather)) — il token si imposta comodamente dalla dashboard, vedi [Collega Telegram](#collega-telegram).

## Installazione

1. Installa NUT e configura il tuo UPS (vedi la sezione [Collega NUT](#collega-nut) per farlo dalla dashboard, oppure manualmente con `nut-scanner -U` per rilevare il dispositivo USB).
2. Copia gli script:
   ```bash
   cp bin/ups-web.py bin/ups-notify.sh bin/ups-boot-check.sh bin/ups-emergency.sh /usr/local/bin/
   chmod +x /usr/local/bin/ups-*.sh
   ```
3. Copia il config di esempio e valorizzalo (il token Telegram può restare vuoto, si imposta dalla dashboard):
   ```bash
   cp config/nutcontroller.conf.example /etc/nut/nutcontroller.conf
   ```
4. Installa le unit systemd:
   ```bash
   cp systemd/ups-web.service systemd/ups-boot-check.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now ups-web.service
   systemctl enable ups-boot-check.service   # oneshot, parte da solo al boot
   ```
5. Collega `ups-notify.sh` a `upsmon` in `/etc/nut/upsmon.conf`:
   ```
   NOTIFYCMD /usr/local/bin/ups-notify.sh
   NOTIFYFLAG ONLINE  SYSLOG+WALL+EXEC
   NOTIFYFLAG ONBATT  SYSLOG+WALL+EXEC
   NOTIFYFLAG LOWBATT SYSLOG+WALL+EXEC
   ```
6. (Opzionale) Aggiungi `ups-emergency.sh` a cron per la protezione degli altri CT:
   ```
   * * * * * /usr/local/bin/ups-emergency.sh
   ```
7. Apri `http://<ip-del-ct>/` e collega Telegram e NUT dai due pulsanti in alto a destra.

## Configurazione

Tutto il comportamento è centralizzato in `/etc/nut/nutcontroller.conf` (chiave=valore, vedi `config/nutcontroller.conf.example`):

| Chiave | Significato |
|---|---|
| `BOT_TOKEN`, `CHAT_ID` | Credenziali del bot Telegram. Si impostano da UI, non serve editare il file a mano. |
| `PROXMOX_HOST` | IP dell'host Proxmox da cui spegnere/riavviare gli altri CT in emergenza. |
| `NUT_CT_ID` | ID del CT che ospita NutController stesso (escluso dagli spegnimenti di emergenza). |
| `WEB_URL` | URL della dashboard, incluso nei messaggi Telegram. |
| `BATTERY_VOLTAGE`, `BATTERY_AH`, `BATTERY_EFFICIENCY`, `LOAD_WATTS` | Parametri della batteria, usati come fallback quando l'UPS non riporta dati diretti. |
| `BATTERY_RUNTIME_FULL` | Secondi di autonomia a batteria piena, riportati dall'UPS: usati per scalare la barra dell'autonomia in dashboard (0–100%). |
| `THRESHOLD_RUNTIME_LOW` | Sotto questa autonomia stimata (secondi) scatta lo spegnimento di emergenza degli altri CT. Deve stare sopra `battery.runtime.low` dell'UPS, altrimenti l'UPS forza il proprio shutdown (FSD) prima che gli altri CT vengano spenti in ordine. |
| `THRESHOLD_RUNTIME_RESTORE` | Sopra questa autonomia stimata, gli altri CT vengono riaccesi automaticamente al ripristino della corrente. |

`BOT_TOKEN`/`CHAT_ID` vengono riscritti automaticamente in questo file quando li imposti dal pulsante "Collega Telegram" della dashboard — il resto del file (commenti, altre chiavi) resta invariato.

## Dashboard web

`http://<ip-del-ct>/` (Flask, porta 80, servita da `ups-web.service`):

- Stato UPS in tempo reale (online / su batteria / batteria scarica / in carica) con autonomia stimata da `battery.runtime`.
- Carico, consumo stimato (`ups.realpower.nominal × load%`), uptime di sistema.
- Grafici storici (carica batteria, consumo) su intervalli da 1h a 1 anno, con export CSV.
- Storico blackout con statistiche (totale, durata media, evento peggiore, ultimo evento) e tabella ordinabile.
- Due pulsanti di impostazione in alto a destra, sotto l'orario di aggiornamento: **Collega Telegram** e **Collega NUT**.

## Impostazioni guidate

Entrambi i pulsanti in alto a destra sono rossi quando non collegati, verdi quando tutto funziona, e aprono una modale con la configurazione guidata — pensata per non dover mai editare i file di configurazione a mano.

### Collega Telegram

1. Crea un bot con [@BotFather](https://t.me/BotFather) su Telegram e copia il token.
2. In dashboard, clic su **Collega Telegram** → incolla il token → **Collega**.
3. Il server valida il token (`getMe`) e si mette in ascolto (`getUpdates`) per un massimo di 2 minuti.
4. Apri Telegram, cerca il bot appena creato e invia un messaggio qualsiasi (es. `/start`): il `chat_id` viene rilevato automaticamente, senza doverlo cercare a mano.
5. Compare il pulsante **Salva**: da quel momento token e chat id sono scritti in `nutcontroller.conf` e il bot dei comandi (`/stats`, `/history`) riparte con le nuove credenziali.

Il pulsante **Scollega** rimuove le credenziali e ferma il bot. Se cambi bot mentre uno è già collegato, il precedente resta attivo finché non premi "Salva" sul nuovo — annullando il collegamento in corso il bot originale riprende automaticamente.

### Collega NUT

Pensato per il caso "ho sostituito l'UPS o non ricordo come l'ho configurato": non serve editare `ups.conf` a mano né ricordare nome driver/porta.

1. Clic su **Collega NUT**: la modale mostra lo stato attuale (driver, porta, se l'UPS risponde).
2. **Rileva UPS collegate (USB)** lancia una scansione USB (`nut-scanner -U`), **in sola lettura**: non modifica nulla, elenca solo i dispositivi trovati con driver/porta suggeriti.
3. Selezioni il dispositivo rilevato (i valori sono precompilati ma modificabili) e premi **Salva e riavvia driver**.
4. Il server scrive `driver`/`port`/`desc` nella stanza `[myups]` di `/etc/nut/ups.conf` e riavvia **solo** `nut-driver@myups` (non tocca `upsd`/`upsmon`): qualche secondo di interruzione del monitoraggio, poi la dashboard torna verde se l'UPS risponde.

Setup NUT di riferimento di questa installazione (per chi deve ricostruirlo a mano): UPS collegato via USB, `driver = usbhid-ups`, `port = auto`, nome dispositivo `myups`, `nut.conf` in `MODE=standalone`, `upsd.users` con un utente `admin` in modalità `master` usato da `upsmon.conf` (`MONITOR myups@localhost 1 admin <password> master`).

## Bot Telegram

Comandi disponibili (rispondono solo al `chat_id` collegato):

- `/start`, `/help` — messaggio di benvenuto e link alla dashboard.
- `/stats` — stato, carica, autonomia, consumo attuali.
- `/history` — ultimi 20 blackout registrati.

Le notifiche automatiche (inizio/fine blackout, batteria scarica, ecc.) partono da `ups-notify.sh` (hook `NOTIFYCMD` di `upsmon`) e da `ups-boot-check.sh` (se il riavvio del CT stesso è avvenuto durante un blackout ancora in corso).

## Logica di emergenza

`ups-emergency.sh`, eseguito da cron ogni minuto:

- Se l'autonomia stimata (`battery.runtime`) scende sotto `THRESHOLD_RUNTIME_LOW`, oppure l'UPS segnala `LB` (low battery), spegne via SSH tutti i CT Proxmox tranne `NUT_CT_ID` (`pct shutdown`).
- Quando l'autonomia torna sopra `THRESHOLD_RUNTIME_RESTORE`, riaccende i CT spenti.
- Lo stato dell'emergenza è tracciato in `/var/lib/nut/emergency.state` per non ripetere azioni già eseguite.

`battery.charge` (la percentuale) di alcuni UPS aggiorna la stima "a scatti" e non è affidabile in tempo reale: per questo le soglie di emergenza usano `battery.runtime` (secondi), riportato direttamente dall'UPS, non un calcolo teorico Ah/W.

## File di stato e log

Tutti sotto `/var/lib/nut/` (esclusi dal repository, vedi `.gitignore`, perché sono stato runtime — non codice):

| File | Contenuto |
|---|---|
| `blackout.flag` | Timestamp Unix di inizio blackout, presente solo mentre un blackout è in corso. |
| `blackout.log` | Storico testuale di tutti gli eventi (`INIZIO`/`FINE blackout`), usato da dashboard e bot. |
| `metrics.jsonl` | Una riga JSON al minuto (carica batteria, carico%, consumo), alimenta i grafici storici. |
| `emergency.state` | Presente quando è attiva una procedura di emergenza (CT spenti in attesa di riaccensione). |

## Struttura del repository

```
nutcontroller/
├── bin/
│   ├── ups-web.py           # Dashboard Flask + bot Telegram + API di impostazione
│   ├── ups-notify.sh        # Hook NOTIFYCMD di upsmon: notifiche Telegram sugli eventi UPS
│   ├── ups-boot-check.sh    # Eseguito al boot: notifica se il riavvio è avvenuto durante un blackout
│   └── ups-emergency.sh     # Cron: spegne/riaccende gli altri CT Proxmox in base all'autonomia
├── systemd/
│   ├── ups-web.service
│   └── ups-boot-check.service
├── config/
│   └── nutcontroller.conf.example   # Template senza segreti: copiare in /etc/nut/nutcontroller.conf
├── .gitignore
└── README.md
```

## Sicurezza

- La dashboard e le sue API (incluse quelle di collegamento Telegram/NUT) **non hanno autenticazione**: pensata per una rete locale/fidata. Se esposta oltre la LAN, mettila dietro un reverse proxy con autenticazione o una VPN.
- `nutcontroller.conf` contiene il token del bot Telegram in chiaro: non committarlo (già escluso da `.gitignore`), e limita i permessi del file (`chmod 600`) se il CT è multi-utente.
- Lo spegnimento di emergenza (`ups-emergency.sh`) usa SSH verso l'host Proxmox: assicurati che la chiave usata abbia solo i permessi necessari (`pct shutdown`/`pct start`), non accesso root pieno se evitabile.

## Limiti noti

- `battery.charge` di alcuni UPS (in particolare i modelli con firmware "budget") resta fermo a 100% in float anche con batteria non del tutto carica: la dashboard mostra una stima più realistica basata sui dati storici post-blackout quando disponibile, ma il valore istantaneo va preso con cautela.
- Il riavvio del driver da "Collega NUT" interrompe per pochi secondi la lettura dell'UPS: se in quella finestra si verifica un blackout reale, l'evento potrebbe non essere registrato correttamente.
- Un solo dispositivo UPS (`myups`) è supportato dalla dashboard; installazioni con più UPS richiedono modifiche manuali a `ups.conf`.
