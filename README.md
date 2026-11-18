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
Only a rule behind such a remnant wraps, onto one line: two plain rules under the prompt are a draft's
rule and the box, and still refuse (*v0.1.30*).

A displaced sleep row answers to its session id or journal key only (*v0.1.22*): its pane, and the
pane's wake name with it, went to another session, so `deliver wake-w3-p3` reaches the newcomer or
refuses, never the dead handle's mailbox. `close <session id>` drops a displaced row whose pane is
gone and sets its queued mail aside in the mailbox's `retired-<time>/` directory. Since *v0.1.40* the
same goes for an asleep row whose sleeper is gone (its pane closed while it slept; no pane with its
terminal, no 💤 pane running its stub): `wake`
can't bring it back, so `close <wake name or session id>` drops it, and `spawn -- --resume <id>`
resumes it afterwards (a resume of a row like that names `close` first). A pane list Herdr doesn't
answer never counts as gone.

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
one whose flags are unknown), **opencode** (`-s <session>`) and **codex**
(`resume --no-daemon <session>`). opencode and codex are never slept
(`PROFILES[kind]["no_sleep"]`, the reason `scan` shows); mail, insomnia,
`ask`, `close` and `close --self` work for all three.

### opencode panes (*v0.1.34*)

Mail, `close`, `close --self` and `move` need a composer check for the pane's
kind (`PROFILES[kind]["composer"]`, dispatched by `composer_for`); opencode
1.19 has its own, `opencode_composer_state`, built from screens of a lab run
(`tests/fixtures/opencode-1.19.4`, from ansible-orchestration
`docs/macos/captures/`). Herdr does not see an opencode draft (the pane stays
`idle`/`done`), and `agent prompt` into one sends the draft with the mail, so
this check is all that protects it.

- The composer is the `┃` block closed by `╹▀▀▀…`; its last row is the agent
  row (`┃  Build · <model> <provider>`). A transcript user message is a `┃`
  block too, and one opencode queued touches the composer: a composer row is
  told from it by its background, the agent row's. Without styling every `┃`
  row above counts, so a plain screen fails towards `draft`.
- `empty`: blank rows, or the home screen's grey hint (`Ask anything… "…"`).
- `draft`: text on a composer row (`[Pasted ~N lines]` included), shell mode
  (agent row `Shell`), an open `@` autocomplete row.
- `unknown` (refuse): `esc interrupt` or `retrying in` in the footer (a turn,
  or a provider retry loop after a 5xx or 429; Esc ends it), a permission or
  question dialog, no composer bottom on screen.

