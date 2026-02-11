# Robot Config Service

A Python service that runs on a Raspberry Pi (or similar) robot to automatically configure Skupper tunnels to an OpenShift cluster based on event IDs. The robot cannot reach the data center directly; it uses a fixed redirect URL and API (with basic auth) to get the current event and a Skupper token, then runs an Ansible playbook to establish the tunnel.

## Architecture

- **Runs on the robot** (e.g. Ubuntu with MicroShift) as a systemd oneshot service.
- **Redirect URL**: A fixed short URL (with basic auth) that redirects to the current OpenShift cluster, e.g. to `https://web-hub-controller.apps.../control/eventId`. The service follows redirects and then uses that cluster for the API.
- **API**: On the resolved cluster URL, the service calls the eventId endpoint and `/control/getToken` (with `robot_name` and basic auth).
- **Skupper**: Ansible playbook on the robot applies the token and brings the Skupper tunnel up.

## Behaviour

The service runs **once at startup** and then exits:

1. **Redirect** → resolve cluster URL (follows redirects with auth).
2. **Event ID** → query current event from cluster.
3. **No cached event** (e.g. first boot or missing `/etc/eventid`): get token, run playbook, cache event ID.
4. **Cached event exists**:
   - Wait a short initial delay, then check Skupper tunnel status (with retries) so we don’t run the playbook just because MicroShift/Skupper weren’t ready yet.
   - If **event changed** or **tunnel is down** (no “connected to … other site” in `skupper status -n skupper`): get token, run playbook, update cached event.
   - If event unchanged and tunnel up: do nothing.

So after a reboot we give Skupper time to start and only run the playbook when the tunnel is actually down (or the event changed).

## Requirements

- Python 3.7+
- `requests`
- Ansible and `kubernetes.core` collection (for the playbook)
- MicroShift on the robot
- Network access to the redirect URL and OpenShift API (before the tunnel is up)

## Quick start

From the repo root:

**1. Create inventory** (YAML; use `.yml` so group vars apply). Add your robots, SSH key, and **override vars** so the playbook generates the systemd override on each robot:

```bash
cp deploy/inventory.robots.example deploy/inventory.robots.yml
nano deploy/inventory.robots.yml
```

```yaml
robots:
  vars:
    ansible_user: root
    ansible_ssh_private_key_file: ~/.ssh/robot-hackathon
    api_username: "your_api_user"
    api_password: "your_api_password"
    redirect_url: "https://your-short-redirect.example.com/path"
  hosts:
    robot1:
      ansible_host: 192.168.1.100
```

**2. Test connectivity and deploy:**

```bash
ansible robots -i deploy/inventory.robots.yml -m ping
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml
```

**3. Start the service on all robots:**

```bash
ansible robots -i deploy/inventory.robots.yml -m systemd -a "name=robot-config-service state=started" --become
```

