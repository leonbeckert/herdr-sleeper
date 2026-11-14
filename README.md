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

   *v0.1.4:* Herdr shows the resumed session id on the pane a moment before it
   lists the agent, and a rename in between fails with `agent_not_found`. A
   wake outside the lock therefore retries the rename for up to 15 s; under the
   lock (the watcher) the name is recorded as owed in `names.json`, and the
   watcher hands it over on a later tick once Herdr lists the agent (event
   `renamed`). An owed name is dropped when the pane runs another session, or
   after an hour with a WARN naming the `herdr agent rename` to run by hand.
3. **Command** — `herdr plugin action invoke djbclark.herdr-sleeper.wake`
   wakes the context pane (only that pane), or `wake-all` / `list` / `log`
   from anywhere. When the pane's stub does the wake, `wake` waits up to
   30 s for the session to come back before reporting success (a stub that
   exec'd but whose session Herdr has not reported yet counts as started).

An agent Herdr lists without a name (a pane restarted outside Herdr keeps
its session's title but not its agent name) sleeps under that title when it
is a valid name no other live pane or sleeping session goes by (*v0.1.18*);
the wake hands it to the resumed agent, so a `deliver` by the visible name
reaches it.

The stub resets the pane's line discipline before it waits for Enter (*v0.1.19*): an agent killed
by a signal can leave the tty without CR-to-NL and signal keys, so Enter echoed `^M` and ctrl-c `^C`
and the pane could not be woken by hand.

Mail also goes into a Claude pane that Herdr reports as `working` only because background agents,
shells or workflows still run while its prompt waits (*v0.1.20*): the line over the composer reads
`✻ Waiting for … to finish` and the composer is empty. A running turn, a draft or an open menu still
hold it. The background-task panel under the composer (`⏺ main`, one row per task) counts as footer.
A colour code the terminal mis-parsed can leave its tail (`;2;136;136;136m`) in front of the rule under
the composer and make that rule wrap onto a second line; both still count as the rule (*v0.1.28*).

A displaced sleep row answers to its session id or journal key only (*v0.1.22*): its pane, and the
pane's wake name with it, went to another session, so `deliver wake-w3-p3` reaches the newcomer or
refuses, never the dead handle's mailbox. `close <session id>` drops a displaced row whose pane is
gone and sets its queued mail aside in the mailbox's `retired-<time>/` directory.

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
another agent takes the pane. Herdr's saved command cannot be read back, so
the journal row records whether Herdr accepted ours (`resume_reported`). A
pane whose stub runs in the terminal it was typed into but whose row has no
such record (slept by v0.1.1), or whose claim a refused wake released, is
claimed again with the stub's argv on the next tick (outcome
`resume-reported`), so it survives a restart too. A refusal is recorded and
not retried every tick. A command Herdr would refuse (an apostrophe in
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
that way in a new terminal (Herdr re-allocates terminal ids on restore), and
a later sleep of that session replays them on wake. The same bare form in the
terminal that already ran the session, or from another process there later,
is a resume by hand without flags, and the snapshot records it as such.

After a reboot the watcher may start before the Herdr server. A tick that
finds no server (`server_not_running`, `server_unavailable` while it shuts
down, or a refused or missing socket) is
retried every poll interval instead of backing off, and logged once per
outage. State files are written to a temp file that is synced to the drive
before it replaces the old one, and the directory after; on macOS the sync is
`F_FULLFSYNC`, because `fsync` there leaves the data in the drive's write cache
(plain `fsync` elsewhere, or where `F_FULLFSYNC` is refused). That is meant to
keep a power cut from leaving an empty journal; it has not been tested with a
real power cut.

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
restarted. With claude-account's launcher (see "Subscription slots") a session
on another slot than the active one is restarted the same way, already
2 minutes after it went idle; event `restarted` with `kind` `account`),
`wake_refuse_from` (see Messages below), `answer_dialogs` and `limit_monitor` (see "Subscription slots"),
`auto_resume`, `auto_resume_max_age` and `auto_resume_exclude` (see "Auto-resume after a network or API error"), `dry_run` (the watcher and `scan` log `would sleep (dry run)` once per idle stretch
instead of sleeping), `watcher` (`"plugin"`, the default: hooks spawn and
heal the watcher; `"external"`: a service manager runs
`herdr-sleeper watch` and hooks never spawn one, but report it when it is
missing);
overridable by `HERDR_SLEEPER_*` env (booleans take true/false/1/0), lowest
to highest precedence. State: `<plugin state dir>/` per Herdr session —
`sleeping.json` journal, `panes.json` crash-recovery snapshot, `idle.json`
idle clocks, `events.jsonl`, `sleeper.log`, `watcher.pid`/`watcher.lock`,
`insomnia.json`, `spawned.json`, `names.json` (owed names), `limited.json` (sessions and slots at their usage limit).
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

The target is a pane id, agent name, wake name, or session id; a live
claude whose Herdr name is gone (Herdr can drop it and keep only the title)
is also found by its Claude session name, as long as exactly one session
carries it (*v0.1.8*). The message
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
[relayed from mini2/orchestrator via herdr-sleeper, msg 3f9a…; not from the owner]

rebase onto main, then ping me
```

so an agent running with bypassed permissions does not take it for its
owner's instructions. `events.jsonl` records each delivery (id, sender,
target, size) but never the text. `wake_refuse_from` lists senders whose
messages wait for the target's next wake instead of causing one (agents
that read untrusted content). Exit codes: 0 delivered (the caller's own
message was typed in), 3 queued, 1 refused, 2 bad input. *v0.1.8:* the
queued line names the real reason (`status working`, `status blocked`,
`composer draft: text on the prompt line (a draft or an open question menu),
N chars`, `no live agent runs the session`, …), never a bare "not idle"; and
when older queued mail went in first but the agent went busy before this
message, the answer is 3 (`typed K older message(s) first; <id> waits (…)`),
not 0. Any local process running as the same user can send.

**Released within seconds** (*v0.1.10*). While any mail waits, the watcher
retries it every 5 s between its ticks (only mail; a directory listing when
nothing waits), so a cleared draft or an agent going idle lets it in within
seconds, and the reason `presence` shows is at most that old. Everything
queued for a session goes in as one prompt (up to 32 KB): one prompt per
message let only one through per tick, because the agent turns `working`
as soon as the first lands.

**Why mail waits.** A hold is correct whenever the sleeper cannot prove the
prompt is free: Herdr's status is not idle/done, or the composer is not
empty (a draft and an open question menu look alike and both hold). It is
never silent (*v0.1.8*): the reason is kept in `mailbox/<session>/.held`
(0600; reason, since, last checked; a dot file, never mail) by `deliver` and
on every watcher tick, and removed once the mailbox is empty. Once the oldest
queued message has waited 10 minutes the watcher logs a `mail-held` event
(target, pane, session, queued count, oldest age, reason; again when the
reason changes or after 30 minutes). For a `status …` hold it adds
`herdr_rule`, the detection rule Herdr's status comes from (`herdr agent
explain --json`, read-only; only the rule's id, state, region and priority
are kept, never the screen preview). Bodies and draft text never appear in
any of these. The watcher's mail flush runs even when the sleep part of a
tick failed, and a malformed mailbox file is moved aside to `*.damaged` like
an unreadable one, so it cannot hold up other mailboxes. The
sender name is self-declared, so the audit trail is only as good as that.
`herdr agent prompt` takes the text as an argument, so while it runs the
message is visible in the process list to processes of the same user; the
plugin itself never logs it, not even inside a Herdr error.

`presence [--json]` lists every live agent and every sleeper (state, Herdr
name, Claude session name, pane, kind, cwd, session id, slept since), with
no argv, for agents looking for a peer. Every row with a session also
carries its mail (*v0.1.8*): `mail_queued`, `mail_oldest_s` and `mail_held`
in `--json` (0/null without mail; a row from an older machine has none,
which reads as unknown), and `[3 queued 122m: <reason>]` in the text listing
and in the other-machine lines `ListAgents` gets.

### Replies and threads (*v0.1.25*)

A relayed message names its message id, and a reply names the message it
answers, so a reader who sees only the reply can still reach the question:

```sh
echo "yes, merge it" | herdr-sleeper deliver --re 3f9a…        # no target: back to 3f9a's sender
herdr-sleeper show 3f9a…      # the message, what it answers, and its replies
herdr-sleeper thread 3f9a…    # who asked whom: every message and ask of the thread, oldest first
```

- `--re ID` takes a message id or an ask id. It shows in the header
  (`[relayed from mini2/orc via herdr-sleeper, msg 7b2c…, re 3f9a…; not from
  the owner]`), and the message joins ID's thread (a message without `--re`
  starts its own).
- **No target:** `deliver --re ID` goes back to whoever sent ID to the
  calling session, by that sender's session id when it is known (it survives
  renames and sleep; from another machine as `<machine>/<session id>`). It
  refuses a message that was not sent to the caller, and an owner's answer
  (answer the owner in your own session).
- **Archive:** every message queued here is also kept in
  `messages/<id>.json` (0600, in the same 0700 state dir whose mailbox held
  the body already) for 14 days, then dropped; a message sent to another
  machine keeps a sent copy here. `show` and `thread` read it. `show` looks
  on the other saved machines when this one lacks the id, and `thread` asks
  every saved machine, so a conversation that crossed machines reads as one.
- **Asks:** `ask --re ID` ties an owner question to the relayed message it
  is about. The ask tab then shows that message's first lines, so the owner
  answers with the original ask in front of them, and the owner's answer
  (msg `ask-<ask id>`, `re` the ask) joins the same thread.

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

## Insomnia

An agent that must stay up (it holds an SSH master or a tunnel, waits on a
deploy, or serves other agents) claims **insomnia** for its own session and
releases it when it no longer needs it:

```sh
herdr-sleeper insomnia claim --reason "ssh master to db-1" [--for 4h]
herdr-sleeper insomnia release
herdr-sleeper insomnia list [--json]
```

A claim is one more eligibility check (rule 1 below): the session is never
slept on idle, over `max_live`, or under memory pressure, and `sleep-pane`
refuses it too. `restart_outdated` still restarts it onto a new build,
because a restart is not a sleep. Claims live in `insomnia.json` in the
state dir, keyed by the Claude session id, so they survive a Herdr restart
(new terminal ids, lost agent names) and a reboot.

- `claim` acts only on the calling agent's own session: the pane comes from
  `HERDR_PANE_ID` and counts only when that pane's agent process is an
  ancestor of the command (a variable inherited from another pane or session
  claims nothing). Run it from the agent's own shell tool. Claiming again
  renews the claim with the new reason and `--for`. A claim follows its pane
  and terminal: after `/clear` (a new session id in the same process) it
  still holds, and the watcher re-keys it (`insomnia-carried`).
- `--for` claims expire; the watcher drops an expired claim with an
  `insomnia-expired` event. A claim on a session idle for more than 24 h, or
  on a session that no longer runs, logs `insomnia-stale` once a day and is
  kept: claims are never dropped silently.
- `release --pane TARGET --force` (pane id, agent or session name, or session
  id) frees a forgotten claim of another session; it is logged with who did
  it. Without `--force`, `release` only drops the caller's own claim.
- A claim shows in `presence` (`[insomnia: …]`, `insomnia` in `--json`) and
  in the pane's sidebar row as the token `insomnia=<reason>` (metadata source
  `herdr-sleeper-insomnia`; the watcher reports it again after a restart).
- `insomnia.json` is never quarantined: moving it aside would drop every
  claim. A file that cannot be read, or holds anything malformed, keeps
  *every* session awake (logged every tick, a `WARNING:` in `presence`) until
  it is fixed by hand; `claim` and `release` refuse to write over it.
- A `restart_outdated` restart whose wake fails leaves a claimed session
  asleep, as it does a keep-listed one (`restart-failed`, notified); the next
  message or focus wakes it.

## Spawn and close

An orchestrator starts named sessions and closes them again:

```sh
echo "build B-17 in this worktree" | herdr-sleeper spawn --cwd ~/development/app-b17 \
    --name app-b17 [--kind claude|codex] [--from orc] [--workspace w3] [--json] \
    [-- --dangerously-skip-permissions --model opus]
herdr-sleeper close app-b17 [--force]
herdr-sleeper close --self [--reason "B-17 done"]   # from inside the session: close me once this turn ends
herdr-sleeper close --self --cancel
```

`spawn` refuses a working directory that is not inside a git checkout below
`~/development` (realpath), a name that is taken (a live agent or a sleeper),
and claude flags that would not survive a sleep: everything after `--` goes
through the same replay filter as a wake, and `-n`/`--name` or a session
selector (`--resume`, `--continue`) is refused, since the name comes from
`--name` and a spawn starts a new session. It then:

1. creates a tab with `--no-focus`, labelled with the name, in the caller's
   workspace (or `--workspace`);
2. waits for the shell and runs `herdr agent start <name> --kind claude …
   -- -n <name> <flags>`, so the session can sleep and is found by name after
   a Herdr restart;
3. hands the first prompt (stdin, the same rules as `deliver`) to the
   mailbox and types it in once the agent is idle, inside the relay envelope
   (`[relayed from <sender> via herdr-sleeper …; not from the owner]`);
4. records who spawned it in `spawned.json` (shown as `spawned_by` in
   `presence --json`; the watcher drops a record an hour after its session is
   gone) and logs `spawned`.

An agent that does not get ready (`agent_not_ready`: a folder-trust or login
prompt; spawn never answers one) has its tab closed again, since nothing was
typed into it, and logs `spawn-failed`. Exit codes: 0 started and prompted,
3 started with the prompt queued, 1 refused or failed, 2 bad input.

`close` takes a spawn name, pane id, agent name, wake name or session id.
Under the global lock and the session's mailbox lock, from fresh state, it
refuses a session that is busy (not idle or done), has a draft in the
composer, runs background work (the same checks as a sleep), has undelivered
mail, or holds insomnia, unless `--force` (logged, with what it overrode;
`--force` also drops the claim). It never closes the calling session. A live
claude gets a SIGTERM first and up to 25 s to exit, then its pane closes (its
tab, when the pane is all the tab holds). A sleeping session is closed only
when its pane is provably still its own (same terminal, or restarted with our
💤 label) and runs our stub or a bare shell (after ctrl-c in the stub the pane
may hold someone's work); its journal row is dropped and its claim released.
An orphaned, displaced or waking row is never closed, not even with
`--force`. Either way the transcript stays on disk, and `close` prints
the line that resumes it by hand.

### Closing yourself (`close --self`, *v0.1.13*)

A session that is done closes itself: it runs `herdr-sleeper close --self
[--reason TEXT]` from its own shell tool as the last step of its final turn.
`close NAME` refuses the calling session, because the SIGTERM would end the
very turn that asked; `--self` only records the wish and prints one line
(`<name> closes after this turn ends; a message delivered before then
cancels it`). It is always local: no `--machine`, no target, no `--force`.

- The caller is identified like an insomnia claim (`HERDR_PANE_ID`, and that
  pane's agent process must be an ancestor of the command); outside an agent
  pane, without a session id, or for a kind whose composer cannot be checked
  for a draft (anything but claude), it refuses (exit 1). Running it again
  keeps the wish and its start (`--reason` replaces the reason).
- The wish lives in `closing.json` in the state dir, keyed by session id
  (`at`, `name`, `pane`, `reason`, `by`), and logs `self-close-requested`.
- Each watcher tick, after the mail flush, closes a session with a pending
  wish once it is idle or done, through the same code as `close` (every
  refusal and the asleep-pane checks unchanged, never `--force`, `closed`
  logged with `by: "self"`, `via: "close --self"`). A session that fell
  asleep meanwhile is closed through the sleeping path. A refused close stays
  pending and is retried next tick; a busy turn or a draft is retried
  silently, any other refusal (insomnia, undelivered mail, background work, a
  sleep row in flux) logs `self-close-held` once. Holding insomnia therefore
  keeps the session open until it is released.
- Anything typed into the session before then cancels the wish: `deliver`,
  a peer's SendMessage, or mail the watcher flushes. The typed prompt gets one
  more line, `[herdr-sleeper: your pending close --self was cancelled by this
  message; run it again when you are done]`, and `self-close-cancelled` is
  logged (`via: "deliver"`). The watcher re-checks the wish under the
  session's mailbox lock, so a message typed in a moment before the close,
  while Herdr still shows the turn as idle, wins. `close --self --cancel`
  drops the wish by hand (`via: "cancel"`).
- A wish older than 24 h is dropped with `self-close-expired`; the session
  stays open. `presence` shows a pending wish as `[closing: <reason>]`
  (`closing` in `--json`: the reason, `"pending"` without one, else null).
  `scan` does not act on wishes; only the watcher does.

## Other machines

Herdr's saved SSH machines (`herdr machine list`) extend `deliver`, `wake`,
`close`, `presence`, `insomnia list` and `spawn` to another machine:

```sh
echo "review MR !4" | herdr-sleeper deliver dev2/impl-1 --from orc   # or: deliver impl-1 --machine dev2
herdr-sleeper wake dev2/impl-1
herdr-sleeper presence --all                # this machine and every enabled saved one, as machine/name
herdr-sleeper presence --machine dev2 [--json]
herdr-sleeper insomnia list --machine dev2
echo "build B-17" | herdr-sleeper spawn --machine dev2 --cwd ~/development/app --name impl-1 [-- FLAGS]
herdr-sleeper close dev2/impl-1 [--force]
```

The command runs as `herdr-sleeper …` on that machine over SSH, as the same
account, so the remote checks (cwd, flags, busy, draft, insomnia) apply to
the remote state, and nothing about the remote session is decided here.
Details:
- **Where:** only machines in Herdr's catalog
  (`$XDG_STATE_HOME/herdr/client/endpoints.json`, read, never written) are
  used. Each must be enabled and have a plain label (`[a-z0-9-]`) and an
  alias or `user@host` target. A catalog that can't be read refuses the
  command; a target is never guessed.
- **`LABEL/NAME` is remote** only when LABEL is a saved machine; this
  machine's own label means the local session (so the `dev1/name` that
  `presence --all` prints works on dev1 too). A target without `/` never
  reads the catalog, and an unreadable catalog keeps `LABEL/NAME` local. A
  local name that starts with a saved label and `/` needs its pane id.
  The receiving end gets the target after `--` with an internal `--here`,
  so it never routes again.
- **Renamed machines:** `machine_aliases = ["orch=mini2", "impl=mini1"]`
  in the config keeps former labels working. `orch/NAME` and
  `--machine orch` then mean `mini2`, both for a saved machine and for this
  machine's own former label, so addresses written down before a rename
  still reach the session. Each entry is `"old=new"` with two different
  plain labels; anything else is a config error.
- **SSH:** `ssh -T -a -x -o BatchMode=yes -o ClearAllForwardings=yes <target>`
  (no prompt an agent could not answer, no agent, X11 or port forwarding),
  with every argument quoted for the remote shell. Arguments with quotes,
  backslashes, `$`, backticks or control characters are refused here, so the
  quoting holds in any remote shell, and a target must match
  `[A-Za-z0-9_][A-Za-z0-9_.:@-]*` (never an option). Exit 127 there means
  `herdr-sleeper` is not on that account's PATH. The alias's key and
  pinned host key come from `~/.ssh/config`. Each call is its own
  connection, not Herdr's shared one, which lives in a directory scoped to
  Herdr.
- **Who:** the sender reaches the other side as `<machine>/<sender>`, so
  the envelope says `[relayed from dev1/orc via herdr-sleeper, …; not from
  the owner]` and a reply can find its way back. `machine` in the config
  sets the label (default: the short hostname). It has to match what the
  other machines call this one. A broken config refuses every remote path,
  as it refuses local delivery. `wake_refuse_from` matches the sender as it
  arrives: `dev1/orc`, not `orc`.
- **Retries:** `deliver` fixes the message id before it sends. When the
  connection is lost or the answer doesn't come in time, it prints that the
  outcome is unknown and the `--id` to retry with; the remote mailbox then
  deduplicates instead of typing the message twice. Otherwise the remote exit
  code passes through. `deliver-remote` and `deliver-remote-done` are logged
  here; `deliver-remote-done` carries `reason`, the remote's status line
  (e.g. `queued msg … not ready yet (status working)`), so a `SendMessage`
  relay, whose output goes nowhere, can be followed with `log` (*v0.1.8*).
- **Claude hook:** `ListAgents` adds every saved machine's sessions as
  `<machine>/<name>`. The machines are asked in parallel, 6 s at most for all
  together; an unreachable one is named, and an error there never drops this
  machine's sleepers from the list. `presence --all` asks the same way. `SendMessage` to `<machine>/<name>` is relayed by a detached
  `deliver` and the tool call is denied with a reason that says so.

A message from another machine is a peer's message like any other relayed
one, never the owner's word.

## Subscription slots

ansible-orchestration's `claude-account` (roles/macos_herdr) runs every
claude on one of two subscription slots: `~/.local/bin/claude` is a launcher
that reads `~/.claude-accounts/active`, records the slot in
`~/.claude-accounts/run/<pid>` and execs the build it names on a
`# real-binary: PATH` line. `installed_build` follows that line, so a
launcher never reads as a new build.

```
herdr-sleeper account [--json]              # each live claude session's slot
herdr-sleeper account --restart [--json]    # restart the idle ones on the other slot now
```

- A record counts only when it was written after its process started (else
  the pid was reused); a session without one runs on slot `a`, Claude's
  default login. `explicit` (a hand-set `CLAUDE_CONFIG_DIR`) is never moved.
- `--restart` is the owner's switch (`claude-account switch`): every idle
  session on the other slot is slept and woken at once (`--resume`, the
  launcher picks the new slot), focused panes included; drafts, background
  work and busy panes refuse and are reported `pending`. The watcher moves
  those at their next idle (`restart_outdated`, not in a dry run).

### The usage limit and one-time dialogs (*v0.1.15*)

A session that reaches its slot's usage limit stops at Claude's menu
`1. Stop and wait for limit to reset` / … / `3. Switch to usage credits`
(Herdr: `blocked`), and a session resumed on a slot it never ran on stops at
one-time dialogs. The watcher answers the screens it knows exactly, on every
machine and with `restart_outdated` off too (`answer_dialogs`, default on):

| Screen | Answer |
|---|---|
| the limit menu (its first option line together with the `/usage-credits` hint) | `Enter` on option 1, only from there; never another option (option 3 bills credits) |
| "Make auto mode your default permission mode?" | `No, keep bypass permissions` |
| a feature tip (`Yes, try it` / `Not now`) | `Not now` |
| the Claude in Chrome intro (`docs/en/chrome`, `Enter to confirm`) | `Enter` |

- Only the bottom 20 lines count, and the last line with the `❯` glyph must be
  one of the dialog's options, so the same words quoted in a transcript above
  a composer never match. The cursor moves with `Down` only, the screen is read
  again after each step, and `Enter` goes only onto the target.
- A dialog is answered on a later tick than the one that first saw it (20 s;
  5 minutes when the pane is focused: someone may be looking). Any other
  blocked screen gets no key and one `unknown-dialog` event per state.
- After option 1: the slot is recorded as hit (`limited.json`). When the active
  slot is another one and has room, the session is restarted onto it at once;
  every account restart of a limited session (this one, `restart_outdated`,
  `account --restart`) queues `Your slot hit its usage limit; you were
  restarted on slot <x> (same transcript). Continue the task you were on.`
  A limited session left on its slot gets `Your slot's usage limit has reset`
  once the reset of its fullest window has passed.
- A pane whose journal row is a displaced handle of another session
  (*v0.1.16*) has no room for a second row: its live session is restarted in
  place instead (exit, then `agent start --resume` in the same pane), the
  displaced row untouched. A start that fails after the exit is reported with
  the manual `claude --resume …` command (event `restart-failed`, `in_place`).
- *v0.1.17:* Claude Code draws its cursor in reverse video over the first
  letter of a dim prompt suggestion; that cell counts as part of the
  suggestion, not as a draft, so mail reaches idle sessions that show one.
- The menu's option 2 (`Wait here, then continue automatically at …`) is
  recognised and never sent; only option 1 is, and only from its default
  position.
- Room comes from claude-account's `usage.json` (both windows, a passed reset
  counts 0, 95 % is used up). It lags, so a hit slot has no room until a reset
  after the hit has passed or a newer reading shows room; a slot never seen has
  room. When no slot has room, sessions wait at option 1 and `limit_monitor`
  (e.g. `mbp/dev-mini-setup`) gets one line per slot and reset, once per reset
  window. Events: `dialog-answered`, `limit-wait`, `limit-continue`,
  `limit-all-used-up`, `unknown-dialog`.

