import os
import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# Import Dispatcharr's UI notification tool
try:
    from core.utils import send_websocket_update
except ImportError:
    def send_websocket_update(*args, **kwargs):
        pass

# Import file locking to prevent worker collisions
try:
    import fcntl
except ImportError:
    fcntl = None

from .parser import convert_xspf_to_m3u

logger = logging.getLogger(__name__)

# --- CLEAN DIRECTORY PATHS ---
PLUGIN_DIR = "/data/plugins/xspf-playlist-converter"
M3U_DIR = os.path.join(PLUGIN_DIR, "m3us")
LAST_RUN_FILE = os.path.join(PLUGIN_DIR, "last_run.json")
LOCK_FILE = os.path.join(PLUGIN_DIR, "scheduler.lock")
DEFAULT_PORT = 9192

# --- HELPER FUNCTIONS ---
def _parse_sources(sources_str):
    """Strict parser: looks for 'Name | URL', ONE PER LINE. Auto-adds .m3u."""
    sources = []
    if not sources_str:
        return sources
    
    # Split strictly by newlines
    lines = sources_str.splitlines()
    
    for line in lines:
        line = line.strip()
        
        # Ignore empty lines or comments
        if not line or line.startswith('#'):
            continue
            
        # Split by pipe
        parts = line.split('|')
        if len(parts) >= 2:
            raw_name = parts[0].strip()
            # Safety: replace spaces with dashes so URLs don't break
            safe_name = raw_name.replace(' ', '-')
            
            # Auto-add the .m3u extension if they didn't type it
            if not safe_name.lower().endswith('.m3u'):
                filename = f"{safe_name}.m3u"
            else:
                filename = safe_name
                
            # Re-join the rest in case the URL actually contained a pipe character
            url = '|'.join(parts[1:]).strip()
            sources.append({"filename": filename, "url": url})
            
    return sources

def _get_live_settings():
    """Fetches the latest settings directly from the Dispatcharr DB."""
    try:
        from apps.plugins.models import PluginConfig
        # Check both hyphens and underscores to ensure we find the DB entry
        keys_to_try = ["xspf-playlist-converter", "xspf_playlist_converter"]
        for key in keys_to_try:
            cfg = PluginConfig.objects.filter(key=key).first()
            if cfg and hasattr(cfg, 'settings'):
                return cfg.settings
    except Exception as e:
        logger.error(f"Error fetching live settings: {e}")
    
    return {}


def _read_last_run():
    try:
        if os.path.exists(LAST_RUN_FILE):
            with open(LAST_RUN_FILE, 'r') as f:
                data = json.load(f)
                return datetime.fromisoformat(data.get("last_run_time"))
    except Exception:
        pass
    return datetime.min

def _write_last_run():
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        with open(LAST_RUN_FILE, 'w') as f:
            json.dump({"last_run_time": datetime.now().isoformat()}, f)
    except Exception as e:
        logger.error(f"Failed to save last run time: {e}")

# --- MINI WEB SERVER LOGIC ---
class SecureM3UHandler(BaseHTTPRequestHandler):
    """A tiny web server that ONLY serves the configured M3U files from the m3us/ folder."""
    
    def do_GET(self):
        settings = _get_live_settings()
        sources_str = settings.get("xspf_sources", "")
        sources = _parse_sources(sources_str)
        
        allowed_filenames = [s["filename"] for s in sources]
        requested_file = os.path.basename(self.path)

        if requested_file in allowed_filenames:
            filepath = os.path.join(M3U_DIR, requested_file)
            if os.path.exists(filepath):
                self.send_response(200)
                self.send_header("Content-type", "audio/mpegurl")
                self.end_headers()
                with open(filepath, 'rb') as f:
                    self.wfile.write(f.read())
                return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"404 Not Found - XSPF Converter Plugin")

    def log_message(self, format, *args):
        pass

