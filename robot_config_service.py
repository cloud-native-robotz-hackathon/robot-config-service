#!/usr/bin/env python3
"""
Robot Config Service - MVP

This service runs on a Raspberry Pi robot as a systemd service and checks on startup:
1. Reads locally cached event ID from /var/run/robot-config-service/eventid
2. Queries a fixed redirect URL that points to the current OpenShift event cluster
3. On that cluster, queries the event ID and checks if it's a new event
4. If new event: gets skupper token and runs ansible playbook to set up tunnel
5. If no cached event ID (robot rebooted): just checks if skupper tunnel is up

The service runs once on startup - event IDs don't change while the robot is running.
"""

import os
import sys
import json
import logging
import subprocess
import socket
import time
import requests
from urllib.parse import urlparse
from requests.auth import HTTPBasicAuth
from pathlib import Path
from typing import Optional

# Configuration
EVENT_ID_FILE = Path("/var/run/robot-config-service/eventid")
REDIRECT_URL = os.getenv("REDIRECT_URL", "")
ANSIBLE_PLAYBOOK_PATH = os.getenv("ANSIBLE_PLAYBOOK_PATH", "/opt/robot-config-service/ansible/configure-robot.yml")
# Path to cache file holding skupper token (YAML); service writes before running playbook so playbook can run standalone
SKUPPER_TOKEN_FILE = os.getenv("SKUPPER_TOKEN_FILE", "/var/run/robot-config-service/skupper-token")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
RCS_HUBCONTROLLER_USER = os.getenv("RCS_HUBCONTROLLER_USER", "")
RCS_HUBCONTROLLER_PASSWORD = os.getenv("RCS_HUBCONTROLLER_PASSWORD", "")


