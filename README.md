# herdr-sleeper — sleep idle resumable agents, wake them later

A Herdr plugin that sleeps idle agent panes after a user-chosen window and
wakes them — on focus, by pressing Enter in the pane, or by command. It is
the standalone [`scripts/herdr-sleeper`](https://github.com/djbclark/herdr/tree/herdr-sleeper/scripts/herdr-sleeper)
reference implementation reborn as a plugin: the fail-closed safety rules
are unchanged, and the plugin adds what only a plugin can do (an in-process
idle watcher, wake-on-focus, a sidebar placeholder for slept panes,
notifications).

Grown from a study of the two existing community plugins — good ideas
adopted from both (see *Credits* at the end); the safety state machine,
argv replay filter, session-live checks, and config validation are ours and
carried over from the standalone script's three review rounds plus its
live-deployment iteration.

Building something similar, for Herdr or another multiplexer?
[LESSONS.md](LESSONS.md) collects the non-obvious traps we hit: pane and
session identity across restarts, the plugin runtime, typing into panes, idle
clocks, watcher lifecycle and testing against a real server. Each comes with
evidence. It is written to be useful whether or not you use this plugin.

## Install

```bash
herdr plugin install djbclark/herdr-sleeper
```

Requires Herdr ≥ 0.9.1 on macOS or Linux, and Python 3.9 or newer on the
PATH the Herdr server sees (stdlib only — no packages, no venv; config
parsing falls back to a built-in TOML subset below Python 3.11). Slept panes
survive a Herdr restart with their stub from Herdr 0.9.2 on (reported resume
commands) while `session.resume_agents_on_restore` is on, which is the default;
see *Restarts and reboots*.

For local development:

```bash
git clone https://github.com/djbclark/herdr-sleeper
herdr plugin link <checkout>
```

## What it does

After an agent has been idle for a user-chosen window (`idle = "12h"` —
default 12 h), it exits the agent process (SIGTERM for claude and opencode:
both shut down cleanly; direct children that outlive it, such as MCP
servers, get a SIGTERM once they are reparented to launchd/init), keeps the pane, reports a `sleeper` placeholder so the sidebar keeps its
row, leaves a wake stub in the pane ("press Enter to resume"), prefixes the
pane label with 💤, and posts a notification with the freed MB.

Wake paths:

1. **Focus the pane** — the `pane.focused` hook wakes it (disable with
   `wake_on_focus = false`). When the pane's stub does it, the hook waits
   (outside the global lock) until the session is up, then finishes the
   record and hands the agent its name back.
2. **Press Enter in the pane** — a bare Enter: the wake stub execs the agent
   back into its session after the same fail-closed checks and restores the
   pane label; the next watcher tick hands the agent its name back once the
   session is seen running. A typed line is not a wake request (an
   orchestrator's `pane run` must not be eaten): the stub echoes it as
   ignored and keeps waiting.
3. **Command** — `herdr plugin action invoke djbclark.herdr-sleeper.wake`
   wakes the context pane (only that pane), or `wake-all` / `list` / `log`
   from anywhere. When the pane's stub does the wake, `wake` waits up to
   30 s for the session to come back before reporting success (a stub that
   exec'd but whose session Herdr has not reported yet counts as started).

Lifecycle: `stop` stops the watcher and keeps it stopped (a marker in the
state dir; hooks spawn nothing, a restarted watcher idles) until `start`.
`uninstall` runs before removing the plugin: it stops the watcher, wakes
every sleeper, and lists what is left (orphaned, displaced, failed wakes);
the state dir stays while anything is listed. `ps` and `lsof` are looked up
with the system directories added, so a launchd job's bare PATH works.

The idle clock is `state_change_seq` movement from the watcher's polling of
`agent list` (default 60 s) — no transcript mtime, no launchd, and the
clock restarts on any state change between polls. `done` (idle-but-unviewed)
accrues like `idle`, and a pane slept while `done` shows
`claude · sleeping · done`. Clocks persist in `idle.json` (wall clock), keyed
pane + terminal id + `state_change_seq` + session, so a watcher restart
keeps them; a Herdr restart, a damaged file, or any mismatch starts the
clock again (a later sleep, never an earlier one). Resets reach the file
before the tick sleeps anything. `scan` honours the same clocks (an
unclocked pane's stretch starts now), so it no longer sleeps everything at
once.
It also honours `dry_run`; `scan --dry-run` previews without it.

Supported kinds: **claude** (original argv replayed on wake; a session
started without flags resumes with none, and `wake --without-flags` resumes
one whose flags are unknown) and
**opencode** (`-s <session>`). Codex is deliberately absent — see the
comment on `PROFILES` in the script.

### Restarts and reboots

Herdr saves a pane's label and a resume command a reporter gave it, not its
processes and not the reporter's claim. So the claim on a slept pane carries
the stub's own argv as the pane's resume command
(`pane report-agent … -- env HERDR_SESSION=… /abs/path/herdr-sleeper stub
<pane> <session> <state dir>`), and only when the stub was actually typed.
After a server restart (`herdr update`, a crash, a reboot) Herdr types that
command into the restored shell, so the pane comes back with its 💤 label
and its stub, and Enter, focus and `wake` work as before. The next watcher
tick sees label + new terminal id + our stub for this pane and session,
claims the pane again (sidebar row, resume command for the next restart) and
records the new terminal id in the same step (event `reconciled`, outcome
`reclaimed-after-restart`). A restarted pane without a stub is left alone:
focus and `wake` start the agent into the bare shell as before. Herdr drops
the command when the pane is released, which every wake does, or when
another agent takes the pane. A command Herdr would refuse (an apostrophe in
a path, a control character, over 64 arguments or 8 KiB; checked locally
first, mirroring `validate_resume_argv`) or a refusal from Herdr
(`resume_not_accepted`, `invalid_resume_argv`, an older Herdr) falls back to
the plain claim and logs `resume-not-reported`; such a pane comes back as a
shell that wakes on focus. A lab against a real, headless Herdr 0.9.5 server
(no client attached) confirmed all of this: the stub comes back after a
restart, the next tick re-claims the pane with its new terminal id, and Enter
(before or after that tick), focus and `wake` resume the same session, after
which Herdr's saved plan for the pane is Claude's own again (LESSONS.md, 10.9).

A slept pane whose 💤 label was removed before a restart is orphaned (by hand
only), even though Herdr types the stub back in; reconcile then claims and
releases the pane, which drops the saved command, so later restarts type
nothing there. A pane slept without a stub because its shell did not come back
after the exit keeps Herdr's own Claude session: a restart resumes it awake,
and reconcile drops the row and the 💤 label once it sees the session running
twice. A late exit (the agent left after the exit wait) is labelled and claimed
when reconcile settles it, so a restart does not orphan it.

Sessions that were awake are Herdr's: it restores them natively as a bare
`claude --resume <session>`, without the flags they were started with. The
crash-recovery snapshot keeps the flags of a row whose session comes back
that way, and a later sleep of that session replays them on wake.

After a reboot the watcher may start before the Herdr server. A tick that
finds no server (`server_not_running`, `server_unavailable` while it shuts
down, or a refused or missing socket) is
retried every poll interval instead of backing off, and logged once per
outage. State files are written with an fsync of the file and then of the
directory, so a power cut does not leave an empty journal.

With `watcher = "external"`, hooks never start a watcher, so nothing would
notice a service that did not come up. A missing one (no `stop`, and nothing
holds `watcher.lock`) is therefore reported: a WARN and a `watcher-missing`
event at server start and, at most once an hour, from any plugin activity; a
`WARNING:` header in `list` and `presence` (on stderr with `--json`); and a
short note in the ListAgents hook's context.

## Use

```bash
herdr plugin action invoke djbclark.herdr-sleeper.list
herdr plugin action invoke djbclark.herdr-sleeper.sleep-pane   # context pane
herdr plugin action invoke djbclark.herdr-sleeper.wake         # context pane
herdr plugin action invoke djbclark.herdr-sleeper.wake-all
herdr plugin action invoke djbclark.herdr-sleeper.log
```

Config: `<plugin config dir>/config.toml` — Herdr's
`~/.config/herdr/plugins/config/djbclark.herdr-sleeper/config.toml` (run
`herdr plugin list` to see it) — `idle` (duration), `agents` (list),
`exclude` (panes or names: the Herdr agent name, and for claude also the
Claude session name and the pane title, which survive a restart), `poll_seconds`, `notify`, `wake_on_focus`,
`max_live` (more live sessions than this: the longest-idle ones sleep
early, 0 = no cap), `pressure_idle` (the shorter window for that early
sleep, default `30m`, never later than `idle`; under macOS memory pressure
warn/critical one session per tick sleeps early too; a candidate that
refuses, a draft or a background shell, is passed over for the next
longest-idle one), `restart_outdated` (an idle claude session whose process runs another
build than the installed `claude` resolves to, per `lsof`'s txt entry, is
slept and woken at once onto the new build, one per tick, after every check a
sleep needs and at least `pressure_idle` of quiet; keep-listed sessions too,
since a restart is not a sleep; event `restarted` with both builds, then
`restart-failed` if the woken session still runs the old build or the wake
failed. One try per session every 6 h, so a restart that does not take never
loops. An npm/bun install shows only its runtime to `lsof`, so it is never
restarted),
`wake_refuse_from` (see Messages below), `dry_run` (the watcher and `scan` log `would sleep (dry run)` once per idle stretch
instead of sleeping), `watcher` (`"plugin"`, the default: hooks spawn and
heal the watcher; `"external"`: a service manager runs
`herdr-sleeper watch` and hooks never spawn one, but report it when it is
missing);
overridable by `HERDR_SLEEPER_*` env (booleans take true/false/1/0), lowest
to highest precedence. State: `<plugin state dir>/` per Herdr session —
`sleeping.json` journal, `panes.json` crash-recovery snapshot, `idle.json`
idle clocks, `events.jsonl`, `sleeper.log`, `watcher.pid`/`watcher.lock`.
The directory is 0700 and every file 0600 (files from earlier versions are
tightened when next written); the two logs rotate to `<name>.1` at 1 MB.
`panes.json` keeps only the argv a wake would replay, never the raw command
line, and log/event reasons name options and draft locations, never values
or draft text.
A legacy standalone-script journal (`~/.local/state/herdr-sleeper/`) is
adopted once, on the default session's first startup. An older config —
v0.1.0's manual-run location
`~/.config/herdr/plugins/djbclark.herdr-sleeper/config.toml` or the
standalone `~/.config/herdr-sleeper/config.toml` — is translated once when
the plugin config is missing (`interval_minutes` dropped); when the plugin
config already exists it is never rewritten, and startup logs that the
older file is not read. An older config that cannot be translated keeps
sleeping off, and so does a plugin config with no `exclude` while an older
file still has one (merge it, or set `exclude = []`).

Keybindings work like any plugin action:

```toml
[[keys.command]]
key = "prefix+z"
type = "plugin_action"
command = "djbclark.herdr-sleeper.sleep-pane"
description = "sleep this agent pane"
```

## Messages between agents

A slept agent has no process, so `herdr agent prompt` and Claude Code's
`SendMessage` cannot reach it. `deliver` can:

```sh
echo "rebase onto main, then ping me" | herdr-sleeper deliver ts-api --from orchestrator
```

The target is a pane id, agent name, wake name, or session id. The message
(stdin, up to 64 KB; no control characters but newline and tab, since a CR
would submit early and an ESC would drive the TUI) goes into a 0600 file in
`mailbox/<session>/` first.
A live, idle agent gets it typed in at once. A sleeping one is woken through
the usual wake path and gets it as soon as it is idle again. A busy or
blocked agent (a permission prompt is never typed into) gets it from the
watcher once it is idle. The file is removed only after `agent prompt`
succeeded (at least once; `--id` deduplicates retries, and a retried id
that is still queued tries the wake and the typing again). Each message is
typed only after a fresh look: the pane runs that session, idle or done, no
sleep transaction holds it, and the composer is empty (`agent prompt` into a
draft would send the draft along). A sleep and a delivery to the same session
exclude each other through the session's mailbox lock. A session with pending
mail is never put to sleep. The agent sees

```
[relayed from orchestrator via herdr-sleeper, msg 3f9a…; not from the owner]

rebase onto main, then ping me
```

so an agent running with bypassed permissions does not take it for its
owner's instructions. `events.jsonl` records each delivery (id, sender,
target, size) but never the text. `wake_refuse_from` lists senders whose
messages wait for the target's next wake instead of causing one (agents
that read untrusted content). Exit codes: 0 delivered, 3 queued, 1 refused,
2 bad input. Any local process running as the same user can send. The
sender name is self-declared, so the audit trail is only as good as that.
`herdr agent prompt` takes the text as an argument, so while it runs the
message is visible in the process list to processes of the same user; the
plugin itself never logs it, not even inside a Herdr error.

`presence [--json]` lists every live agent and every sleeper (state, Herdr
name, Claude session name, pane, kind, cwd, session id, slept since), with
no argv, for agents looking for a peer.

**Claude Code hooks.** `claude-hook` is a hook command for two built-in
tools. Register it in `~/.claude/settings.json` as a `PostToolUse` hook with
matcher `ListAgents` and a `PreToolUse` hook with matcher `SendMessage`:

- after `ListAgents`, it adds the sleeping sessions (by their Claude session
  names, which the plugin reads from each transcript's `agent-name`
  records) as extra context;
- before `SendMessage` to one of those names (the Claude session name only;
  a live session carrying the same name keeps it), it queues the message, starts
  a detached `deliver-pending` that wakes the session and types the message
  in, and denies the tool call with a reason saying so. The sender
  therefore does not resend, and the reply comes back to the sender's
  session name through the normal cross-session path.

Live peers are left to `SendMessage`. A message that `deliver` would reject
(control characters, over 64 KB) is denied with that reason. Any error lets
the tool run as if the plugin were absent. The hook never waits for the
global lock, which a sleep transaction may hold longer than a hook may run.

## Safety rules (all fail closed)

Inherited verbatim from the standalone script; the deltas the plugin
introduces are marked.

1. Eligible only when: a supported kind with a session id (and, for claude,
   an on-disk transcript — nothing to resume otherwise); Herdr status
   `idle`/`done`; pane not focused; not excluded; session id not open in
   another pane; idle ≥ window; original argv safely replayable
   (`--fork-session`, `--print`, `--session-id`, positional prompts,
   anything after `--`, options of unknown arity → refuse).
2. Everything is re-checked after the journal write and immediately before
   the exit, including that `state_change_seq` and the session id have not
   moved; the exit targets the pane id, never the name; "exited" needs
   Herdr to drop the agent *and* process-info to show no agent process —
   unreadable process-info is unknown, not gone. *Delta:* the exit is a
   SIGTERM to the pane's identified agent process (refused when the pid
   cannot be identified) — a signal cannot append to a composer draft the
   way a typed `/exit` could. It would discard the draft, though, so for
   claude the last look also requires: an empty composer (Claude's dim prompt
   suggestion is not a draft; text on the prompt line or above the box's
   lower rule is), no background work in the footer (`N shells`,
   `N monitors`, running subagents — patterns from
   [netresearch/herdr-bg-activity](https://github.com/netresearch/herdr-bg-activity)),
   no shell process below the agent (a background Bash task), and no pending
   `ScheduleWakeup` in the transcript tail, and no session cron job anywhere
   in the transcript (the watcher reads it incrementally). The screen is
   read with `--format ansi` for that. The positive-empty-composer check
   still guards kinds exited by typing.
3. The record is written *before* the exit. If the agent is still there
   after the wait it stays `exit-requested`; the next tick reconciles
   (dropped only after two *consecutive* sightings with a real process, or
   when the pane runs a different session; `asleep` once verifiably gone;
   `displaced` when our own wake put another session there — by hand
   only). A record is never deleted because its pane vanished; the manual
   resume command is printed instead. A pane that moved (new pane id, same
   `terminal_id`) is followed.
4. *v0.1.1:* pane identity. Herdr re-allocates every terminal id when it
   restores a session and re-applies the saved manual label, so a changed
   `terminal_id` on a pane that still carries our 💤 label is a restart,
   not reuse: same pane id + 💤 label + a bare shell wakes, and the new
   terminal id is recorded only with a wake that succeeded. cwd is not
   identity evidence (workspace ids restart at max(restored)+1, so a fresh
   pane can get a recycled pane id in the same cwd — but never our label);
   a restored pane `cd`'d elsewhere is refused with "cd back". A new
   terminal without the label, or one running another session, is a reused
   pane id: the record is re-keyed `orphan:<first 8 of uuid>` with phase
   `orphaned` (one `orphaned` event), never woken automatically (focus,
   `wake-all` and `wake <orphan key or name>` refuse it, by key), and
   `list` prints its manual resume line. A legacy record with no terminal
   id needs the label as well. A record is dropped only when the *same*
   terminal now runs a different session (a deliberate replacement). The
   crash-recovery snapshot keeps an orphan copy the same way. *v0.1.2:* a
   restarted pane that runs our stub for this pane and session (brought back
   by the reported resume command) is re-claimed, and only then is its new
   terminal id recorded; a row orphaned after a restart has its saved resume
   command dropped (claim, then release), so no later restart types a stub
   for it.
5. Wake refuses if the session id is live in any pane or any process that
   selects it (our own stub is recognised by its argv structure, never by
   substring), when that cannot be verified, when an `exit-requested`
   agent is still there (reconcile's two-sighting rule decides, not one
   focus), or when the same terminal moved to another cwd. *Delta:*
   focus-wakes are debounced by a per-pane lock (one click fires several
   `pane.focused` hooks) and give up after 10 s on a busy global lock (the
   next focus retries; a pane running another session is skipped silently);
   the in-pane stub resolves the agent binary and checks the transcript
   first, runs the replay filter and the liveness check again under the
   lock, and then marks the record `waking` with its pid rather than popping
   it (exec keeps the pid). Every other wake path refuses a `waking` record
   whose pid is alive; the watcher finishes it once the session runs, or
   puts it back to `asleep` (and re-claims the row) if the pid died; ctrl-c
   or a failed exec puts it back at once. An empty process list from Herdr
   is "unknown", never "no stub". A taken agent name is retried once as
   `wake-<pane>`; a start that answers before the session id is known is
   confirmed from `pane get`, never logged as a mismatch. `sleep-pane`
   refuses a pane whose record is kept for another session.
6. A non-object state file, malformed config, unknown keys, booleans where
   numbers go, conflicting env aliases, or a `nan`/negative/`inf` window
   disables sleeping (logged every tick) — `wake`/`list`/`log` keep
   working, and damaged state files are quarantined to
   `*.damaged-<timestamp>`, never overwritten. A file lock serialises
   overlapping runs; the journal is re-read under it before every write.
   On Python < 3.11 the config is read by a strict TOML subset parser that
   refuses (rather than misreads) anything tomllib would read differently.
7. *v0.1.1:* the watcher never gives up. Any exception costs one tick and
   backs off (poll × 2ⁿ, at most 10 min); one watcher per state dir, held
   by `watcher.lock`; SIGTERM (code change) finishes the transaction in hand
   first; `list`, `scan`, `sleep-pane` and every focus hook respawn a
   missing watcher, or replace a pre-v0.1.1 one that holds no lock (without
   inheriting the caller's `HERDR_SLEEPER_*` overrides). A recorded watcher
   pid is trusted only when its argv is `… herdr-sleeper watch` and, for a
   v0.1.1 record, it holds `watcher.lock`. One failing pane costs that pane,
   not the tick. *v0.1.2:* no Herdr server is retried at the poll interval,
   not backed off; a missing external watcher is reported (see *Restarts and
   reboots*).

Known limits: SIGTERM leaves a small window between the final recheck and
the process handling it (only a native Herdr operation could close it);
the stub line is POSIX shell (`printf`, `env`), so nushell/pwsh panes get
focus/command wakes only.

## Tests

`python3 -m pytest tests/ -q` — 269 tests: the decision logic, the
sleep/wake state machine, pane identity and orphans, the watcher tick and
loop (idle clocks, backoff, server-down retries, the watcher lock), the
focus hook, the stub, the resume command and the re-claim after a restart
(the fake `herdr` has a `restart()`), the 3.9 TOML subset parser, and legacy
migration, against a fake `herdr`, and the CLI's real output shapes (the
"ok" calls answer success with nothing). v0.1.2 was run on Python 3.9 (Apple's
`/usr/bin/python3`) and 3.12. The fake does not prove what a real Herdr
restores; the lab in LESSONS.md, 10.9 does.

## Credits

- [`dalogax/herdr-agent-hibernate`](https://github.com/dalogax/herdr-agent-hibernate) —
  the SIGTERM exit path, the seq-based idle clock, wake-on-focus with a
  per-pane lock, `terminal_id` identity, code-stamped watcher replacement,
  `agent_name_taken` handling ideas.
- `prabhatgmp/herdr-park` — the sidebar claim (`pane report-agent`), the
  in-pane Enter stub, `notification show`, freed-MB reporting.
- [`scripts/herdr-sleeper`](https://github.com/djbclark/herdr/tree/herdr-sleeper/scripts/herdr-sleeper)
  (the standalone ancestor) — everything fail-closed.
