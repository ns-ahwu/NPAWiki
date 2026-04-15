# NPLAN-6275: Secure Private Artifact Repository

> **Wiki purpose:** Persistent context for LLM-assisted development, self-study, and XFN knowledge transfer.
> New session? Start here. This page bootstraps full project context in one read.

---

## What Is This?

NPA Publishers currently pull OS updates from public Ubuntu archives and container images from Docker Hub. This creates:
- **Supply chain risk** — no centralized scan/verify before packages reach the appliance
- **Reliability risk** — public repo outages/rate limits break Publisher upgrades
- **Compliance risk** — some customers cannot allow direct internet egress from appliances

**The fix:** Route all Publisher software updates exclusively through a Netskope-controlled private repository (Cloudsmith), accessed only via the NPA tunnel. No direct internet dependency from the appliance.

---

## Architecture

```
Before:  Publisher ──► public internet ──► Docker Hub / Ubuntu archives

After:   Publisher ──► NPA tunnel ──► NewEdge ──► Cloudsmith private repo
                                                   ├── APT packages  (dl.netskope.pro)
                                                   └── Docker images (docker.netskope.pro)
```

**Key points:**
- Reuses the existing NPA tunnel — no new network path required on the Publisher side
- Cloudsmith is net-new infra: Netskope-managed, mirrors Ubuntu + Docker Hub, adds GPG signing
- DNS for `dl.netskope.pro` and `docker.netskope.pro` resolved via NPA (not public DNS)

---

## Phases

| Phase | Target | What |
|---|---|---|
| **Phase 1** | R135/R136 | Manual enable/disable via wizard CLI; APT + Docker switched to Cloudsmith |
| **Phase 2** | Apr 2026 | Cosign image signature verification, vulnerability scanning, policy enforcement, SBOM |

---

## Phase 1: How It Works

### The Config File (`private_repo_config.json`)

Provisioned by Netskope, SCP'd to Publisher by operator. Stored at `resources/private_repo_config.json`.

```json
{
  "token":        "<cloudsmith-entitlement-token>",
  "gpg_key":      "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...",
  "docker_domain": "docker.netskope.pro",
  "dl_domain":    "dl.netskope.pro",
  "repo_name":    "npapublisher"
}
```

All fields required. Missing/empty → enable aborts before touching any system files.

### Enable Flow (`--enable_private_repo`)

```
1. Read + validate private_repo_config.json
2. Write GPG keyring → /etc/apt/trusted.gpg.d/netskopenpa-npapublisher-archive-keyring.gpg
3. Write Docker auth → /root/.docker/config.json
4. mv sources.list → sources.list.bak        (APT ignores .bak)
5. mv docker-ce.list → docker-ce.list.bak
6. Write Cloudsmith entries → /etc/apt/sources.list.d/netskopenpa-npapublisher.list
7. apt-get update  (non-blocking, progress dots printed every 3s)
8. settings.json → PrivateRepo.Enabled = true
```

### Disable Flow (`--disable_private_repo`)

Exact reverse — `.bak` files restored, Cloudsmith list deleted, Docker auth cleaned, `apt-get update` run, setting toggled false. **Idempotent** (safe to run multiple times).

### Wizard Menu Indicator

```
Private Repo:
        Enabled     ← appears when enabled; absent when disabled
```

---

## Codebase Map

### `npa_publisher_wizard` (Go)

| Path | Role |
|---|---|
| `privaterepo/privaterepocore.go` | Core logic: GPG, Docker auth, APT source management |
| `privaterepo/menu.go` | `EnablePrivateRepo()` / `DisablePrivateRepo()` + `aptGetUpdate` injectable |
| `upgradehelper/upgradehelper.go` | `StartSystemUpdates()`, `isTmpWorldWritable()`, digest comparison |
| `newedge_wizard.go` | CLI flags `--enable_private_repo` / `--disable_private_repo` |
| `wizard/wizard.go` | Interactive menu + `GenerateLogBundle` |

