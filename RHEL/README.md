# RHEL NPA Publisher Support (NPLAN-417 / ENG-660862)

> **Wiki purpose:** Persistent context for LLM-assisted development, self-study, and XFN knowledge transfer.
> New session? Start here. Read `General/NPA-Context.md` first for product background.

---

## What Is This?

NPA Publisher was originally Ubuntu 22.04 / Docker CE only. This project adds support for **Red Hat Enterprise Linux (RHEL) 9.x**, which:
- Uses **Podman** (not Docker CE) as the container runtime
- Uses **yum/dnf** (not apt) for OS packages
- Uses **NetworkManager + ifcfg files** (not netplan) for network config
- Has **SELinux enforcing** by default (requires `:z` volume labels)
- Has **firewalld** instead of ufw (requires explicit forward rules for SNAT)

**Status:** Beta since R130. Deployment guide published for GA-level use (RHEL 9.0–9.6).

---

## Architecture Difference vs Ubuntu

```
Ubuntu:   wizard ──► Docker CE daemon ──► containers
                     docker.service

RHEL:     wizard ──► Podman (daemonless) ──► containers
                     podman.socket (socket-activated)
                     docker-compose-plugin (stdin-capable, talks to podman.socket)
```

**Key:** The wizard uses the Docker CLI interface throughout — `docker ps`, `docker restart`, `docker logs`, etc. On RHEL, Podman provides a Docker-compatible API via `podman.socket`. The wizard detects RHEL via `IsRHEL()` and adjusts behaviour where needed.

---

## Installation

Script-based bootstrap (no AMI). Operator runs:

```bash
# RoW
curl -fsSL https://<endpoint>/bootstrap.sh | sudo bash

# China
curl -fsSL https://<aliyun-endpoint>/bootstrap.sh | sudo bash
```

Bootstrap detects RHEL via `/etc/redhat-release`, sets `IS_RHEL=true`, installs Podman + docker-compose-plugin, configures podman.socket, and launches the wizard.

**Requirements:** RHEL 9.x family, 8 GB RAM, 2 CPU cores minimum.

---

## Podman Setup (What `provision_shared.sh` Does)

```
1. Install: podman podman-docker jq
2. Install: docker-compose-plugin from Docker's RHEL repo
3. Enable:  podman.socket  (Docker-compatible API socket)
4. Enable:  podman-restart.service  (auto-restart containers after reboot)
5. Create:  /etc/containers/nodocker  (suppress "emulation" warning)
6. Create:  /etc/containers/registries.conf.d/00-shortnames.conf
           → unqualified-search-registries = ["docker.io"]
           → short-name-mode = "permissive"
7. SELinux: extract wizard to /opt/npa_wizard (not $HOME) when Enforcing
            volume mounts use :z (shared) or :Z (private) label
```

`.bash_profile` waits for `podman.socket` to be ready (instead of `docker.service`).

---

## Codebase Map

### `npa_publisher_wizard` (Go)

| Path | Role |
|---|---|
| `linuxdistro/linuxdistro.go` | `IsRHEL()`, `IsUbuntu()`, `SystemUpdatesAvailable()`, `IsRebootRequired()` |
| `proxyhelper/proxyhelper_rhel.go` | RHEL proxy: `/etc/yum.conf` + `/etc/systemd/system/podman.service.d/publisherwizard.conf` |
| `dockerhelper/dockerhelper.go` | `RestartContainerEngine(usePodman bool)`, `GetContainerLogPath()` (journald), `FindContainerByAncestor()` |
| `networkhelper/networkconfig.go` | `createRHELDHCPNetworkConfig()`, `createRHELDNSConfig()`, ifcfg-* file management |
| `upgradehelper/upgradehelper.go` | `startPublisher()` with `:z` SELinux label; `rhelOsSettingMigration()`, cron service name |
| `wizard/wizard.go` | `ShowMainMenu()` RHEL conditions; `GenerateLogBundle()` + `getActualUserHomeDir()` |

### `npa_publisher` (provisioning scripts)

| Path | Role |
|---|---|
| `bakery_shared/provision_shared.sh` | `is_rhel()`, `install_docker_ce()` (Podman path), firewalld config, SELinux wizard extraction |
| `bakery_generic_onthely/bootstrap.sh` | Platform detection, passes `IS_RHEL/IS_BWAN/IS_CHINA` args |

---

## Key Design Decisions

### Why `docker-compose-plugin` instead of `podman-compose`
`podman-compose` (Python wrapper) does not support stdin piping (`docker compose -f - up -d`). The wizard passes compose config via stdin. `docker-compose-plugin` (official Docker binary) supports stdin and communicates with Podman via `podman.socket`.

### Why `ancestor=new_edge_access:latest` (not `new_edge_access`)
Podman uses fully-qualified image names. `ancestor=new_edge_access` matched all three containers (`new_edge_access:latest`, `new_edge_access/ba_any_app_be:latest`, `new_edge_access/ba_any_app_fe:latest`), causing `docker restart` to fail with multi-ID errors. Adding `:latest` scopes the filter correctly.

### Why journald log handling in `GetContainerLogPath()`
Podman defaults to `journald` log driver (not `json-file`). `docker inspect --format={{.LogConfig.LogPath}}` returns exit code 125 with "can't evaluate field LogPath" on Podman. Detection: check exit code 125 or that error string → export logs via `docker logs <name>` to `/tmp/<name>-journal.log`.

