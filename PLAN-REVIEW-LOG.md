# Plan Review Log: Improve the susops codebase

Act 1 (grill) skipped at user request (`skip interview`). Plan was synthesized from four parallel codebase audits (over-engineering/dead-code, structural/testability, test-coverage, correctness/resource-leaks), advisor-reviewed for prioritization, and the dead-code deletions were grep-verified per symbol before locking. MAX_ROUNDS=5.

## Round 1 — Codex

**Findings**
- The plan omits the largest security issue: unauthenticated localhost RPC can invoke every allowlisted method, and `list_shares` returns passwords. Fix: add a per-daemon bearer token or Unix socket auth before treating this as security-reviewed.
- PID/port files are still written world-readable (`0644`), despite the README calling out `chmod 600` as the cheap hardening. Fix: create `workspace`/`pids` as `0700` and write PID/port/token files as `0600`.
- `socat` local UDP builds a shell command with unquoted `conn.ssh_host` and `fw.dst_addr` in [socat.py](/Volumes/data/private/development/susops/susops/src/susops/core/socat.py:86). Fix: build the remote command with `shlex.join` or quote every user/config-derived token and add metacharacter tests.
- Decision A relies on “facade only runs in daemon,” but README still documents direct `SusOpsManager()` use. Fix: either deprecate/update that API path or make process handling correct for non-daemon managers too.
- The zombie fix still does not address PID reuse: `is_running` and `stop` trust any PID file and can report or kill an unrelated same-user process. Fix: record and verify process identity, at minimum cmdline plus create time, before returning running or sending signals.
- Non-child PIDs must not automatically become “not running”; adopted live masters after daemon restart are non-child but valid. Fix: use `psutil` to identify zombies, then `waitpid` only as best-effort reaping, preserving live identity-matched non-children.
- `_try_reconnect` contradicts its own contract: [facade.py](/Volumes/data/private/development/susops/susops/src/susops/facade.py:1494) returns success for “PID alive, socket dead.” Fix: only return `True` when `is_socket_alive` is true; treat live-without-socket as pending or stale.
- The plan ignores concurrent lifecycle races: RPC handlers run in executor threads, but `start`/`stop`/reconnect share no per-tag lock and `ProcessManager.start` blindly overwrites PID files. Fix: add a per-connection lifecycle lock around check-start-stop-reconnect-fetch transitions.
- Decision B’s `_share_lock` is under-specified and can deadlock or stall if held across `server.stop()`, `start_forward`, or `_config_lock`. Fix: lock only for snapshot/pop/insert, release before I/O or config writes, and test concurrent `list_shares` with start/stop.
- The aiohttp cleanup fix is incomplete: leaving `_runner` set after timeout makes `is_running()` lie forever and retry cleanup can double-schedule. Fix: keep a cleanup future, clear state only in a success callback, log failures, and block restart while cleanup is pending.
- `find_master_pid` on macOS via broad `cmdline` matching can adopt the wrong process. Fix: require `name/exe == ssh`, exact `ControlPath=<socket>`, `ControlMaster=yes`, same UID, and verify with `ssh -O check`.
- `stop()` PID unlinking is marked lower priority, but it is the same class of untracked-process leak as the zombie bug, and `kill_all()` has the same unconditional unlink behavior. Fix: make exit confirmation or SIGKILL escalation part of Phase 1 for both paths.
- The zombie test plan is self-contradictory: Phase 4 says to keep a Linux skip until after 1.1, but 1.1 is Phase 1. Fix: ship 1.1 with mocked `psutil` tests plus a real zombie test where supported.
- Browser consolidation can regress detected launch commands because Linux currently discards `Browser.launch_cmd` arguments. Fix: pass full `Browser` objects from `detect_browsers()` into the base method and preserve main-thread alert/error marshalling.
- Observability is still weak: psutil failures, cleanup timeouts, reconnect false positives, and bandwidth sampler exceptions are mostly swallowed. Fix: add structured warning/debug logs with PID/name/tag/exception at each new failure branch.

