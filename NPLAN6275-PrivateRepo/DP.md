# NPLAN-6275 Phase 1 — Developer Progress & Technical Details

> **Wiki purpose:** Engineering implementation details, design decisions, bug RCAs, and code map.
> For PRD / HLD / Phase 2 roadmap → see [`README.md`](./README.md).
> Intended audience: engineers continuing Phase 2, LLM-assisted troubleshooting/enhancement sessions.

---

## Codebase Map

### `npa_publisher_wizard` (Go) — primary repo

| Path | Role |
|---|---|
| `privaterepo/privaterepocore.go` | Core logic: GPG keyring, Docker auth, APT source management |
| `privaterepo/menu.go` | `EnablePrivateRepo()` / `DisablePrivateRepo()` + `aptGetUpdate` injectable |
| `privaterepo/privaterepocore_test.go` | Full test suite for core (mock + concrete on `/tmp`) |
| `privaterepo/menu_test.go` | Enable/disable flow tests incl. `aptGetUpdate` |
| `upgradehelper/upgradehelper.go` | `StartSystemUpdates()`, `isTmpWorldWritable()`, digest comparison, `getDockerAPIToken()` |
| `upgradehelper/osupgrade.go` | OS upgrade path; `changeTmpDirTo777()` |
| `newedge_wizard.go` | CLI flags `--enable_private_repo` / `--disable_private_repo` |
| `wizard/wizard.go` | Interactive menu + `GenerateLogBundle()` |
| `settingshelper/settingshelper.go` | `PrivateRepo.Enabled` field in `settings.json` |
| `nsconfighelper/nsconfighelper.go` | Reads `enable_private_repo` FF from `nsconfig.json` |
| `dockerhelper/dockerhelper.go` | Docker image pull path switch (private vs public namespace) |

### `npa_publisher` (C++) — observability

| Path | Role |
|---|---|
| `src/agent/stitchercommandhandler.cpp` | `populateCapabilities()` — includes `private_repo_enabled` |
| `src/agent/agenthandler.cpp/.h` | `privateRepoIsEnabled()` — reads `sm_nsConfig` |
| `src/agent/publishersettings.cpp/.h` | `NsConfig::remote::publisher.enablePrivateRepo`; `readNsConfig()` |
| `test/agenthandlermock.h` | `MOCK_CONST_METHOD0(privateRepoIsEnabled, bool())` |
| `test/stitchercommandhandler_tests.cpp` | 3 test cases for `private_repo_enabled` capability |

---

## Phase 1 Commits

### ENG-771738: Main Phase 1 wizard code (PR #519)
**Merged:** Mar 17 2026 | **Hash:** `fa82385`

New files / key changes:
- `privaterepo/privaterepocore.go` (~684 lines) — entire APT + Docker + GPG management
- `privaterepo/menu.go` (~202 lines) — `EnablePrivateRepo` / `DisablePrivateRepo` flows
- `privaterepo/privaterepocore_test.go` (~1187 lines) — comprehensive test suite
- `privaterepo/menu_test.go` (~302 lines)
- `newedge_wizard.go` — `--enable_private_repo` / `--disable_private_repo` CLI flags
- `settingshelper/settingshelper.go` — `PrivateRepo` struct added
- `dockerhelper/dockerhelper.go` — switch pull path based on private repo enabled
- `wizard/wizard.go` — menu indicator

Notable decisions captured in commit:
- `ConfigFile.Validate()` — catches missing fields early, before touching system files
- Dynamic `repo_name` from config (falls back to `npapublisher`)
- APT file targeting limited to `sources.list` + Docker CE files only (protects custom PPAs)
- RHEL skip check for `--enable_private_repo` (Ubuntu only in Phase 1)

### ENG-905321: Docker image digest strategy (PR #590)
**Merged:** Mar 17 2026 | **Hash:** `3f0f2fa`

**Problem:** Same image on Docker Hub vs Cloudsmith has different manifest digest but same config digest. Original code compared manifest digests across registries → always showed "update available" even when image was identical.

**Fix in `upgradehelper/upgradehelper.go`:**
- Private repo → use **config digest** (`.config.digest`) via Docker Registry API v2
- Public repo → use **manifest digest** (`RepoDigests` field)
- `getDockerAPIToken()` — adds `Authorization: Basic <base64>` to token request when credentials found in `~/.docker/config.json` (Cloudsmith requires auth to issue bearer token, unlike Docker Hub)

