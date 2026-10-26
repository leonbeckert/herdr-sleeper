# Sleeping and waking idle agent panes: lessons from a Herdr plugin

These notes come from building a Herdr plugin that sleeps idle coding-agent panes (Claude Code, opencode) after a
configurable window and resumes them later with `--resume <session-id>`. It was built in two stages. The first was
a standalone script driven by launchd, with four review-and-hardening rounds. The second was a plugin (v0.1.0,
then v0.1.1) that went through two adversarial multi-model reviews, a live soak against a real Herdr server, and a
day of live use.

They are written for anyone building the same thing: the authors of `herdr-agent-hibernate` and `herdr-park`, Herdr
upstream (discussion #631), and people doing this on tmux or Zellij. Most traps are not specific to Herdr. Herdr is
just where we found them.

Each lesson gives the trap, the evidence, and what to do instead. Herdr citations are `file:line` in the
herdr repository at **0.9.1** (`master` at `fff6c820`; the running binary was a 0.9.1 preview). The agent was
**Claude Code 2.1.x** (2.1.290 at the time of writing). A lesson marked *[version-specific]* depends on that
behaviour; re-check it against newer versions. "Review" means a finding from one of the code reviews. "Live" means
something that happened on a real session on 2026-10-05.

The invariant behind every lesson: **the only durable handle to a slept agent is its session id plus the argv
and cwd needed to resume it. Losing that record is the one unrecoverable failure.** Starting the same session
twice, which forks its history, is the second worst. Everything else (a late sleep, a refused wake, a noisy log)
is cheap by comparison.

---

## 1. Process and session identity

**1.1 Never delete a record because of indirect evidence.**
- *Trap:* we had a reconcile rule: "the pane now runs a different session, so drop the entry." Pane ids get reused
  (see 2.2). When that happened, the rule deleted the only handle to a session that was still asleep. A second
  path did the same: a crash-recovery snapshot pruned rows for panes that "now run something else".
- *Evidence:* live. A slept session could afterwards be found only by grepping the plugin's event log. Both v0.1.0
  reviews found the same path independently.
- *Instead:* delete a record only on positive proof that the human replaced the session. That proof is the
  *same* terminal (same terminal id, same server run) now running another session. When ownership is unclear,
  move the record out of the pane namespace into an "orphaned" state. Never wake an orphan automatically, list it
  with its manual resume command, and log it once.

**1.2 "Is this session already open somewhere?" must look at every process, and must recognise your own
processes by argv structure.**
- *Trap 1:* we skipped our own wake stub with a substring test: the `ps` line contains `herdr-sleeper` and
  ` stub `. A real `claude --resume U --add-dir …/herdr-sleeper …` whose prompt mentions "stub" then counted as
  ours, and the session was resumed a second time.
- *Trap 2:* a short-lived helper of ours carried the session id in its argv. It then blocked every wake for 90 s as
  "already live".
- *Evidence:* reviews (v0.1.0 F5 and G1.6; v0.1.1 #7).
- *Instead:* match your own processes by exact argv position: the interpreter, then the script basename, then the
  subcommand, then the pane id, then the uuid. Count every other process that selects the session as live. Don't
  let helpers carry the session id.
- *What "selects the session" means for Claude Code [version-specific]:* `claude --resume U`, the
  `node …/cli.js --resume U` form, and `claude --continue` started in the same cwd, which resumes implicitly. If
  you can't read the cwd of a `--continue` process (`lsof` fails), the answer is "unknown", not "no".

**1.3 The agent is rarely the only process in its foreground job.**
- *Trap:* an agent pane's foreground process group includes the agent's MCP servers.
- *Evidence:* lab probe. `pane process-info` on a Claude pane listed a Python MCP server first and a `node … mcp`
  process second, with the agent further down. Herdr itself scans the whole job (`identify_agent_in_job`, used at
  `src/app/agents.rs:439-440`).
- *Instead:* look for the agent anywhere in the job. Never assume the first entry, or the process-group leader, is
  the agent.

**1.4 The session id arrives after the agent appears.**
- *Trap:* code that compares the resumed session id with the expected one sees "no session" and treats it as a
  mismatch.
- *Evidence:* lab probe. After a resume, Herdr reported the agent kind with no session for about 2 s at normal
  load. Under heavy load it took more than 30 s. `agent start` can return before the session is known.
- *Instead:* poll for the session id. Record "started, session not yet reported" as its own state and let a later
  pass confirm it.

**1.5 Address panes by pane id, never by agent name.**
- *Trap:* agent names are not stable. In the standalone script's deployment, names were seen reassigned
  wholesale. While a pane sleeps, another agent can take its name, and `agent start` then rejects the wake as a
  duplicate (`src/app/agents.rs:164-170`, error code `agent_name_taken`). A wake by exec in the pane brings the
  agent back with no name at all, so an orchestrator using `agent prompt <name>` loses it.
- *Instead:* send the exit and every check to the pane id. Restore the name afterwards: `agent rename <pane-id>`
  resolves a pane id before trying a name (`src/app/terminal_targets.rs:79-86`). If the name is taken, fall back to
  another name and log it.

## 2. Herdr restart and restore semantics

**2.1 Terminal ids change on every restore and on nothing else.**
- *Trap:* we keyed the wake gate on terminal id. After a server restart (including `herdr update`), every slept
  pane was refused forever as "recycled".
- *Evidence:* live. Restore calls `TerminalId::alloc()` for every pane (`src/persist/restore.rs:450, 574, 667`).
  The id is `term_<unix-micros><counter>` (`src/terminal/id.rs:15-22`). A shell respawn inside one server run
  keeps the terminal id (`src/app/api.rs:550-558`). Soak scenario P8 confirmed this on a real restart:
  `term_65d20916a736fa` became `term_65d209258aa882`.
- *Instead:* treat a changed terminal id as "a restart happened". It does not mean "a different pane". You still
  need other evidence that the pane at this id is yours (2.2).

**2.2 Workspace ids are reused across restarts. Working directory proves nothing. A restored manual label does.**
- *Trap:* within one run, workspace ids only go up. After restore, the counter is set to max(restored)+1
  (`src/workspace.rs:104`, `reserve_workspace_ids` at `:151-172`). So if the highest-numbered workspace is closed
  and the server restarts, its id is handed out again, and its first pane is `p1` again. A new workspace created
  without `--cwd` inherits the focused pane's cwd (`src/app/api/workspaces.rs:58-63`). Our v0.1.1 first pass
  treated "same pane id and same cwd" as "the restored slept pane". A reviewer then built a case where a fresh
  workspace gets an old session resumed into it on the first click.
- *Evidence:* review (v0.1.1 #1), checked against the source. Pane numbers inside a workspace that *survives*
  are never reused (`src/workspace.rs:1474`, test `pane_public_numbers_are_stable_and_not_reused_after_close`).
  The gap is only after a restart.
- *Instead:* restore re-applies the saved manual pane label to the new terminal (`src/persist/restore.rs:453,
  577-578, 674-675`). If you mark slept panes with a label, a pane that carries your label after a restart is the
  restored slept pane, and a pane without it is a reused id. Treat cwd as a precondition for waking ("cd back"),
  not as identity.

**2.3 Only record the new identity after a wake succeeds.**
- *Trap:* when we re-stamped the record with the new terminal id before the wake's other checks had passed, a
  refused wake left the record "owned" by a terminal never proven to be ours. A later rule then deleted the record.
- *Evidence:* review (v0.1.1 #2).
- *Instead:* keep the candidate identity in memory, and write it only together with the state change of a wake
  that started. The one other action that may adopt a new terminal is a re-claim after a restart, and only on
  stronger evidence than the label: our own stub for this pane and this session running in it (10.2).

**2.4 `state_change_seq` restarts at 0 on every server run.**
- *Trap:* we keyed idle clocks on `state_change_seq`. After a restart, small counter values match by coincidence.
- *Evidence:* live, and in the source: it is server-global, initialised to 0 (`src/app/mod.rs:489`), never restored,
  and incremented only when an agent's state changes (`src/app/actions.rs:1692-1699`).
- *Instead:* don't use it as a key across restarts on its own. Combine it with the terminal id, which changes on
  every restore, and the session id.

**2.5 Herdr resumes agents on restore by itself, and your claim decides whether it resumes a slept one.**
- *Trap:* `session.resume_agents_on_restore` defaults to true (`src/config/model.rs:278`). A slept pane whose
  persisted session ref survives can be resumed by Herdr on the next restart, behind the sleeper's back.
- *Evidence:* source. Reporting an agent through the hook-authority path (`pane report-agent`, which is what a
  sidebar "sleeping" claim is) clears the terminal's persisted session ref (`src/terminal/state.rs:781`, in
  `set_hook_authority_at`, reached from `src/app/actions.rs:1490`). Restore also replays the pane's saved screen
  history (`initial_history_ansi`, `src/persist/restore.rs:638, 657`). After a restart, a slept pane therefore
  shows your old "press Enter to resume" text although no stub is running in it.
- *Instead:* decide on purpose which of the two (Herdr or the sleeper) owns resume after a restart. Don't word the
  in-pane hint so that it only makes sense while a stub is actually running. Since Herdr 0.9.2 the claim can also
  name what Herdr should run after a restart (10.1).
- *Untested:* a late agent hook report landing after your claim takes authority back.

**2.6 Startup hooks run at server start and on live-handoff import. They do not run on plugin install.**
- *Evidence:* `run_plugin_startup_hooks` is called from `src/server/headless/bootstrap.rs:88` (server start) and
  `:197` (handoff import). A plugin linked into a running server gets no startup hook.
- *Trap:* a detached watcher started by an earlier server can still be alive when the next startup hook runs.
- *Instead:* make startup idempotent, and provide a manual "ensure" action.

## 3. The plugin runtime

**3.1 The plugin config dir is `<config>/plugins/config/<plugin-id>/`.**
- *Trap:* that is the path in `HERDR_PLUGIN_CONFIG_DIR` (`src/plugin_paths.rs:15-19`, exported at
  `src/app/api/plugins/env.rs:15-29`). We wrote the operator's config to `<config>/plugins/<plugin-id>/`, and the
  watcher ran for a day without its excludes.
- *Evidence:* live. Debug builds use `herdr-dev` instead of `herdr` as the config dir name
  (`src/config/io.rs:22-27`).
- *Instead:* when the plugin runs by hand without Herdr's env (manual runs, the in-pane stub), make its fallback
  paths compute exactly what Herdr would. If an older config file exists and the current file lacks a
  safety-relevant key such as `exclude`, refuse to run. Don't fall back to defaults.

**3.2 Plugin commands inherit the server's whole environment.**
- *Evidence:* Herdr adds a fixed set of variables (`src/app/api/plugins/runtime.rs:39-79`) on top of the inherited
  environment. It never clears it. `HERDR_PANE_ID` is set only when the invocation context has a focused pane
  (`:70-72`).
- *Trap 1:* a server started from inside another Herdr pane carries that outer pane's `HERDR_PANE_ID`. A plugin
  that falls back to the variable acts on a different pane that happens to share the id.
- *Trap 2:* `HERDR_SESSION` is not injected at all. It is there only because the server process set it on itself
  (`src/session.rs:478`). Pane ids are unique only per session, so key state by session.
- *Instead:* read the target pane only from `HERDR_PLUGIN_CONTEXT_JSON`. Assume every other variable may come from
  somewhere you didn't expect (see also 9.2).

**3.3 The interpreter is whatever the server's PATH finds.**
- *Trap:* a command is exec'd directly in the plugin root (`src/plugin_command.rs:5-9`), so a `#!/usr/bin/env
  python3` shebang resolves against the server's PATH. On macOS, a server launched outside a login shell found
  Xcode's `/usr/bin/python3`, which is 3.9.6. Anything that needs 3.11, such as `tomllib`, broke.
- *Evidence:* live. A second trap: the in-pane stub runs under the *pane shell's* PATH, which can be a different
  interpreter. One config file could then parse differently in the two processes.
- *Instead:* target the oldest interpreter you might get. If you need a fallback parser, make it a strict subset
  that refuses anything it can't parse exactly, and test it on every interpreter.

**3.4 There is a global cap of 32 plugin commands in flight, and each event hook is a separate process.**
- *Evidence:* `MAX_PLUGIN_COMMANDS_IN_FLIGHT = 32` (`src/app/api/plugins/runtime.rs:12`). When it is reached, new
  commands fail to launch for *every* plugin (`:83-101`). Every `pane.focused` event starts a process.
- *Trap:* a focus hook that blocks on a lock held for a 25 s exit wait occupies a slot the whole time. Cycling
  through slept panes can then starve every other plugin's hooks.
- *Instead:* make hooks give up quickly with a bounded lock timeout (the next focus retries). Return without
  taking the lock for panes that are not yours.

**3.5 A long-running child must detach from the command's stdout and stderr.** *[derived from source, not
observed]*
- *Evidence:* Herdr pipes a command's stdout and stderr and reads them to EOF before it reports the command
  finished (`src/app/api/plugins/runtime.rs:121-140`, `read_capped_plugin_output` at `:284`). Only that report
  releases the in-flight slot (`src/app/api.rs:145-146`).
- *Trap:* a daemon that inherits those pipes keeps its startup command "running" and holds a slot for its whole
  life.
- *Instead:* spawn the watcher in a new session with stdin, stdout and stderr all set to `/dev/null`.

## 4. Typing into panes

**4.1 `pane run` uses bracketed paste only if the application in the pane turned it on.**
- *Trap:* otherwise the bytes go straight to the line editor (`src/app/api_helpers.rs:24-31`). Our typed wake line
  contained raw ESC bytes to reset terminal modes, which an agent killed by SIGTERM can leave set (alternate
  screen, mouse reporting). Without bracketed paste (bash 3.2, dash, or a shell whose paste mode the dead agent
  left off), those bytes are keystrokes that garble the line and can trigger key bindings.
- *Evidence:* review (v0.1.0 F9), checked in the source. `agent start` refuses control characters in its argv
  (`src/app/agents.rs:156-161`); `pane run` does not.
- *Instead:* never type control bytes. Emit escapes through `printf '\033[…'`.

**4.2 `agent start` needs a pane where the shell is the only foreground process and no agent is reported.**
- *Trap:* `agent start` refuses with `agent_pane_busy` when the terminal has any reported agent, including your
  own sidebar claim (`src/app/agents.rs:187-188`). It also refuses when the foreground job is anything other than
  the shell alone (`src/platform/mod.rs:367-378` via `agents.rs:194-195`). A soak scenario that killed our in-pane
  stub but left the claim failed exactly this way.
- *Instead:* before waking with `agent start`, release your claim and make sure no stub of yours is still in the
  foreground. When `agent start` refuses, treat it as a reason to keep the record.

**4.3 An in-pane stub that reads stdin eats input meant for something else.**
- *Trap:* the sidebar still shows a slept pane, so orchestrators and humans keep sending it text with `pane run`.
  A stub that wakes on any line throws the text away.
- *Evidence:* review (v0.1.0 F10).
- *Instead:* wake only on a bare Enter. Echo anything else back as "ignored".

**4.4 Typed command lines are shell-specific.**
- *Trap:* `VAR=value cmd` and POSIX quoting are wrong in nushell or PowerShell panes. Herdr quotes its own
  `agent start` command per shell (`interactive_shell_command`, `src/platform/macos.rs:208`; quoting test at
  `src/platform/mod.rs:636-654`).
- *Instead:* prefer `agent start` for wakes. If you must type a line, use `env VAR=… cmd`, and limit yourself to
  POSIX shells or say plainly where it won't work.

**4.5 Exiting the agent: SIGTERM, check twice, and allow for slow exits.** *[version-specific: Claude Code
2.1.x, opencode]*
- *Trap:* typing `/exit` into the composer can submit a half-written draft. SIGTERM shuts both agents down
  cleanly and discards the draft. Claude Code can take more than 25 s to exit while its MCP servers shut down.
- *Instead:*
  1. Send SIGTERM to the pane's agent process, never to a process found by name.
  2. Count the agent as gone only when Herdr has dropped it *and* `process-info` shows no agent process.
  3. Write the sleep record *before* the exit, in an "exit requested" phase. If the agent is still there after
     the wait, leave the record in that phase for the next pass.

## 5. Unknown is not absent

**5.1 Every missing or empty field in Herdr's API can mean "could not tell".** Each place we read one as "no"
became a double-resume or a crash path in review:
- *`agent_session`:* omitted when no session ref is stored (`src/api/schema/agents.rs:209-210`). Code that indexed
  it directly crashed the watcher on the first such pane (review v0.1.0 F1). Code must handle both "missing" and
  `null`.
- *`argv`:* optional per process (`src/platform/mod.rs:43-44`). On macOS it is `None` whenever argv can't be read
  (`src/platform/macos.rs:942`). Skipping such a process made a live agent look absent (review G1.3).
- *`foreground_processes`:* an empty list whenever Herdr could not find the foreground job
  (`src/app/api/panes.rs:534-551`, `unwrap_or_default`). An idle shell pane always lists the shell, so an empty list
  means "unknown", not "bare shell" (review v0.1.1 G2). Fakes in unit tests must reproduce this; ours didn't at
  first.
- *`ps` or `lsof` failures:* unknown.

*Instead:* make every predicate three-valued (yes, no, unknown). Both sleep and wake refuse on unknown.

## 6. Idle clocks

**6.1 Herdr gives you no timestamps.**
- *Evidence:* `AgentInfo` has `state_change_seq` and `completion_seq` but no time
  (`src/api/schema/agents.rs:220-223`). The seq moves only on a state transition (`src/app/actions.rs:1692`), not
  on focus. The standalone script used the Claude transcript's mtime, which works only for Claude.
- *Instead:* define idle as "wall-clock time since the observed seq last moved, while idle or done and not
  focused". Persist the clock so a watcher restart doesn't reset every window. Key it by pane, terminal id, seq
  and session id. Any mismatch, damage or doubt starts the clock *now*: a later sleep, never an earlier one.

**6.2 Save a reset to disk before acting on any clock.**
- *Trap:* a focus resets a pane's clock in memory, then the watcher dies before writing it. After a restart, the
  old start time still matches and the pane is slept immediately.
- *Evidence:* review (v0.1.1 G6).
- *Instead:* in each pass, update and save every clock first, and only then run any sleep.

**6.3 Every entry point must honour the clock.**
- *Trap:* a manual `scan` that ignored the persisted clocks slept every eligible pane at once.
- *Evidence:* review (v0.1.1 #9).
- *Instead:* make anything that can sleep read the same clocks.

**6.4 Sleeping a `done` pane erases the "finished, not yet viewed" cue.** Carry the status into the sleeping
label, or leave `done` panes alone for longer. (Review v0.1.0 F22.)

## 7. Watcher lifecycle

**7.1 Never exit on failure.**
- *Trap:* the v0.1.0 watcher exited after 10 failed ticks. Startup hooks don't run again while the server lives
  (2.6), so nothing replaced it, and sleeping stopped without notice.
- *Evidence:* a server stall in which every CLI call timed out at 30 s for minutes (swap pressure) was enough
  (review v0.1.0 F4).
- *Instead:* back off up to a cap and keep retrying. Let any later plugin activity (a focus hook, a list) check
  cheaply that the watcher is alive, and respawn it if not.

**7.2 An unexpected exception should cost one pane, not the service.**
- *Evidence:* one pane with no session id killed the v0.1.0 watcher (F1). In v0.1.1, one malformed pane still
  aborted the rest of each pass (review v0.1.1 #6).
- *Instead:* catch and log per pane and per pass.

**7.3 PID files lie.**
- *Trap 1:* a dead watcher's pid gets reused. A test like "pid alive and command contains `watch`" then matches
  `cargo watch`, or another session's watcher.
- *Trap 2:* two startups racing on read-back of the pid file can leave zero watchers.
- *Evidence:* reviews (v0.1.0 F16 and F20; v0.1.1 #5).
- *Instead:* make the watcher hold an exclusive `flock` on a per-session lock file for its whole life. "Alive"
  means pid alive, exact argv tokens match, and the lock is held. Never send a signal to a pid you haven't verified.

**7.4 Upgrades run two versions at once.**
- *Trap:* hooks run the new file the moment it changes on disk, but the watcher keeps running the old code until
  something replaces it. The old watcher may not know your new liveness protocol (it holds no lock, for example),
  so the new code's "is a watcher running?" check can accept it indefinitely.
- *Evidence:* review (v0.1.1 G3).
- *Instead:* detect an older-protocol watcher and replace it. On SIGTERM, finish the transaction in hand before
  exiting. Don't let a respawned watcher inherit test overrides such as a zero idle window.

**7.5 Retire the previous mechanism completely.**
- *Trap:* we stopped the standalone launchd job with `launchctl bootout` but left its plist installed. It came back
  at the next login, and two sleepers with separate journals acted on the same panes.
- *Evidence:* live.
- *Instead:* when migrating from an external daemon (launchd, systemd, cron), remove or disable its unit file, not
  only the running job. Consider having the new sleeper refuse to run while another sleeper's state is still being
  written.

## 8. Concurrency between the watcher, hooks and the in-pane stub

**8.1 Never drop the record before the resumed agent is verifiably running.**
- *Trap:* our stub dropped the record and then called `exec` on the agent. Two ways to lose the handle followed:
  - Ctrl-C between Enter and `exec`.
  - `exec` failing because the agent binary is not on the pane shell's PATH.
- *Evidence:* reviews (v0.1.0 F7 and G1.2; v0.1.1 G1). For the second, Herdr started the agent through a typed
  command line, so the agent may be a shell function or an nvm shim.
- *Instead:* under the lock, after the final checks, move the record to a `waking` phase that names the stub's pid
  (`exec` keeps the pid, so the pid then names the agent). Every other path refuses a `waking` record whose pid is
  alive. A later pass finishes the record once the session reports. If the pid died with no session, the pass
  puts the record back to asleep. Anything that stops the stub before `exec` puts the record back at once.

**8.2 Run the liveness check and the state change under one lock.**
- *Trap:* a focus wake and the in-pane stub each checked "not live elsewhere" outside the lock and then both
  resumed (review G1.2a).
- *Instead:* re-read the record under the lock and check that it is still the same session in the same phase,
  then re-check liveness and transition, all in one critical section.

**8.3 Restore with replace semantics, never "set if absent".**
- *Trap:* putting a record back with `setdefault` silently lost it when the key had been filled in the meantime
  (review v0.1.1 G4).
- *Instead:* if the slot now names another session, write yours to an orphan key.

**8.4 A manual sleep must not overwrite a record kept for a different session.**
- *Trap:* records in other phases, such as "displaced" or "wake pending", are handles too (review v0.1.1 #4).

**8.5 One sighting is not proof during an exit.**
- *Trap:* a focus during the "exit requested" window saw the agent still running and cleared the record. The agent
  then finished exiting (review v0.1.0 F8).
- *Instead:* require consecutive sightings with a real process, and reset the streak on anything unconfirmed.

**8.6 Don't wait under the lock for something that needs the lock.**
- *Trap:* a `wake` command that waits for the stub to confirm deadlocks if it waits while holding the journal lock,
  because the stub needs that lock.
- *Instead:* release the lock, then wait. Report a slow start as "started, not yet confirmed", not as a failure.

## 9. Testing against a real multiplexer

**9.1 Isolate the plugin registry, not only the session.**
- *Evidence:* the plugin registry is `$XDG_CONFIG_HOME/herdr/plugins.json` (`src/persist/plugin_registry.rs:12`,
  `src/config/io.rs:30`). It is global per config dir, not per session.
- *Trap:* `HERDR_SESSION=lab` alone still runs whatever plugin version the real registry points at.
- *Instead:* run the lab with its own `XDG_CONFIG_HOME` holding a hand-written `plugins.json` that points at the
  development copy. Unset `HERDR_SOCKET_PATH` and `HERDR_CLIENT_SOCKET_PATH`. Keep that directory short (under
  `/tmp`): a deep path made the socket path exceed `sun_path` ("local socket name length exceeds capacity").

**9.2 Don't start the lab server from inside an agent session.**
- *Trap:* a lab server started from a Claude Code session inherited `CLAUDE_CODE_CHILD_SESSION`. Every Claude Code
  it spawned then ran with transcript saving off, so there was nothing to resume. *[version-specific: Claude Code
  2.1.x]*
- *Evidence:* live, and the same inheritance as 3.2.
- *Instead:* unset `CLAUDECODE`, `CLAUDE_CODE_*` and similar marker variables before starting a lab server, and
  check that the test agent's transcript exists before testing a resume.

**9.3 Real agents are slow and load-sensitive.**
- *Trap:* scenarios that waited a fixed time flaked. A resumed Claude Code took more than 30 s to report its
  session.
- *Evidence:* live. At a load average of 223, 8 of 21 soak checks failed. At normal load, the same code passed
  26/27, and the one failure was a fixed 12 s window.
- *Instead:* poll for outcomes with generous ceilings, never sleep a fixed time. Run soaks at low CPU priority.
  Note the load average next to every result.

**9.4 Exercise the real restart.**
- *Trap:* unit tests with a fake Herdr passed while restore broke every wake (2.1). Fakes also drift from the
  real thing: ours returned an empty process list for an idle pane, which Herdr never does.
- *Instead:* the soak must stop and restart the server with a slept pane, reproduce a reused pane id (close the
  highest workspace, restart, create a workspace), and sleep a focused pane. Small trap: a new directory can hold
  `agent start` on Claude Code's folder-trust prompt, so run lab agents in directories that are already trusted.

## 10. Reboots

These come from the first reboot of the deployment host (2026-10-26) and from reading Herdr **0.9.5** (tag
`v0.9.5`); citations in this section are against that tag. On that reboot every awake Claude pane came back
natively; no slept pane was exercised. Two labs against a real, isolated, headless Herdr 0.9.5 server then
confirmed 10.1 and 10.4 and found 10.5 to 10.8; the protocol and results are in 10.9.

**10.1 A claim does not survive a restart; a reported resume command does.**
- *Trap:* a slept pane is saved with its cwd and its 💤 label and nothing else. The stub is a process, and
  processes are not saved. The claim is hook authority, which is not in the snapshot either. Our own claim also
  cleared the pane's persisted agent session (2.5). After a restart the pane is a bare shell with the label:
  Enter does nothing, and only focus or `wake` resumes it.
- *Evidence:* source. Since 0.9.2 (CHANGELOG #4687) `pane report-agent … -- <argv>` stores a resume command for
  the reporter that holds the pane (`src/app/api/panes.rs:1554-1597`, `report_agent_resume` at `:1668`). The
  snapshot saves it (`src/persist/snapshot.rs:375`), restore puts it back on the new terminal
  (`src/persist/restore.rs:457, 603, 703`) and prefers it over a native plan (`pane_restore_startup`,
  `:815-827`), as long as `session.resume_agents_on_restore` is on. Herdr drops it when the reporter releases
  the pane (`forget_reported_resume_of`, `src/terminal/state.rs:2022`, called from the release path at `:1859`),
  when the pane is back at an idle shell (`:1901`), or when another agent takes the pane
  (`reconcile_reported_resume`, `:1996-2009`).
- *Trap 2:* `validate_resume_argv` (`src/agent_resume.rs:58-89`) refuses a path as the first element (restore
  types the command into the shell), an apostrophe anywhere, control characters, more than 64 arguments or
  8 KiB. It checks only the first element for a plain name, so `env VAR=… /abs/path/plugin stub …` passes.
- *Instead:* report the stub's argv as the resume command in the claim, and only when the stub was actually
  typed. Build the typed line and the reported argv from one function, so the process that comes back is exactly
  the one your structural stub match (1.2) recognises. Check the argv locally against the same rules first, and
  on any refusal fall back to the plain claim and log it: a pane without a resume command still wakes on focus.

**10.2 After a restart, claim the pane again, and only on proof.**
- *Trap:* the resume command brings the stub back, but not the claim: the sidebar row is gone, and the stored
  command belongs to a reporter that no longer holds the pane, so the next restart may not have it.
- *Instead:* in reconcile, a row that is asleep, whose pane has our label and a new terminal id, *and* whose
  foreground is our stub for this pane and this session, is ours: claim it again with the same resume command,
  and record the new terminal id in the same step (2.3). Leave a restarted pane without the stub alone; focus
  and `wake` handle it as before.

**10.3 Native restore drops the flags a session was started with.**
- *Trap:* Herdr's own restore plan for Claude is `claude --resume <id>` and nothing else
  (`src/agent_resume.rs:203-210`). A crash-recovery snapshot that records "what runs now" overwrites the flags
  it had with nothing on the first tick after a restart, and a later sleep of that session would replay none.
- *Instead:* when the live argv after the binary is exactly `--resume <same session>`, keep the flags the
  snapshot already had, and let a sleep of such a session take its flags from the snapshot.

**10.4 Plan for the service coming up before, after, or without the multiplexer.**
- *Trap 1:* with an externally supervised watcher (launchd), nothing in the plugin starts one, and nothing said
  that it was missing: sleeping, reconcile and mail flushing stopped silently. On 2026-10-26 the service jobs
  of the deployment host did not start after an OS update; the cause was outside the plugin.
- *Trap 2:* a watcher that starts before the server sees every call fail and backs off exponentially, so it
  catches up minutes after the server is there.
- *Trap 3:* an atomic `os.replace` without an fsync can leave an empty file after a power cut.
- *Instead:* report a missing external watcher wherever someone looks (a WARN and an event at startup, rate
  limited from hooks, a header in `list`, a note in the agents' peer list). Treat "no server" as a reason to
  retry at the poll interval, not as a failure. fsync the file, then the directory.
- *Lab (10.9):* a headless server with no client attached does start a restored resume command, also one whose
  agent label it does not know (`sleeper`). It types the command into the restored login shell before its first
  prompt, so it runs under the pane shell's `PATH` and Python (keep the script an absolute path behind `env`),
  and `process-info` shows the argv after `env` has exec'd (`python … herdr-sleeper stub …`). The claim is not
  restored: `pane get` has no `agent` until the next tick claims the pane again. A watcher that ticked during
  `server stop` got `server_unavailable` ("server is shutting down"), which now counts as "no server" too.

**10.5 Some CLI calls answer success with nothing.**
- *Trap:* the plugin trusts the JSON envelope, not the exit code (Herdr prints `{"error": …}` with exit 0 for
  most calls). But the "ok" calls (`pane report-agent`, `report-agent-session`, `release-agent`,
  `report-metadata`, `send-keys`, `send-text`, `server stop`; `send_ok_request`, `src/cli.rs:755`) print nothing
  on success and print the error JSON on stderr with exit 1. The new resume report used the checked path, so
  every successful report read as a failure: the lab log said `resume-not-reported`, and the claim was sent a
  second time without the command. Herdr had saved the command anyway, so only the log was wrong.
- *Evidence:* lab, first sleep. The unit tests passed because the fake `herdr` replaced the whole call, not the
  process it parses.
- *Instead:* list the commands that answer with nothing and treat exit 0, empty stdout and no error on stderr as
  success for them. Test the parser against the real output shapes (a fake `subprocess.run`), not only the
  decision logic against a fake `herdr`.

**10.6 A pane whose shell never came back keeps Herdr's own resume plan.**
- *Trap:* when the agent's exit does not return the pane to its shell (in the lab, `claude --resume …; sleep 900`),
  Herdr keeps the agent for longer than the exit wait, and then keeps the pane's persisted Claude session. Our
  claim is ignored there (`current_session_owner_conflicts`, `src/terminal/state.rs:1330`, inside
  `set_hook_authority_at`, `:701`), and the next restart resumes the session natively, awake, under the 💤 label.
  The sleep transaction had also given up before labelling and claiming the pane (`exit-uncertain`), and the
  reconcile that later found the agent gone set the row to `asleep` with no label, so a restart would have
  orphaned its own pane.
- *Instead:* when reconcile settles a late exit in the same terminal, label the pane and claim it then, with
  `stub = false` and no resume command. When a session turns out to run again (two sightings) and the pane still
  carries our label, put the pane's own label back. A stub-less pane therefore comes back either awake (Herdr
  kept its session) or as a labelled shell that wakes on focus; it is never lost. `agent list` rows carry no
  `label`; read it from `pane list`.

**10.7 A saved resume command outlives the row it was for.**
- *Trap:* a slept pane whose 💤 label was removed is orphaned after a restart (2.3), but Herdr has still typed our
  stub back in, and keeps the command for every later restart. Neither a `release-agent` without the claim nor
  the stub exiting back to the shell drops it (lab); the stub just prints "no journal entry" each time. The stub
  can also exit before reconcile looks, because reconcile orphaned the row first, so "is our stub running" is
  not a usable test for "is our command saved".
- *Instead:* when reconcile orphans a row that may have reported a resume command (`stub` not false) and no agent
  runs in the pane, claim the pane and release it. A release by the holder drops the command (lab: the next
  restart typed nothing). A claude that starts in the pane also drops it (another agent took the pane).

**10.8 The stub path is not the only wake after a restart.**
- *Lab:* Enter before the first tick (the stub runs, the pane is not claimed yet) resumed the same session; the
  stub's release cannot drop the command without the claim, but the resumed claude takes the pane, and that drops
  it. Focus on a headless server works: `workspace focus` fires `pane.focused` with no client attached, and the
  plugin's hook delegated Enter to the stub (or started the agent with `agent start` in a labelled bare shell).
  After every wake the pane's saved plan was Claude's own again, and a restart after the wake brought the
  session back natively as `claude --resume <id>`, with no stub.

**10.9 Lab protocol and results (2026-10-26, Herdr 0.9.5, Claude Code 2.1.306).**
- *Setup:* `env -i` with a whitelist (HOME, USER, LOGNAME, SHELL, LANG, LC_ALL, TERM, TMPDIR, PATH) plus its own
  `XDG_CONFIG_HOME` and `XDG_STATE_HOME` under a fresh `/tmp` directory; no `HERDR_*` or `CLAUDE*` variables.
  Server `herdr --session sleeper-test server` started in the background, never attached; every `server stop`
  only after checking that `herdr status server` names the lab socket. The plugin was linked from the
  development clone (`herdr plugin link`), config `idle = "24h"`, `watcher = "external"`, `notify = false`,
  `poll_seconds = 3`. Sleeper CLI and watcher ran from the clone with `HERDR_SESSION`, `HERDR_SOCKET_PATH` (the
  lab socket) and `HERDR_BIN_PATH`, so plugin hooks and manual runs shared one state dir. Three throwaway Claude
  sessions were seeded with `claude -p … --setting-sources local --model haiku --session-id <uuid>` in
  separate cwds and resumed interactively in three panes (A, B, C); a fourth pane (plain shell) was there to
  move focus away. The CLI and watcher ran on Homebrew Python 3.15, the in-pane stub on `/usr/bin/python3` 3.9.
- *A, stub, the main case:* `sleep-pane` → `pane get` shows `agent: sleeper`, label `💤 …`, the stub in the
  foreground, and `session.json` holds `agent_resume` = the stub argv (`env … herdr-sleeper stub <pane> <uuid>
  <state dir>`). `server stop` + headless start → the stub runs again (new pid, same pane id, new terminal id),
  the label is back, `pane get` has no agent, `list` shows the row `asleep`, not orphaned. One tick (`scan`, and
  in later rounds the running watcher) → `reconciled` / `reclaimed-after-restart` with the new terminal id, the
  journal row carries it, `pane get` shows `agent: sleeper` again. Wakes, each after its own sleep and restart:
  `wake` (Enter delegated to the stub), Enter by `send-keys` before any tick, and focus (`workspace focus`) —
  every time the same pid exec'd into `claude --resume <same uuid>`, the label went back to the original, and
  the saved plan became Claude's own (`agent_resume` null, `agent_session` = the uuid). Pass, after the fix in
  10.5 (before it, the same results plus a false `resume-not-reported`).
- *A, no stub after a restart:* sleep, ctrl-c in the stub ("left as a shell", claim released, command dropped),
  restart → a labelled bare shell, the watcher left it alone; focus → `identity re-established after a Herdr
  restart`, `agent start` resumed the same uuid. Pass.
- *B, slept without a stub (`claude …; sleep 900`):* `exit-uncertain` (25 s), then reconcile → `asleep`, now
  with the 💤 label and `stub: false`, but no claim (10.6). Restart → Herdr resumed the session natively,
  awake; two ticks later the row was dropped (`running`) and the label cleared. Not the bare-shell outcome the
  plan expected; it is safe (one live copy, nothing lost) and documented in 10.6.
- *C, label removed before the restart:* `pane rename --clear` on a slept pane, restart → Herdr typed the stub
  in anyway, the tick re-keyed the row `orphan:<uuid8>` (`list` prints the manual resume line), and dropped our
  saved command (10.7); Enter in that stub left a shell and resumed nothing ("the journal entry changed"; in the
  race case "no journal entry"). The next restart typed nothing into C. Pass with 10.7 (before it, every
  restart typed the stub again).
- *Server down:* the external watcher, running across a `server stop` with a 4–10 s gap, logged "herdr server
  not reachable … retrying every 3s" once and "reachable again after 2 tick(s)", then re-claimed A in the same
  tick. Pass (after counting `server_unavailable`).
- *Not covered:* a real macOS reboot or loginwindow restart; an awake session busy at the restart; opencode;
  Python 3.12 for the in-pane stub. A best-effort `agent rename` back to the slept name failed once right after a
  stub wake (`agent_not_found`, the agent was not registered yet); logged, not retried.

---

## Asks for Herdr upstream

Only the multiplexer can make these safe. Each item names the workaround it would remove.

1. **A pane identity that survives restore.** Either keep terminal ids across restore, or add a persisted pane
   uid, or expose "restored from terminal X" on the pane. *Removes:* the label-as-evidence rule (2.1–2.3).
2. **Monotonic workspace ids across restarts.** Persist the counter rather than deriving max(restored)+1.
   *Removes:* the reused-pane-id class (2.2, 1.1).
3. **A server-run id and timestamps.** A per-run instance id on `agent list`, plus a wall-clock time for the last
   state change (or a seq that is persisted). *Removes:* the composite idle-clock key (2.4, 6.1).
4. **A session registry.** "Which pane or process has agent session U open," server-wide. Herdr already resolves
   sessions per terminal. *Removes:* `ps` and `lsof` argv parsing (1.2), which is the main double-resume risk.
5. **Unknown that is distinguishable from empty in `pane process-info`.** For example, `foreground_processes: null`
   when the job could not be read, and an explicit marker when argv is unreadable (5.1).
6. **A first-class sleeping state, as in discussion #631.** A pane state that keeps the sidebar row, the agent
   name and the session ref across restarts. It would refuse `agent start` for any other session, and wake through
   `agent start --resume` with the name kept. Plugins would then need no claim, no in-pane stub and no journal
   for the common case.
7. **Plugin runtime documentation and options.** The things we had to learn from source or from incidents:
   - the config dir path (3.1);
   - full environment inheritance, including `HERDR_SESSION` (3.2);
   - startup hooks run on server start and handoff, not on install (2.6);
   - the 32-command cap is shared by all plugins (3.4);
   - stdout and stderr are read to EOF (3.5).

   A supervised long-running plugin process type would remove the detached-daemon problems in 7.1–7.4. A manifest
   field naming the interpreter would remove 3.3.