VERDICT: REVISE
### Claude's response (Round 1 → revision)

Verified each external claim against source before acting. **Accepted (11):**
- **#2 file perms** → new fix 1.8 (0700 dir + 0600 pid/port files). Verified: config.yaml is already 0600 (`config.py:209`) while pid/port are 0644 — a real inconsistency, README.md:94 documents the chmod-600 one-liner as undone.
- **#3 socat injection** → new fix 1.7. Verified `socat.py:88` quotes `sock` but not `ssh_host`/`dst_addr`. shlex.quote all config-derived tokens + metacharacter test.
- **#5/#12 PID reuse + unconditional unlink** → merged into a promoted, first-class fix 1.5 (identity-check via psutil cmdline before kill; SIGKILL escalation; unlink only after confirmed exit; covers `kill_all` too).
- **#6 non-child PIDs** → rewrote Key decision A. This was a genuine bug in my proposed approach: the running verdict now comes solely from `psutil` status (correct for orphaned-but-live adopted masters); `waitpid` is best-effort reaping whose ECHILD is ignored. The earlier "non-child → not running" wording is explicitly retracted.
- **#7 _try_reconnect socket-authoritative** → folded into 1.1 with a Risks caveat scoping it to reconnect paths so it doesn't break the initial-start socket-bind window.
- **#8 per-tag lifecycle lock** → new fix 1.6. Verified RPC runs in executor threads (`rpc_server.py:98`) and the race is self-documented at `facade.py:579`.
- **#9 _share_lock deadlock** → sharpened Key decision B with an explicit global lock order (lifecycle → config/share, leaf, released before I/O).
- **#10 aiohttp cleanup** → rewrote 1.3: keep cleanup future, clear state only on success callback, log on timeout, block restart while pending. My original "leave _runner set" was incomplete — Codex correctly noted it makes is_running() lie forever.
- **#11 find_master_pid strict match** → sharpened 1.4 (exe==ssh, exact ControlPath, ControlMaster=yes, same UID, verify with `ssh -O check`).
- **#13 zombie-test contradiction** → fixed Phase 4 wording: 1.1 ships first with psutil-mocked tests + real zombie test where supported.
- **#14 browser launch_cmd regression** → added Phase 3 regression guard: pass full `Browser` objects, preserve main-thread alert marshalling, assert launched cmd == detected cmd.
- **#15 observability** → added a cross-cutting Phase 1 requirement: structured logs at every new failure branch.

**Rejected (1, with reason):**
- **#1 RPC bearer-token authentication.** Verified real (`list_shares` returns `ShareInfo.password` over unauthenticated 127.0.0.1 `/rpc`). But README.md:98 documents this as a deliberate single-user trust zone, and adding token/Unix-socket auth is a feature, not a codebase-improvement fix — scope creep. Did the cheap, in-theme half (#2 file perms, 1.8) and recorded bearer-token auth as an explicitly deferred, owner-decision item in Risks + Out of scope.

**Partially accepted:**
- **#4 direct SusOpsManager use** is subsumed by the rewritten Decision A (is_running is now correct for non-daemon/in-process and non-child PIDs); no separate API-deprecation work added.

## Round 2 — Codex

Most prior findings acknowledged; 8 new implementation-trap findings (PID identity unsound, lock-order self-contradiction, start() pending-vs-stale underspecified, reconnect test misses the key regression, socat shlex.quote breaks EXEC grammar, file-perm migration/atomicity, Phase 4 missing tests for new fixes, browser assertion wrong). VERDICT: REVISE. (Full critique above this entry in the file.)

### Claude's response (Round 2 → revision)

