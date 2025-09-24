## 0) Prereqs (Ubuntu 22.04, Python 3.11.13)

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv
python3.11 --version
```

## 1) Create bot + token

* In Telegram, talk to **@BotFather** → `/newbot` → get your **bot token**.
* Get **your Telegram user ID** from **@userinfobot**.

## 2) Install the app

```bash
git clone <your-repo-or-scp> telewatch
cd telewatch
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
nano .env
# Paste TELEGRAM_BOT_TOKEN and your ALLOWED_USER_IDS
```

## 3) Run it (foreground)

```bash
source .venv/bin/activate
python main.py
```

Send `/start` to your bot—should reply with commands.

## 4) Run as a service (recommended)

Create `telewatch.service` in this folder (already included), then:

```bash
sudo cp telewatch.service /etc/systemd/system/
sudo sed -i "s|/path/to/telewatch|$PWD|g" /etc/systemd/system/telewatch.service
sudo systemctl daemon-reload
sudo systemctl enable --now telewatch
sudo systemctl status telewatch
```

### telewatch.service

```ini
[Unit]
Description=TeleWatch personal bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/telewatch
ExecStart=/path/to/telewatch/.venv/bin/python /path/to/telewatch/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

# Optional: environment file if you prefer not to use .env
# EnvironmentFile=/path/to/telewatch/telewatch.env

[Install]
WantedBy=multi-user.target
```

---

# Usage examples

* Add the 72-minute alarm aligned to your example:

```
/add_alarm "Please check 55's network" 2025-09-23T16:10:00Z 72
```

* List alarms:

```
/list_alarms
```

* Disable / enable:

```
/disable_alarm 1
/enable_alarm 1
```

* Add endpoint check (every 20 min, 3s timeout, warn if >2s):

```
/add_endpoint https://example.com/health 20 3.0 2.0
```

* List endpoints:

```
/list_endpoints
```

* Delete:

```
/delete_endpoint 1
```

---

# Notes & rationale

* **No Docker**, runs via **systemd**.
* **Long polling** (no webhook/cert hassle).
* **UTC everywhere**: alarms compute the *next* fire as `last_trigger + n * interval >= now`.
* **Coalesce + misfire grace**: prevents spam after downtime.
* **Simple RBAC**: `ALLOWED_USER_IDS` whitelist (for personal use).
* **SQLite** with SQLAlchemy async; tables auto-created on first run.
* **Extend later**: you can add more fields (e.g., retries, alert quiet hours) without changing architecture.
