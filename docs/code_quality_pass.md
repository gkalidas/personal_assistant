# Code-Quality Pass — Handoff Document

> **Purpose:** A complete, self-contained plan so any AI (or developer) can continue
> the codebase-wide quality pass exactly as started. Read this top-to-bottom before
> touching code.
>
> **Started:** 2026-06-22  •  **Owner of decisions:** Ganesh (user)
> **Last updated by:** Claude (Opus 4.8)
>
> **Related work item (not part of the quality pass):** building the
> auto-read → auto-apply security loop (official LLM-security sources + safe
> auto-hardening) and the threat model are documented separately in
> [`docs/self_improving_security.md`](./self_improving_security.md). See its §6
> build checklist.

---

## 1. What this effort is

A systematic, file-by-file quality pass over the entire `personal_assistant`
codebase. The user explicitly chose:

- **Scope:** *Everything, prioritized* — in this order:
  `security/` → `core/` → `modules/` → `dashboard/` → `scripts/`
- **Depth:** *Thorough* —
  1. **Docstring every function** (all ~604 functions).
  2. **Split** any function that is **>~40 lines** *or* has **mixed concerns**
     into smaller single-responsibility functions.
  3. **Optimize** real time/space-complexity problems (not micro-tuning).
  4. **Break files into multiple files** where a module has grown to mix
     unrelated concerns.
  5. **Test after every file**, fix any breakage before moving on.
  6. **One commit per file.**

This is behavior-preserving refactoring. **No functional changes** unless fixing
a bug that is explicitly flagged and verified.

---

## 2. The per-file process (follow exactly)

For each file, in priority order:

1. **Audit** the file:
   ```bash
   python3 - <<'EOF'
   import ast
   p='PATH/TO/FILE.py'
   tree=ast.parse(open(p).read())
   for n in ast.walk(tree):
       if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
           ln=(n.end_lineno or n.lineno)-n.lineno+1
           d=bool(ast.get_docstring(n))
           if not d or ln>40:
               print(f"  L{n.lineno} {n.name}: {ln}ln doc={d}")
   EOF
   ```
