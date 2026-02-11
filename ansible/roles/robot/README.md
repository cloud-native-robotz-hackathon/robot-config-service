# Robot role

Ansible role used by the Robot Config Service on the robot (MicroShift/Skupper environment).

## Purpose

- **reset-microshift.yaml**: Reset MicroShift (stop, clean, restart) so the robot starts from a clean state before applying Skupper.
- **download-kubeconfig.yaml**: Download or configure the kubeconfig used by the playbook to talk to the robot’s MicroShift (e.g. from a config server or local path).

## Usage

The role is included from `ansible/configure-skupper.yml`:

```yaml
- name: Reset MicroShift
  ansible.builtin.include_role:
    name: robot
    tasks_from: reset-microshift.yaml

- name: Download kubeconfig
  ansible.builtin.include_role:
    name: robot
    tasks_from: download-kubeconfig.yaml
```

That playbook runs on the robot with `connection: local` and uses the inventory at `ansible/inventory` (localhost).

## Requirements

- MicroShift (or compatible Kubernetes) on the target host
- Role is intended to run on the robot, not from a control node

## Variables

See `defaults/main.yml` and `vars/main.yml` for role defaults and variables. The playbook passes `ansible_hostname` and uses a kubeconfig path like `kubeconfig-{{ ansible_hostname }}`.