All 8 accepted as legitimate; two implemented with a *lazier* sound fix than proposed:
- **#1 PID identity** → 1.5 now uses **`create_time` matching** (canonical PID-reuse defense, uniform across ssh/socat/pac which share no argv marker), reusing the `psutil.Process` Decision A already builds, with a legacy-file fallback — instead of a full structured-metadata PID format. Identity check extended to `is_running`/`is_tunnel_running` (fixes reuse-blocked restart), not just kill paths.
- **#5 socat quoting** → 1.7 now **validates host/addr at the config boundary** (hostnames/IPs have no legitimate metacharacters) instead of escaping into socat's nested `EXEC:'…'` single-quote grammar — which Codex correctly noted `shlex.quote` breaks. Sounder and simpler.
- **#2 lock contradiction** → reconciled Decision B + 1.6: one global order (lifecycle outermost; config/share leaf, snapshot-only, released before I/O); added the `remove_connection`/`_remove_forward` refactor as an explicit prerequisite (they currently hold `_config_lock` across I/O).
- **#3 start() pending-vs-stale** → 1.1 now defines the bounded grace state (wait-for-socket timeout or failed `ssh -O check`; gated on the auth watcher so a master isn't killed mid-2FA).
- **#4 reconnect regression** → added the exact test: `is_tunnel_running=True` + `is_socket_alive=False` → `_try_reconnect` returns `False` (the original wedge).
- **#6 perms migration/atomicity** → 1.8 now `chmod`s pre-existing dir/files and writes via `os.open(...,0o600)`, mirroring the atomic write+chmod at `config.py:209-230`.
- **#7 per-fix tests** → added 4.3a: one focused regression test for each new Phase 1 fix.
- **#8 browser assertion** → Phase 3 now asserts the `Browser` object is preserved and the final command equals the *transformed* launch command (PAC arg appended, `open -a`→`open -na`), not raw `launch_cmd`.

No rejections this round — the findings were concrete implementation traps, exactly the cross-model value the review is for.

## Round 3 — Codex

Most prior blockers are addressed on paper: PID identity, lock ordering, pending start semantics, `_try_reconnect` regression coverage, file modes, browser launch assertions, and the extra tests are all materially improved.

Remaining findings:

- `PLAN.md` still defines socat validation as “hostname/IP charset,” which is too restrictive for existing `ssh_host` semantics. `user@host`, SSH aliases, and common internal names may be valid OpenSSH destinations but not hostnames/IPs. Fix: validate `ssh_host` as a single shell-safe OpenSSH destination token, rejecting whitespace and shell metacharacters; apply stricter hostname/IP rules only where they are truly required.

- Recovered/adopted SSH master PIDs are not clearly covered by the new `pid:create_time` identity format. Current recovery paths write raw PIDs in `facade.py`, so strict `find_master_pid` adoption could immediately downgrade back to legacy PID-reuse risk. Fix: add a `ProcessManager.track_existing(name, pid)` helper that records `pid:create_time`, and use it for every recovered/adopted PID.

- `ProcessManager.start()` has an unhandled race: the child can exit before `psutil.Process(pid).create_time()` is read. Fix: if create-time lookup fails or the process is already gone, treat start as failed and do not write a PID file.

- Legacy PID fallback is still too forgiving for stale old files. If a legacy PID file exists, the plan can still accept a reused PID until something rewrites it. Fix: on first successful legacy validation, upgrade the file to `pid:create_time`; if validation is ambiguous and the socket is dead, unlink it as stale.

- The lifecycle lock is still described as covering `fetch`, which can block `stop()`/`remove_connection()` for the full fetch timeout. Fix: reserve the per-connection lifecycle lock for state transitions and process mutations; either make fetch cancellable or explicitly document and test the stop-blocking behavior.

VERDICT: REVISE
### Claude's response (Round 3 → revision)

