# Multi-Robot Deployment Guide

This guide explains how to deploy the Robot Config Service to multiple robots using Ansible.

## Prerequisites

1. **Control Machine** (where you run Ansible):
   - Ansible 2.9+ installed
   - SSH access to all robots
   - Python 3 installed

2. **Robots** (target machines):
   - Ubuntu (tested on Ubuntu 20.04+)
   - SSH access with sudo privileges
   - Python 3 installed
   - Network connectivity to OpenShift cluster

## Quick Start

1. **Create inventory file** (YAML format; use `.yml` extension so Ansible parses it correctly):
   ```bash
   cp deploy/inventory.robots.example deploy/inventory.robots.yml
   nano deploy/inventory.robots.yml
   ```

2. **Add your robots to inventory:**
   ```yaml
   robots:
     vars:
       ansible_user: root
       ansible_ssh_private_key_file: ~/.ssh/robot-hackathon
     hosts:
       abcwarrior:
         ansible_host: 192.168.122.180
       robot2:
         ansible_host: 192.168.1.102
   ```

3. **Test connectivity:**
   ```bash
   ansible robots -i deploy/inventory.robots.yml -m ping
   ```

4. **Deploy to all robots:**
   ```bash
   ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml
   ```

5. **Configure each robot:**
   ```bash
   # SSH to each robot and edit override file
   ssh root@192.168.122.180   # or your robot host
   sudo nano /etc/systemd/system/robot-config-service.service.d/override.conf
   ```
   Set `API_USERNAME`, `API_PASSWORD`, and `REDIRECT_URL` (see Step 4 below). Then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl start robot-config-service
   ```

## Detailed Steps

### Step 1: Prepare Inventory

Edit `deploy/inventory.robots.yml` with your robot information. Use **YAML format** and a `.yml` extension so Ansible applies group variables correctly:

```yaml
robots:
  vars:
    ansible_user: root
    ansible_ssh_private_key_file: ~/.ssh/robot-hackathon
  hosts:
    abcwarrior:
      ansible_host: 192.168.122.180
    robot2:
      ansible_host: 192.168.1.102
```

**Options:**
- Use hostnames as Ansible host names and `ansible_host` for the real IP or hostname
- Set `ansible_user` (e.g. `root` or `ubuntu`) and `ansible_ssh_private_key_file` under `vars:` for all robots
- Add per-host vars (e.g. `team: team-1`) under each host if needed

### Step 2: Test Connectivity

```bash
# Ping all robots
ansible robots -i deploy/inventory.robots.yml -m ping

# Check Python version
ansible robots -i deploy/inventory.robots.yml -m command -a "python3 --version"

# Check sudo access
ansible robots -i deploy/inventory.robots.yml -m command -a "sudo whoami" --become
```

### Step 3: Deploy Service

```bash
# Deploy to all robots
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml

# Deploy to specific robot
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --limit abcwarrior

# Dry run (check mode)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --check
```

### Step 4: Configure Each Robot

**Option A: From inventory (recommended)**  
Set `api_username`, `api_password`, and `redirect_url` in your inventory (group or host vars). The playbook will generate the override file from `deploy/templates/override.conf.j2`:

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

Then run the playbook (or only the config/override step):

```bash
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml
# Or update only override: --tags config
```

Optional inventory vars (see template): `ansible_playbook_path`, `log_level`, `tunnel_check_initial_delay`, `tunnel_check_retries`, `tunnel_check_interval`, `redirect_url_is_cluster`, `redirect_retries`, `redirect_retry_delay`.

**Note:** If you store credentials in the inventory, keep that file private (e.g. `deploy/inventory.robots.yml` is in `.gitignore`).

**Option B: Manual configuration on each robot**  
If you do not set the vars in inventory, copy the example override and edit it on each robot. The override must use **`KEY=VALUE`** for each variable:

```bash
sudo nano /etc/systemd/system/robot-config-service.service.d/override.conf
```

```ini
[Service]
Environment="API_USERNAME=admin"
Environment="API_PASSWORD=your_password"
Environment="REDIRECT_URL=https://your-redirect-service.example.com/redirect"
```

Then reload systemd and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl start robot-config-service
```

### Step 5: Start Service

```bash
# Start on all robots
ansible robots -i deploy/inventory.robots.yml -m systemd \
  -a "name=robot-config-service state=started" --become

# Check status
ansible robots -i deploy/inventory.robots.yml -m systemd \
  -a "name=robot-config-service" --become
```

