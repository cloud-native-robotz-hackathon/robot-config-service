# Robot Config Service

A Python service that runs on a Raspberry Pi (or similar) robot to automatically configure Skupper tunnels to an OpenShift cluster based on event IDs. The robot cannot reach the data center directly; it uses a fixed redirect URL and API (with basic auth) to get the current event and a Skupper token, then runs an Ansible playbook to establish the tunnel.

Important env. variables:

|Variable|Example value|Description|
|---|---|---|
|RCS_GIT_REPO|`https://github.com/cloud-native-robotz-hackathon/robot-auto-register-78b09.git`|Repo to fetch hub controller url|
|RCS_GH_TOKEN|`github_pat_xxxx`| GitHub Token to have access to private repo. For the repo `robot-auto-register-78b09` it's in Bitwarden.|
|RCS_HUBCONTROLLER_USER|`admin`|Username to access hub controller|
|RCS_HUBCONTROLLER_PASSWORD|`password`|Password to access hub controller, documented in Bitwarden|

## Local development

### robot-config-service.py at the laptop

Create a copy of `robot-config-service.env.example`: `cp -v robot-config-service.env.example robot-config-service.env`. Adjust `robot-config-service.env`

```shell
podman build -t local -f Containerfile.development .
podman run -v $(pwd):/opt/app-root/src --user 0 -ti --rm local bash 
source robot-config-service.env
python robot_config_service.py
```

### Ansible at the robot

```shell
ssh -l root -i ~/.ssh/robot-hackathon gort
# File  /opt/robot-config-service/skupper-token have to exist
cd /opt/robot-config-service/ansible/
ansible-playbook -i inventory configure-robot.yml

```