### `npa_publisher` (C++)

| Path | Role |
|---|---|
| `src/agent/stitchercommandhandler.cpp` | Assessment payload, `populateCapabilities()` |
| `src/agent/agenthandler.cpp/.h` | `privateRepoIsEnabled()` — reads `sm_nsConfig` |
| `src/agent/publishersettings.cpp/.h` | `NsConfig` struct, `readNsConfig()` parses `nsconfig.json` |

---

## Key Design Decisions

### Why `mv` (not `cp`) for backup
APT only reads exact filenames: `sources.list` and `sources.list.d/*.list`. Files ending in `.bak` are silently ignored. Moving (not copying) ensures zero residual entries — no chance of duplicate repo URLs after enable.

### Why fixed template (not line-by-line rewrite)
Original approach (`replaceRepoFileEndpoint()`) parsed each APT source line and substituted the URL domain. This caused a path-mangling bug: Docker CE's path `/linux/ubuntu` was preserved, creating invalid Cloudsmith URLs (`/deb/linux/ubuntu` → 404). Fixed by discarding the old files entirely and injecting a known-correct Cloudsmith template.

### Digest strategy for cross-registry image comparison
- Public Docker Hub → use **manifest digest** (`RepoDigests` field)
- Private Cloudsmith → use **config digest** (`.config.digest`) — same image on different registries has same config digest but different manifest digest

### `aptGetUpdate` as injectable var
Needed to skip the real `apt-get update` in unit tests (which would hit `/etc/apt/`). Pattern: `var aptGetUpdate = func(...) { if TEST_PRIV_REPO=1 { return } ... }`. Same pattern used for `GpgDearmor`, `isTmpWorldWritable`.

---

## Bugs Fixed (Phase 1)

### ENG-948009 — Stale APT cache after enable/disable
**Root cause:** Wizard switched APT sources but never ran `apt-get update`. Cache was stale → `apt list --upgradeable` returned wrong results → wizard showed "No system updates available" immediately after enable.
**Fix:** `aptGetUpdate()` called in both `EnablePrivateRepo` and `DisablePrivateRepo` with a non-blocking goroutine progress spinner.
**Fixed in:** Build 135.0.0.10669