### Auto-resume after a network or API error (*v0.1.27*)

When the network drops, every running Claude turn ends within about three
minutes (Claude's own retries are spent by then) on `API Error: Can't reach
the API server … (ENOTFOUND)`, and the session sits at its prompt, idle or
done. The watcher continues such turns once the API answers again
(`auto_resume`, default on; plan: ansible-orchestration
`docs/macos/plan.herdr-sleeper-auto-resume.md`, B-437):

- Detection reads the transcript, never the screen: the last main-chain
  record that counts must be an `assistant` record with
  `isApiErrorMessage: true`. A prompt, an interrupt, a slash command, a
  relayed message, a real reply, or a prompt still waiting in Claude's own
  queue (after it, or queued during the turn and never taken) means someone
  went on. Subagent, meta and bookkeeping records are skipped; a half-written
  or damaged record there means no stall.
- Resumed: network errors (`apiError` `connection_failed`, `no_response`,
  `stream_idle_timeout`, `computer_slept`), cut streams (`streamCutCause`),
  server errors (status 500–599, 529 included; the first try after 60 s), and
  another Cloudflare edge's 403 challenge (after 120 s, at most 2 in 6 h).
  Records before Claude Code 2.1.317 carry only the text; the known texts
  count. Never: usage limits (the limit menu and Claude's own quota resume
  handle those), logins, TLS, billing, invalid requests, an aborted stream,
  and anything unknown (one `auto-resume-unclassified` event, field values
  only).
- The gate: `GET https://api.anthropic.com/api/hello` through
  `/usr/bin/curl` (TLS verified, no curlrc) must answer 200 with `hello` for
  10 s straight. It is probed only while stalls wait: every 5 s between ticks
  (every 30 s after 10 minutes down), never under the lock.
- The note goes through the mailbox like any message, as
  `msg ar-<session8>-<error8>` from `herdr-sleeper`: `Auto-resume: your last
  turn ended on an API error (connection_failed, attempt 1/4); the API
  answers again. Continue the task you were on. Before repeating a step with
  side effects (deploy, push, mail), check whether it already happened. No
  reply needed.` One note per error record, across watcher restarts too.
- Before it is queued: an idle or done claude pane, its session open in this
  pane only, no journal row, no pending `close --self`, no scheduled wakeup or
  cron, not in `auto_resume_exclude`, the error younger than
  `auto_resume_max_age` (24 h) and than the first start of a v0.1.27 watcher
  (`auto-resume.json`: the first deploy resumes nothing older); seen on two
  ticks in the same state, 20 s after the gate came up (5 minutes on a
  focused pane); 0, 1, 5, 15 minutes after each new error; at most 4 notes
  per error chain and 6 per session in 6 h (counted in the transcript, so
  partial output between cuts does not reset it); then `mail_refusal`
  (draft, menu, status, journal). At most 4 notes per tick, oldest error
  first. With a 60 s poll the wait between ticks ends early once a note is due.
- Right before typing (after `mail_refusal`), in every path (the watcher's
  flush, its 5 s retries, a `deliver` that flushes the box), the note is
  checked again on fresh state: the same pane, every check above with the
  config as it is now, and the same error still the last record. A note lives
  2 minutes (the gate's word is no fresher); the tender queues a new one while
  the stall is still due. Otherwise it is deleted (`auto-resume-dropped`),
  never typed.
- A pane whose stall will be answered is not slept (`scan` shows
  `auto-resume pending (…, gate unknown)`); a sleeper is never woken for it.
  At the cap: one `auto-resume-gave-up` event, and the pane may sleep.
- Config: `auto_resume = false` (this machine), `auto_resume_max_age = "24h"`,
  `auto_resume_exclude = ["name"]` (pane id, Herdr name, title or Claude
  name; the sleep `exclude` does not count: orchestrators should continue).
  Events: `auto-resume`, `auto-resume-gave-up`, `auto-resume-dropped`,
  `auto-resume-unclassified`, `auto-resume-error`, `auto-resume-gate`. No
  Pushover. Codex panes are not covered (Codex waits for its connection
  itself).

## Safety rules (all fail closed)

Inherited verbatim from the standalone script; the deltas the plugin
introduces are marked.

1. Eligible only when: a supported kind with a session id (and, for claude,
   an on-disk transcript — nothing to resume otherwise); Herdr status
   `idle`/`done`; pane not focused; not excluded; session id not open in
   another pane; no insomnia claim (*v0.1.3*; unreadable claims hold every
   session); idle ≥ window; original argv safely replayable
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
8. *v0.1.27:* auto-resume types only into an idle claude pane whose
   transcript's last main-chain record is a transient API error that no
   prompt followed, only through the mailbox and `mail_refusal`, only after a
   verified HTTPS answer from the API (probed outside the lock), within a
   budget counted in the transcript, and only after the note is checked
   again right before typing. An unknown error shape, an unreadable time or
   transcript, a cut tail that does not reach back 6 h, or any failure means
   no note.

Known limits: SIGTERM leaves a small window between the final recheck and
the process handling it (only a native Herdr operation could close it);
the stub line is POSIX shell (`printf`, `env`), so nushell/pwsh panes get
focus/command wakes only.

## Tests

`python3 -m pytest tests/ -q` — 528 tests: the decision logic, the
sleep/wake state machine, pane identity and orphans, the watcher tick and
loop (idle clocks, backoff, server-down retries, the watcher lock), the
focus hook, the stub, the resume command and the re-claim after a restart
(the fake `herdr` has a `restart()`), the 3.9 TOML subset parser, and legacy
migration, insomnia claims and their expiry, staleness and damage, spawn
and close (and `close --self`), the limit menu and one-time dialogs (fixtures of the 2.1.306 screens), auto-resume (its
classifier rows on 2.1.231–2.1.322 record shapes, the gate on a fake clock with a 60 s poll; an autouse fixture keeps
every test off the network), against a fake `herdr`; the other-machine paths against a fake `ssh`; and the CLI's real output shapes (the
"ok" calls answer success with nothing). v0.1.3 was run on Python 3.9 (Apple's
`/usr/bin/python3`) and 3.12. The `state` fixture redirects every module-level
path in the state dir, so a new state file cannot reach the live one. The fake does not prove what a real Herdr
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
