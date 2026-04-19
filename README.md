# AMP Discord ChatOps Engine

A Discord bot that lets you manage AMP game servers on a Raspberry Pi directly from Discord. Includes a live control panel, automatic inactivity shutdown via GPIO, and optional voice-triggered server provisioning.

---

## Features

- **Persistent Dashboard** — A live-updating embed with a dropdown and buttons to start, stop, and restart any AMP instance without leaving Discord
- **Smart Start** — Powers on the host PC via a GPIO pulse, waits for it to boot, then starts the selected server — all from one button
- **Inactivity Shutdown** — When all servers have been empty for 30 minutes, the bot stops them gracefully and powers off the host PC via GPIO
- **Crash Detection** — Posts an alert in your dashboard channel if a server goes offline unexpectedly, distinguishing crashes from intentional stops
- **Voice Provisioning** — Optionally wakes the host PC automatically when someone joins a designated Discord voice channel
- **Public IP Command** — Retrieves the host PC's current public IP, visible only to the user who ran the command
- **Role-Gated Access** — Optionally lock dashboard controls to a specific Discord role

---

## Requirements

- Python 3.10+
- A Raspberry Pi with `gpioset` installed (`libgpiod-tools`)
- [AMP (Application Management Panel)](https://ampframework.net/) running on your host PC
- A Discord bot with the following intents enabled in the Developer Portal:
  - **Server Members Intent** (required for role-based access control)
  - **Voice State Intent** (enabled automatically by the bot)

### Python Dependencies

```
discord.py
aiohttp
python-dotenv
```

Install with:

```bash
pip install discord.py aiohttp python-dotenv
```

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Atom5233/AMP-game-management-Discord-bot
cd AMP-game-management-Discord-bot
```

### 2. Create your `.env` file

Copy the example file and fill in your values:

```bash
cp .env.example .env
nano .env
```

See the [Configuration](#configuration) section below for a description of every variable.

### 3. Enable the required Discord intents

Go to the [Discord Developer Portal](https://discord.com/developers/applications), select your application, navigate to the **Bot** tab, and enable:

- **Server Members Intent**

### 4. Invite the bot to your server

In the Developer Portal, go to **OAuth2 > URL Generator**. Select the `bot` and `applications.commands` scopes, then grant the following permissions: `Send Messages`, `Embed Links`, `Read Message History`. Use the generated URL to invite the bot.

### 5. Run the bot

```bash
python amp_bot.py
```

The bot will log in, attempt to discover your AMP instances automatically, and sync its slash commands.

### 6. Spawn the dashboard

In Discord, run `/spawn_dashboard` in the channel where you want the control panel to appear. The dashboard will stay in that channel and update automatically every 2 minutes. This command is admin-only.

---

## Configuration

All settings are loaded from the `.env` file. Never commit this file to version control — add it to your `.gitignore`.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Your Discord bot token |
| `AMP_URL` | Yes | Base URL of your AMP panel, e.g. `http://192.168.1.10:8080` |
| `AMP_USER` | Yes | AMP login username |
| `AMP_PASSWORD` | Yes | AMP login password |
| `DASHBOARD_CHANNEL_ID` | Yes | ID of the channel where the dashboard and alerts will be posted |
| `ALLOWED_ROLE_ID` | No | ID of the role required to use dashboard buttons. Set to `0` to allow all users |
| `VOICE_LOBBY_ID` | No | ID of the voice channel that triggers auto power-on when joined. Set to `0` to disable |
| `GPIO_PIN` | No | GPIO pin number wired to the PC power button header |
| `GPIOSET_PATH` | No | Full path to the `gpioset` binary. Defaults to `/usr/bin/gpioset` |
| `WATCH_INTERVAL` | No | How often (in seconds) to poll server status. Defaults to `120` — keep this high on a Pi 1B |
| `IDLE_TIMEOUT` | No | How long (in seconds) all servers must be empty before the PC is shut down. Defaults to `1800` (30 minutes) |

---

## GPIO Wiring

The bot shuts down the host PC by sending a momentary GPIO pulse that simulates a physical power button press.

1. Connect a GPIO pin on the Pi to the two power button header pins on your PC motherboard — the same connector your case power button plugs into
2. Set `GPIO_PIN` in your `.env` to the pin number you used
3. Make sure the Pi and PC share a common ground

> **Important:** Your PC's power button action must be set to **Shut down** rather than Sleep or Hibernate, otherwise the GPIO pulse will put it to sleep instead of turning it off. On Windows, this is under **Control Panel > Power Options > Choose what the power buttons do**.

---

## Slash Commands

| Command | Who Can Use It | Description |
|---|---|---|
| `/spawn_dashboard` | Admins only | Posts the persistent control panel in the current channel |
| `/refresh_servers` | Admins only | Re-discovers AMP instances from the panel without restarting the bot |
| `/status` | Everyone | Shows the current status and player count of all servers |
| `/myip` | Everyone | Shows the host PC's public IP address, visible only to you |

---

## Fallback Server List

On startup, the bot tries to discover your AMP instances automatically. If that fails (for example, if AMP is unreachable), it falls back to the `FALLBACK_SERVERS` list defined near the top of `amp_bot.py`. Edit this to match at least one of your servers so the bot remains usable if discovery fails:

```python
FALLBACK_SERVERS = {
    "minecraft": {
        "label":       "Minecraft",
        "instance_id": "your-instance-id-here",
        "emoji":       None,
    },
}
```

The instance ID is the first 8 characters of the AMP instance UUID. You can find this in the AMP web panel under the instance settings.

---

## Running on Boot (Raspberry Pi)

To start the bot automatically whenever the Pi boots, set up a systemd service:

```bash
sudo nano /etc/systemd/system/chatops.service
```

Paste the following, then save and exit:

```ini
[Unit]
Description=AMP Discord ChatOps Engine
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/AMP-game-management-Discord-bot
ExecStart=/usr/bin/python3 /home/pi/AMP-game-management-Discord-bot/amp_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable chatops
sudo systemctl start chatops
```

To check the bot's logs in real time:

```bash
sudo journalctl -u chatops -f
```

---

## License

MIT