"Most prior blockers addressed on paper." 5 finer findings — all accepted, all real:
- **ssh_host validation** (1.7): split into two tiers — `ssh_host` validated as a shell-safe *token* (allows `user@host`, ssh-config aliases) while `dst_addr`/`src_addr` keep the strict hostname/IP rule. Codex correctly caught that a hostname/IP regex would reject valid OpenSSH destinations.
- **Adopted PIDs** (1.4/1.5): added `ProcessManager.track_existing(name, pid)` to stamp `pid:create_time`; all recovery/adoption paths (`find_master_pid` callers, orphan sweep) route through it instead of writing raw PIDs.
- **start() create_time race** (1.5 edge a): read create_time right after Popen; child already gone → start fails, no PID file written.
- **Legacy-file forgiveness** (1.5 edges c/d): upgrade `<pid>`-only files to `pid:create_time` on first successful validation; if ambiguous and socket dead, unlink as stale — bounds the reuse window to a single poll for existing installs.
- **fetch holding the lifecycle lock** (1.6): scoped the lock to fetch's master start/teardown transitions only, not the download body, so `stop`/`remove_connection` aren't blocked for the fetch timeout.

No rejections — these were precise, correct refinements that make the Phase 1 fixes actually hold rather than regress on edges.

## Round 4 — Codex

Prior five findings are addressed textually in `PLAN.md`: socat validation, adopted PID metadata, `create_time` start race, legacy PID upgrade, and fetch lock scope are all covered.

Remaining findings:

- [PLAN.md](/Volumes/data/private/development/susops/susops/PLAN.md:17) assumes “the auth watcher already tracks an in-progress prompt,” but the current manager has no explicit auth-pending registry. `_spawn_auth_watcher` just starts a thread, and `_start_times` is uptime state, not prompt state. Fix: add a per-tag auth-pending/deadline map, mark before spawning the watcher, clear on auth success/exit/timeout, and use that for stale decisions.

- `_try_reconnect` can still wedge on a fresh auth prompt. It starts a master directly at [facade.py](/Volumes/data/private/development/susops/susops/src/susops/facade.py:1502), returns `False` if the socket is not alive at [facade.py](/Volumes/data/private/development/susops/susops/src/susops/facade.py:1513), but does not spawn the auth watcher or clean up the pending master. Fix: share the same pending-auth/cleanup path between `start()` and reconnect, or stop the socket-dead reconnect master before returning `False`.

- [PLAN.md](/Volumes/data/private/development/susops/susops/PLAN.md:27) mischaracterizes `_sweep_orphan_masters` as an adopted-PID writer. It is actually a kill sweep with a loose substring match at [ssh.py](/Volumes/data/private/development/susops/susops/src/susops/core/ssh.py:264). That bypasses the new PID identity discipline. Fix: factor one strict SSH-ControlMaster matcher and use it for both `find_master_pid` and `_sweep_orphan_masters`; require same UID and exact `ControlPath`, not substring.

- Phase independence is still contradicted by test placement. [PLAN.md](/Volumes/data/private/development/susops/susops/PLAN.md:7) says phases are independently shippable, but [PLAN.md](/Volumes/data/private/development/susops/susops/PLAN.md:71) puts Phase 1 regression tests in Phase 4. Fix: move 4.3a into Phase 1 acceptance criteria; Phase 4 can keep only unrelated coverage/debt tests.

- [PLAN.md](/Volumes/data/private/development/susops/susops/PLAN.md:37) still allows `O_TRUNC` PID/port writes while claiming to mirror the config’s atomic pattern. Concurrent readers can observe empty files and transiently think the daemon/tunnel disappeared. Fix: add a tiny atomic write helper using temp file, chmod, and replace for PID and port files.

VERDICT: REVISE
### Claude's response (Round 4 → revision)