### ENG-924234: Observability — `private_repo_enabled` in assessment (PR #595)
**Merged:** Mar 19 2026 | **Hash:** `80a6ec5`

C++ Publisher reports private repo state in every Stitcher heartbeat:
```json
"capabilities": { "private_repo_enabled": true, ... }
```

**Initial implementation** (commit `51fdb8a`): static `isPrivateRepoEnabled()` in `stitchercommandhandler.cpp` reading `$HOME/resources/nsconfig.json` directly.

**Refactored per code review** (NS-TomYang / ns-wendyh) (commit `eb81bb2`):
- Moved to `AgentHandler::privateRepoIsEnabled()` — same pattern as `baAnyAppIsEnabled()`
- `enablePrivateRepo` field added to `NsConfig::remote::publisher` struct
- Parsed by `readNsConfig()` alongside other nsconfig fields — reuses `sm_nsConfig` already loaded at startup
- `stitchercommandhandler.cpp` now calls `m_agentHandler->privateRepoIsEnabled()` — file stays clean
- Removed `#include <fstream>` from `stitchercommandhandler.cpp`

**Behaviour:**

| `enable_private_repo` in nsconfig.json | Assessment field |
|---|---|
| `"1"` | `true` |
| `"0"` / `""` / missing / malformed file | `false` |

Also added to wizard (`80a6ec5`):
- `GenerateLogBundle()` collects `/etc/apt/*` and `private_repo_config.json` when private repo enabled
- `GetPrivateRepoConfigPath()` exported wrapper

### ENG-948009: Stale APT cache after enable/disable (PR #596)
**Merged:** Mar 25 2026 | **Hash:** `8f446bc`

**Root cause:** `EnablePrivateRepo` and `DisablePrivateRepo` swapped APT sources but never ran `apt-get update`. APT's package metadata cache is keyed to active sources → after source switch, cache is stale → `apt list --upgradeable` returns empty → wizard shows "No system updates currently available" even when upgrades exist.

**Fix in `privaterepo/menu.go`:**
```go
var aptGetUpdate = func(executor exechelper.Executor, label string) {
    if os.Getenv("TEST_PRIV_REPO") == "1" { return }  // no-op in unit tests
    fmt.Printf("Updating package lists from %s", label)
    done := make(chan struct{})
    go func() {  // non-blocking progress dots
        ticker := time.NewTicker(3 * time.Second)
        defer ticker.Stop()
        for { select { case <-ticker.C: fmt.Print("."); case <-done: return } }
    }()
    _, err := executor.ExecCommandWithArgumentReturnOutputBytes("apt-get", "update")
    close(done)
    ...
}
```
- Called after `backupAndReplaceAllRepoFiles()` in `EnablePrivateRepo`
- Called after `restoreAllRepoFilesFromBackup()` in `DisablePrivateRepo`
- **Fixed in:** Build 135.0.0.10669

### ENG-954838: GCP `/tmp` 755 breaks apt-key (PR #599)
**Merged:** Apr 2026

