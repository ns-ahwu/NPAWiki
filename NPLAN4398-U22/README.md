# NPLAN-4398 — Publisher Host OS Upgrade: Ubuntu 20 → 22

> **Wiki purpose:** Full context for LLM sessions working on AI-based customer troubleshooting, feature enhancement, or bug investigation for the Publisher Host OS upgrade feature.
> New session? Start here. This page bootstraps full project context in one read.

---

## What Is This?

- Ubuntu 20.04 reached end-of-support in 2024; all existing NPA Publisher VMs must be upgraded to Ubuntu 22.04.
- The challenge: the Publisher is a hardened appliance VM — the upgrade must happen non-interactively, survive reboots, restart services cleanly, and work in both manual (admin-initiated) and unattended/auto-upgrade modes.
- **The fix:** A two-phase upgrade flow implemented in Go inside `npa_publisher_wizard`, coordinated with Stitcher (DP) and Management Plane (MP). Phase 1 = system package prep + reboot. Phase 2 = `do-release-upgrade` + final reboot.

**Epic:** [ENG-490476](https://netskope.atlassian.net/browse/ENG-490476) — closed  
**Core stories (ahwu):** [ENG-490524](https://netskope.atlassian.net/browse/ENG-490524) (manual), [ENG-490526](https://netskope.atlassian.net/browse/ENG-490526) (auto)

---

## Architecture

### Before (no HostOS upgrade capability)
```
Stitcher ──k_msg_type_publisher_upgrade──► Publisher container
                                           (docker image upgrade only)
```

### After (NPLAN-4398)
```
MP (PATCH /orca/publishers/v2/upgrade_check)
  ↓ need_os_upgrade + tag + pkg_upgrade
Stitcher ──k_msg_type_publisher_upgrade_v2──► New Publisher (NP)
         ──k_msg_type_publisher_upgrade────► Old Publisher (OP, backward compat)
                                           ↓
                                    resources/trigger_upgrade (JSON)
                                           ↓
                               npa_publisher_wizard -upgrade_unattended
                               (AutoUpgrader state machine)
                                           ↓
                        ┌──────────────────────────────────┐
                        │  osStart → osFinish → sysStart   │
                        │        → dockerStart → allDone   │
                        └──────────────────────────────────┘
```

**Manual path:** Admin runs `sudo ./npa_publisher_wizard` → menu → "Upgrade the Operating System to Ubuntu 22" → `--start-os-upgrade` logic → reboot → `--finish-os-upgrade` cron hook.

**Auto path:** Stitcher → trigger_upgrade file → wizard cronjob invokes `-upgrade_unattended` on each wake-up, reads/writes state file, one phase per invocation.

### Key Architectural Decisions

| Decision | Why the naive approach was rejected | What was chosen |
|---|---|---|
| Two-phase split (prep + do-release-upgrade) | `do-release-upgrade` must run after a fresh reboot; cannot do both in one process lifetime | Phase 1 ends with reboot; a cron hook (`/etc/cron.d/post-reboot-upgrade`) triggers Phase 2 |
| State machine file (`auto_upgrade_state`) for auto upgrade | The wizard process dies at every reboot; no in-memory state survives | Persisted integer state file (0–4), one phase per cronjob invocation |
| `DEBIAN_FRONTEND=noninteractive` + `--force-confold --force-confdef` | Interactive prompts (pink debconf screen, apt "keep/replace" questions) would block indefinitely | Set debconf noninteractive via `debconf-set-selections` at start; roll back on failure |
| Stage-wise rollback (`rbFuncs` slice) | A partial failure could leave the system in an unusable half-upgraded state (noexec /tmp, noninteractive debconf stuck) | Each stage registers a rollback function; `executeRollBackFunctions()` unwinds in order on failure |
| `SkipOSUpgrade()` EHF check | EHF (emergency hotfix) builds on old publishers must not trigger an OS upgrade | Wizard checks an S3/Aliyun endpoint with the docker tag; returns true → skip OS upgrade |
| Use `pgrep -f` + `BlockUserInputLoggingPostRebootUpgrade()` | While do-release-upgrade is running post-reboot, re-entering the wizard UI would show a blank menu or race | Wizard detects its own post-reboot process is running and switches to live log tail mode |
| Upgrade Barrier concept | Some publisher releases cannot support HostOS upgrade (pre-R125 wizard); upgrading HostOS on them would break the Publisher | `can_os_up` capability flag in publisher assessment; MP/Stitcher gates OS upgrade on this key |

---

## Phases / Release Timeline

| Phase | Tickets | Release | Scope |
|---|---|---|---|
| Manual upgrade | ENG-490524, ENG-503464, ENG-503465 | R121 / build ~9096 | UI menu entry, prep + do-release-upgrade, stage-wise errors |
| Auto upgrade | ENG-490526, ENG-507144, ENG-507116 | R125 / build ~9409 | Trigger file, state machine, Stitcher V2 API, upgrade barrier |
| Bug fixes | ENG-575557, ENG-611757, ENG-611371 | R126+ | Guacamole retag, PAM so missing, release-upgrade-available file cleanup |

---

## How It Works

### Manual Upgrade Flow (step by step)

1. Admin launches `sudo ./npa_publisher_wizard` (interactive, TTY required).
2. Selects: Upgrade → "Upgrade the Operating System to Ubuntu 22".
3. Wizard calls `startOSUpgrade()` in `wizard/wizard.go`.
4. `ExecuteHostOSUpgrade(hu, false)` — **Phase 1 (prep)**:
   - Stop Publisher container (+ Guacamole if AnyApp enabled).
   - Remount `/tmp` with exec (if mounted noexec).
   - Set debconf noninteractive.
   - Unhold `docker-ce*` packages.
   - Fix grub debconf (EFI-not-supported systems only) — `FixGrubSettingsForPackageUpgrade`.
   - Run: `apt-get update`, `--fix-broken install`, `upgrade` (force-confold), `dist-upgrade`, `autoremove`, `clean`.
   - Fix PAM module (`pam_tally2.so` → `pam_faillock.so`) if `pam_faillock.so` exists.
   - Write cron hook to `/etc/cron.d/post-reboot-upgrade`: `@reboot root ./npa_publisher_wizard --finish-os-upgrade`.
   - Reboot.
5. On reboot, cron hook fires: `./npa_publisher_wizard --finish-os-upgrade`.
6. `FinishHostOSUpgradeAndPostUpgradeProcess(hu)` → `ExecuteHostOSUpgrade(hu, true)` — **Phase 2**:
   - Stop `google-startup-scripts` (GCP) to avoid `/tmp` permission race.
   - Remount `/tmp` exec again.
   - Set debconf noninteractive again.
   - Unhold docker packages.
   - Delete `/var/lib/ubuntu-release-upgrader/release-upgrade-available` (ENG-611371).
   - Run `do-release-upgrade -f DistUpgradeViewNonInteractive`.
7. On success: `PostHostOSUpgradeSuccess()` — fix docker apt repo (`add-apt-repository`), start Publisher + Guacamole, write `finished-hostos-upgrade` flag, reboot.
8. On failure: `PostHostOSUpgradeFailed()` — rollback, start Publisher + Guacamole, write `failed-hostos-upgrade` flag.
9. After final reboot, wizard's `PrintOSUpgradeResultMessage()` shows a one-time success/failure banner and removes the flag file.

While Phase 2 runs, if admin re-enters the wizard it detects the upgrade process and shows live log tail: `"Host OS upgrade is currently in progress, please wait..."`.

### Auto Upgrade Flow (step by step)

1. Stitcher sends `k_msg_type_publisher_upgrade_v2` to Publisher (new publishers only).
2. Publisher writes `resources/trigger_upgrade`:
   ```json
   {
     "host_os_upgrade": true,
     "package_upgrade": true,
     "publisher_image_tag": "9410"
   }
   ```
3. Cronjob triggers: `./npa_publisher_wizard -upgrade_unattended`.
4. `ExecuteUnattenededAutoUpgrade()` reads trigger file (no state file) → determines start state:
   - `osStart` (0) if `host_os_upgrade == true` AND `SkipOSUpgrade()` returns false.
   - `sysStart` (2) if no OS upgrade needed.
5. State machine advances one step per invocation, persisted in `auto_upgrade_state`:
   - `0 osStart` → Phase 1 prep (same as manual, no reboot in this path — reboot handled externally).
   - `1 osFinish` → Phase 2 do-release-upgrade.
   - `2 sysStart` → `StartSystemUpdates()` (apt upgrade).
   - `3 dockerStart` → `StartPublisherUpgrade()` (pull + retag + replace wizard + launch new wizard).
   - `4 allDone` → cleanup trigger file + state file, write `upgrade_failed_reason` if any error.
6. `ExecuteUnattenededAutoUpgradePostWorkflow()` handles reboot (if required) or final cleanup.

---

## Codebase Map

| File | Role |
|---|---|
| `newedge_wizard.go` | Entry point. Dispatches all CLI args: `--finish-os-upgrade` (Option 12), `-upgrade_unattended` (Option 8), interactive menu (Option 1) |
| `upgradehelper/osupgrade.go` | `HostOSUpgrader` — all Host OS upgrade logic: stage constants, prep, do-release-upgrade, rollback, post-upgrade success/fail, disk space check, skip-OS-upgrade EHF check |
| `upgradehelper/upgradehelper.go` | `StartPublisherUpgrade`, `StartSystemUpdates`, `StartUpgradeProcess`; also publisher image pull/verify/retag, docker repo digest utilities |
| `wizard/autoupgrade.go` | `AutoUpgrader` state machine for unattended mode; `ExecuteUnattenededAutoUpgrade`, `ExecuteUnattenededAutoUpgradePostWorkflow` |
| `wizard/wizard.go` | `startOSUpgrade()` (manual path entry), `GetHostOSUpgradeMenu()`, `PreCheckAutoUpgrade()`, `FinishAutoUpgrade()`, `PostAutoUpgradeFailed()` |
| `filehelper/filemanager.go` | `GetAutoUpgradeInfo()` — parses `trigger_upgrade` JSON into `UpdateInfo{HostOsUpgrade, PkgUpgrade, PublisherImageTag}` |
| `hwspechelper/hwspechelper.go` | `GetDiskSpace()` — used by `CheckDiskSpaceEnough()` before starting upgrade |

---

## Key State / File Paths

| Path (relative to wizard home) | Purpose |
|---|---|
| `resources/trigger_upgrade` | JSON written by Publisher; read by wizard to start auto upgrade |
| `auto_upgrade_state` | Persisted state integer (0–4) for auto upgrade state machine; deleted on completion |
| `resources/upgrade_failed_reason` | Written by `WriteUpgradeFailedReason()`; contains error code + timestamp |
| `finished-hostos-upgrade` | One-time flag; triggers success banner on next wizard launch, then deleted |
| `failed-hostos-upgrade` | One-time flag; triggers failure banner + appends message to wizard log |
| `/etc/cron.d/post-reboot-upgrade` | Cron hook: `@reboot root ./npa_publisher_wizard --finish-os-upgrade`; written before 1st reboot, removed on success/fail |
| `/var/lib/ubuntu-release-upgrader/release-upgrade-available` | Ubuntu's internal flag; deleted before `do-release-upgrade` to avoid stale state (ENG-611371) |

---

## Error Codes

Error codes are typed `UpgradeErrorCode` (int), categorised by high byte:

| Category | High byte | Examples |
|---|---|---|
| Success | `0x00` | `ErrSuccess` = 0 |
| Host OS Upgrade (legacy) | `0x02` | `ErrHostOSNotEnoughSpace` (519), `ErrHostVersionNotLatest` (520) |
| Publisher Upgrade | `0x03` | `ErrPublisherDockerPull` (769), `ErrPublisherSameImageID` (770) |
| Host-Level OS Upgrade (NPLAN-4398) | `0x06` | `ErrHostOSFailedStopPublisher`, `ErrAptUpdate`, `ErrDoRelUpgrade`, `ErrOSUpgradeDiskSpaceNotEnough` |

The auto upgrade path writes the final error code + Unix timestamp to `resources/upgrade_failed_reason` for MP/Stitcher to read.

---

## Bugs Fixed

| Bug | Root cause | Fix | Ticket |
|---|---|---|---|
| Guacamole not restarted after HostOS upgrade | `PostHostOSUpgradeSuccess` called `StartStoppedPublisher` but not Guacamole retag | Added `StartAndRetagGuacamoleContainer` call in post-upgrade success path | ENG-575557 |
| PAM fix fails when `pam_faillock.so` absent | `fixPamModule` returned error even if the target `.so` didn't exist (e.g. AWS machines) | Added early-return guard: skip fix if `pam_faillock.so` missing | ENG-611757 |
| `do-release-upgrade` fails because "upgrade already available" stale file | `/var/lib/ubuntu-release-upgrader/release-upgrade-available` from previous run caused do-release-upgrade to skip or fail | Delete the file in `PrepareUpgrade()` before running do-release-upgrade | ENG-611371 |
| `libpam-systemd` upgrade failure blocks reboot into Phase 2 | Ubuntu pushed `systemd`/`libpam-systemd` update that broke in-flight; pam module mismatch | `fixPamModule()` replaces all `pam_tally2.so` refs with `pam_faillock.so` in `/etc/pam.d/*` | ENG-517804 |
| Auto upgrade reboot hook not removed on failure | Rollback functions list was not populated for auto upgrade path, so cron hook remained after failure | `rbFuncs` now includes `rollBackAssignRebootHook` in the `isAfterReboot` branch | ENG-490526 |

---

## Test Patterns

### Manual Upgrade Test (key steps)
1. Provision Ubuntu 20 publisher VM with easter egg (`touch ~/easter_egg`) for QA builds.
2. Run `sudo ./npa_publisher_wizard` → Upgrade → "Upgrade the Operating System to Ubuntu 22".
3. Wait for 1st reboot (pkg prep). During do-release-upgrade, log output should stream live.
4. Expect a 2nd reboot (PAM-related package post-upgrade). This is known/expected.
5. After final reboot: one-time "Host Operating System successfully upgraded" banner.
6. Verify: `lsb_release -a` → Ubuntu 22.04; Docker running; Publisher tunnel active; TCP/UDP flows.
7. Error case: trigger race condition during dist-upgrade (run `sudo apt-get update` concurrently) — wizard should detect failure, rollback, restart Publisher, show failure message.

### Auto Upgrade Test Matrix (ENG-490526 / test doc 4991647790)

| Case | From | To | Expected |
|---|---|---|---|
| 01 | <R125 (u20) | R125 (u20, barrier) | Image = 9409, HostOS stays Ubuntu 20 |
| 02 | <R125 | >R125 (barrier+1) | Auto upgrade FAILS — pre-R125 wizard has no `publisher_u22` namespace knowledge |
| 04 | R125 (u20) | >R125 (u22, barrier+1) | Image = 9410, HostOS upgraded to Ubuntu 22, ~30 min on t2.micro |
| 05 | >R125 (u22) | R125 (u22) | Downgrade path — image = 9409, HostOS remains Ubuntu 22 (already upgraded) |

### Injectable Test Variables
- `chinaDockerSigV1URLOverride` / `chinaDockerSigV2URLOverride`: set via `--sigv1 <url> --sigv2 <url>` CLI flags or `SetChinaSigURLOverrides()` — override Alicloud OSS signature URLs for testing.
- `upgradeDockerAndReplaceWizard` / `getFreeDiskSpace` / `retrievePublisherImageSize`: package-level vars in `upgradehelper`, swappable in tests.
- `isPrivateRepoEnabled`: overridable func var for private repo digest path testing.
- `GetUpgradeOSErrorForTest(state int)` — returns private error values for unit test mock injection.
- Disk space check bypass: `touch bypass_check_free_space` (relative to wizard home).

---

## Observability / Troubleshooting Guide

### Customer Upgrade Stuck or Failed — What to Check

1. **Log file:** `logs/publisher_wizard.log` (or `logs/local_broker_wizard.log` for LBr). The stage log format is: `======[Host OS Upgrade]==== <success|failed> <stage name>`.
2. **One-time flag files:** `finished-hostos-upgrade` or `failed-hostos-upgrade` in wizard home — if either exists, the upgrade just completed.
3. **Cron hook left behind:** `/etc/cron.d/post-reboot-upgrade` — if present after upgrade completion it means rollback did not delete it; safe to remove manually.
4. **Failed reason file:** `resources/upgrade_failed_reason` — contains error code (int) and Unix timestamp from the auto upgrade path.
5. **State file stuck:** `auto_upgrade_state` — contains integer 0–4; safe to inspect. If stuck at 1 (`osFinish`), Phase 2 do-release-upgrade may have failed. Delete file + trigger file to retry from scratch.
6. **Disk space:** Minimum 5 GB free required for HostOS upgrade (`ReqSpaceOSUpgrade = 5` in osupgrade.go). Use `df -h`.
7. **Skip OS upgrade for EHF:** If the publisher is running an EHF tag, wizard checks `https://s3.us-west-2.amazonaws.com/publisher.netskope.com/latest/skip_os_upgrade/<tag>` — returns 200 = skip. For QA builds, URL uses the `qa/` prefix.

### Common Errors

| Symptom | Likely cause | Action |
|---|---|---|
| Upgrade stops after 1st reboot, Publisher not starting | Phase 2 cron hook fired but `do-release-upgrade` failed | Check `logs/publisher_wizard.log` for `stageDoRelUpgrade failed`; check `/var/log/dist-upgrade/` |
| "Disk space is NOT enough" in log | Less than 5 GB free | Free up disk space; re-run upgrade |
| "failed to fix pam module" log but no actual error | `pam_faillock.so` absent on this machine type | Harmless since ENG-611757; upgrade continues |
| Publisher container not running after successful upgrade | Guacamole (AnyApp) start failed | Check `settingshelper.ReadSettings().BrowserAccessAnyApp.Enabled`; Guacamole container logs |
| Auto upgrade keeps restarting from osStart | `auto_upgrade_state` file deleted or corrupt | State file is written after each successful stage; if missing, wizard restarts from trigger file |
| "OS is the latest, skip Host OS upgrade" from post-reboot hook | Hook fired after OS already upgraded | Normal — wizard checks OS version before do-release-upgrade in `--finish-os-upgrade` path |

---

## Backward Compatibility

- **Old publisher (pre-R125):** Stitcher sends `k_msg_type_publisher_upgrade` (V1) — docker image upgrade only. No HostOS upgrade triggered.
- **New publisher (R125+):** Stitcher sends `k_msg_type_publisher_upgrade_v2` with `host_os_upgrade` flag. Publisher writes `trigger_upgrade` JSON. Wizard reads and acts.
- **BWAN systems:** Explicitly skipped for all OS upgrade paths (`utils.IsBwanSystem()` check).
- **RHEL/CentOS publishers:** HostOS upgrade is Ubuntu-only. RHEL publishers skip all `osupgrade.go` paths.

---

## Confluence / Design Docs

| Doc | URL |
|---|---|
| Design doc — Manual upgrade | https://netskope.atlassian.net/wiki/spaces/NH/pages/4697227279 |
| Design doc — Auto upgrade | https://netskope.atlassian.net/wiki/spaces/NH/pages/4669014262 |
| Technical details — Auto upgrade integration flow | https://netskope.atlassian.net/wiki/spaces/NH/pages/4767253439 |
| Test docs (parent) | https://netskope.atlassian.net/wiki/spaces/NH/pages/5142480603 |
| Test doc — E2E Auto Upgrade with Upgrade Barrier | https://netskope.atlassian.net/wiki/spaces/NH/pages/4991647790 |
| Test doc — Manual Upgrade (RC build in AWS-Oregon) | https://netskope.atlassian.net/wiki/spaces/NH/pages/4749590776 |

---

*Wiki built by ns-ahwu · Source: ENG-490476 epic, git commits 9d68894 (manual) and 505e203 (auto), Confluence design docs 4697227279 / 4669014262 / 4767253439*

---

<details>
<summary>Appendix: Original prompt used to generate this wiki</summary>

```
Using the idea mentioned by Andrej Karpathy in @NPAWiki/General/how-to-create-llm-wiki.md
Create a wiki based on this project:
1. The related JIRA tickets are in: /jira https://netskope.atlassian.net/browse/ENG-490476 and my tasks are in those assignee as "ahwu@netskope.com"
2. The design docs are in: /confluence https://netskope.atlassian.net/wiki/x/9oBLFgE and /confluence https://netskope.atlassian.net/wiki/spaces/NH/pages/4767253439/Technical+Details+How+to+integrate+HostOS+Upgrade+flow+to+autoupgrade /confluence https://netskope.atlassian.net/wiki/spaces/NH/pages/4697227279/Design+doc+Manual+upgrade+existing+publisher+Host+OS+from+Ubuntu+20+to+22?atlOrigin=eyJpIjoiZDhlOTRiOTEzZjZiNDc5M2I1NmVkNDkzYTgzNzlkYWYiLCJwIjoiYyJ9
and all the test plan under the sub-page of /confluence https://netskope.atlassian.net/wiki/spaces/NH/pages/5142480603/Test+docs?atlOrigin=eyJpIjoiNGYyNWYzOWNiYjQ3NDkwMzlhOTAwNmRmYjM4MTA3OWEiLCJwIjoiYyJ9

3. The related code changes are in: @npa_publisher_wizard/ mostly in @npa_publisher_wizard/wizard/autoupgrade and @npa_publisher_wizard/upgradehelper/osupgrade.go and @npa_publisher_wizard/upgradehelper/upgradehelper.go
(You can use `git log | grep $TICKET_ID`) from step above , especially the ENG-490524 and ENG-490526 are the core ones.

Please create a wiki for future LLM session to read, the wiki can be used as a context for AI-based customer troubleshooting, feature enhancement...etc

Let's do it in @/home/ubuntu/NPA/NPAWiki/NPLAN4398-U22/
```

</details>