Prior 5 confirmed addressed; 5 new findings — all accepted, all verified against source (one was a factual error in my plan):
- **Auth-pending registry** (1.1): verified `_start_times` is uptime state and there's no explicit prompt registry. Added a minimal per-tag `_auth_deadline` map set when `_spawn_auth_watcher` starts (1234) and cleared on the socket-up path (793)/exit/timeout — lazier than a full registry, reuses the existing watcher lifecycle.
- **`_try_reconnect` auth wedge** (1.1): verified the body (1494-1513) spawns a master, waits 10s, returns False with no watcher/cleanup. Under socket-authoritative reconnect this would re-spawn every poll during a 2FA prompt. Fix: reconnect shares the pending-auth path (no re-spawn while pending) and stops a socket-dead spawned master before returning False (no orphans).
- **`_sweep_orphan_masters` mischaracterized** (1.4/1.5b): Codex was right and my plan was wrong — verified `ssh.py:264` is a KILL sweep using a loose `sock_str in arg` substring match, not a PID writer. Removed it from the adoption-writer list and added: factor ONE strict ControlMaster matcher (exact ControlPath, same UID) shared by `find_master_pid` and `_sweep_orphan_masters`, replacing the dangerous substring kill.
- **Phase independence vs test placement** (#4): moved the per-Phase-1-fix regression tests into Phase 1 acceptance criteria; Phase 4 keeps only pre-existing coverage/debt tests. Now consistent with "phases independently shippable."
- **Non-atomic PID/port writes** (1.8): verified `SusOpsClient` polls the port file every 100ms, so `O_TRUNC` exposes an empty-file window. Switched to an atomic temp+chmod+`os.replace` helper for both PID and port files.

No rejections — including a correct catch of my own factual error, which is exactly the value of an independent reviewer reading the actual code.

## Round 5 — Codex

Prior five findings are addressed in the revised `PLAN.md`.

The plan now specifies a real auth-pending marker, reconnect behavior that avoids both false success and master pileup, strict shared matching for `find_master_pid` and `_sweep_orphan_masters`, Phase 1 regression tests as Phase 1 acceptance criteria, and atomic `0600` PID/port writes. The `_sweep_orphan_masters` mischaracterization is corrected.

I do not see a remaining material blocker. Residual implementation details to be careful with: guard `_auth_deadline` under the lifecycle lock or a tiny auth lock, and keep endpoint validation compatible with documented values like `0.0.0.0` and ordinary host aliases.

VERDICT: APPROVED
### Claude's response (Round 5 → converged)

APPROVED. Folded in the two non-blocking residual notes: `_auth_deadline` must be accessed under the per-tag lifecycle lock (cross-thread: watcher + monitor + RPC); endpoint validation (1.7) must stay compatible with documented bind values (`0.0.0.0`, `172.17.0.1`, `localhost`) and host aliases — reject metacharacters, don't narrow accepted addresses.

**Converged after 5 rounds.** Codex started at REVISE with 14 findings (round 1), then 8 (round 2), 5 (round 3), 5 (round 4), and APPROVED at round 5. Of ~33 distinct findings: ~31 accepted (many were genuine flaws in the *approach*, not just omissions — the non-child-PID verdict bug, the nested-shell quoting trap, the `_try_reconnect` auth pileup, the non-atomic port-file race, and a factual error in my plan about `_sweep_orphan_masters`); 1 rejected with logged reason (RPC bearer-token auth — deliberate documented single-user trust model, scope creep); 1 partially folded (direct `SusOpsManager` use, subsumed by the rewritten Decision A).

## Implementation outcome

All four phases implemented and committed (9 commits). Full suite: 418 passed,
1 failed, 26 skipped — the single failure (tests/tui/test_shares_screen.py::
test_shares_screen_stop_share) is PRE-EXISTING (red at the baseline commit
524ccdc before any change, identical `assert 0 == 1`), a TUI-pilot timing issue
unrelated to this work. Net +14 passing tests, zero regressions. OpenAPI spec
fresh (RPC surface unchanged).

- Phase 1 (correctness): zombie/identity in process.py, share-dict lock, async
  cleanup-future, strict cross-platform master matcher, per-tag start lock,
  config-boundary host validation, atomic 0600 pid/port files. DEFERRED: the
  coupled socket-authoritative-reconnect + auth-deadline bundle (its
  don't-kill-mid-2FA path can't be verified without real auth prompts) — split
  to a tested follow-up; the primary zombie wedge is fixed.
- Phase 2: ~110 LOC grep-verified dead code deleted.
- Phase 3: browser launch consolidated into one base method; real path now tested.
- Phase 4: BandwidthSampler extracted to core/bandwidth.py (nettop parser now
  unit-tested); _http_probe_via_socks seam extracted; kill_all + cancel_forward
  coverage added.