### ENG-954838 — `apt-key` fails when `/tmp` is 755 on GCP
**Root cause:** GCP's `google-startup-scripts` resets `/tmp` to `755` after startup (see ENG-540499). `apt-key` runs as `_apt` user (not root) and must create temp files in `/tmp`. With 755, only root can write → `Couldn't create temporary file /tmp/apt.conf.XXXXXX` → `apt-get update` exits 100.
**Fix:** `isTmpWorldWritable()` checks `mode & 0002` before apt. If false: save original perm, `chmod 777 /tmp`, run apt, restore original perm via defer.
**Fixed in:** Build 135+

---

## Observability (ENG-924234, R137)

The C++ Publisher reports private repo state in every assessment heartbeat to the Stitcher:

```json
"capabilities": {
  "private_repo_enabled": true,
  "nwa_ba": false,
  "auto_upgrade": false,
  ...
}
```

**Implementation:** `AgentHandler::privateRepoIsEnabled()` reads `PublisherSettings::sm_nsConfig.remote.publisher.enablePrivateRepo` (loaded at startup by `readNsConfig()`). Same pattern as `baAnyAppIsEnabled()`.

**Behaviour by nsconfig value:**

| `enable_private_repo` in nsconfig.json | Assessment field |
|---|---|
| `"1"` | `true` |
| `"0"` / `""` / missing / malformed file | `false` |

---

## Test Patterns

### Go
- `TEST_PRIV_REPO=1` env var: path helpers return `/tmp/test_*`; `aptGetUpdate` is a no-op
- `setUp(t)` must be called per sub-test — shared `settingshelper` global pollutes across tests
- Concrete tests use real `/tmp` files; mock tests use `mocks.Executor` + `mocks.FileManager`

### C++
- `MOCK_CONST_METHOD0(privateRepoIsEnabled, bool())` in `AgentHandlerMock`
- Use `EXPECT_CALL(...).WillOnce(Return(true/false))` — not `setenv("HOME")/file-writing` (old pattern)

---

## Phase 1 E2E Test Matrix

| Case | Scenario | Pass Criteria |
|---|---|---|
| 01-A | Manual enable → upgrade → disable | APT from Cloudsmith; Docker from `docker.netskope.pro`; disable restores originals |
| 01-B | Auto-upgrade after manual enable | `trigger_upgrade.json` triggers image upgrade from private repo |
| 02-A | Malformed config JSON | Enable aborts with error; no system files modified |
| 02-B | Empty JSON field | Enable aborts with error |
| 03 | Cloudsmith connectivity blocked | `apt-get update` fails non-fatally; enable still completes |
| Matrix | AnyApp ✓/✗ × Private Repo ✓/✗ | All 4 combinations must work |

---

## Phase 2 Roadmap (April 2026)

| Feature | Description |
|---|---|
| Cosign verification | Client-side container image signature verification before pull |
| Vulnerability scanning | Continuous scan on Cloudsmith upload + periodic re-scan |
| Policy enforcement | Block artifacts below configurable CVE severity threshold |
| SBOM generation | Software Bill of Materials tracking |
| China region | Integrate `UseChinaRegistry()` path with private repo |
| RHEL/CentOS | YUM/DNF repository support |
| Auto-provisioning | Config JSON distributed at provision time (no manual SCP) |

---

## How AI (Claude Code) Accelerated This Project

Developed using **Claude Code (Anthropic Sonnet 4.6, 1M context)** as a pair-programming partner across the full SDLC. The workflow is inspired by [Karpathy's LLM Wiki concept](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — plain markdown files co-located with the code give the LLM persistent, scoped context across sessions without any RAG or database infrastructure.

### The Folder-per-Ticket Pattern

```
npa_publisher_wizard/
├── NPLAN6275-PrivateRepo/   ← EPIC scope: design, docs, TOI, wiki
├── ENG-948009/              ← BUG: stale apt cache RCA + analysis
├── ENG-954838/              ← BUG: GCP /tmp permissions RCA
└── ENG-924234/  (npa_pub)  ← STORY: observability E2E + QA doc
```

Each `ENG-9XXXXX/` folder = one JIRA ticket. Hand the folder to the LLM → full ticket context, no re-explaining, no context bleed between tickets. The EPIC folder holds cross-cutting context (design decisions, user guide, TOI). This is the engineering-workflow variant of the LLM wiki idea: structured by JIRA hierarchy instead of a flat general knowledge base.

### Where AI Contributed

| Phase | Contribution |
|---|---|
| **Design** | Interpreted CloudSmith support email → identified root cause (URL path mangling) → proposed architectural fix (backup+template vs parse+rewrite) |
| **Implementation** | Full feature across Go + C++: APT management, Docker auth, digest strategy, progress spinner, `isTmpWorldWritable`, cross-codebase refactoring per code review feedback |
| **Bug fixing** | Full RCA for ENG-948009 and ENG-954838 — diagnosed system-level behaviour, not just patched symptoms |
| **Testing** | Unit tests alongside implementation; fixed test isolation issues; adapted mocks after refactoring |
| **Documentation** | Beta guide, TOI deck, RCA docs, PR descriptions, QA matrices — grounded in actual implementation, not boilerplate |

### The Core Insight

> Give AI the right folder (right scope of context), and it can act as a senior engineer across the full vertical slice of a ticket — RCA → fix → test → doc — without needing to re-establish context each session.

---

*NPLAN-6275 · Phase 1 complete · Phase 2 target: Apr 2026*
*AI tooling: Claude Code, Anthropic Sonnet 4.6 (1M context)*