Mail into an opencode pane gets a word joiner (U+2060) in front of every
`@` and `!` that starts a token: a typed `@path` becomes an attachment opencode
reads itself and a leading `!` a shell command, both past its guardrail plugin.
`agent prompt` pastes, which kept them as text in the lab; the joiner keeps
them text regardless. opencode panes are never slept for now, whatever
`agents` says (v0.1.35, "opencode lanes" below).

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
`wake_refuse_from` (see Messages below), `never_show` (see the search's view tab), `answer_dialogs` and `limit_monitor` (see "Subscription slots"), `codex_stuck` (see "A Codex pane stuck after its turn"),
`auto_resume`, `auto_resume_max_age` and `auto_resume_exclude` (see "Auto-resume after a network or API error"), `ask_gate` (default on; see "Turn-end count and ask gate"), `ask_mirror` (default on: open asks on the owner's phone through moshi-hook), `dry_run` (the watcher and `scan` log `would sleep (dry run)` once per idle stretch
instead of sleeping), `watcher` (`"plugin"`, the default: hooks spawn and
heal the watcher; `"external"`: a service manager runs
`herdr-sleeper watch` and hooks never spawn one, but report it when it is
missing);
overridable by `HERDR_SLEEPER_*` env (booleans take true/false/1/0), lowest
to highest precedence. State: `<plugin state dir>/` per Herdr session —
`sleeping.json` journal, `panes.json` crash-recovery snapshot, `idle.json`
idle clocks, `events.jsonl`, `sleeper.log`, `watcher.pid`/`watcher.lock`,
`insomnia.json`, `spawned.json`, `names.json` (owed names), `limited.json` (sessions and slots at their usage limit), `codex-stuck.json` (Codex panes at a menu or their usage limit).
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
  is about. The ask panel then shows that message under the question, so the
  owner answers with the original ask in front of them, and the owner's answer
  (msg `ask-<ask id>`, `re` the ask) joins the same thread.

### Asking the owner: the ask panel (*v0.1.32*)

An agent that needs the owner never waits in a menu. It adds an item to its
session's **ask panel** and keeps working:

```sh
echo "canary green 40 min; rollback is one play" | herdr-sleeper ask \
    --question "Deploy the gateway now or tonight?" --option now --option tonight --recommend 2
herdr-sleeper ask --todo "Run ./bin/vault-grant 4h on mini2"   # something only the owner can do
herdr-sleeper ask --todo "Paste the guard into the VM's root shell" --copy 'find /etc/cron.d …'
                                                                # c copies the command exactly (--copy-file for a script)
herdr-sleeper ask --list [--all] [--everywhere] [--json]       # --everywhere: every saved machine
herdr-sleeper ask --inbox                                       # the same; exit 3 names an unreachable machine
herdr-sleeper ask --withdraw ID                                 # no longer needed
herdr-sleeper ask --done ID                                     # a to-do the session checked is done itself
```

- **One panel per session**, split to the right of the asker's pane in the
  asker's own tab (60/40), opened on the first item. Later items join it; none
  replaces another. Up to 9 open items per session, each answered on its own.
  The session's TUI reflows only when the panel opens and when it closes.
- **The panel** lists the items as numbered cards: who waits (machine/name)
  and where the session is now (re-read every 10 s), the selected item's
  question or to-do (❗) with its answers, its context wrapped to the pane and
  paged with `m`, and the other items one line each (`Tab` or `#N` switches).
  The answers are pinned: the options (the recommended one marked ★), `?`, or
  `d`/`x` for a to-do are never paged away; on a short pane they shrink to one
  line each. `z` zooms the panel (an 80-column terminal leaves it about 22).
- **On the phone** (v0.1.52, buttons at most 40 characters since v0.1.53, owner 2026-11-17): Moshi shows no Herdr panes,
  so the watcher also puts every open item on the owner's phone with
  `moshi-hook ask` (`ask_mirror`, default on; only when moshi-hook is paired
  and end-to-end encrypted). The card names the asker and its place and carries
  the question and up to 900 characters of context; the buttons are the options
  (numbered, ★ recommended, at most 5 plus "talk it over", or 6) or, for a
  to-do, done and can't. A tap goes the panel's way (`send_answer`), marked
  "(answered on the phone)"; whichever answers first wins, and an item closed
  in the panel or withdrawn ends its phone question. A command is copied on a
  Mac (`c`), never from the phone. A question without options gets "talk it
  over" and "later: leave it open". Dismissed, timed out after 24 h, "later" or
  "talk it over" on the phone: not asked there again (the pane keeps the item).
  Moshi unreachable: again after 10 min. A tap that couldn't be matched or
  handed on is asked once more, at most 6 phone questions per item; a mirror
  that died 3 times is given up. A live mirror holds `ask-mirrors/<id>.lock`
  for its whole life, so a sleeping Mac or a slow herdr call never starts a
  second card; it ends its `moshi-hook ask` on every way out (SIGTERM, error),
  and the watcher ends one a dead mirror left behind (by its process group).
  The card's text is an argument of `moshi-hook ask` (it reads no stdin), so a
  local process listing shows it: asks carry no secrets anyway. A tap answers at
  once, without the panel's `y` confirm. State in `ask-mirrors/<id>.json`.
- **Copy and paste** (v0.1.47): the context is wrapped and indented for reading,
  so a command selected out of it breaks in a shell. A command the owner runs
  elsewhere goes in `--copy` (`--copy-file`): the card shows it (a long one in
  full under `m`) and `c` then Enter copies exactly it through OSC 52, which
  Herdr hands to the owner's clipboard. `--copy` refuses escapes, control,
  invisible and direction characters, so what is shown is what is copied. A
  paste into the panel is one block (bracketed paste; a terminal without it: a
  read with a line break and more text, and the reads right after it): its line
  breaks are text, and only a `y` or Enter typed on its own sends. A paste whose
  end marker never comes ends after 0.3 s of quiet.
- **Answering:** a question takes an option number, `N NOTE` (also `N: NOTE`,
  `N) NOTE`: option N plus a note, `2 after 18:00`), free text, or `?` (talk
  it over in the session: the session is told, the item stays open). A to-do
  takes `d` (done), `x REASON` (can't or won't), or a note. Every answer is
  confirmed: the confirm line names the item number, its question and the
  answer, and a single `y` or Enter sends it (`2`, Enter, `y`: three keys); a
  stray `1` meant for a Claude menu next door is never sent on its own. At the
  confirm step nothing typed is lost: typing on adds to the answer (after a
  space), Esc or Backspace bring it back to edit, only Ctrl-U clears it.
  Option-Backspace and Ctrl-W delete the last word; a plain Esc while typing
  clears the input, an Option key combination never does. A
  single Ctrl-D does nothing; only a terminal that is gone ends the panel.
  `s` focuses the session's pane, re-resolved live; a sleeping session is not
  focused (that would wake it). Session text is shown without escape, control
  or bidi characters, and the typed answer is cleaned the same way before it is
  stored.
- **Talk, then answer:** `?` queues msg `ask-<id>-talk` and keeps the item
  open; the later answer is msg `ask-<id>`. The record keeps both ids, and its
  `answer` is exactly the text the session received in `ask-<id>`, so `show`
  and `thread` never show what the session did not get.
- **Sidebar:** the panel reports itself as agent `ask`, state `blocked`,
  labelled `needs you` (a Claude menu still says `blocked`), named by machine,
  asker and count and by the selected item: `❓ mini2/orch (2): decide: Push the
  guardrail fix…` or `❗ … to-do: …` (token `$ask` the same). Every value it
  passes to a Herdr flag starts with that mark, never with `-`. It reads its
  state back and reports again until Herdr shows it blocked, and re-checks
  every 30 s.
- **Priority relies on the sidebar's sort.** The design needs
  `ui.agent_panel_sort = "priority"` (blocked rows first). Herdr sorts the
  rows of a machine whose connection is not Online below every live row, even
  idle ones, with their cached state, so a "needs you" row from a stale mini
  endpoint can sink. Two things cover that: a toast with a sound when items
  arrive (several within 4 s give one toast and one sound), and a reminder
  toast every 30 min for an item open longer than 30 min.
- **Delivery:** the answer is queued and the item closed in one step under the
  session's mailbox lock (only tried; when a delivery holds it the panel shows
  "retrying" and tries again every half second, never blocking) and then the
  global lock, so a withdraw either wins or finds it closed, and what is
  stored is what was queued. A detached `deliver-pending` types it in (waking
  a sleeper); the panel never waits. After `/clear` the answer goes to the
  session that now runs under the asker's agent name in the same pane on the
  same terminal (a name or pane id alone can belong to a later session).
  Whether the panel had the focus is kept in the record (`focused`) as
  forensics only; it is not part of the answer's header.