# --- PLUGIN LOGIC ---
class Plugin:
    """Dispatcharr Plugin - XSPF Playlist Converter."""

    name        = "XSPF Playlist Converter"
    description = "Automatically downloads XSPF playlists, converts them to standard M3U format, and hosts them internally for Dispatcharr."
    version     = "1.7.0"
    author      = "tjvaldeze"

    @property
    def fields(self):
        """Dynamically render the UI, including the generated URLs!"""
        settings = _get_live_settings()
        sources_str = settings.get("xspf_sources", "")
        server_port = int(settings.get("web_server_port", DEFAULT_PORT))
        sources = _parse_sources(sources_str)
        
        if sources:
            url_list = ["✅ Copy the links below into Dispatcharr's 'Playlists' page:\n"]
            for s in sources:
                url_list.append(f"🔗 http://127.0.0.1:{server_port}/{s['filename']}")
            
            # Using \n\n forces distinct lines in the UI
            urls_display = "\n\n".join(url_list)
        else:
            urls_display = "⚠️ No valid sources configured. Use the format: name | https://url.xspf"

        return [
            {
                "id": "_section_instructions",
                "label": "ℹ️ Instructions",
                "type": "info",
                "description": "Enter your XSPF sources below. The plugin will convert them to M3U and run an internal web server. Copy the generated URLs at the bottom of this page into Dispatcharr."
            },
            {
                "id": "xspf_sources",
                "label": "📡 XSPF Sources (Format: name | url)",
                "type": "text",
                "default": "init7-tv7 | https://api.init7.net/tvchannels.xspf",
                "help_text": "Type a name, a pipe (|), and the URL. Put each stream on a NEW LINE. We automatically add the .m3u extension!"
            },
            {
                "id": "update_interval_hours",
                "label": "⏱️ Auto-Update Interval (Hours)",
                "type": "number",
                "default": 12,
                "help_text": "How often the plugin automatically downloads the latest playlists in the background. Set to 0 to disable."
            },
            {
                "id": "web_server_port",
                "label": "🔌 Internal Web Server Port",
                "type": "number",
                "default": DEFAULT_PORT,
                "help_text": "The port used to host the generated M3U files internally. (Click 'Restart Web Server' in the Actions tab if you change this)."
            },
            {
                "id": "_section_generated_urls",
                "label": "📋 Your Generated M3U URLs",
                "type": "info",
                "description": urls_display # <--- Now uses 'description' so it renders as large, main-body text!
            }
        ]

    actions = [
        {
            "id": "run_conversion",
            "label": "Manual Conversion",
            "description": "Download and convert all configured XSPF playlists right now.",
            "button_label": "▶️ Run Conversion Now",
            "button_variant": "filled",
            "button_color": "blue",
            "background": True,
        },
        {
            "id": "restart_webserver",
            "label": "Restart Web Server",
            "description": "Restarts the internal web server. Use this if you changed the port setting.",
            "button_label": "🔄 Restart Web Server",
            "button_variant": "outline",
            "button_color": "orange",
        }
    ]

    def __init__(self):
        self._bg_thread = None
        self._stop_event = threading.Event()
        self.httpd = None
        
        os.makedirs(M3U_DIR, exist_ok=True)
        
        self._start_scheduler()
        self._start_web_server()

    def _start_web_server(self):
        def serve():
            try:
                settings = _get_live_settings()
                port = int(settings.get("web_server_port", DEFAULT_PORT))
                self.httpd = HTTPServer(("", port), SecureM3UHandler)
                logger.info(f"XSPF Mini Web Server started on port {port}")
                self.httpd.serve_forever()
            except Exception as e:
                logger.error(f"Failed to start XSPF Web Server on port: {e}")

        threading.Thread(target=serve, name="xspf-webserver", daemon=True).start()

    def _stop_web_server(self):
        if self.httpd:
            logger.info("Shutting down XSPF web server...")
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def _start_scheduler(self):
        def scheduler_loop():
            logger.info("XSPF Auto-Scheduler started.")
            while not self._stop_event.is_set():
                try:
                    settings = _get_live_settings()
                    interval_hours = float(settings.get("update_interval_hours", 12))
                    
                    if interval_hours > 0:
                        last_run = _read_last_run()
                        hours_since = (datetime.now() - last_run).total_seconds() / 3600
                        if hours_since >= interval_hours:
                            self._run_conversion_bg(settings, is_auto=True)
                except Exception as e:
                    logger.error(f"Error in scheduler: {e}")
                
                self._stop_event.wait(60)

        self._bg_thread = threading.Thread(target=scheduler_loop, name="xspf-scheduler", daemon=True)
        self._bg_thread.start()

    def _run_conversion_bg(self, settings, is_auto=False):
        os.makedirs(M3U_DIR, exist_ok=True)
        
        lock_fd = None
        if fcntl:
            try:
                lock_fd = open(LOCK_FILE, 'w')
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError):
                if lock_fd: lock_fd.close()
                return

        try:
            if is_auto:
                settings = _get_live_settings()

            sources_str = settings.get("xspf_sources", "")
            sources = _parse_sources(sources_str)
            
            if not sources:
                logger.warning("No valid XSPF sources configured.")
                return

            if not is_auto:
                send_websocket_update('updates', 'update', {
                    "type": "plugin", "plugin": self.name, "message": f"Downloading {len(sources)} XSPF playlist(s)..."
                })

            success_count = 0
            for s in sources:
                url = s["url"]
                filename = s["filename"]
                save_path = os.path.join(M3U_DIR, filename)
                
                logger.info(f"Processing XSPF: {url} -> {save_path}")
                if convert_xspf_to_m3u(url, save_path):
                    success_count += 1
                else:
                    logger.error(f"Failed to convert {url}")
            
            # --- START GARBAGE CLEANUP ---
            allowed_filenames = [s["filename"] for s in sources]
            try:
                for existing_file in os.listdir(M3U_DIR):
                    if existing_file.endswith(".m3u") and existing_file not in allowed_filenames:
                        old_file_path = os.path.join(M3U_DIR, existing_file)
                        os.remove(old_file_path)
                        logger.info(f"Deleted orphaned M3U file: {existing_file}")
            except Exception as e:
                logger.error(f"Error cleaning up orphaned M3U files: {e}")
            # --- END GARBAGE CLEANUP ---

            if success_count > 0:
                msg = f"Success! Converted {success_count}/{len(sources)} playlists."
                logger.info(msg)
                if not is_auto:
                    send_websocket_update('updates', 'update', {
                        "type": "plugin", "plugin": self.name, "message": msg
                    })
                _write_last_run()
            else:
                if not is_auto:
                    send_websocket_update('updates', 'update', {
                        "type": "plugin", "plugin": self.name, "message": "❌ Conversions failed. Check logs."
                    })
                    
        except Exception as e:
            logger.error(f"Error in background conversion: {e}", exc_info=True)
            if not is_auto:
                send_websocket_update('updates', 'update', {
                    "type": "plugin", "plugin": self.name, "message": f"❌ Error: {str(e)}"
                })
        finally:
            if lock_fd:
                try:
                    if fcntl: fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except OSError:
                    pass

    def run(self, action: str, params: dict, context: dict):
        settings = context.get("settings", {})

        if action == "run_conversion":
            threading.Thread(
                target=self._run_conversion_bg, args=(settings, False), daemon=True
            ).start()
            return {
                "status": "ok",
                "message": "Conversion started in background. Check notifications for progress.",
                "background": True,
            }
            
        elif action == "restart_webserver":
            try:
                self._stop_web_server()
                self._start_web_server()
                
                new_port = int(settings.get("web_server_port", DEFAULT_PORT))
                return {
                    "status": "success",
                    "message": f"Web server restarted successfully on port {new_port}.",
                }
            except Exception as e:
                return {"status": "error", "message": f"Error restarting web server: {e}"}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context: dict):
        self._stop_event.set()
        self._stop_web_server()
        if self._bg_thread and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=5)