**Root cause:** GCP's `google-startup-scripts` resets `/tmp` to `755` after startup (pre-existing: ENG-540499). `apt-key` runs as `_apt` user (not root) and must create temp files in `/tmp`. With `755`, only root can write → `Couldn't create temporary file /tmp/apt.conf.XXXXXX` → `apt-get update` exits 100 → entire system update aborts.

**Fix in `upgradehelper/upgradehelper.go`:**
```go
var isTmpWorldWritable = func() bool {
    info, err := os.Stat("/tmp")
    if err != nil { return true }  // assume writable if stat fails
    return info.Mode()&0002 != 0
}
```

In `StartSystemUpdates()`:
```go
origTmpPerm := os.FileMode(0)
defer func() {
    if origTmpPerm != 0 {
        executor.ExecCommand("chmod", fmt.Sprintf("%o /tmp", origTmpPerm))
    }
}()
if !isTmpWorldWritable() {
    tmpInfo, _ := os.Stat("/tmp")
    origTmpPerm = tmpInfo.Mode().Perm()   // save original (e.g. 0755)
    executor.ExecCommand("chmod", "777 /tmp")
}
```

- Checks before every `apt-get` run
- Saves original permissions → restores via `defer` after apt completes (per reviewer ns-whsiung)
- Injectable var → unit-testable without touching real `/tmp`
- **Fixed in:** Build 135+

---

## Key Design Decisions

### Why `mv` (not `cp`) for APT source backup
APT reads only exact filenames: `sources.list` and `sources.list.d/*.list`. Files ending in `.bak` are silently ignored. `mv` (not `cp`) ensures zero residual entries — no duplicate repo URLs after enable.

### Why fixed template (not line-by-line URL rewrite)
Original `replaceRepoFileEndpoint()` (~135 lines) parsed each APT source line and substituted the URL domain. **Bug:** Docker CE's path `/linux/ubuntu` was preserved → invalid Cloudsmith URL `/deb/linux/ubuntu` → 404. The CloudSmith support chain (`askCloudSmith.md`) traced this.

**Fix:** Discard old files entirely (via `mv` to `.bak`), inject known-correct Cloudsmith template into `netskopenpa-npapublisher.list`. Removed 135 lines of fragile string-manipulation.

APT template written:
```
deb [signed-by=.../netskopenpa-npapublisher-archive-keyring.gpg]
    https://npa-repository.netskope.com/TOKEN/npapublisher/deb/ubuntu jammy main universe restricted multiverse
deb [...] .../jammy-security main universe restricted multiverse
deb [...] .../jammy-updates main universe restricted multiverse
deb [...] .../jammy-backports main universe restricted multiverse
```

### Cross-registry digest strategy
- Docker Hub → **manifest digest** (`RepoDigests`)
- Cloudsmith → **config digest** (`.config.digest`) — same image, different registry = same config digest but different manifest digest
- `getDockerAPIToken()` sends `Authorization: Basic` header — Cloudsmith (unlike Docker Hub) requires auth to issue a bearer token

### Injectable vars for testability
Pattern used throughout: `var fn = func(...) { ... }` allows tests to override without build tags.

| Injectable var | Purpose |
|---|---|
| `aptGetUpdate` | Skip real `apt-get update` in unit tests (`TEST_PRIV_REPO=1`) |
| `isTmpWorldWritable` | Control `/tmp` perm check in tests |
| `GpgDearmor` | Stub GPG dearmor in menu tests |
| `isPrivateRepoEnabled` | (C++) via mock: `MOCK_CONST_METHOD0(privateRepoIsEnabled, bool())` |

### `TEST_PRIV_REPO=1` env var gate
Set in `setUp(t)` — gates all injectable vars + redirects file paths to `/tmp/test_*`. Must be called in **each sub-test** that modifies state (shared `settingshelper` global state can pollute later sub-tests if `setUp` is skipped).

---

## Test Patterns

### Go unit tests
- `setUp(t)` sets `TEST_PRIV_REPO=1` → path helpers return `/tmp/test_*`; `aptGetUpdate` is no-op
- **Cross-test pollution:** shared `settingshelper` global state. `setUp(t)` must be called per sub-test
- Concrete tests use real `/tmp` files; mock tests use `mocks.Executor` + `mocks.FileManager`
- After `mv` to `.bak`: assert `os.IsNotExist(err)` for original file (it's gone — not just modified)

### C++ unit tests
- `MOCK_CONST_METHOD0(privateRepoIsEnabled, bool())` in `test/agenthandlermock.h`
- Use `EXPECT_CALL(m_agentHandleMock, privateRepoIsEnabled()).WillOnce(Return(true/false))`
- Do **NOT** use old `setenv("HOME")/file-writing` pattern — that was pre-refactoring

---

## Supportability

### Log grep patterns
```bash
# On Publisher machine
grep -i "private\|chmod\|namespace\|401\|gpg\|cloudsmith\|apt-get" logs/publisher_wizard.log | tail -50

# Key log messages to know
"/tmp is not world-writable, chmod to 777 before apt-get"    # ENG-954838 triggered
"Updating package lists from private repository"              # ENG-948009 fix running
"Failed to read Private Repo settings"                        # bad/missing configJSON
"Failed to write GPG keyring"                                 # GPG dearmor failed
```

### Log bundle
When `private_repo_enabled = true`, `GenerateLogBundle` automatically adds:
- `/etc/apt/` directory
- `resources/private_repo_config.json`

### Sanity check after enable
Only these files should change:

| File | Change |
|---|---|
| `/etc/apt/sources.list` | Gone (mv'd to `.bak`) |
| `/etc/apt/sources.list.d/<docker-ce>.list` | Gone (mv'd to `.bak`) |
| `/etc/apt/sources.list.d/netskopenpa-npapublisher.list` | Created |
| `/etc/apt/trusted.gpg.d/netskopenpa-npapublisher-archive-keyring.gpg` | Created |
| `/root/.docker/config.json` | Updated (Cloudsmith auth added) |
| `resources/settings.json` | `PrivateRepo.Enabled` toggled |

`nsconfig.json` and all other files must remain untouched.

---

## Known Open Issues (Phase 1)

| Ticket | Issue |
|---|---|
| ENG-952882 | System updates take longer when private repo enabled (larger package index from Cloudsmith) |
| ENG-955137 | Publisher upgrade fails when `docker_domain` reachable but `dl_domain` blocked |
| ENG-924234 | Merged (C++ observability PR #834) |
| Phase 2 item | `dockerhelper.go:154` — check `UseChinaRegistry()` before private repo namespace |

---

## Phase 2 Progress

### ENG-976617: Script-based install from Cloudsmith (PR #852, `npa_publisher`)

**Goal:** Fresh Publisher installs should use Cloudsmith from day one — no S3, no Docker Hub — mirroring what `--enable_private_repo` does for existing machines.

#### Cloudsmith public bootstrap repo

Files uploaded to `netskopenpa/bootstrap` (public, no token required):
```
https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/bootstrap.sh
https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/provision_shared.sh
https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/cleanup.sh
https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/npa-publisher.ubuntu.service
https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/npa-publisher.centos.service
```

Upload script: `bakery_generic_onthely/upload_to_cloudsmith.sh` (checks CLI, auth, file presence; `--republish` to overwrite).

#### Dual-support install command

```bash
# Standard (S3 / DockerHub) — default, unchanged:
curl -fsSL .../bootstrap.sh | sudo bash

# Private repo (Cloudsmith) — pass token explicitly to sudo:
curl -fsSL https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0/bootstrap.sh \
  | sudo CLOUDSMITH_TOKEN="<token>" \
        S3_PUBLISHER_GENERIC_PATH="https://npa-repository.netskope.com/public/bootstrap/raw/versions/1.0.0" \
        bash
```

**IMPORTANT:** Must use `sudo CLOUDSMITH_TOKEN=... bash`, NOT `sudo bash`. Plain `sudo bash` strips env vars — the token never reaches `provision_shared.sh`. See below.

#### `provision_shared.sh` additions

| Function | When called | What it does |
|---|---|---|
| `is_private_repo()` | throughout | returns true only when `CLOUDSMITH_TOKEN` is explicitly exported |
| `setup_private_repo()` | before `update_packages` | GPG keyring + APT sources + `private_repo_config.json` + `settings.json` |
| `docker_login_private_repo()` | after `install_docker_ce` | `docker login` to register credentials with the running daemon |

`PUBLISHER_REPO` switches to `${DOCKER_DOMAIN}/${REPO_NAME}/netskopeprivateaccess/publisher_u22` when private repo is enabled. `load_publisher_image` uses `docker pull` directly (not `sg docker`) in this path.

#### Key design decisions

**Why `docker login` after `install_docker_ce` (not manual `config.json` write before):**
Docker 29+ with containerd requires credentials to be registered via the daemon API on first run. Writing `/root/.docker/config.json` before Docker is installed is ignored by containerd's credential resolver on first pull. `docker login` makes the proper API call after the daemon is up.

**Why `docker pull` directly (not `sg docker`) for private repo:**
The bootstrap runs as root (`sudo CLOUDSMITH_TOKEN=... bash`). Root has unconditional Docker socket access — no group membership needed. `sg docker` would change HOME to `/home/ubuntu`, making the Docker client look for credentials in `/home/ubuntu/.docker/config.json` (wrong location). Running `docker pull` as root reads from `/root/.docker/config.json` — same as `sudo ./npa_publisher_wizard`.

**Why `sudo CLOUDSMITH_TOKEN=... bash` not `sudo bash`:**
`sudo` strips environment variables by default. `sudo VAR=val bash` passes the variable directly to the sudo invocation — not as inheritance, but as part of the command. This is why `export` in the parent shell isn't enough.

**Docker image path in Cloudsmith:**
```
npa-docker.netskope.com / <repo_name> / <docker_hub_namespace> / <image>
npa-docker.netskope.com / npapublisher / netskopeprivateaccess / publisher_u22
```
Both `REPO_NAME` (Cloudsmith repo) and `netskopeprivateaccess` (Docker Hub org) are required path segments.

#### `fix_docker_ce_apt_source_for_private_repo()` — why docker.com is used during install

During `install_docker_ce`, `add-apt-repository` creates `archive_uri-https_download_docker_com_linux_ubuntu-jammy.list` pointing to download.docker.com. docker-ce is then installed from that source (unavoidable on a cold-start machine with no pre-existing Docker). Immediately after, `fix_docker_ce_apt_source_for_private_repo` backs that file up to `.bak`.

Final APT state is **identical to a wizard-enabled machine**:
- `/etc/apt/sources.list.bak`
- `/etc/apt/sources.list.d/archive_uri-https_download_docker_com_linux_ubuntu-jammy.list.bak`
- `/etc/apt/sources.list.d/netskopenpa-npapublisher.list`

All **future** `apt-get upgrade` (including docker-ce updates) will use Cloudsmith only. The one-time docker.com install is expected and matches how wizard-enabled machines were originally provisioned.

Verified — `apt-get update` after full bootstrap shows only Cloudsmith sources:
```
Get:1 https://npa-repository.netskope.com/.../ubuntu jammy InRelease
Get:2 https://npa-repository.netskope.com/.../ubuntu jammy-security InRelease
Get:3 https://npa-repository.netskope.com/.../ubuntu jammy-updates InRelease
Get:4 https://npa-repository.netskope.com/.../ubuntu jammy-backports InRelease
```

#### Known open issue — `docker ps` requires sudo after private-repo install

When `docker_login_private_repo` runs as root with `HOME=/home/ubuntu` (set by provision_shared.sh), `docker login` writes credentials to `/home/ubuntu/.docker/config.json` **owned by root, mode 600**. When ubuntu user later runs `docker ps`, the Docker CLI hits permission denied on the config file. Fix (not yet implemented): `chown ubuntu:ubuntu /home/ubuntu/.docker/config.json` after `docker login`, or write directly to `/root/.docker/config.json`.

#### RHEL

RHEL is not yet supported for private repo install. If `CLOUDSMITH_TOKEN` is exported on RHEL, a graceful warning is printed and installation continues using standard public repos.

#### Verified

Full bootstrap flow tested end-to-end on AWS Ubuntu 22.04 (ENG-976617 stack):
- APT fetches from `npa-repository.netskope.com` only (zero ubuntu.com/AWS mirror traffic)
- Docker image pulled from `npa-docker.netskope.com/npapublisher/netskopeprivateaccess/publisher_u22`
- Publisher container running after install
- `NPA publisher installation succeeded`

---

<details>
<summary>📎 Reference: Prompt used to generate this wiki</summary>

```
Let's slightly refactor our wiki in @/home/ubuntu/NPA/NPAWiki/NPLAN6275-PrivateRepo/

Based on our context in the aforementioned chat and what we have done in
@/home/ubuntu/NPA/npa_publisher_wizard/NPLAN6275-PrivateRepo/

1. Make the README.md as the overall PRD, HLD in this project (both the expected
   flow in Phase 1 and 2), the PRD: /confluence https://netskope.atlassian.net/wiki/x/74CnUQE
   the Phase-1 design doc: /confluence https://netskope.atlassian.net/wiki/x/mQbHXwE

2. Then, the most important, make DP.md tracking my progress detail, what Phase-1 did,
   the engineering, technical details (some of them from README.md), the bug fixed ...etc

3. The related code changes are in: @npa_publisher_wizard/ mostly in @privaterepo/
   (You can use `git log | grep $TICKET_ID`) from /jira ENG-771732, to check the
   sub-tasks, sub-stories from it.

Please create a wiki for future LLM session to read, the wiki can be used as a context
for AI-based customer troubleshooting, feature enhancement...etc

Aside from each of the doc, make an expandible reference section at the end of document
of this raw prompt for future reference (other devs can use similar prompt to create
project wiki)
```

**To create a similar wiki for another project:**
1. Fetch JIRA epic → get all linked stories
2. `git log --oneline | grep $TICKET_ID` on each story
3. `git show <hash> --stat` to understand scope; `git show <hash>` for commit message details
4. Fetch Confluence design docs + beta guide + test docs
5. Write `README.md` (PRD/HLD) + `DP.md` (engineering details)
6. See [`General/how-to-create-llm-wiki.md`](../General/how-to-create-llm-wiki.md) for full guide

</details>

---

*NPLAN-6275 Phase 1 · Repos: `npa_publisher_wizard` (Go) · `npa_publisher` (C++)*
*Epic: ENG-771732 (P1 done) · ENG-976595 (P2 open)*
