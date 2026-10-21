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
parsing falls back to a built-in TOML subset below Python 3.11).

For local development:

```bash
git clone https://github.com/djbclark/herdr-sleeper
herdr plugin link <checkout>
```

## What it does

After an agent has been idle for a user-chosen window (`idle = "12h"` —
default 12 h), it exits the agent process (SIGTERM for claude and opencode:
both shut down cleanly), keeps the pane, reports a `sleeper` placeholder so the sidebar keeps its
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

Supported kinds: **claude** (original argv replayed on wake) and
**opencode** (`-s <session>`). Codex is deliberately absent — see the
comment on `PROFILES` in the script.

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
`exclude` (panes or names), `poll_seconds`, `notify`, `wake_on_focus`,
`dry_run` (the watcher logs `would sleep (dry run)` once per idle stretch
instead of sleeping), `watcher` (`"plugin"`, the default: hooks spawn and
heal the watcher; `"external"`: a service manager runs
`herdr-sleeper watch` and hooks never spawn one);
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
   `ScheduleWakeup` or session cron job in the transcript tail. The screen is
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
   crash-recovery snapshot keeps an orphan copy the same way.
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
   not the tick.

Known limits: SIGTERM leaves a small window between the final recheck and
the process handling it (only a native Herdr operation could close it);
the stub line is POSIX shell (`printf`, `env`), so nushell/pwsh panes get
focus/command wakes only.

## Tests

`python3 -m pytest tests/ -q` — 186 tests: the decision logic, the
sleep/wake state machine, pane identity and orphans, the watcher tick and
loop (idle clocks, backoff, the watcher lock), the focus hook, the stub,
the 3.9 TOML subset parser, and legacy migration, against a fake `herdr`.
Verified on Python 3.9 (Apple's `/usr/bin/python3`) and 3.14.

## Credits

- [`dalogax/herdr-agent-hibernate`](https://github.com/dalogax/herdr-agent-hibernate) —
  the SIGTERM exit path, the seq-based idle clock, wake-on-focus with a
  per-pane lock, `terminal_id` identity, code-stamped watcher replacement,
  `agent_name_taken` handling ideas.
- `prabhatgmp/herdr-park` — the sidebar claim (`pane report-agent`), the
  in-pane Enter stub, `notification show`, freed-MB reporting.
- [`scripts/herdr-sleeper`](https://github.com/djbclark/herdr/tree/herdr-sleeper/scripts/herdr-sleeper)
  (the standalone ancestor) — everything fail-closed.
