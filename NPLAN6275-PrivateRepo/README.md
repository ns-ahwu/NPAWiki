# NPLAN-6275: Secure Private Artifact Repository

> **Wiki purpose:** PRD + HLD for both Phase 1 and Phase 2. Start here for product/architecture context.
> For engineering implementation details, bug fixes, and code changes → see [`DP.md`](./DP.md).

---

## Problem Statement

NPA Publisher appliances pull OS updates from public Ubuntu archives and container images from Docker Hub. This creates:

- **Supply chain risk** — no centralized scan/verify before packages reach the appliance
- **Reliability risk** — public repo outages / Docker Hub rate limits break Publisher upgrades
- **Compliance risk** — regulated customers cannot allow direct internet egress from appliances
- **Operational complexity** — software from multiple disparate public sources is hard to audit

---

## Solution Overview

Route all Publisher software delivery exclusively through Netskope-controlled private infrastructure (Cloudsmith), accessed only via the NPA tunnel.

```
Before:  Publisher ──► public internet ──► Docker Hub / Ubuntu archives

After:   Publisher ──► NPA tunnel ──► NewEdge ──► Cloudsmith private repo
                                                   ├── APT packages  (npa-repository.netskope.com)
                                                   └── Docker images (npa-docker.netskope.com)
```

**Key architectural properties:**
- Reuses existing NPA tunnel — no new network path required on the Publisher side
- DNS for `npa-repository.netskope.com` and `npa-docker.netskope.com` resolved via NPA
- Cloudsmith is net-new infra: Netskope-managed, mirrors Ubuntu archives + Docker Hub, adds GPG signing
- Zero direct internet dependency from the appliance once enabled

---

## Glossary

| Term | Definition |
|---|---|
| **Wiz** | NPA Publisher Wizard (`npa_publisher_wizard`) |
| **NSC / nsconfig** | `resources/nsconfig.json` — feature flag store, pulled from MP |
| **REPOC** | Repository config (`resources/private_repo_config.json`) — stores token, GPG key, domains |
| **eToken** | Cloudsmith entitlement token — used to authenticate APT/Docker pulls |
| **aToken** | API token — used to request eToken from Cloudsmith |
| **configJSON** | `private_repo_config.json` — provisioned per-tenant |

---

## Phase 1: Foundational Repository (R135/R136)

### Scope

| Component | Objective |
|---|---|
| APT packages | Pull from `npa-repository.netskope.com/.../ubuntu`, not `security.ubuntu.com` |
| Docker images | Pull from `npa-docker.netskope.com`, not Docker Hub |
| Auth | Authenticate via eToken — no manual user/password intervention |
| Enable/Disable | Manual opt-in via wizard CLI; fully reversible |

### Config File (`private_repo_config.json`)

Provisioned by Netskope per-tenant. SCP'd to Publisher by operator before enabling.

```json
{
  "token":         "<cloudsmith-entitlement-token>",
  "gpg_key":       "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...",
  "docker_domain": "npa-docker.netskope.com",
  "dl_domain":     "npa-repository.netskope.com",
  "repo_name":     "npapublisher"
}
```

| Field | Granularity |
|---|---|
| `token` | Per-tenant |
| `gpg_key` | Per-repo |
| `dl_domain` | Same across all tenants (`npa-repository.netskope.com`) |
| `docker_domain` | Same across all tenants (`npa-docker.netskope.com`) |
| `repo_name` | Dynamic from config; falls back to `npapublisher` |

All fields required — missing/empty causes enable to abort before touching any system file.

### Phase 1-A: Existing Publisher (Manual Enable)

```
1. Operator obtains configJSON from tenant portal
2. SCP configJSON → resources/private_repo_config.json on Publisher
3. sudo ./npa_publisher_wizard --enable_private_repo
4. Perform system package and Docker image upgrades from private repo
5. To revert: sudo ./npa_publisher_wizard --disable_private_repo
```

### Phase 1-B: New Script-Based Install (Not in Phase 1 scope)

> Deferred — installer would inject token/GPG key as arguments; provision script sets up configJSON automatically. Future work in Phase 2.

### Enable Flow (what the wizard does)

```
1. Read + validate private_repo_config.json (all fields required)
2. Write GPG keyring  → /etc/apt/trusted.gpg.d/netskopenpa-npapublisher-archive-keyring.gpg
3. Write Docker auth  → /root/.docker/config.json
                        { "auths": { "<docker_domain>": { "auth": base64(token:token) } } }
4. mv sources.list    → sources.list.bak            (APT ignores .bak)
5. mv docker-ce.list  → docker-ce.list.bak
6. Write Cloudsmith   → /etc/apt/sources.list.d/netskopenpa-npapublisher.list
7. apt-get update     (non-blocking; progress dots every 3s)
8. settings.json      → PrivateRepo.Enabled = true
```