### Why `getActualUserHomeDir()` for log bundle
Under `sudo`, `$HOME` may still point to the original user's home (not `/root`). The actual publisher home (where logs live) is resolved via `$SUDO_USER` env var. Without this, log bundle on RHEL missed all publisher logs.

### Why `:z` volume label for SELinux
SELinux Enforcing denies container access to host volumes without a label. `:z` = shared label (multiple containers can access), `:Z` = private label (single container). The wizard uses `:z` for the resources bind mount.

---

## RHEL-Specific Behaviours (vs Ubuntu)

| Behaviour | Ubuntu | RHEL |
|---|---|---|
| Container runtime | Docker CE daemon | Podman + podman.socket |
| Package manager | apt-get | yum / dnf |
| Updates available exit code | 0 | `yum check-updates` returns **100** when updates exist |
| Reboot required check | `/var/run/reboot-required` exists | `needs-restarting -r` exit code != 0 |
| Cron service | `cron` | `crond` |
| DNS management | systemd-resolved | NetworkManager + `/etc/resolv.conf` |
| Network config files | netplan (`/etc/netplan/`) | ifcfg (`/etc/sysconfig/network-scripts/ifcfg-*`) |
| Proxy config | `/etc/apt/apt.conf.d/` + docker service.d | `/etc/yum.conf` + podman service.d |
| Log driver | json-file | journald |
| SELinux | Not enforcing | Enforcing (requires `:z` volume labels) |
| Firewall | ufw | firewalld |
| China Docker repo | `centos/docker-ce.repo` | `rhel/docker-ce.repo` |

---

## Bugs Fixed

### SNAT blocked by firewalld (ENG-783905)
**Root cause:** firewalld blocks forwarded traffic by default. Publisher correctly sets up `iptables SNAT_FORWARD` rules, but traffic from `tun0 → eth0` is denied by `FWD_DENIED_GENERIC`. SNAT mode completely broken; NoNAT mode unaffected.
**Fix:**
```bash
firewall-cmd --permanent --zone=publisher_tunnel --set-target=ACCEPT
firewall-cmd --permanent --zone=publisher_tunnel --add-forward
firewall-cmd --reload
```
**Verified:** iperf3 throughput 107 Mbits/sec after fix.

### SSH lockout from legacy MAC spec (provision_shared.sh)
**Root cause:** `hardening_ssh()` appended `MACs hmac-sha1,umac-64@openssh.com,hmac-ripemd160` to `sshd_config`. RHEL 9's newer OpenSSH rejects `hmac-ripemd160` as invalid → sshd crash-loops → SSH locked out. The `is_cent_os` guard in `provision_shared.sh` incorrectly applied to RHEL.
**Fix:** Remove/skip the legacy MAC spec in the RHEL provisioning path.

### Proxy config not working on RHEL (ENG-829067)
**Root cause:** Ubuntu proxy writes to `/etc/apt/apt.conf.d/` and `docker.service.d/`. Neither exists on RHEL.
**Fix:** `proxyhelper_rhel.go` writes to `/etc/yum.conf` (`proxy=http://...`) and `/etc/systemd/system/podman.service.d/publisherwizard.conf` (`Environment=http_proxy=...`).

### China `.prc_dp` marker lost on reboot
**Root cause:** Marker written to `/tmp/.prc_dp` which is cleared on reboot.
**Fix:** Write to `$ORIGINAL_HOME/.prc_dp` which persists.

### `yum update` fails on dependency conflicts
**Root cause:** Upstream RHEL repo sync issues cause transient dependency conflicts.
**Fix:** Added `--skip-broken` fallback: try `yum update` first; on failure, retry with `--skip-broken`.

---

## Known Issues / Gaps

| Issue | Status |
|---|---|
| Podman short-name resolution warning on first launch | Cosmetic — wizard still starts; tracked ENG-773483 |
| China region: `UseChinaRegistry()` before private repo namespace check | Phase 2 item (`dockerhelper.go:154`) |
| SNAT (Private App Tunneling) broken until firewalld forward enabled | Documented fix; must confirm baked into provision_shared.sh |

---

## Feature Gap vs Ubuntu (Confluence 5590254704)

| Feature | Ubuntu | RHEL | Notes |
|---|---|---|---|
| Wizard UI menus | ✓ | ✓ | Fixed ENG-660868 |
| System package upgrade | ✓ | ✓ | yum path |
| Docker image upgrade | ✓ | ✓ | Fixed ENG-661068 |
| Network settings | ✓ | ✓ | Fixed ENG-661097 (ifcfg) |
| Troubleshooter | ✓ | ✓ | Fixed ENG-661100 |
| Auto-upgrade | ✓ | ✓ | Fixed ENG-740964 |
| Proxy config | ✓ | ✓ | Fixed ENG-829067 |
| Log bundle | ✓ | ✓ | Fixed ENG-833897 |
| SNAT (Private App Tunneling) | ✓ | ⚠️ | Needs firewalld forward enabled |
| Private Repo (NPLAN-6275) | ✓ | Phase 2 | Not yet integrated |

---

## China Region

- Docker repo: `mirrors.aliyun.com/docker-ce/linux/rhel/`
- Container registry: Aliyun ACR
- `$releasever` substitution (`sed -i "s/$releasever/9/g"`) removed — no longer needed with official RHEL repo
- `.prc_dp` marker: `$ORIGINAL_HOME/.prc_dp` (not `/tmp`)

---

*RHEL Support · Beta since R130 · Epic: ENG-660862 / NPLAN-417*
*NPA Publisher context: see `General/NPA-Context.md`*