## Advanced Usage

### Deploying to Specific Groups

```bash
# Deploy only to a specific robot
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --limit abcwarrior

# Deploy to robots matching pattern
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --limit "robot*"
```

### Updating Existing Deployment

The playbook is idempotent and supports **tags** for partial updates:

```bash
# Full update (all files and config)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml

# Update only the Python service script (e.g. after code changes)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags python

# Update only Ansible playbook, inventory, and roles
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags ansible

# Update only systemd unit and override
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags systemd

# Install or update only dependencies (apt, pip, kubernetes.core)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags deps
```

After updating the Python script, restart the service on the robots:

```bash
ansible robots -i deploy/inventory.robots.yml -m systemd \
  -a "name=robot-config-service state=restarted" --become
```

### Checking Service Status

```bash
# Check service status on all robots
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "systemctl status robot-config-service" --become

# View logs
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "journalctl -u robot-config-service -n 50" --become

# Check log file
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "tail -n 50 /var/log/robot-config-service.log" --become
```

## Troubleshooting

### Connection Issues

```bash
# Test SSH connection (use your inventory user and key)
ssh -i ~/.ssh/robot-hackathon root@192.168.122.180

# Test with verbose output
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml -vvv
```

### Permission Issues

```bash
# Ensure ansible_user has sudo access (if not root)
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "echo 'ansible_user ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ansible" \
  --become
```

### Service Issues

```bash
# Check service logs
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "journalctl -u robot-config-service -n 100 --no-pager" --become

# Verify override config (must have API_USERNAME=, API_PASSWORD=, REDIRECT_URL=)
ansible robots -i deploy/inventory.robots.yml -m shell \
  -a "cat /etc/systemd/system/robot-config-service.service.d/override.conf" \
  --become
```

## Inventory Examples

Use **YAML format** (`inventory.robots.yml`) so group variables are applied correctly.

### Using SSH keys and root

```yaml
robots:
  vars:
    ansible_user: root
    ansible_ssh_private_key_file: ~/.ssh/robot-hackathon
  hosts:
    abcwarrior:
      ansible_host: 192.168.122.180
    robot2:
      ansible_host: 192.168.1.101
```

### With per-host or team variables

```yaml
robots:
  vars:
    ansible_user: root
    ansible_ssh_private_key_file: ~/.ssh/robot-hackathon
  hosts:
    gort:
      ansible_host: 192.168.8.105
      team: team-1
    robot2:
      ansible_host: 192.168.1.102
      team: team-2
```

### Using a different user (e.g. ubuntu with sudo)

```yaml
robots:
  vars:
    ansible_user: ubuntu
    ansible_ssh_private_key_file: ~/.ssh/robot_key
    ansible_ssh_common_args: '-o StrictHostKeyChecking=no'
  hosts:
    robot1:
      ansible_host: 192.168.1.100
```

## Best Practices

1. **Use SSH keys** instead of passwords
2. **Use the YAML inventory** (`inventory.robots.yml`) so group vars apply correctly
3. **Test on one robot** before deploying to all (`--limit abcwarrior`)
4. **Use tags** for fast partial updates (e.g. `--tags python` after code changes)
5. **Keep inventory in version control** (exclude secrets)
6. **Document robot-specific configurations**

## Playbook Tags

The playbook supports tags for selective execution:

| Tag        | Use case |
|-----------|----------|
| `python`  | Update only the Python service script (and ensure install dirs exist). Use after code changes. |
| `ansible` | Update only the Ansible playbook, inventory, and roles on the robot. |
| `systemd` | Update only systemd unit file and override template. |
| `config`  | Generate override.conf from inventory vars (api_username, api_password, redirect_url). Use with systemd. |
| `deps`    | Install or update only dependencies (apt, pip, kubernetes.core collection). |

```bash
# Update only the Python file (most common after development)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags python

# Update only Ansible assets (configure-skupper.yml, inventory, roles)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags ansible

# Update only systemd unit and override (or generate override from inventory with --tags config)
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags systemd

# Regenerate override from inventory variables only
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags config

# Install or update only dependencies
ansible-playbook -i deploy/inventory.robots.yml deploy/deploy-robots.yml --tags deps
```

After a `--tags python` run, restart the service on the robots so the new code is used.

## See Also

- [../README.md](../README.md) - Main documentation