### Disable Flow (exact reverse, idempotent)

```
1. Remove GPG keyring
2. Remove Docker auth entry
3. mv sources.list.bak    → sources.list
4. mv docker-ce.list.bak  → docker-ce.list
5. Delete netskopenpa-npapublisher.list
6. apt-get update
7. settings.json → PrivateRepo.Enabled = false
```

### Validation

```bash
# Wizard menu shows:
Private Repo:
        Enabled

# APT source present:
cat /etc/apt/sources.list.d/netskopenpa-npapublisher.list

# GPG keyring present:
ls /etc/apt/trusted.gpg.d/netskopenpa-npapublisher-archive-keyring.gpg

# Docker auth present:
cat /root/.docker/config.json   # contains npa-docker.netskope.com

# Settings persisted:
grep -i privaterepo resources/settings.json   # "Enabled": true
```

---

## Phase 2: Supply Chain Security Hardening (Target: Apr 2026)

### Token Distribution Architecture (HLD)

Phase 1 requires manual provisioning of `private_repo_config.json`. Phase 2 automates this via a managed token delivery path:

**Selected approach: Stitcher-mediated token delivery**

```
MP (addonman) ──► nexus ──► Cloudsmith
                              │
                          eToken issued
                              │
Stitcher ◄── MP ◄── Publisher (via existing MPAuth/datapath)
    │
    └──► Publisher receives eToken via existing assessment channel
```

Rationale over direct Publisher→MP:
- Builds on existing assessment/datapath — no new network path
- Easily extends the AutoUpgrade design
- MP can do tenant-level block/control centrally

### Phase 2 Scope (ENG-976595 stories)

| Story | Key | What |
|---|---|---|
| Script-based install from private endpoint | ENG-976617 | bootstrap.sh/provision_shared.sh uses private endpoints end-to-end from first install |
| Wizard MP token path | ENG-976620 | Wizard consumes token via addonman→nexus→Cloudsmith; no manual configJSON |
| C++ publisher core changes | ENG-976622 | TBD during design; assessment/capabilities updates for Phase 2 |
| provision_shared.sh changes | ENG-976623 | RHEL + Ubuntu provisioning via private endpoints only; China region |
| Failure logging / token expiry | ENG-976624 | Detect/log token expiry, GPG expiry, connectivity failure; surface in menu + log bundle |

### Phase 2 Security Features (NPLAN-6275 original scope)

| Feature | Description |
|---|---|
| Cosign verification | Client-side container image signature verification before pull |
| Vulnerability scanning | Continuous scan on Cloudsmith upload + periodic re-scan |
| Policy enforcement | Block artifacts below configurable CVE severity threshold |
| SBOM generation | Software Bill of Materials per artifact |

### Known Constraints / Limitations for Phase 2

- If Publisher is not registered or not deployed with private-repo-capable build, MP cannot deliver the token — operator must still manually provision on first install
- Token refresh must handle the case where Stitcher is disconnected (fallback to public repos)
- China region: separate endpoint and registry; needs dedicated Phase 2 handling
- RHEL: private repo not yet integrated; APT-side work done, yum/podman path TBD

---

## Related Tickets (Phase 1)

| Ticket | Status | What |
|---|---|---|
| ENG-771738 | Closed | Main Phase 1 wizard code change |
| ENG-905321 | Closed | Docker image digest strategy (config digest for cross-registry) |
| ENG-924234 | Code Review | Observability: `private_repo_enabled` in C++ assessment payload |
| ENG-948009 | Closed | Bug: stale apt cache after enable/disable |
| ENG-952882 | In Progress | System updates slower when private repo enabled |
| ENG-954838 | Resolved | Bug: GCP `/tmp` 755 breaks apt-key |
| ENG-955137 | Open | Publisher upgrade fails when docker_domain reachable but dl_domain blocked |
| ENG-953187 | Open | Doc tracking / update for Phase 1 |

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

**How to create a similar wiki for another project:**
See [`General/how-to-create-llm-wiki.md`](../General/how-to-create-llm-wiki.md)

</details>

---

*NPLAN-6275 · Phase 1 complete (R135/R136) · Phase 2 target: Apr 2026*
*Repos: `npa_publisher_wizard` (Go) · `npa_publisher` (C++) · Epic: ENG-771732 (P1) / ENG-976595 (P2)*