- **Pane ownership:** a pane counts as a panel only when its foreground argv is
  exactly `<script> ask-panel <panel id> <state dir>` (or `ask-pane <id>
  <dir>` for a tab-design item). Only that proof lets `ask` join a pane and
  lets `close`, the panel or a withdraw close one; a pane id that came back
  after a restart with something else in it (even another session's panel) is
  left alone. `ask` checks that the new pane runs the panel within 3 s; if it
  does not, the split is closed at once and the next ask opens a new one.
- **No item without a panel:** the watcher (every 30 s), `ask` and `ask --list`
  look for open items whose panel is gone (it ended, or the owner closed it).
  Seen gone twice 45 s apart (a restarted panel fixes its own pane id first),
  the panel is reopened when the asker is live, or asleep with its own wake
  stub in its pane; otherwise the items become `lost`, shown as LOST in
  `ask --list` and the inbox, and the asker is told in the output of its next
  ask.
- **Inbox:** `ask --inbox` lists every open item on every saved machine and
  names a machine it cannot reach (exit 3). The plugin action `ask-inbox`
  opens the same as a popup of cards (`[[panes]] ask-inbox`); bind it in the
  Herdr config, e.g. `[[keys.command]] key = "prefix+i"`, `type = "popup"`,
  `command = "herdr-sleeper ask-inbox"`.
- **Closing:** the panel closes its own pane (never the tab) when nothing is
  open. When the asker sleeps and the panel has the focus, it waits until the
  owner moves away first: closing a focused pane hands the focus to the
  sleeping session next door, and focus wakes it. Only an answer's delivery
  may wake a session. `close` refuses a session with open items; `--force`
  withdraws them (and names them in its output and the log) and closes the
  panel first, so the tab closes whole.
- **Restart:** the panel reports `env … ask-panel <panel id> <state dir>` as
  its resume command (no `exec`). On start it writes its current pane id onto
  its items before anything else, and `ask` looks for the panel in the asker's
  tab by argv before it would open a second one. Items from the tab design
  (v0.1.31) still load and answer in their own tab; the session's next ask
  adopts them into its panel.
- **Trust:** only the panel queues mail of kind `owner-answer`, the only kind
  `envelope()` heads with `[the owner's answer to your ask …]`; `deliver`,
  `--question`, `--option` and the context refuse that text. This does not
  prove the keys came from the owner's keyboard: Herdr 0.9.5 writes `pane
  send-text` into a pane like typed keys, and the state dir is the same
  user's, so a same-user agent set on forging an answer can. The confirm step
  stops stray keys, not such an agent.

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

**Turn-end count and ask gate** (*v0.1.45*). Registered also as a
`UserPromptSubmit` and a `Stop` hook (no matcher), `claude-hook` notes how
each turn ends. It reads no transcript: the prompt's origin comes from the
`UserPromptSubmit` payload, the reply from the `Stop` payload's
`last_assistant_message`, both classified in memory. The origin is
`notification`, `relay`, `ask-answer` or `sleeper-note` only when the prompt
*starts* with that envelope and, for the sleeper's own envelopes, the message
archive holds that message id queued for this very session (a relayed message
or an answer the owner pastes into his own prompt stays `owner`; the ao
mini-to-MacBook header `[from X via herdr-sleeper` has no archive here and
counts as `relay`), `no-prompt` when no prompt came since the last Stop,
else `owner`. A batch of mail typed in as one prompt counts as a whole: any
peer's message makes it `relay`, else an owner's answer `ask-answer`. A note
counts as the sleeper's own only when its archived copy carries the `note`
mark that only the watcher writes, never by its text; `deliver --from
herdr-sleeper` is refused to sessions (the watcher's own notes to the monitor
pass). Owner, relay and ask-answer
prompts start a turn **chain**; a notification, a no-prompt turn and a note the
sleeper typed in itself (a continue note after a usage limit or its reset, an
auto-resume note) carry on the chain before them, so a background event's turn
during the owner's chat counts as his. An owner chain ends 2 h after his last
prompt; relay and ask-answer chains don't expire. Since v0.1.50 the chain only
feeds the counts: it no longer exempts a turn from the ask gate. No chain, or an expired one: the turn's own origin; an owner
chain that runs out while the turn runs is checked again at its Stop. A
prompt's markers and a Stop's read of them happen under the session's turn
lock (tried for 1 s). A prompt that can't take it still marks its turn,
with chain `unknown`, and a prompt hook that fails marks it `unknown` too:
such a turn's Stop never blocks. The lock files stay; only week-old markers
are cleaned up. A reply waits on the owner when its last three paragraphs ask him to
do something (please sign in, please run/open/approve/confirm, still waiting
on you, run this in a separate terminal, "Once you're in, I'll …", bitte melde
dich an, …: a *todo*) or its last two ask him something (should I, which one,
let me know which, a closing `?`: a *question*). Code blocks, `>` quote and
callout lines, inline quotes ("…", “…”), a `?` that only ends a heading or a
bold label, a wait its own sentence negates ("nothing is waiting on you",
"I am not, in fact, waiting on you"; a ", but" clause or an opening "No
problem," doesn't negate) and closing
pleasantries ("let me know if anything breaks") don't count: a request inside
a quote or a callout is never read as one.

The owner's rule (2026-11-17, sharpened the same day): a session that ends its
turn waiting on him shows as a blocked question in Herdr, and everything that
waits on him goes into `herdr-sleeper ask`, also while he chats with the
session: he looks only at the panes that need him, never at a chat. So when a
turn ends with a reply that waits on the owner, no ask of the session covers it
(an ask covers the reply when it was opened during this turn or the reply names
its id; an older ask about something else doesn't, and an unknown count blocks
nothing), it runs in a Herdr pane (`HERDR_PANE_ID`; `ask` needs one), and the
Stop payload's `stop_hook_active` is `false`, the `Stop` hook prints one JSON
object and exits 0, whoever started the turn's chain:

```json
{"decision": "block", "reason": "This reply seems to wait on the owner, and no ask covers it (none opened this turn, none named by its id), so nobody will see it: the owner looks only at the ask panels, also while he chats with you. File it now with `echo CONTEXT | herdr-sleeper ask --question '...' --option '...' --option '...'` for a decision, or `echo CONTEXT | herdr-sleeper ask --todo '...'` for something only the owner can do (one decision or to-do per ask; a command he runs goes in --copy), then end your turn. If it waits on an ask you already hold, name its id. If your reply does not wait on the owner, just end your turn again."}
```

Claude Code then continues the turn with that reason; the next Stop carries
`stop_hook_active: true` and is never blocked, so a reply the classifier
misread costs one extra step, never a loop. `ask_gate = false` turns the gate
off (the turns are still counted); with `dry_run` the hook logs `would block
the turn end (…) (dry run)` to `sleeper.log` instead. A config that can't be
read or any failure blocks nothing.

`turn-ends.jsonl` keeps one row per turn with the origin and chain, the
reply's class, the session, pane, Herdr name, turn length, open-ask count and
`gate` (`blocked`, `dry-run`,
`off`, or null when the gate didn't apply), never text. The hooks print
nothing else, never wait on a lock (a held one drops the row, not the block;
one Herdr call of at most 2 s keeps the Stop inside its 5 s) and leave only an
error's class name. `herdr-sleeper turn-ends [--hours N] [--json]` sums them
by origin and session, with what the gate did and the range the file still
holds; a turn a block sent back (`stop_hook_active`) is not counted again as a
wait without an ask.

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
    --name app-b17 [--kind claude|codex|opencode] [--from orc] [--workspace agents|w3] [--json] \
    [-- --dangerously-skip-permissions --model opus]
herdr-sleeper close app-b17 [--force]
herdr-sleeper close --self [--reason "B-17 done"] [--task … --how … --result …]   # from inside the session: close me once this turn ends
herdr-sleeper close --self --cancel
```

The tab goes to the workspace `--workspace` names (a label or an id; a missing
one is refused), else to the workspace labelled `agents` on that machine, made
on first use (*v0.1.42*, owner 2026-11-15: spawned sessions live in `agents`,
never among the caller's tabs).

`spawn` refuses a working directory that is not inside a git checkout below
`~/development` (realpath), a name that is taken (a live agent or a sleeper),
and claude flags that would not survive a sleep: everything after `--` goes
through the same replay filter as a wake, and `-n`/`--name` or a session
selector (`--resume`, `--continue`) is refused, since the name comes from
`--name` and a spawn starts a new session.

The one exception (*v0.1.38*): `-- --resume <session id>`, given once and
alone, brings a closed claude session back into a named tab, its first
message on stdin as usual. It is refused when that session sleeps (wake it),
runs anywhere on the machine (deliver to it), its liveness can't be verified,
or its transcript is missing or records another working directory (pass
that one as `--cwd`). A second resume of the same session while the first is
starting is refused (a lock in the state dir). A claude that comes up with a
different session id gets nothing typed and its tab closed
(`resume-mismatch`). The spawn record keeps `resumed`; `-n <name>` gives the
resumed session the new name. Codex and opencode sessions can't be resumed
this way yet.

Steps:

1. creates a tab with `--no-focus`, labelled with the name, in `--workspace`
   or else the `agents` workspace (made on first use, under a lock);
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
3 started with the prompt queued (also when it waits for the session id,
below), 1 refused or failed, 2 bad input.

**A late session id** (*v0.1.46*). A claude may report its session id well
after `agent start` returns (2026-11-17 on mini2: 13–43 s, three spawns in a
day). `spawn` waits up to 90 s for it (was 15 s, after which the prompt was
dropped and spawn exited 1). Still none: the first prompt is kept in the
spawn record under its message id, with the pane's terminal id, and spawn
says so and exits 3 (started, prompt queued, as for a session that is not
idle yet). The watcher (every tick, and every 5 s while it waits) moves it
into the session's mailbox once the pane reports a session, logs
`deliver-queued` then, and from there it is typed in like any `deliver`, once
the agent is idle. The id makes it go in once, even if the watcher stops
between queueing and letting go of it. The pane counts only on the recorded
terminal (Herdr reuses pane ids); a pane moved meanwhile, or one after a Herdr
restart, is found by its agent name. The prompt is dropped, with a
`spawn-prompt-dropped` event and a log line, when that agent is missed for
45 s (pane gone, its id taken by another terminal, the agent quit), after an
hour without a session id, or with its spawn record. A `--resume` that comes
up as another session has its tab closed (`spawn-failed`,
`resume-mismatch`), as spawn's own check does. An agent that quits during
spawn's own wait ends the wait and fails the spawn (exit 1, nothing queued).
A remote spawn's time limit grows by the 90 s (`REMOTE_SPAWN_SECONDS`, 590 s).

### opencode lanes (`--kind opencode`, *v0.1.35*)

An opencode lane is spawned only on a machine with the opencode-ao guardrail
installed: `/usr/local/libexec/opencode-ao/policy.json`, root-owned and not
group- or world-writable (ansible-orchestration `docs/macos/plan.opencode-herdr.md`).
Without it (the MacBook) the spawn is refused. It is refused as well:

- for a cwd in the policy's Klinikon roots;
- for a checkout (from the cwd up to its git root) with a `.opencode/`
  directory, a `tui.json(c)` (its plugins load in the TUI, outside the
  guardrail), an `opencode.json(c)` that is not valid JSON(C) or sets any key
  but `$schema`, `theme`, `keybinds`, `username`, `share`, `tui` and `layout`,
  an `AGENTS.md`, `CLAUDE.md` or `CONTEXT.md` that links out of the checkout
  (opencode reads them at start, with no hook), or a project skill in
  `.claude/skills` or `.agents/skills` that has the name of a global or
  built-in one (found as the shim finds skills: every `SKILL.md` up to five
  levels down, named by its front matter);
- when `~/.opencode` exists;
- on a machine already at the policy's `max_lanes`.

The lanes are counted, the name re-checked and the new lane reserved under the
global lock, so two spawns at once can't both take the last lane or the same
name. The reservation carries a token; it is renewed (ownership and room
checked again) once the tab exists and again right before `agent start`, and
only its owner updates or drops it.

After `--` only `-m/--model <provider/model>` is accepted; the session keeps
its model when resumed with `-s <id>`, so nothing has to be replayed. Then:

1. the shim must prove it loaded: within 10 s a load marker
   `<marker_dir>/shim-<pid>.json` for the pane's opencode pid, with the
   installed shim's sha256, written after the start. Otherwise the tab is
   closed (a plugin that fails to load is silent in opencode);
2. the home screen's empty composer, within 30 s (anything else is
   `agent_not_ready`, and the tab is closed);
3. the envelope is pasted with `herdr agent prompt --wait --until working`
   (never `--prompt`: argv is readable with `ps`);
4. opencode reports its session id only with the first prompt, so `spawn`
   waits up to 60 s for the `ses_…` id.

Only `agent_blocked` means the prompt was not typed (exit 1, the lane stays).
Any other failure, a timeout included, may have typed it, so the session poll
decides; with no session the exit is 3 and the message says to look at the
pane before sending it again. The lane stays open and keeps its name.

opencode lanes don't sleep yet: `assess` refuses them, and `wake` and the
in-pane stub refuse an opencode row, until a wake checks the shim's marker too
(the plan's Phase 6).

A spawned session answers to its spawn name even when Herdr has dropped the
agent's name (`deliver`, `close`, `presence`, the default `--from`), for every
kind. `herdr-sleeper whoami [--json]` prints the calling agent's name, its
`<machine>/<name>` address, pane, place, kind and session.

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
An orphaned or waking row is never closed, not even with `--force`; neither is
a displaced one whose pane exists (an asleep or displaced row whose sleeper is
gone is dropped, see above). Either way the transcript stays on disk, and `close` prints
the line that resumes it by hand.

### Codex sessions (`--kind codex`, *v0.1.36*)

Codex 0.163 has its own composer check, `codex_composer_state`, from a lab
run (`tests/fixtures/codex-0.163.0`, from ansible-orchestration
`docs/macos/captures/`): the composer is the block in one background around
the last `›` row (a spacer, its rows, a spacer), with only the status line and
hint below it. Empty takes positive evidence: one input row with nothing
but the dim placeholder (its words change from session to session), a `›`
that isn't dim, no `↑`/`↓` scroll marker, and the `? for shortcuts` hint,
which Codex shows only for an empty composer. A taller block or a scroll
marker is a draft; a dimmed composer (transcript navigation dims a draft
too) or a missing hint is unknown. A
transcript message is a block in another shade with a dim `›`, a dialog's
selected row has its own shade, and `• Working (… esc to interrupt)` above the
composer is a running turn: all unknown or not the composer, so nothing is
typed.

A Codex TUI runs its session in a shared app-server daemon unless started
with `--no-daemon`, and the daemon hands every session's hooks the
environment of the pane that started it: Herdr's hook then reports the
session to that pane (LESSONS 4.10). So `spawn --kind codex` always passes
`--no-daemon`, takes only `-m/--model` after `--` (a positional is the first
prompt or a subcommand; `-c`, `--remote` and the rest are refused), and types
the first prompt like an opencode lane: Codex reports its session only once a
prompt made it write its rollout. A folder-trust dialog never shows an empty
composer, so such a spawn closes its tab without typing (`agent_not_ready`).
A close sends SIGTERM (Codex exits within a second); the pane closes right
after, so the keyboard protocol Codex leaves on doesn't matter there.

### Moving a session (`move`, *v0.1.33*)

`herdr-sleeper move NAME --workspace W [--label T] [--create] [--from SENDER]`
moves a live session into a new tab of workspace `W` (a label or an id; two
workspaces with the same label are refused, `--create` makes a missing one)
without taking the focus, for whoever keeps the owner's layout. It refuses,
never forced: a busy session, a draft in the composer, a kind whose composer
cannot be read, the pane with the keyboard focus, a tab shared with another
pane (an ask panel stays behind otherwise), an open ask, queued mail, and the
calling session. Herdr gives a moved pane a new id and keeps its terminal; the
move checks the terminal and logs `moved`. `--machine M` (or `M/NAME`) moves on
another saved machine, never across machines.

A sleeper moves without waking (*v0.1.39*, B-465): only a row that is provably
its pane's (phase asleep, same terminal) whose pane runs its wake stub or a bare
shell, under the same refusals (focus and a delivery would wake it). The row is
re-keyed to the new pane id in the same lock and the claim is re-reported with
the new pane's stub argv, so a Herdr restart types a stub that names the new
pane. The running stub keeps the old id in its argv and finds its row under
the pane it runs in now (the shell's `HERDR_PANE_ID` or its argv pane, each
resolved to Herdr's current id through the alias, or the fresh shell's id after
a restart, proven by our stub for that session in that pane's foreground;
resolved again under the lock, since a `move` may run while it waits), else
under its argv pane (a drag the watcher has not followed yet: re-keyed before it
turns `waking`, only from the same terminal; otherwise the stub starts over and
the next Enter finds the followed row), else the one row for its session
recorded in the terminal its pane has now (a `move` and then a drag before a
tick; the same terminal is the same pane). Never a row elsewhere by session id
alone: an old stub left in an orphaned pane would take the row of the same
session asleep in another pane. A stub writes its pid to
`stubs/<uuid>.pid` when it starts; a stub started by v0.1.38 or older writes
none and still runs that code (a deploy restarts only the watcher), so `move`
refuses it: wake that session and move it awake, or move it after a Herdr
restart. A drag by hand is followed by reconcile through the terminal id (and
re-claimed the same way); a restart before a tick followed it is followed
through the 💤 label and the retyped stub.

A moved session keeps `HERDR_PANE_ID=<old id>` in its shell; Herdr answers the
old id as an alias until its next restart, so every "which session am I"
lookup (`ask`, `insomnia`, `close --self`, `deliver --from`) resolves it
through `pane get` (`own_pane`); attributing a session through such a followed
id needs the process-ancestry proof (a detached child may keep a stale
variable). A session holding insomnia (its own claim or one made in its pane
before a `/clear`) or closing
itself is not moved: both records name its pane.

### Closing yourself (`close --self`, *v0.1.13*)

A session that is done closes itself: it runs `herdr-sleeper close --self
[--reason TEXT]` from its own shell tool as the last step of its final turn.
`close NAME` refuses the calling session, because the SIGTERM would end the
very turn that asked; `--self` only records the wish and prints one line
(`<name> closes after this turn ends (checked every 5 s, closed once
safe); a message delivered before then cancels it`). It is always local: no `--machine`, no target, no `--force`.

- The caller is identified like an insomnia claim (`HERDR_PANE_ID`, and that
  pane's agent process must be an ancestor of the command); outside an agent
  pane, without a session id, or for a kind whose composer cannot be checked
  for a draft (anything but claude and opencode), it refuses (exit 1). Running it again
  keeps the wish and its start (`--reason` replaces the reason).
- The wish lives in `closing.json` in the state dir, keyed by session id
  (`at`, `name`, `pane`, `reason`, `by`), and logs `self-close-requested`.
- Each watcher tick, after the mail flush, and every 5 s between ticks while
  a wish is pending (*v0.1.31*: a session that said it closes itself no longer
  stays up to a whole poll after its turn), closes a session with a pending
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

### Closed sessions: `closed` and `reopen` (*v0.1.51*)

Sessions close themselves when done, which frees their memory; the owner still
wants to see what each one was for and get it back quickly (owner 2026-11-17).
Every close that goes through herdr-sleeper keeps a record, and `reopen`
resumes a session from it.

```sh
herdr-sleeper close --self --task "build the closed-session view" \
    --how "one record per close, written by close_session" \
    --result "v0.1.49 pushed (abc1234); AO bump B-123 left open"
herdr-sleeper close --self --summary-file -   # `task:` / `how:` / `result:` paragraphs on stdin
herdr-sleeper close app-b17 --task … --how … --result …   # a closer can give one too
herdr-sleeper closed [--all | --machine M] [--since 7d] [--grep TEXT] [--limit N] [--json]
echo "also do B-18" | herdr-sleeper reopen [MACHINE/]NAME|SESSION-ID [--name NEW] [--workspace W] [--from X]
```

- **The record.** `closed.jsonl` in the state dir (0600, append-only, one line
  per close): when, the machine (config `machine`), its place as Herdr showed
  it (`workspace › tab`, read before the tab closes) and workspace label, the
  name and Claude name, kind, session id, cwd, the replayable claude flags
  (without `-n`), model (the transcript's last reply), subscription slot,
  who spawned it and when, who closed it (`self`, the closer's name, or `gone`),
  the refusals a `--force` overrode, and three owner-readable lines: `task`,
  `how` it was resolved, the `result` (each one line, up to 500 characters).
- **Where the summary comes from.** `close --self --task/--how/--result` (or
  `--summary-file PATH|-`; the flags win) stores it with the wish; the record
  marks it `summary_source: "session"` (`"closer"` for `close NAME …`). A close
  without one is never refused (`close --self` prints a hint); the record then
  takes the transcript's first prompt (without the paste wrapper and relay
  header) as the task and its last reply as the result (`"transcript"`;
  codex and opencode: the last message the session sent, `"last message"`).
  Text taken from a transcript goes through a credential filter (private key
  blocks, GitHub/GitLab/Slack/AWS/OpenAI-style tokens, `Bearer`/`Basic`
  values, URL passwords, `*_TOKEN=value`/`"token": "…"` assignments, 40+
  character mixed-case runs), run before the text is cut to 500 characters;
  commit and session ids, paths and kebab-case names stay. Given summaries pass the agents' own Bash guardrails already.
  `close NAME` on another machine sends its summary over stdin, not argv.
- **Closed without a close.** A spawned session whose tab was closed by hand
  or whose agent quit is recorded (`closed_by: "gone"`, with when it was first
  missed) once it has been missing for 10 minutes and its spawn is an hour old; one missed tick (a Herdr
  restart, an account restart) records nothing. A sleep row retired because its
  pane is gone is recorded too. Sessions that were never spawned and vanish
  by hand are not (Herdr restarts and `/clear` make "gone" too noisy there).
- **`closed`** lists the records newest first, one block per session: time,
  `machine › workspace › tab`, kind, who closed it, how long it ran, who
  spawned it, then name, session id, cwd, task, how, result, and the exact
  `herdr-sleeper reopen MACHINE/NAME` line (by session id when a newer record
  holds that name). `--all` asks every saved machine in parallel (an
  unreachable one is named, exit 3); only the known fields cross machines.
- **In Herdr:** the plugin action *Show closed sessions (all machines) and
  reopen one* opens a popup (90% × 80%, like the ask inbox) with the newest 15
  records of every machine, numbered, the newest next to the prompt: Enter
  refreshes, a number reopens that session after a `y` (`--from
  closed-sessions`), q closes.
- **`reopen`** finds the newest record whose name, Claude name or session id
  (or an 8+ character id prefix) matches, on its machine (`MACHINE/NAME` goes
  there over SSH, with the note on stdin), and resumes it through `spawn
  --resume`: the same cwd, its old workspace when that still exists (else
  `agents`), its name (`--name` when that is taken), its flags. Every spawn
  check holds: the transcript must be on that machine and record that cwd, the
  session must be neither live nor asleep, the cwd must be a checkout below
  `~/development`. The first prompt says it was reopened, by whom, gives its
  record and the note, and tells it to check the state before acting. A
  `reopened` line is appended and `closed` shows it. Claude only for now; for
  codex and opencode `reopen` prints the resume command. Transcripts are
  shared across subscription slots, so the session starts on the active slot.
- **Retention.** A record is about 1 KB. Once the file passes 4 MB it is
  rewritten without records older than 365 days (at most the newest 5000).
  Nothing here deletes a transcript; one that Claude Code pruned meanwhile
  makes `reopen` refuse with the reason.

### Searching the running sessions: `search` (*v0.1.54*)

The owner wants to find, from a key, which running session talked about something
(owner 2026-11-17; Herdr has no search across panes, and reading the panes is slow
and shows a Claude pane's visible screen only). `search` reads each running
session's transcript (live and asleep) on the machine it runs on and lists the
sessions that mention a word, newest hit first.

```sh
herdr-sleeper search WORDS [--json]    # this machine and every saved machine that answers
herdr-sleeper search-view-open         # the popup (plugin action `search`; bind it, e.g. prefix+/)
```

- **What is searched.** What was said: the prompts typed in and the replies
  (Claude text blocks; Codex `message` items of the user and the assistant).
  Tool calls, tool output, reasoning, Claude Code's own records and Codex's setup
  messages are left out. Any case; the words as one phrase. Plain-ASCII words
  filter the transcript's lines as bytes first, so ~300 MB of transcripts take
  about a second. The filter compares bytes, so `ß` is not found by `ss` and a JSON escape (`\/`,
  `\u0041`) not by its character; Claude and Codex write neither escape. opencode sessions are
  not searched yet.
- **A row.** `machine › workspace › tab`, the name, kind and state, the number of
  hits, the newest one's time and a snippet around it (who said it, the credential
  filter of the closed-session records applied). A session whose transcript can't
  be found is named in one `not searched:` line.
- **The popup.** Type the words, Enter; a number goes to that session's pane
  (`herdr agent focus`; a sleeper shows its stub, Enter there wakes it); Enter on
  its own searches again, q closes.
- **A session on another machine opens in a view tab** (*v0.1.61*). Herdr's API can't
  switch the owner's window to another machine (upstream #5589, not planned), and a
  session's process can't move between machines. So the popup opens a tab
  `view:<machine>/<tab label or name>` right after the owner's tab, in the workspace
  they're in, focused. The tab runs `herdr-sleeper view MACHINE PANE`:
  - It attaches over SSH to the saved machine's target (`ssh -tt … herdr terminal
    attach <terminal>`) under a small terminal relay that carries keystrokes, output
    and the window size.
  - Nothing moves: input is shared with every other client of that session. While
    the view is open, the session takes the view's size, and it gets its own size
    back when the view closes.
  - The tab closes itself after 15 minutes without a keystroke, when the session
    closes, sleeps or is replaced, or when its machine misses two checks (30 s each).
    The owner leaves by closing the tab; `prefix+q` inside it would detach their own
    client.
  - A view never opens in a `parked` or `…agents` workspace. The Goto handoff
    (*v0.1.59*: focus there, tab label in the clipboard, `prefix+g`, `/`, paste)
    remains the fallback when no view can open.
  - The `view:` label is the marker that presence, layout's mover and placement
    measures skip.
  - Config `never_show = ["mbp"]` (a list of saved machine labels): this machine
    never searches those machines and never opens a view of their sessions; an
    unreadable config refuses the view. It is a local backstop. The boundary stays
    with what each machine's keys allow (the MacBook relay's allowlist refuses both
    `search` and Herdr's own `--machine` forwarding). Rows always carry the machine
    that was asked, never one they name themselves, and a view checks that the pane
    still holds the session the search found.
- **Other machines.** Each saved machine runs `search --here --json` over the same
  SSH as `presence`, in parallel (30 s at most); the words cross only in the
  `presence` argument form (letters, digits, spaces and `./:@=,+~%_-`). A machine
  that can't run it is named, not retried: a key whose forced command doesn't know
  `search` (a relay that allows only messages and presence) keeps that machine's
  transcripts where they are.
- **Not for agents.** Its output is other sessions' words; an agent that runs it
  would copy them into its own transcript. Refused under `CLAUDECODE`
  (`HERDR_SLEEPER_SEARCH_AS_AGENT=1` for a test). Nothing is stored.

## Other machines

Herdr's saved SSH machines (`herdr machine list`) extend `deliver`, `wake`,
`close`, `presence`, `insomnia list` and `spawn` to another machine:

```sh
echo "review MR !4" | herdr-sleeper deliver dev2/impl-1 --from orc   # or: deliver impl-1 --machine dev2
herdr-sleeper wake dev2/impl-1
herdr-sleeper presence --all                # this machine and every enabled saved one, as machine/name (exit 3: one did not answer)
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
| the limit menu (its options `Stop and wait for limit to reset` and `Switch to usage credits`, the cursor on a numbered row; *v0.1.48:* not the hint above them, which 2.1.306 renamed from `/usage-credits` to `/rate-limit-options`) | `Enter` on option 1, only from there; never another option (option 3 bills credits) |
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
  once the reset of the window that was fullest at the hit has passed
  (*v0.1.48:* kept with the hit, so a five-hour hit no longer waits for the
  weekly reset), or (*v0.1.48*) once a reading taken after the hit shows an
  early reset while `resets_at` stays where it was (a free weekly reset,
  2026-11-17): below 50 % and below the slot's reading at the hit by 25 points
  or half of it (`levels` in `limited.json`), since an idle pane's status line
  can still write a reading from before the hit. A slot hit again within 6 h
  of an early reset waits for its real reset (event `limit-continue`, `why`
  `early reset` or `reset passed`).
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

### A Codex pane stuck after its turn (*v0.1.44*, B-485)

Herdr takes a Codex pane's state from Codex's lifecycle hooks, and two screens
open after the turn has ended: a menu (Codex 0.169's "Approaching rate limits,
switch model?") and the usage limit (`■ You've hit your usage limit … try
again at 2:34 AM.`). Herdr keeps showing the pane idle, and mail to it waits on
the composer check. The watcher reads the screen of every Codex pane that is not
`working` (`codex_stuck`, default on; `dry_run` logs `would report …` instead):

- The usage limit counts only as Codex's `■` error cell and only as the last
  paragraph above the composer, so the same words further up the transcript or
  quoted in a reply never match. On a later sighting than the first (20 s;
  5 minutes when focused) it logs `codex-limit` and notes it once to
  `limit_monitor` and to whoever waits on the pane: the senders of mail still
  queued for it and the last one who relayed it mail (the message archive), by
  session id like `deliver --re`, at most three; nobody on record: no note.
  Once the time on screen has passed (this machine's local time, its next
  occurrence after the first sighting; no time shown: 5 h later), one
  `Your Codex usage limit has reset. Continue the task you were on.` is queued
  through the mailbox (`codex-limit-continue`); the empty composer on that
  screen takes it. Right before it is typed in, the pane must still show the
  limit, the incident still be open and no dry run on, else it is dropped
  (`codex-continue-dropped`): someone may have gone on by hand. A note dropped
  while the incident is still open is queued again on a later tick
  (`codex-limit-continue-lost`). An unreadable screen proves nothing. The incident ends (`codex-limit-end`) when the screen no
  longer shows the limit, and a continue still queued then is taken back; a turn
  after the continue that hits the limit again is a new incident. State:
  `codex-stuck.json`, so a watcher restart repeats nothing. No account switch.
  A state that showed neither screen for 5 minutes is not read again until the
  pane's state changes (both screens open as the turn ends).
- A menu (an unreadable composer plus `enter select`, `esc back` or a selected
  `› N.` option on the bottom 20 lines) logs `codex-dialog` and notes the pane,
  its place and the selected option to `limit_monitor`, once per state. No key
  is ever pressed in a Codex dialog.

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
  partial output between cuts does not reset it; a cut tail that hides the
  chain's start or the turn's prompt counts as spent); then `mail_refusal`
  (draft, menu, status, journal). At most 4 notes per tick, oldest error
  first. With a 60 s poll the wait between ticks ends early once a note is due.
- In every typing path (the watcher's flush, its 5 s retries, a `deliver` that
  flushes the box) the note is checked again on fresh state: first every check
  above, with the config as it is now (`dry_run` too) and the pane still
  running that session; then `mail_refusal` (status, composer); then, right
  before typing, the transcript once more (the text is ready before the first
  check: nothing runs between the last look and typing). A note lives 2
  minutes, and only while the gate stays up without a break since it was
  queued. Only the watcher probes the API, so only the watcher types a note: a
  `deliver` in another process drops it and types its own message, which
  resumes the session anyway. The tender queues a new one while the stall is
  still due. Otherwise it is deleted (`auto-resume-dropped`), never typed.
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
   anything after `--`, options of unknown arity → refuse; *v0.1.56:*
   `--plugin-dir`, `--allowed-tools`/`--disallowed-tools` replay with one
   value, a second separate value refuses as positional input).
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

`python3 -m pytest tests/ -q` — 724 tests: the decision logic, the
sleep/wake state machine, pane identity and orphans, the watcher tick and
loop (idle clocks, backoff, server-down retries, the watcher lock), the
focus hook, the stub, the resume command and the re-claim after a restart
(the fake `herdr` has a `restart()`), the 3.9 TOML subset parser, and legacy
migration, insomnia claims and their expiry, staleness and damage, spawn
and close (and `close --self`), the opencode composer (fixtures of the 1.19.4 screens) and opencode lanes, the limit menu and one-time dialogs (fixtures of the 2.1.306 screens), auto-resume (its
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
