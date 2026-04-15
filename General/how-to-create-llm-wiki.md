# How to Create a Karpathy-Style LLM Wiki for Engineering Projects

> A practical guide for engineers using Claude Code (or any LLM) to build persistent, compounding knowledge bases that survive context resets and accelerate future sessions.

---

## The Core Idea

Andrej Karpathy proposed treating an LLM not as a one-shot query engine but as a **knowledge curator** — one that reads sources, synthesises information into structured markdown files, and builds a wiki that compounds in value over time. Plain markdown files, co-located with the work, are the most effective way to give an LLM persistent, non-hallucinated context.

The engineering variant used in this project extends this: **mirror your JIRA hierarchy in the filesystem**. Each folder = one scope of context. The LLM is handed exactly the right folder for the current task.

---

## Wiki Structure

```
NPAWiki/
├── General/                        ← Cross-project context
│   ├── NPA-Context.md                  Product architecture, data path
│   ├── network-programming-l4-vs-l7.md General domain knowledge
│   └── how-to-create-llm-wiki.md       This guide
│
├── <EPIC-NAME>/                    ← One folder per EPIC/Feature
│   └── README.md                       Auto-renders on GitHub; bootstraps a new session
│
└── <EPIC-NAME-2>/
    └── README.md
```

**Rules:**
- `General/` — domain knowledge that applies across all projects (product architecture, protocols, tooling)
- `<EPIC-NAME>/README.md` — the single source of truth for one feature/project
- Use `README.md` (not `FEATURE.md`) — GitHub renders it automatically when browsing the folder
- Keep it in a **public or private GitHub repo** — version controlled, shareable, LLM-accessible via `@file` reference

---

## What Goes in an EPIC README.md

Structure every EPIC wiki page with these sections (adapt as needed):

### 1. Header block
```markdown
# <Feature Name>

> **Wiki purpose:** <one line — who is this for?>
> New session? Start here. This page bootstraps full project context in one read.
```

### 2. What Is This?
- Problem statement (2-3 bullet points)
- The fix / approach in one sentence

### 3. Architecture
- Before/after diagram (ASCII is fine)
- Key architectural decisions and **why** — not just what

### 4. Phases
Table: Phase | Target release | What's in scope

### 5. How It Works
- Step-by-step flow for the main user-facing operation
- Config file formats (with example JSON/YAML)

### 6. Codebase Map
Table of key files and their roles — the LLM uses this to navigate without searching.

### 7. Key Design Decisions
Each decision as: **title** → why the simpler/obvious approach was rejected → what was chosen instead.
This is the highest-value section for a new LLM session — it prevents re-litigating settled questions.

### 8. Bugs Fixed
For each bug: **Root cause** (one sentence), **Fix** (one sentence), **Fixed in** (build/version).

### 9. Test Patterns
The non-obvious patterns: injectable vars, env var gates, mock setup gotchas.

### 10. Phase N Roadmap
What is NOT done yet and why.

### 11. How AI Accelerated This
Optional but useful for team knowledge sharing — what AI contributed and the folder-per-ticket pattern.

---

## The Folder-per-Ticket Pattern (Inside the Project Repo)

The wiki in `NPAWiki/` is the **persistent, shareable** layer. But during active development, also maintain ticket-scoped folders **inside the source repo**:

```
npa_publisher_wizard/
├── NPLAN6275-PrivateRepo/   ← EPIC: design notes, vendor comms, TOI, beta guide
├── ENG-948009/              ← BUG: RCA, analysis, QA explanation
├── ENG-954838/              ← BUG: RCA, JIRA-paste doc
└── ENG-924234/              ← STORY: E2E steps, QA verification matrix
```

**What goes in each ticket folder:**

| File type | When to create |
|---|---|
| `rca.md` | Any bug — root cause, fix, verification, affected scope |
| `analysis.md` | Complex bugs needing QA/stakeholder explanation |
| `e2e-test.md` | Stories with integration test steps |
| `QA_verification.md` | Structured test matrix for QA team |
| `pr-overview.md` | PR summary with before/after evidence |
| `design-notes.md` | Architectural decisions made during implementation |

When the EPIC is complete, **distill** the ticket folders into the `NPAWiki/EPIC/README.md`. The ticket folders are working notes; the wiki is the clean summary.

---

## How to Start a New Session Using the Wiki

When starting a new Claude Code session on a project that has a wiki:

```
Read @/home/ubuntu/NPA/NPAWiki/General/NPA-Context.md and
@/home/ubuntu/NPA/NPAWiki/<EPIC-NAME>/README.md

We are continuing work on <EPIC>. The current task is <JIRA ticket>.
The ticket folder is at <path>/ENG-XXXXXX/.
```

This gives the LLM:
1. **Product context** (General/) — no need to explain what NPA is
2. **Feature context** (EPIC README) — architecture, design decisions, codebase map
3. **Task context** (ticket folder) — exactly what needs to be done now

---

## How to Build a Wiki Page (Step by Step)

### Step 1: Gather raw material in parallel
```
- Fetch the JIRA EPIC and linked stories
- Fetch relevant Confluence pages (design docs, beta guides, test docs)
- Search git log for related commits: git log --oneline | grep ENG-XXXXXX
- Use git show <hash> to understand what changed
- Find key source files: grep -rn "feature_keyword" src/
```

### Step 2: Write the README.md
Follow the template above. Focus on:
- **Why** decisions were made (not just what)
- **Non-obvious** patterns (the stuff a new engineer or LLM would get wrong)
- **Current status** clearly marked (Beta / GA / Phase N complete)

### Step 3: Commit to the wiki repo
```bash
cd ~/NPA/NPAWiki
git add <EPIC>/README.md
git commit -m "<EPIC>: Add wiki — <one line summary>"
git push
```

### Step 4: Keep it updated
- After each significant bug fix: add to "Bugs Fixed" section
- After each phase completion: update status, add next phase roadmap
- After code review changes: update "Key Design Decisions" if approach changed

---

## Tips for High-Quality Wiki Pages

**Do:**
- Write for a reader who has never seen the codebase — but is a competent engineer
- Include actual file paths (`privaterepo/privaterepocore.go`) not vague descriptions
- Document the bugs that were fixed and why — this prevents regression in future phases
- Include the "before/after" for architectural changes
- Keep the Phase N roadmap section — it tells the LLM what NOT to implement yet

**Don't:**
- Copy-paste raw code — summarise the pattern instead
- Document things already obvious from reading the code
- Let it go stale — an outdated wiki is worse than no wiki (the LLM will trust it)

---

## Example: This Wiki Was Built With This Process

The `NPLAN6275-PrivateRepo/README.md` in this repo was built by:
1. Fetching 3 Confluence pages (PRD, beta guide, E2E test doc) in parallel
2. Reading git commits across 2 repos (Go wizard + C++ publisher)
3. Distilling 3 months of development into a single 300-line README
4. Result: a new LLM session can bootstrap full project context in one read, with no hallucination risk — everything in the wiki came directly from the actual code and docs

---

*Guide author: ns-ahwu · Inspired by [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)*