# Redirect: retries and delay (s) for transient connection/network failures at boot
REDIRECT_RETRIES = int(os.getenv("REDIRECT_RETRIES", "3"))
REDIRECT_RETRY_DELAY = int(os.getenv("REDIRECT_RETRY_DELAY", "10"))
# If set (1/true/yes), REDIRECT_URL is the cluster/eventId URL; do not perform a GET to follow redirects
REDIRECT_URL_IS_CLUSTER = os.getenv("REDIRECT_URL_IS_CLUSTER", "").lower() in ("1", "true", "yes")
# GitHub raw: repo URL (e.g. https://github.com/org/robot-auto-register-78b09.git) and token for private repos
RCS_GIT_REPO = os.getenv("RCS_GIT_REPO", "").strip()
RCS_GH_TOKEN = os.getenv("RCS_GH_TOKEN", "").strip()
RCS_GIT_BRANCH = os.getenv("RCS_GIT_BRANCH", "main").strip() or "main"
# Optional delay (s) before service does anything (e.g. at boot so network/ansible are ready)
SERVICE_STARTUP_DELAY = int(os.getenv("SERVICE_STARTUP_DELAY", "60"))
# Playbook retries and delay (s) between attempts for transient failures
PLAYBOOK_RETRIES = max(1, int(os.getenv("PLAYBOOK_RETRIES", "2")))
PLAYBOOK_RETRY_DELAY = int(os.getenv("PLAYBOOK_RETRY_DELAY", "30"))
# Full ansible playbook stdout/stderr are appended to this file; set empty to disable
ANSIBLE_OUTPUT_LOG = os.getenv("ANSIBLE_OUTPUT_LOG", "/var/log/robot-config-service-ansible.log").strip()

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/robot-config-service.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class RobotConfigService:
    """Main service class for robot configuration management."""
    
    def __init__(self):
        self.event_id_file = EVENT_ID_FILE
        self.redirect_url = REDIRECT_URL
        if not self.redirect_url and not RCS_GIT_REPO:
            logger.error("Either REDIRECT_URL or RCS_GIT_REPO environment variable is required - service cannot run")
            raise ValueError("REDIRECT_URL or RCS_GIT_REPO environment variable is required")
        self.ansible_playbook_path = ANSIBLE_PLAYBOOK_PATH
        self.cluster_base_url = None  # Will be set after following redirect
        self.auth = None
        if RCS_HUBCONTROLLER_USER and RCS_HUBCONTROLLER_PASSWORD:
            self.auth = HTTPBasicAuth(RCS_HUBCONTROLLER_USER, RCS_HUBCONTROLLER_PASSWORD)
            logger.info("Basic authentication configured")
        else:
            logger.warning("RCS_HUBCONTROLLER_USER or RCS_HUBCONTROLLER_PASSWORD not set - API calls may fail")
        self.robot_name = socket.gethostname()
        logger.info(f"Robot hostname: {self.robot_name}")
        
    def get_cached_event_id(self) -> Optional[str]:
        """Read cached event ID from file."""
        try:
            if self.event_id_file.exists():
                with open(self.event_id_file, 'r') as f:
                    event_id = f.read().strip()
                    if event_id:
                        logger.info(f"Found cached event ID: {event_id}")
                        return event_id
        except Exception as e:
            logger.error(f"Error reading cached event ID: {e}")
        return None
    
    def cache_event_id(self, event_id: str) -> bool:
        """Write event ID to cache file."""
        try:
            # Ensure directory exists
            self.event_id_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.event_id_file, 'w') as f:
                f.write(event_id)
            logger.info(f"Cached event ID: {event_id}")
            return True
        except PermissionError:
            logger.error(f"Permission denied writing to {self.event_id_file}")
            return False
        except Exception as e:
            logger.error(f"Error caching event ID: {e}")
            return False
    
    def _github_raw_base_url(self) -> Optional[str]:
        """Build raw.githubusercontent.com base URL from RCS_GIT_REPO (e.g. https://github.com/org/repo.git)."""
        if not RCS_GIT_REPO:
            return None
        try:
            parsed = urlparse(RCS_GIT_REPO.strip())
            if parsed.netloc != "github.com" or not parsed.path:
                logger.warning(f"RCS_GIT_REPO does not look like a GitHub repo URL: {RCS_GIT_REPO}")
                return None
            path = parsed.path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            if "/" not in path:
                return None
            return f"https://raw.githubusercontent.com/{path}/{RCS_GIT_BRANCH}"
        except Exception as e:
            logger.warning(f"Could not derive GitHub raw base from RCS_GIT_REPO: {e}")
            return None

    def _fetch_cluster_url_from_github_raw(self, raw_base: str) -> Optional[str]:
        """Fetch cluster URL from GitHub raw: try .../ROBOT_NAME then .../catch-all. Uses RCS_GH_TOKEN if set."""
        headers = {}
        if RCS_GH_TOKEN:
            headers["Authorization"] = f"token {RCS_GH_TOKEN}"
        for name in (self.robot_name, "catch-all"):
            url = f"{raw_base.rstrip('/')}/{name}"
            try:
                logger.info(f"Fetching cluster URL from GitHub raw: {url}")
                response = requests.get(url, timeout=10, headers=headers or None)
                if response.status_code == 200:
                    cluster_url = response.text.strip()
                    if cluster_url:
                        logger.info(f"Resolved cluster URL from {name}: {cluster_url}")
                        return cluster_url
                elif response.status_code != 404:
                    logger.warning(f"GitHub raw {url} returned {response.status_code}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Failed to fetch {url}: {e}")
        return None

    def get_cluster_url(self) -> Optional[str]:
        """Resolve cluster URL. If REDIRECT_URL_IS_CLUSTER is set, use REDIRECT_URL as-is.
        If RCS_GIT_REPO is set, fetch from GitHub raw: try .../ROBOT_NAME then .../catch-all.
        Otherwise follow redirect URL (with auth on each request) to get the cluster URL.
        """
        if REDIRECT_URL_IS_CLUSTER and self.redirect_url:
            cluster_url = self.redirect_url.split('?')[0].rstrip('/')
            logger.info(f"Using REDIRECT_URL as cluster URL (no redirect): {cluster_url}")
            return cluster_url
        raw_base = self._github_raw_base_url()
        if raw_base:
            last_error = None
            for attempt in range(REDIRECT_RETRIES):
                cluster_url = self._fetch_cluster_url_from_github_raw(raw_base)
                if cluster_url:
                    return cluster_url
                if attempt < REDIRECT_RETRIES - 1 and REDIRECT_RETRY_DELAY > 0:
                    logger.info(f"Retrying in {REDIRECT_RETRY_DELAY}s")
                    time.sleep(REDIRECT_RETRY_DELAY)
            logger.error("Could not fetch cluster URL from GitHub raw (tried robot name and catch-all)")
            return None
        if not self.redirect_url:
            logger.error("No REDIRECT_URL and GitHub raw fetch did not return a URL")
            return None
        last_error = None
        for attempt in range(REDIRECT_RETRIES):
            try:
                url = self.redirect_url
                seen = set()
                for _ in range(10):  # max redirects
                    if url in seen:
                        logger.error("Redirect loop detected")
                        return None
                    seen.add(url)
                    logger.info(f"Following redirect URL: {url}")
                    response = requests.get(
                        url,
                        timeout=10,
                        allow_redirects=False,
                        auth=self.auth
                    )
                    response.raise_for_status()
                    if response.is_redirect and response.headers.get('Location'):
                        next_location = response.headers['Location']
                        if next_location.startswith('/'):
                            # Resolve relative to the URL we just requested (not redirect_url)
                            parsed = urlparse(response.url)
                            url = f"{parsed.scheme}://{parsed.netloc}{next_location}"
                        else:
                            url = next_location
                        continue
                    # No redirect: use final URL (e.g. 200 or same-host redirect already followed)
                    cluster_url = response.url.rstrip('/')
                    logger.info(f"Resolved cluster URL: {cluster_url}")
                    return cluster_url
                logger.error("Too many redirects")
                return None
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(f"Redirect attempt {attempt + 1}/{REDIRECT_RETRIES} failed: {e}")
                if attempt < REDIRECT_RETRIES - 1 and REDIRECT_RETRY_DELAY > 0:
                    logger.info(f"Retrying in {REDIRECT_RETRY_DELAY}s")
                    time.sleep(REDIRECT_RETRY_DELAY)
        logger.error(f"Error following redirect URL after {REDIRECT_RETRIES} attempts: {last_error}")
        return None
    
    def query_event_id(self, cluster_url: str) -> Optional[str]:
        """Query OpenShift cluster endpoint for current event ID.
        cluster_url is the cluster base URL; we append control/eventId.
        """
        try:
            base = cluster_url.split('?')[0].rstrip('/')
            event_id_endpoint = f"{base}/control/eventId"
            logger.info(f"Querying event ID from {event_id_endpoint} with robot_name={self.robot_name}")
            response = requests.get(
                event_id_endpoint,
                params={'robot_name': self.robot_name},
                timeout=10,
                headers={'Content-Type': 'application/json'},
                auth=self.auth
            )
            response.raise_for_status()
            
            # Handle both JSON and plain text responses
            try:
                data = response.json()
                event_id = data.get('event_id') or data.get('eventId') or str(data)
            except ValueError:
                event_id = response.text.strip()
            
            logger.info(f"Received event ID: {event_id}")
            return event_id
        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying event ID endpoint: {e}")
            return None
    
    def _control_base(self, cluster_url: str) -> str:
        """Derive control base URL from cluster_url (cluster base -> .../control)."""
        return f"{cluster_url.split('?')[0].rstrip('/')}/control"

    def report_init_status(self, cluster_url: str, status: str) -> None:
        """POST robot init status to /control/initStatus (form: robot_name, status)."""
        try:
            control_base = self._control_base(cluster_url)
            url = f"{control_base}/initStatus"
            response = requests.post(
                url,
                data={'robot_name': self.robot_name, 'status': status},
                timeout=10,
                auth=self.auth
            )
            response.raise_for_status()
            logger.debug(f"initStatus reported: {status}")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Could not report initStatus {status!r}: {e}")

    def query_skupper_token(self, cluster_url: str) -> Optional[str]:
        """Query OpenShift cluster endpoint for skupper token.
        cluster_url is the cluster base URL; derive control base for getToken.
        Retries the /getToken call until HTTP 200 is returned.
        """
        control_base = self._control_base(cluster_url)
        token_endpoint = f"{control_base}/getToken"
        logger.info(f"Querying skupper token from {token_endpoint} with robot_name={self.robot_name}")
        self.report_init_status(cluster_url, "⏳ Querying skupper token")
        while True:
            try:
                response = requests.get(
                    token_endpoint,
                    params={'robot_name': self.robot_name},
                    timeout=10,
                    headers={'Content-Type': 'application/json'},
                    auth=self.auth
                )
                if response.status_code == 200:
                    # Handle both JSON and plain text responses
                    try:
                        data = response.json()
                        token = data.get('token') or data.get('skupper_token') or str(data)
                    except ValueError:
                        token = response.text.strip()
                    logger.info("Successfully retrieved skupper token")
                    self.report_init_status(cluster_url, "✅ Successfully retrieved skupper token")
                    return token
                logger.warning(
                    f"getToken returned HTTP {response.status_code}, retrying in 5s..."
                )
            except requests.exceptions.RequestException as e:
                logger.warning(f"Error querying skupper token endpoint: {e}, retrying in 5s...")
            time.sleep(5)
    
    def check_skupper_tunnel(self) -> bool:
        """Check if skupper tunnel is up and connected to another site.
        Requires positive evidence of a connection (e.g. 'connected to ... other site')
        to avoid false positives when Skupper is still starting after reboot.
        """
        try:
            logger.info("Checking skupper tunnel status")
            result = subprocess.run(
                ['skupper', 'status', '-n', 'skupper'],
                capture_output=True,
                text=True,
                timeout=10
            )
            stdout = (result.stdout or "").lower()
            if result.returncode != 0:
                logger.warning("Skupper tunnel appears to be down or not configured")
                logger.debug(f"Skupper status output: {result.stderr}")
                return False
            # Require positive evidence of a connection (e.g. "connected to 1 other site")
            connected = "connected to" in stdout and "other site" in stdout
            if connected:
                logger.info("Skupper tunnel is up and running")
                logger.debug(f"Skupper status: {result.stdout}")
                return True
            logger.info("Skupper is enabled but not connected to any other sites")
            logger.debug(f"Skupper status: {result.stdout}")
            return False
        except FileNotFoundError:
            logger.warning("Skupper command not found, cannot verify tunnel status")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("Skupper status check timed out")
            return False
        except Exception as e:
            logger.warning(f"Error checking skupper tunnel: {e}")
            return False

    def _remove_token_file_after_tunnel_up(self) -> None:
        """If the skupper tunnel is up, remove the token cache file (so it is only removed after success)."""
        token_path = Path(SKUPPER_TOKEN_FILE)
        if not token_path.exists():
            return
        time.sleep(15)  # give tunnel time to establish after playbook
        if self.check_skupper_tunnel():
            try:
                token_path.unlink()
                logger.info(f"Tunnel established; removed token file {token_path}")
            except OSError as e:
                logger.warning(f"Could not remove token file {token_path}: {e}")
        else:
            logger.info("Token file left in place (tunnel not yet up); playbook can be re-run by hand")

    def _run_ansible_playbook_once(self, token: str) -> bool:
        """Run ansible playbook once. Returns True on success, False on failure."""
        token_path = Path(SKUPPER_TOKEN_FILE)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token)
        token_path.chmod(0o600)

        env = os.environ.copy()
        env['SKUPPER_TOKEN_FILE'] = str(token_path)
        env['RCS_HUBCONTROLLER_URL'] = str(self.cluster_base_url)

        ansible_dir = os.path.dirname(self.ansible_playbook_path)
        playbook_name = os.path.basename(self.ansible_playbook_path)
        inventory_path = os.path.join(ansible_dir, 'inventory')

        cmd = ['ansible-playbook', '-i', inventory_path, playbook_name]
        if LOG_LEVEL.upper() == 'DEBUG':
            cmd.extend(['-vv'])
        logger.info(f"Running ansible playbook: {self.ansible_playbook_path}")
        logger.info(f"Ansible command: cwd={ansible_dir!r}, cmd={cmd!r}, SKUPPER_TOKEN_FILE={token_path!r}")

        result = subprocess.run(
            cmd,
            cwd=ansible_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )

        if ANSIBLE_OUTPUT_LOG:
            try:
                with open(ANSIBLE_OUTPUT_LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] returncode={result.returncode} cmd={cmd}\n")
                    f.write(f"{'='*60}\n")
                    if result.stdout:
                        f.write("--- stdout ---\n")
                        f.write(result.stdout)
                        if not result.stdout.endswith("\n"):
                            f.write("\n")
                    if result.stderr:
                        f.write("--- stderr ---\n")
                        f.write(result.stderr)
                        if not result.stderr.endswith("\n"):
                            f.write("\n")
            except OSError as e:
                logger.warning(f"Could not write ansible output to {ANSIBLE_OUTPUT_LOG}: {e}")

        if result.returncode == 0:
            logger.info("Ansible playbook completed successfully")
            logger.debug(f"Ansible stdout: {result.stdout}")
            return True
        logger.error(f"Ansible playbook failed with return code {result.returncode}")
        logger.error("Ansible stdout (last 4096 chars): %s", (result.stdout or "")[-4096:])
        logger.error("Ansible stderr (last 4096 chars): %s", (result.stderr or "")[-4096:])
        if len(result.stdout or "") > 4096 or len(result.stderr or "") > 4096:
            logger.error("Output was truncated above; set LOG_LEVEL=DEBUG and re-run for full -vv ansible output")
        return False

    def run_ansible_playbook(self, token: str) -> bool:
        """Run ansible playbook to configure skupper tunnel, with optional retries."""
        try:
            for attempt in range(1, PLAYBOOK_RETRIES + 1):
                if attempt > 1:
                    logger.info(f"Retrying playbook (attempt {attempt}/{PLAYBOOK_RETRIES}) after {PLAYBOOK_RETRY_DELAY}s delay")
                    time.sleep(PLAYBOOK_RETRY_DELAY)
                if self._run_ansible_playbook_once(token):
                    return True
            return False
        except subprocess.TimeoutExpired:
            self.report_init_status(cluster_url, "🚨 Ansible playbook timed out")
            logger.error("Ansible playbook timed out")
            return False
        except FileNotFoundError:
            self.report_init_status(cluster_url, "🚨 Ansible playbook not found at {self.ansible_playbook_path}")
            logger.error(f"Ansible playbook not found at {self.ansible_playbook_path}")
            return False
        except Exception as e:
            self.report_init_status(cluster_url, "🚨 Error running ansible playbook")
            logger.error(f"Error running ansible playbook: {e}")
            return False
    
    def process_event(self) -> bool:
        """Main processing logic: check event ID and configure if needed.
        Runs playbook when: new event, tunnel down, or no cached event (e.g. first boot / reboot).
        """
        cached_event_id = self.get_cached_event_id()

        # Get cluster URL and current event ID (needed for both branches)
        cluster_url = self.get_cluster_url()
        if not cluster_url:
            logger.warning("Could not resolve cluster URL from redirect, skipping this cycle")
            if not cached_event_id:
                logger.warning("No cached event ID - manual intervention may be needed")
            return False

        current_event_id = self.query_event_id(cluster_url)
        if not current_event_id:
            logger.warning("Could not retrieve event ID from cluster, skipping this cycle")
            if not cached_event_id:
                logger.warning("No cached event ID - manual intervention may be needed")
            return False

        # No cached event ID (first boot, reboot with cleared cache, or eventid file missing)
        if not cached_event_id:
            self.report_init_status(cluster_url, "EID unknown")
            logger.info("No cached event ID - will configure tunnel and cache current event")
            token = self.query_skupper_token(cluster_url)
            if not token:
                logger.error("Could not retrieve skupper token - manual intervention may be needed")
                return False
            self.report_init_status(cluster_url, "⏳ Starting configure robot")
            if not self.run_ansible_playbook(token):
                self.report_init_status(cluster_url, "🚨 Failed to configure robot")
                logger.error("Failed to configure skupper tunnel")
                return False
            self.report_init_status(cluster_url, "✅ 🤖 configured")
            if self.cache_event_id(current_event_id):
                logger.info("Tunnel configured and event ID cached")
                self._remove_token_file_after_tunnel_up()
                return True
            logger.warning("Tunnel configured but failed to cache event ID")
            return False

        # Cached event ID exists: run playbook only if event changed.
        event_changed = cached_event_id != current_event_id
        if not event_changed:
            self.report_init_status(cluster_url, "EID known")
            logger.info(f"Event ID unchanged ({current_event_id}), no action")
            return True

        logger.info(f"New event ID detected: {current_event_id} (was: {cached_event_id}) - reconfiguring tunnel")
        token = self.query_skupper_token(cluster_url)
        if not token:
            logger.error("Could not retrieve skupper token, cannot configure tunnel")
            return False
        self.report_init_status(cluster_url, "⏳ Starting configure robot")
        if not self.run_ansible_playbook(token):
            self.report_init_status(cluster_url, "🚨 Failed to configure robot")
            logger.error("Failed to configure skupper tunnel")
            return False
        self.report_init_status(cluster_url, "Okay")
        if self.cache_event_id(current_event_id):
            logger.info("Successfully configured for new event")
            self._remove_token_file_after_tunnel_up()
            return True
        logger.warning("Configuration completed but failed to cache event ID")
        return False
    
    def run(self):
        """Run the service - checks event ID once on startup."""
        logger.info("Robot Config Service starting...")
        if self.redirect_url:
            logger.info(f"Redirect URL: {self.redirect_url}")
        if RCS_GIT_REPO:
            logger.info(f"Cluster URL source: GitHub raw (RCS_GIT_REPO)")
        if SERVICE_STARTUP_DELAY > 0:
            logger.info(f"Waiting {SERVICE_STARTUP_DELAY}s before starting (SERVICE_STARTUP_DELAY)")
            time.sleep(SERVICE_STARTUP_DELAY)
        
        # Read cached event ID at startup
        cached_event_id = self.get_cached_event_id()
        if cached_event_id:
            logger.info(f"Starting with cached event ID: {cached_event_id}")
        else:
            logger.info("No cached event ID found - will check tunnel status")
        
        try:
            success = self.process_event()
            if success:
                logger.info("Robot Config Service completed successfully")
            else:
                logger.warning("Robot Config Service completed with warnings")
        except Exception as e:
            logger.error(f"Error in Robot Config Service: {e}", exc_info=True)
            sys.exit(1)


def main():
    """Entry point."""
    EVENT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    Path(SKUPPER_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    service = RobotConfigService()
    service.run()


if __name__ == "__main__":
    main()
