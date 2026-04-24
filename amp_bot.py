#!/usr/bin/env python3
"""
AMP-game-management-Discord-bot
Infrastructure controller for AMP game servers on a Raspberry Pi.
Includes: Persistent UI, Inactivity Shutdown, Voice Provisioning, GPIO out-of-band management.
"""

import os
import json
import asyncio
import logging
import urllib.parse
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN    = os.getenv("BOT_TOKEN")
AMP_URL      = os.getenv("AMP_URL", "http://192.168.0.232:8080")
AMP_USER     = os.getenv("AMP_USER")
AMP_PASSWORD = os.getenv("AMP_PASSWORD")

ALLOWED_ROLE_ID      = int(os.getenv("ALLOWED_ROLE_ID", 0))
DASHBOARD_CHANNEL_ID = int(os.getenv("DASHBOARD_CHANNEL_ID", 0))
VOICE_LOBBY_ID       = int(os.getenv("VOICE_LOBBY_ID", 0))  # VC channel ID to enable voice provisioning

GPIO_PIN     = os.getenv("GPIO_PIN", "18")
GPIOSET_PATH = os.getenv("GPIOSET_PATH", "/usr/bin/gpioset") 

WATCH_INTERVAL      = int(os.getenv("WATCH_INTERVAL", 120))  # Seconds — kept high for Pi 1B CPU
IDLE_TIMEOUT        = int(os.getenv("IDLE_TIMEOUT", 1800))   # Seconds of 0 players before PC is shut down
SHUTDOWN_GRACE_SECS = 30   # Wait for servers to stop cleanly before GPIO power-off
BUTTON_COOLDOWN     = 5    # Per-user button cooldown in seconds to prevent spam

STATE_FILE = "bot_state.json"  # Persists dashboard message ID across restarts

# Fallback server list — used if AMP instance discovery fails
FALLBACK_SERVERS = {
    "minecraft": {
        "label":       "Minecraft",
        "instance_id": "INSTANCE_ID_HERE",
        "emoji":       None,
    },
}

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("ChatOps")

logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# GLOBAL STATE
# ---------------------------------------------------------------------------

class BotState:
    def __init__(self):
        self.servers              = {}
        self.watched              = set()
        self.last_states          = {}
        self.intentional_stops    = set()   # Tracks servers stopped intentionally to suppress crash alerts
        self.global_idle_since    = None
        self.hardware_lock        = asyncio.Lock()
        self.dashboard_msg_id     = None
        self.button_cooldowns     = {}      # {user_id: last_press_timestamp}

    def save(self):
        """Persist bot state (currently just the dashboard message ID) to disk."""
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"dashboard_msg_id": self.dashboard_msg_id}, f)
        except Exception as e:
            log.error(f"Failed to save state: {e}")

    def load(self):
        """Load persisted state from disk if available."""
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                self.dashboard_msg_id = data.get("dashboard_msg_id")
                if self.dashboard_msg_id:
                    log.info(f"Loaded dashboard_msg_id {self.dashboard_msg_id} from state file.")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error(f"Failed to load state: {e}")

state = BotState()

def now() -> float:
    return datetime.now(timezone.utc).timestamp()

# AMP returns 10 or 20 for a running server depending on version
RUNNING_STATES = {10, 20}

STATE_LABEL = {0: "Offline", 5: "Starting", 10: "Online", 20: "Online"}

# ---------------------------------------------------------------------------
# PERSISTENT UI
# ---------------------------------------------------------------------------

class ServerDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=srv["label"], value=k)
            for k, srv in list(state.servers.items())[:25]
        ] if state.servers else [discord.SelectOption(label="No servers loaded", value="none")]

        super().__init__(
            placeholder="Select a game server...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="persistent_server_select"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected_server = self.values[0]


class DashboardView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot
        self.selected_server = None
        self.add_item(ServerDropdown())

    async def verify(self, interaction: discord.Interaction) -> bool:
        if ALLOWED_ROLE_ID != 0 and not any(r.id == ALLOWED_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return False
        if not self.selected_server or self.selected_server == "none":
            await interaction.response.send_message("Please select a server from the dropdown first.", ephemeral=True)
            return False
        if self.selected_server not in state.servers:
            await interaction.response.send_message("Server not found. Try `/refresh_servers`.", ephemeral=True)
            return False
        return True

    async def check_cooldown(self, interaction: discord.Interaction) -> bool:
        """Returns True if the user may press a button, False if still on cooldown."""
        uid = interaction.user.id
        last = state.button_cooldowns.get(uid, 0)
        if now() - last < BUTTON_COOLDOWN:
            remaining = int(BUTTON_COOLDOWN - (now() - last)) + 1
            await interaction.response.send_message(
                f"Please wait {remaining}s before pressing another button.", ephemeral=True
            )
            return False
        state.button_cooldowns[uid] = now()
        return True

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, custom_id="btn_start")
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.verify(interaction):
            return
        if not await self.check_cooldown(interaction):
            return
        label = state.servers[self.selected_server]["label"]
        await interaction.response.send_message(f"Sending start command to **{label}**...", ephemeral=True)
        amp_cog = self.bot.get_cog("AMPInterface")
        ok, _ = await amp_cog.execute_action(state.servers[self.selected_server]["instance_id"], "Core/Start")
        if not ok:
            await interaction.followup.send(f"⚠️ Start command failed for **{label}**. Check AMP connectivity.", ephemeral=True)
            return
        state.watched.add(self.selected_server)
        state.intentional_stops.discard(self.selected_server)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.verify(interaction):
            return
        if not await self.check_cooldown(interaction):
            return
        label = state.servers[self.selected_server]["label"]
        await interaction.response.send_message(f"Sending stop command to **{label}**...", ephemeral=True)
        amp_cog = self.bot.get_cog("AMPInterface")
        state.intentional_stops.add(self.selected_server)
        ok, _ = await amp_cog.execute_action(state.servers[self.selected_server]["instance_id"], "Core/Stop")
        if not ok:
            state.intentional_stops.discard(self.selected_server)
            await interaction.followup.send(f"⚠️ Stop command failed for **{label}**. Check AMP connectivity.", ephemeral=True)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.secondary, custom_id="btn_restart")
    async def btn_restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.verify(interaction):
            return
        if not await self.check_cooldown(interaction):
            return
        label = state.servers[self.selected_server]["label"]
        await interaction.response.send_message(f"Sending restart command to **{label}**...", ephemeral=True)
        amp_cog = self.bot.get_cog("AMPInterface")
        state.intentional_stops.add(self.selected_server)
        ok, _ = await amp_cog.execute_action(state.servers[self.selected_server]["instance_id"], "Core/Restart")
        if not ok:
            state.intentional_stops.discard(self.selected_server)
            await interaction.followup.send(f"⚠️ Restart command failed for **{label}**. Check AMP connectivity.", ephemeral=True)
            return
        state.watched.add(self.selected_server)

    @discord.ui.button(label="Smart Start", style=discord.ButtonStyle.primary, custom_id="btn_smartstart")
    async def btn_smartstart(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Powers on the host PC via GPIO, waits for AMP to wake up, then starts the selected server."""
        if not await self.verify(interaction):
            return
        if not await self.check_cooldown(interaction):
            return
            
        await interaction.response.send_message("Triggering host PC power via GPIO... waiting for AMP to boot.", ephemeral=True)
        hw_cog  = self.bot.get_cog("HardwareOps")
        amp_cog = self.bot.get_cog("AMPInterface")
        label = state.servers[self.selected_server]["label"]
        
        # Step 1: Send the power pulse
        await hw_cog.trigger_pc_power()
        
        # Step 2: Dynamic wait for AMP (up to 3 minutes)
        parsed_url = urllib.parse.urlparse(AMP_URL)
        host_ip    = parsed_url.hostname
        host_port  = parsed_url.port or 8080
        
        amp_awake  = False
        start_time = now()
        while now() - start_time < 180:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host_ip, host_port), timeout=1.0)
                writer.close()
                await writer.wait_closed()
                amp_awake = True
                break
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
                await asyncio.sleep(5)
                
        if not amp_awake:
            await interaction.followup.send(
                f"⚠️ Sent the power pulse, but the Host PC ({host_ip}) never woke up. "
                "Please check the hardware connection.", ephemeral=True
            )
            return
            
        # Step 3: AMP is awake — give it a moment to fully initialize then start the server
        await asyncio.sleep(5)
        ok, _ = await amp_cog.execute_action(state.servers[self.selected_server]["instance_id"], "Core/Start")
        if not ok:
            await interaction.followup.send(
                f"⚠️ Host PC is awake, but the start command failed for **{label}**. "
                "Check AMP connectivity.", ephemeral=True
            )
            return
            
        state.watched.add(self.selected_server)
        state.intentional_stops.discard(self.selected_server)
        await interaction.followup.send(f"✅ Host PC is awake! Start command sent to **{label}**.", ephemeral=True)

# ---------------------------------------------------------------------------
# COG: AMP API INTERFACE
# ---------------------------------------------------------------------------

class AMPInterface(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.http_session = None
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        self.instance_sessions = {}

    async def cog_load(self):
        # Increased timeout to 30 seconds for the Pi 1B to handle AMP discovery
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        loaded = await self.refresh_instances()
        if not loaded:
            log.warning("Instance discovery failed — loading fallback server list.")
            state.servers.update(FALLBACK_SERVERS)

    async def cog_unload(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

    async def refresh_instances(self) -> bool:
        """Attempts dynamic AMP instance discovery. Returns False on failure."""
        try:
            async with self.http_session.post(
                f"{AMP_URL}/API/Core/Login",
                json={
                    "username": AMP_USER, "password": AMP_PASSWORD,
                    "token": "", "rememberMeToken": "", "rememberMe": False
                },
                headers=self.headers
            ) as r:
                data = await r.json()
                if not data.get("success"):
                    log.warning("ADS login failed during instance discovery.")
                    return False
                ads_sid = data["sessionID"]

            async with self.http_session.post(
                f"{AMP_URL}/API/ADSModule/GetInstances",
                json={"sessionID": ads_sid},
                headers=self.headers
            ) as r:
                instances = await r.json()

            found = {}
            for controller in instances:
                for inst in controller.get("AvailableInstances", []):
                    name    = inst.get("FriendlyName") or inst.get("InstanceName", "")
                    inst_id = inst.get("InstanceID", "")[:8]
                    module  = inst.get("ModuleName", "")
                    if not name or not inst_id or name == "ADS01":
                        continue
                    key = name.lower().replace(" ", "_")
                    found[key] = {"label": name, "instance_id": inst_id, "module": module}

            if found:
                state.servers.clear()
                state.servers.update(found)
                log.info(f"Discovered {len(found)} instance(s) from AMP.")
                return True

            log.warning("GetInstances returned no usable instances.")
            return False

        except Exception as e:
            log.error(f"Instance discovery error: {e}")
            return False

    async def _get_instance_session(self, instance_id: str, force: bool = False):
        cached = self.instance_sessions.get(instance_id)
        if not force and cached and now() - cached["time"] < 300:
            return cached["sid"]
        try:
            async with self.http_session.post(
                f"{AMP_URL}/API/ADSModule/Servers/{instance_id}/API/Core/Login",
                json={
                    "username": AMP_USER, "password": AMP_PASSWORD,
                    "token": "", "rememberMeToken": "", "rememberMe": False
                },
                headers=self.headers
            ) as r:
                data = await r.json()
                if data.get("success"):
                    self.instance_sessions[instance_id] = {"sid": data["sessionID"], "time": now()}
                    return data["sessionID"]
        except Exception as e:
            log.error(f"Instance login error ({instance_id}): {e}")
        return None

    async def execute_action(self, instance_id: str, endpoint: str):
        for retry in (False, True):
            sid = await self._get_instance_session(instance_id, force=retry)
            if not sid:
                return False, {}
            try:
                async with self.http_session.post(
                    f"{AMP_URL}/API/ADSModule/Servers/{instance_id}/API/{endpoint}",
                    json={"sessionID": sid},
                    headers=self.headers
                ) as r:
                    data = await r.json()
                    if isinstance(data, dict) and data.get("Title") == "Unauthorized Access":
                        continue
                    return True, data
            except Exception as e:
                log.error(f"AMP action error ({endpoint}): {e}")
                await asyncio.sleep(1)
        return False, {}

    async def get_status(self, instance_id: str):
        ok, data = await self.execute_action(instance_id, "Core/GetStatus")
        if not ok:
            return None
        return {
            "state":   data.get("State", -1),
            "players": (data.get("Metrics") or {}).get("Active Users") or {},
            "uptime":  data.get("Uptime", ""),
        }

# ---------------------------------------------------------------------------
# COG: HARDWARE (GPIO)
# ---------------------------------------------------------------------------

class HardwareOps(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def trigger_pc_power(self) -> bool:
        async with state.hardware_lock:
            try:
                log.info("Triggering GPIO power pulse...")
                # Updated to use libgpiod v2.x syntax with the comma toggle
                p = await asyncio.create_subprocess_exec(
                    GPIOSET_PATH, "--chip", "0", "--toggle", "500ms,0", f"{GPIO_PIN}=1",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE
                )
                await p.communicate()
                return p.returncode == 0
            except Exception as e:
                log.error(f"GPIO error: {e}")
                return False

# ---------------------------------------------------------------------------
# COG: AUTOMATION ENGINE
# ---------------------------------------------------------------------------

class AutomationEngine(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_message = None
        self.dashboard_view    = None
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    async def recover_dashboard(self):
        """Attempt to re-attach to the dashboard message after a restart."""
        if not state.dashboard_msg_id:
            return
        channel = self.bot.get_channel(DASHBOARD_CHANNEL_ID)
        if not channel:
            log.warning("Dashboard channel not found — cannot recover dashboard message.")
            return
        try:
            msg = await channel.fetch_message(state.dashboard_msg_id)
            view = DashboardView(self.bot)
            self.dashboard_message = msg
            self.dashboard_view    = view
            state.watched = set(state.servers.keys())
            log.info(f"Dashboard message recovered (ID: {state.dashboard_msg_id}).")
        except discord.NotFound:
            log.info("Previous dashboard message no longer exists — a new one must be spawned.")
            state.dashboard_msg_id = None
            state.save()
        except Exception as e:
            log.error(f"Dashboard recovery error: {e}")

    @app_commands.command(name="spawn_dashboard", description="Spawns the persistent control panel (admin only)")
    @app_commands.default_permissions(administrator=True)
    async def spawn_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = discord.Embed(
            title="Infrastructure Control Panel",
            description="Initializing...",
            color=discord.Color.dark_theme()
        )
        view = DashboardView(self.bot)
        msg = await interaction.followup.send(embed=embed, view=view, wait=True)
        self.dashboard_message = msg
        self.dashboard_view    = view
        state.dashboard_msg_id = msg.id
        state.save()
        state.watched = set(state.servers.keys())

    @app_commands.command(name="refresh_servers", description="Re-discovers AMP instances (admin only)")
    @app_commands.default_permissions(administrator=True)
    async def refresh_servers(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        amp_cog = self.bot.get_cog("AMPInterface")
        loaded = await amp_cog.refresh_instances()
        if not loaded:
            state.servers.update(FALLBACK_SERVERS)
            await interaction.followup.send("Discovery failed — using fallback server list.", ephemeral=True)
        else:
            await interaction.followup.send(f"Loaded {len(state.servers)} server(s).", ephemeral=True)

    @app_commands.command(name="status", description="Check the status of all servers")
    async def status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        amp_cog = self.bot.get_cog("AMPInterface")
        lines = []
        for k, srv in state.servers.items():
            data = await amp_cog.get_status(srv["instance_id"])
            if not data:
                lines.append(f"**{srv['label']}** — Error retrieving status")
                continue
            label   = STATE_LABEL.get(data["state"], "Unknown")
            players = data["players"].get("RawValue", "?") if isinstance(data["players"], dict) else "?"
            lines.append(f"**{srv['label']}** — {label} ({players} players)")
        await interaction.followup.send("\n".join(lines) if lines else "No servers configured.")

    @app_commands.command(name="myip", description="Show the public IP of this server (only visible to you)")
    async def myip(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.ipify.org?format=json") as r:
                    ip = (await r.json()).get("ip", "Unknown")
        except Exception as e:
            log.error(f"IP fetch error: {e}")
            ip = "Could not fetch IP"
        embed = discord.Embed(title="Public IP", description=f"`{ip}`", color=discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tasks.loop(seconds=WATCH_INTERVAL)
    async def monitor_loop(self):
        try:
            amp_cog = self.bot.get_cog("AMPInterface")
            hw_cog  = self.bot.get_cog("HardwareOps")
            if not amp_cog or not hw_cog:
                return

            poll_targets = {
                k: amp_cog.get_status(v["instance_id"])
                for k, v in state.servers.items()
                if k in state.watched
            }
            results = await asyncio.gather(*poll_targets.values())

            total_players = 0
            any_running   = False
            status_lines  = []

            for key, data in zip(poll_targets.keys(), results):
                if not data:
                    continue

                srv_state = data["state"]
                prev      = state.last_states.get(key)
                state.last_states[key] = srv_state

                # Crash detection — only alert if the stop was not intentional
                if prev in RUNNING_STATES and srv_state == 0:
                    if key not in state.intentional_stops:
                        channel = self.bot.get_channel(DASHBOARD_CHANNEL_ID)
                        if channel:
                            await channel.send(
                                f"**[ALERT]** {state.servers[key]['label']} went offline unexpectedly."
                            )
                    state.intentional_stops.discard(key)

                label   = STATE_LABEL.get(srv_state, "Unknown")
                players = data["players"].get("RawValue", 0) if isinstance(data["players"], dict) else 0
                status_lines.append(f"**{state.servers[key]['label']}**: {label} | Players: {players}")

                if srv_state in RUNNING_STATES:
                    any_running = True
                    try:
                        total_players += int(players)
                    except (ValueError, TypeError):
                        pass

            # Inactivity shutdown — powers off the host PC after IDLE_TIMEOUT seconds of 0 players
            if any_running:
                if total_players == 0:
                    if state.global_idle_since is None:
                        state.global_idle_since = now()
                        log.info("All servers empty — inactivity timer started.")
                    elif now() - state.global_idle_since >= IDLE_TIMEOUT:
                        log.info("Inactivity timeout reached — stopping servers and powering off host PC.")
                        channel = self.bot.get_channel(DASHBOARD_CHANNEL_ID)

                        # Step 1: Stop all running servers gracefully via AMP
                        for k in list(state.watched):
                            if state.last_states.get(k) in RUNNING_STATES:
                                state.intentional_stops.add(k)
                                await amp_cog.execute_action(state.servers[k]["instance_id"], "Core/Stop")
                                log.info(f"Stop command sent to {state.servers[k]['label']}.")

                        # Step 2: Allow servers time to shut down cleanly before cutting power
                        await asyncio.sleep(SHUTDOWN_GRACE_SECS)

                        # Step 3: Power off the host PC via GPIO
                        success = await hw_cog.trigger_pc_power()
                        if success:
                            log.info("Host PC power-off signal sent via GPIO.")
                            if channel:
                                await channel.send(
                                    f"No players detected for {int(IDLE_TIMEOUT/60)} minutes. "
                                    "All servers stopped and host PC powered off."
                                )
                        else:
                            log.error("GPIO power-off signal failed.")
                            if channel:
                                await channel.send(
                                    "Inactivity timeout reached but the GPIO power-off signal failed. "
                                    "Please check the hardware connection."
                                )

                        state.global_idle_since = None
                        state.watched.clear()
                else:
                    state.global_idle_since = None
            else:
                state.global_idle_since = None

            # Update dashboard embed — reuse the existing view to preserve dropdown state
            if self.dashboard_message:
                embed = discord.Embed(
                    title="Infrastructure Control Panel",
                    color=discord.Color.blurple()
                )
                embed.add_field(
                    name="Active Instances",
                    value="\n".join(status_lines) if status_lines else "No servers are being watched.",
                    inline=False
                )
                if state.global_idle_since:
                    mins_left = max(0, int((IDLE_TIMEOUT - (now() - state.global_idle_since)) / 60))
                    embed.set_footer(text=f"Powering off in {mins_left} minute(s) due to inactivity")
                else:
                    embed.set_footer(text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

                try:
                    await self.dashboard_message.edit(embed=embed, view=self.dashboard_view)
                except discord.NotFound:
                    self.dashboard_message = None
                    self.dashboard_view    = None
                    state.dashboard_msg_id = None
                    state.save()
                except Exception as e:
                    log.error(f"Dashboard update error: {e}")

        except Exception as e:
            log.error(f"Monitor loop error: {e}")

    @monitor_loop.before_loop
    async def before_monitor(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if VOICE_LOBBY_ID == 0:
            return
        if before.channel == after.channel:
            return
        if not (after.channel and after.channel.id == VOICE_LOBBY_ID):
            return

        log.info(f"Voice provisioning triggered by {member.display_name}")
        channel    = self.bot.get_channel(DASHBOARD_CHANNEL_ID)
        any_online = any(s in RUNNING_STATES for s in state.last_states.values())

        if any_online:
            if channel:
                await channel.send(
                    f"{member.mention} joined the lobby. Servers are already online — use the dashboard to connect."
                )
            return

        if channel:
            await channel.send(
                f"{member.mention} joined the lobby — waking host PC via GPIO. "
                "Once the host is up, use the **Smart Start** button on the dashboard to launch a server."
            )
        hw_cog = self.bot.get_cog("HardwareOps")
        await hw_cog.trigger_pc_power()

# ---------------------------------------------------------------------------
# BOT
# ---------------------------------------------------------------------------

class AMPChatOpsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True  # Required for role-based access control (ALLOWED_ROLE_ID)
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await self.add_cog(AMPInterface(self))
        await self.add_cog(HardwareOps(self))
        await self.add_cog(AutomationEngine(self))
        self.add_view(DashboardView(self))
        await self.tree.sync()
        log.info("Cogs loaded and slash commands synced.")

    async def on_ready(self):
        log.info(f"ChatOps Engine ready — logged in as {self.user} (ID: {self.user.id})")
        engine_cog = self.get_cog("AutomationEngine")
        if engine_cog:
            await engine_cog.recover_dashboard()

if __name__ == "__main__":
    # Variables are module-level (assigned from os.getenv above), so check globals directly.
    missing = [k for k in ("BOT_TOKEN", "AMP_USER", "AMP_PASSWORD") if not globals().get(k)]
    if missing:
        log.error(f"Missing required configuration variables: {', '.join(missing)}.")
    elif DASHBOARD_CHANNEL_ID == 0:
        log.error("DASHBOARD_CHANNEL_ID is not set.")
    else:
        state.load()
        bot = AMPChatOpsBot()
        bot.run(BOT_TOKEN)