Keep `inventory.robots.yml` private (it’s in `.gitignore`). Optional override vars and manual override: see [Configuration](#configuration). **Detailed steps, tags, troubleshooting:** [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md).

## Configuration

Service behaviour is controlled by the systemd override at `/etc/systemd/system/robot-config-service.service.d/override.conf`. The normal approach is to set **override vars in the inventory** (see Quick start); the playbook generates the override from `deploy/templates/override.conf.j2`. Do not commit real values.

**Override vars in inventory** (group or host vars):

- **Required:** `api_username`, `api_password`, `redirect_url`
- **Optional:** `ansible_playbook_path`, `log_level`, `tunnel_check_initial_delay`, `tunnel_check_retries`, `tunnel_check_interval`, `redirect_url_is_cluster`, `redirect_retries`, `redirect_retry_delay`

To only refresh the override from inventory: `ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags config`

**Alternative: manual override on each robot**  
If you omit the override vars from the inventory, on each robot:

```bash
sudo mkdir -p /etc/systemd/system/robot-config-service.service.d/
sudo cp robot-config-service.service.d-override.conf.example /etc/systemd/system/robot-config-service.service.d/override.conf
sudo nano /etc/systemd/system/robot-config-service.service.d/override.conf
```

Use **`KEY=VALUE`** for each variable, for example:

```ini
[Service]
Environment="API_USERNAME=your_username"
Environment="API_PASSWORD=your_password"
Environment="REDIRECT_URL=https://your-redirect-service.example.com/redirect"
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl start robot-config-service
```

### Environment variables (override)

| Variable | Required | Description |
|----------|----------|-------------|
| `REDIRECT_URL` | Yes | Full URL (e.g. `https://...`) that redirects to the current OpenShift event cluster. Auth is sent on each redirect. |
| `API_USERNAME` | Yes | Basic auth username for redirect and cluster API. |
| `API_PASSWORD` | Yes | Basic auth password. |
| `ANSIBLE_PLAYBOOK_PATH` | No | Default: `/opt/robot-config/ansible/configure-skupper.yml` |
| `LOG_LEVEL` | No | Default: `INFO` |
| `TUNNEL_CHECK_INITIAL_DELAY` | No | Seconds before first tunnel check after reboot (default: 45). |
| `TUNNEL_CHECK_RETRIES` | No | Number of tunnel checks (default: 3). |
| `TUNNEL_CHECK_INTERVAL` | No | Seconds between retries (default: 30). |
| `REDIRECT_URL_IS_CLUSTER` | No | Set to `1` if REDIRECT_URL is already the cluster/eventId URL (no redirect to follow). |
| `REDIRECT_RETRIES` / `REDIRECT_RETRY_DELAY` | No | Retries and delay (s) when following the redirect URL. |

Playbook is run only if **all** tunnel checks report “not connected”.

## Updating after code changes

To push only the Python service script to existing robots:

```bash
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags python
```

Then restart the service on the robots. See [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) for other tags (`ansible`, `systemd`, `config`, `deps`).

## Repository layout

- `robot_config_service.py` – main service
- `robot-config-service.service` – systemd unit
- `robot-config-service.service.d-override.conf.example` – example override (for manual config; from-inventory uses the template)
- `ansible/configure-skupper.yml` – playbook that applies the Skupper token and brings the tunnel up
- `ansible/inventory` – localhost inventory for that playbook
- `ansible/roles/robot/` – role (reset MicroShift, download kubeconfig, etc.)
- `deploy/deploy-robots.yml` – Ansible playbook to deploy this service to robots
- `deploy/inventory.robots.example` – example inventory (copy to `inventory.robots.yml` and edit)
- `deploy/templates/override.conf.j2` – template for override when using inventory vars
- `deploy/DEPLOYMENT.md` – full deployment and troubleshooting guide

## Ansible playbook (Skupper on the robot)

The service runs `configure-skupper.yml` on the robot with `SKUPPER_TOKEN` set. The API must put the **full Kubernetes Secret YAML** (connection token) into that env var (no base64). The playbook:

- Resets MicroShift and downloads kubeconfig (via role)
- Ensures the `skupper` namespace and Skupper CLI
- Applies the token and creates the link
- Deploys and exposes the reverse proxy

All steps use the robot’s kubeconfig and run locally.

## Logging and service management

- Journal: `journalctl -u robot-config-service -f`
- Log file: `/var/log/robot-config-service.log`

```bash
sudo systemctl start robot-config-service   # run once (oneshot)
sudo systemctl status robot-config-service
sudo systemctl restart robot-config-service # run again manually
```

## Troubleshooting

- **401 Unauthorized on redirect**  
  Ensure `API_USERNAME` and `API_PASSWORD` are set in the override (or in inventory) and that the override uses `Environment="API_USERNAME=..."` (not just the value). Reload systemd and restart the service after editing.

- **Tunnel reported up but it isn’t**  
  The service only treats the tunnel as up if `skupper status -n skupper` output contains “connected to” and “other site”. If Skupper is still starting, the service waits and retries (see `TUNNEL_CHECK_*`).

- **Playbook / token errors**  
  See [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) and the Ansible logs. Ensure `SKUPPER_TOKEN` is the full Secret YAML when the playbook runs.