2. **Read** the whole file. Understand what each function *actually* does vs. its name.
3. **Refactor**:
   - Add a concise docstring to every function (1 line for trivial, a short
     paragraph for non-obvious — say *what* + *why*, not line-by-line *how*).
   - Extract helpers from long/multi-concern functions. Prefer hoisting big
     inline literals (prompt strings, config dicts, lookup tables) to module
     constants. Use the **table-driven pattern** when you see repeated blocks
     (see `security/posture.py` `_COMPONENT_SPECS` for the reference example).
   - Extract `_print_*` / `_save_*` / `_render_*` helpers to keep orchestration
     functions short (see `security/jailbreak_sim.py` for the reference pattern).
   - Flag (don't silently fix) any function that doesn't do what its name says.
     If it's a clear bug, fix it **and add/extend a test**, and call it out in
     the commit message.
4. **Re-audit** the file — confirm 0 missing docstrings and no >40-line functions
   (a function that is 41–45 lines *including its docstring* and is a single
   cohesive loop is acceptable; use judgment, don't over-split display loops).
5. **Test** (see §5). Everything that passed before must still pass.
6. **Commit** the single file with a descriptive message (see §6).
7. Update the `TodoWrite` list / this doc's checklist.

**Guideline, not dogma:** the ~40-line threshold has a tilde. Don't shatter a
cohesive loop into unreadable fragments just to hit a number. Single
responsibility and readability win.

---

## 3. Progress so far

**Buckets 1–2 (`core/sanitizer.py` + all of `security/`) are COMPLETE.**

| # | File | Status | Commit | Notes |
|---|------|--------|--------|-------|
| 1 | `core/sanitizer.py` | ✅ done | `adf86d2` | Split `validate_action` → `_coerce_numeric`, `_scan_string_field`, `_validate_field`; docstring `_is_required`. |
| 2 | `security/jailbreak_sim.py` | ✅ done | `23b3278` | Split `_run_track_b` → `_probe_llm`, `_assess_track_b`; constants; `_print_track_a/b/c`. |
| 3 | `security/posture.py` | ✅ done | `9c0a49d` | Table-driven `_COMPONENT_SPECS` + `_evaluate_component`; `_grade_for`, `_save_posture`. |
| 4 | `security/red_team.py` | ✅ done | `6f97d29` | **Fixed `_test_validator` bug (§4b)**; split `run_red_team` → `_run_single_attack`/`_tally`/`_print_attack_line`. |
| 5 | `security/autodefense.py` | ✅ done | `93e2b69` | Split `run_autodefense` → `_resolve_red_team_report`/`_process_bypass`/`_apply_proposal_outcome`. |
| 6 | `security/scanner.py` | ✅ done | `76dfa67` | Dedup `_cvss_to_severity`; split `scan_packages` → `_query_osv`/`_parse_osv_results`. |
| 7 | `security/auditor.py` | ✅ done | `dd7843a` | Split ollama check → `_ollama_exposed_via_proc`/`_via_probe`; removed dead code. |
| 8 | `security/patcher.py` | ✅ done | `2956e55` | Extract `_run_pip_upgrade`, `_dedup_by_package`, `_classify_unpatchable`; hoist severity order. |
| 9 | `security/load_monitor.py` | ✅ done | `11e6dc2` | Dedup sampling → `_sample_now`; extract `_store_observation`, `_observation_stats`. |
| 10 | `security/watcher.py` | ✅ done | `4ad7767` | `_STATIC_PATTERNS` const; `_merge_patterns`; split `detect_anomalies` into 4 `_check_*`. |
| 11 | `security/guardian.py` | ✅ done | `2366771` | Docstrings; hoist `_cmd_*` + `_build_dispatch`; split `run_daemon`/`_full_scan`. **Daemon restarted.** |
| 12 | `security/threat_intel.py` | ✅ done | `23beb7c` | All 14 docstrings; split 106-line `run_threat_intel` into 5; per-item builders `_nvd_item`/`_github_item`/`_kev_item`. |

**Reference commits** — study these to match the established style before
continuing. `9c0a49d` (table-driven) and `23b3278` (extract + constants) are
the two key patterns.

**Bucket 3 = `core/` (13 files) is NEXT (in progress).** Run the §9 audit
scoped to `core/` for the exact list.

### Conventions confirmed in practice
- Functions of **41–45 lines that are a single cohesive loop/orchestrator with
  a docstring are acceptable** — do not over-split (e.g. `run_red_team` 44,
  `run_threat_intel` 41, `run_daemon` 60, `auto_patch_all` 49 were all left).
- Hoist big inline literals (prompt strings, rule tables, static lists) to
  module constants.
- For repeated `if/elif` print or scoring blocks, prefer a **rule/spec table +
  loop** (see posture `_COMPONENT_SPECS`, threat_intel `_CODE_PATTERN_RULES`).
- Batch one-line docstrings with a small regex script (see git history of
  `guardian.py`/`threat_intel.py` commits) — but verify no blank-line-after-def
  artifact is introduced (it was, once; clean it if so).

---

## 4. Bugs found

### 4a. Already fixed (earlier in session, before the quality pass)
These came from a "does the system work as built" check and are **committed**:

| Bug | Fix commit | Summary |
|-----|-----------|---------|
| Stale guardian daemon | (ops, no code) | PID predated `red_team`/`jailbreak` tasks; restarted service. |
| Farming actions blocked by validator | `137cae3` | `ndvi_health`, `analysis_history`, `summary` had handlers but weren't in `_ACTION_SCHEMA`; after the "block unknown actions" change they were rejected. Added to schema. |
| Shell-meta false positive | `137cae3` | `_SHELL_META_RE` matched bare `&&`/`||`; narrowed to unambiguous injection syntax. |
| Stale dashboard server | (ops, no code) | Server predated module edits; restarted. |
| Dashboard bound to `0.0.0.0` | `703102d` | No-auth dashboard exposed to LAN. Now binds to Tailscale IP (see §7). |

### 4b. ✅ FIXED (commit `6f97d29`) — `security/red_team.py` `_test_validator`
**This was a real latent bug, now fixed during the `red_team.py` pass.**
Kept below for the record / as a worked example of the kind of "function doesn't
do what its name says" bug to watch for in remaining buckets.

At `red_team.py:258`:
```python
ok, msg = validate_action(module, action)   # WRONG
return not ok, msg
```
`validate_action()` returns a `ValidationResult` dataclass, **not** a tuple.
Unpacking it raises `TypeError: cannot unpack non-iterable ValidationResult`,
which is swallowed by the `except Exception` below, so `_test_validator` **always**
returns `(False, ...)`. **The validator layer is never actually tested.** This is
masked because the `action_injection` payloads contain `DROP TABLE`, which the
*sanitizer* catches — so the attacks still show "blocked", just by the wrong layer.

**Fix:**
```python
result = validate_action(module, action)
return (not result.valid), "; ".join(result.errors)
```
After fixing, verify the `action_injection` rows in red_team output now show
`[validator]` (or `[sanitizer+validator]`) and the run is still 34/34 blocked.

---

## 5. Testing — run after every file

Always `source ~/envs/evn_personal_assistant/bin/activate` first (see §7).

```bash
# 1. Import sanity (catches syntax / import-cycle breakage)
python3 -c "import MODULE.PATH; print('OK')"

# 2. Security regression triad (fast, no LLM) — must stay green
python security/red_team.py    2>/dev/null | grep RESULTS      # 34/34 blocked, 0 bypassed
python security/jailbreak_sim.py 2>/dev/null | grep "JAILBREAK SIM"  # 16/16 contained
python security/posture.py --json 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['grade'])"

# 3. Full functional suite (234 tests across 19 sections) — must stay 0 failures
python scripts/full_test_suite.py 2>/dev/null | grep "Failed:"

# 4. Optional: live-LLM jailbreak (slow, needs Ollama up)
python security/jailbreak_sim.py --llm 2>/dev/null | grep "JAILBREAK SIM"  # 21/21
```

**Known-good baselines (as of this handoff):** red_team 34/34 blocked (0%
bypass); jailbreak 16/16 (21/21 with `--llm`); full suite 234/234; posture grade A.

> Note: posture *score* may read 95 (not 100) when the guardian daemon resets
> `pattern_update_latest.json` to a "skipped" status — that's expected and not a
> regression. Grade stays A.

---

## 6. Commit conventions

- One file per commit. Conventional-commit style: `refactor(<area>): …` for
  quality passes, `fix(<area>): …` when a bug is corrected.
- Body: bullet what was extracted/split/optimized; end with a line confirming
  tests still pass.
- **Always** end the commit message with:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  ```
- We are on branch `dev`. Do **not** push or open PRs unless the user asks.

---

## 7. Environment & operational notes (important)

- **venv (always use this):** `~/envs/evn_personal_assistant`
  → `source ~/envs/evn_personal_assistant/bin/activate`
- **Large downloads:** use `aria2c -c -x 16 -s 16 <URL>` (exception: `ollama pull`).
- **Guardian daemon:** `systemctl --user restart gk-guardian.service`. The running
  process holds code in memory — **restart it after editing `security/guardian.py`**
  or scheduled tasks run stale code.
- **Dashboard server:** runs via `uvicorn dashboard.server:app`. Plain uvicorn has
  **no `--reload`**, so **restart it after editing any `dashboard/` or imported
  `modules/` code** or it serves stale code.
  - **Binding (security):** bind to the **Tailscale IP** (`100.67.193.127`), never
    `0.0.0.0` — the dashboard has **no auth** and serves health/finance data.
    `scripts/run_dashboard.py` now does this via `_resolve_host()`
    (override → Tailscale IP → `127.0.0.1` fallback).
  - **Port inconsistency to resolve (not yet done):** `scripts/startup.sh` launches
    on `127.0.0.1:8080`; `scripts/run_dashboard.py` defaults to port `8765`;
    `docs/remote_access.md` lists `8765`. Pick one port and make all three agree
    when you reach the `scripts/` bucket.
- **Never commit:** `user_profile.json`, `*.db`, `.env`, `~/.config/gk/master.key`
  (all gitignored — keep them so).

---

## 8. Remaining work queue

Counts = files still needing work / missing docstrings / functions >40 lines.
(Generated 2026-06-22; re-run the §9 audit to refresh.)

| Bucket | Files | No-doc | >40ln | Priority |
|--------|-------|--------|-------|----------|
| `security/` | — | — | — | ✅ **DONE** (12 files committed) |
| `core/` | — | — | — | ✅ **DONE** (14 files; crypto bug fixed) |
| `modules/` | 27 | 138 | 28 | **3 (NEXT)** |
| `dashboard/` | 9 | 72 | 11 | 4 |
| `scripts/` | 7 | 37 | 8 | 5 |

### core/ bucket — completed files & commits
`sanitizer`(adf86d2), `crypto`(**2261866 — bug fix**), `router`(0850945),
`llm`(adf3caf), `mistake_log`(37249bb), `log`(08de1a6),
`log_archiver`(82f54fb), `guardrails`(526710c), `embedding_router`(07b20d4),
`audio`(39354ac), `analysis`(a353ce7), `doc_reader`(7f005f9),
`knowledge_graph`(9bd1cb5 — dedup hydration/vis), `memory`(2fdc117).

**🔑 Major bug found+fixed in `core/crypto.py` (commit 2261866):** the key at
`~/.config/gk/master.key` was 32 raw bytes (not a base64 Fernet key), so
`encrypt()` threw every call and fell back to **plaintext** — encryption at
rest never worked (0 finance rows had the `enc:` prefix). `_load_or_create_key`
now validates and regenerates an unusable key (safe: an invalid key can't have
encrypted anything). Old invalid key backed up at
`~/.config/gk/master.key.invalid.bak`. Dashboard server restarted so live
writes now actually encrypt.

> `core/sanitizer.py` is already done (bucket 1). Re-run the §9 audit scoped to
> `core/` for the current exact list before starting — counts above were the
> original snapshot.

After `core/`, move to `modules/`, `dashboard/`, `scripts/`. Re-run the §9 audit
at the start of each bucket for an exact list.

---

## 9. Full-codebase audit script

Run this anytime to regenerate the queue:

```bash
cd /home/ganesh/projects/personal_assistant
python3 - <<'EOF'
import ast, pathlib
for area in ['security','core','modules','dashboard','scripts']:
    files=[]
    for p in sorted(pathlib.Path(area).rglob('*.py')):
        if any(x in str(p) for x in ('/.git/','/env','__pycache__')): continue
        try: tree=ast.parse(p.read_text())
        except: continue
        funcs=nodoc=0; longs=[]
        for n in ast.walk(tree):
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
                funcs+=1
                if not ast.get_docstring(n): nodoc+=1
                ln=(n.end_lineno or n.lineno)-n.lineno+1
                if ln>40: longs.append(f"{n.name}:{ln}")
        if funcs and (nodoc or longs):
            files.append((str(p),nodoc,longs))
    print(f"\n== {area} ==")
    for f,nd,longs in files:
        print(f"  {f:<40} {nd:>2}nd  {','.join(longs)}")
EOF
```

---

## 10. Definition of done

- Every function in the codebase has a docstring.
- No function mixes unrelated concerns; long functions are split (judgment on
  the ~40-line line).
- Genuine complexity problems optimized; files split where concerns diverged.
- `red_team.py` `_test_validator` bug fixed and verified.
- After every file: security triad green, full suite 234/234, posture grade A.
- One commit per file, co-author trailer present, still on `dev`.
