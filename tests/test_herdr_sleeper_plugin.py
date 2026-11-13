"""Unit tests for the herdr-sleeper plugin: pure decision logic plus the sleep/wake state
machine, the watcher tick, the focus hook, and the in-pane stub — against a fake `herdr`
(no Herdr, no Claude needed)."""

from __future__ import annotations

import json
import re
import os
import shlex
import signal
import stat
import time
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "herdr-sleeper"
sleeper = types.ModuleType("herdr_sleeper")
sleeper.__file__ = str(SCRIPT)  # a real interpreter sets __file__; exec() does not
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), sleeper.__dict__)

UUID = "20603f77-21a6-4a16-9f2b-77a87efbc665"
ORPHAN = "orphan:20603f77"
REAL_UUID_LIVE = sleeper.uuid_live_elsewhere  # captured before any fixture stubs it
REAL_HEAL = getattr(sleeper, "heal_watcher", None)


def agent(
    pane: str = "w1:p1",
    uuid: str | None = UUID,
    status: str = "idle",
    focused: bool = False,
    kind: str = "claude",
    name: str | None = "a",
    seq: int = 5,
    terminal: str = "term-1",
    cwd: str = "/tmp/proj",
) -> dict[str, Any]:
    a: dict[str, Any] = {"pane_id": pane, "agent_status": status, "focused": focused, "agent": kind,
                         "state_change_seq": seq, "cwd": cwd, "terminal_title_stripped": "Title",
                         "terminal_id": terminal, "workspace_id": "w1", "tab_id": "w1:t1"}
    if name:
        a["name"] = name
    if uuid:
        a["agent_session"] = {"value": uuid}
    return a


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawned `watch` or `deliver-pending` is a fresh interpreter: none of the monkeypatching applies,
    so it would use the real state dir, the real config and whatever HERDR_SOCKET_PATH the test run
    inherited (a live server). Tests that expect a spawn patch Popen themselves."""
    real = sleeper.subprocess.Popen

    def refuse(argv: Any, *a: Any, **k: Any) -> Any:
        if isinstance(argv, (list, tuple)) and str(sleeper.SELF) in [str(x) for x in argv]:
            pytest.fail(f"a unit test tried to start the plugin for real: {argv!r}")
        return real(argv, *a, **k)   # subprocess.run (ps, lsof) goes through Popen too
    monkeypatch.setattr(sleeper.subprocess, "Popen", refuse)


@pytest.fixture(autouse=True)
def no_real_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """claude-account's dir is the live one otherwise: without the launcher, no slot is active."""
    monkeypatch.setattr(sleeper, "ACCOUNTS_DIR", tmp_path / "claude-accounts")


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state dir + transcript root, with a transcript for UUID on disk."""
    d = tmp_path / "state"
    # every module-level path in the state dir, so a new state file can never reach the live one
    live = sleeper.STATE_DIR
    for name, base in list(vars(sleeper).items()):
        if isinstance(base, Path) and (base == live or base.parent == live):
            monkeypatch.setattr(sleeper, name, d / base.name if base != live else d)
    assert "SPAWNED" in vars(sleeper) and sleeper.SPAWNED.parent == d and sleeper.INSOMNIA.parent == d
    monkeypatch.setattr(sleeper, "LEGACY_STATE_DIR", tmp_path / "legacy")
    # Herdr's machine catalog lives outside the plugin dir: never read the live one
    monkeypatch.setattr(sleeper, "MACHINES_CATALOG", tmp_path / "herdr-client" / "endpoints.json")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", tmp_path / "absent.toml")
    monkeypatch.setattr(sleeper, "LEGACY_CONFIG_FILES", [tmp_path / "legacy-config.toml"], raising=False)
    # never spawn a real watcher from a unit test; the heal path has its own tests
    monkeypatch.setattr(sleeper, "heal_watcher", lambda: None, raising=False)
    if hasattr(sleeper, "STOP"):
        monkeypatch.setitem(sleeper.STOP, "requested", False)
    for name in ("RESTART_TRIED", "CRON_SCAN", "INSOMNIA_TOKENS", "DIALOG_SEEN"):   # per-process watcher memory
        monkeypatch.setattr(sleeper, name, {})
    for var in ("HERDR_PLUGIN_CONTEXT_JSON", "HERDR_PANE_ID", "HERDR_PLUGIN_EVENT_JSON"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    monkeypatch.setattr(sleeper, "TRANSCRIPTS", root)
    monkeypatch.setattr(sleeper, "ACCOUNT_CONFIG_DIRS", [])
    return tmp_path


def transcript(tmp_path: Path, uuid: str, age_hours: float, proj: str = "proj") -> Path:
    p = tmp_path / "projects" / proj / f"{uuid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n")
    stamp = time.time() - age_hours * 3600
    os.utime(p, (stamp, stamp))
    return p


EMPTY_SCREEN = "⏺ pong\n────────\n❯ \n────────\n  ◤ graft · 40 nodes\n"


class FakeHerdr:
    """Scripted `herdr` CLI: agents list, per-pane info, screen text, claims, and a call log."""

    def __init__(self, agents: list[dict[str, Any]], screen: str = EMPTY_SCREEN,
                 argv: list[str] | None = None) -> None:
        self.agents = agents
        self.screen: str | None = screen
        self.argv = ["--dangerously-skip-permissions"] if argv is None else argv
        self.calls: list[tuple[str, ...]] = []
        self.exit_leaves_agent = True      # a SIGTERM/prompt removes the agent from its pane
        self.start_uuid: str | None = None  # what agent start reports; None → the requested --resume uuid
        self.process_info_broken = False    # process-info returns an error envelope
        self.process_override: bool | None = None  # force process presence regardless of agent registration
        self.pidless = False              # report the agent process without a pid (argv still readable)
        self.stub_panes: set[str] = set()   # panes whose foreground is our wake stub
        self.stub_uuid = UUID               # the uuid argument our stub process carries
        self.stub_execs_on_enter = False    # Enter to a stub pane resumes the stub's session
        self.start_reports_session = True   # agent start answers with the session id (else it is learned later)
        self.start_session_appears = True   # ... and it then shows up in `pane get`
        self.taken_names: set[str] = set()  # agent names `agent start` rejects as taken
        self.empty_argv = False             # process-info lists the agent with no readable argv
        self.empty_foreground = False       # process-info answers with [] (Herdr could not see the job)
        self.signals: list[tuple[int, int]] = []
        self.helpers: dict[int, str] = {}   # pid -> name of the agent's children (MCP servers …)
        self.comm: str | None = None        # what ps reports for the agent pid (None: its kind)
        self.foreground: str | None = None  # a non-shell foreground in agent-less panes (e.g. "vim")
        self.execvps: list[tuple[str, list[str]]] = []
        self.resumes: dict[str, list[str]] = {}  # pane -> the resume command its claim reported (Herdr saves it)
        self.reject_resume: str | None = None    # error code a report-agent carrying a resume command gets
        self.spawn_uuid = "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"  # what a fresh `agent start` (no --resume) reports
        self.start_error: str | None = None      # error code `agent start` fails with
        self.start_errors: list[str] = []        # codes the next `agent start` calls fail with, one each
        self.tabs_made = 0
        self.tab_labels: dict[str, str] = {}        # tab id -> the label `tab list` reports
        self.workspace_labels: dict[str, str] = {}  # workspace id -> the label `workspace list` reports

    def restart(self, keep_resume: bool = True) -> None:
        """A Herdr server restart: every terminal gets a new id, saved labels come back, claims (hook
        authority) do not, and no process survives. A pane whose claim reported a resume command
        gets it typed back into its shell — our stub; every other slept pane is a bare shell.
        Live agents stand for natively resumed sessions and stay. keep_resume=False: an older
        Herdr that saves no resume commands."""
        if not keep_resume:
            self.resumes.clear()
        for a in self.agents:
            a["terminal_id"] = f"{a.get('terminal_id')}-r"
            if a.get("agent") == sleeper.CLAIM_AGENT:
                a.pop("agent", None)
        self.stub_panes = {p for p, argv in self.resumes.items() if "stub" in argv}

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if pid in self.helpers:
            self.helpers.pop(pid)
            return
        if self.exit_leaves_agent and sig == signal.SIGTERM:
            for a in self.agents:
                if a["pane_id"] in {p for p in self.stub_panes} or True:
                    if a.get("agent") and a.get("pid_hint") == pid:
                        a.pop("agent", None)
                        a.pop("agent_session", None)
            # the fake's processes carry pid 1 for the agent; drop any pane whose agent had no explicit pid
            for a in self.agents:
                if a.get("agent") and a.get("pid_hint", 101) == pid:
                    a.pop("agent", None)
                    a.pop("agent_session", None)

    def process_table(self) -> dict[int, tuple[int, str]]:
        table: dict[int, tuple[int, str]] = {50: (1, "zsh")}
        live = set()
        for a in self.agents:
            if a.get("agent") and a.get("agent") != sleeper.CLAIM_AGENT:
                pid = a.get("pid_hint", 101)
                live.add(pid)
                table[pid] = (50, self.comm or a["agent"])
        for pid, comm in self.helpers.items():
            table[pid] = (101 if 101 in live else 1, comm)
        return table

    def _procs(self, pane: str, a: dict[str, Any] | None) -> list[dict[str, Any]]:
        if pane in self.stub_panes:
            return [{"argv0": "python3", "name": "python3",
                     "argv": ["python3", str(SCRIPT), "stub", pane, self.stub_uuid], "pid": 99}]
        if self.empty_foreground:
            return []
        present = (a is not None and bool(a.get("agent"))) if self.process_override is None else self.process_override
        if not present:
            if self.foreground:
                return [{"argv0": self.foreground, "name": self.foreground, "argv": [self.foreground], "pid": 60}]
            return [{"argv0": "zsh", "name": "zsh", "argv": ["-zsh"], "pid": 50}]   # an idle pane lists its shell
        kind = (a or {}).get("agent") or "claude"
        proc = {"argv0": kind, "name": kind, "argv": [] if self.empty_argv else [kind, *self.argv]}
        if not self.pidless:
            proc["pid"] = a.get("pid_hint", 101)
        return [proc]

    def __call__(self, *args: str, check: bool = True, timeout: float = 0) -> dict[str, Any]:
        self.calls.append(args)
        by_pane = {a["pane_id"]: a for a in self.agents}
        if args[:2] == ("agent", "list"):
            return {"agents": self.agents}
        if args[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": a["pane_id"], "terminal_id": a.get("terminal_id"),
                               "cwd": a.get("cwd"), "label": a.get("label")} for a in self.agents]}
        if args[:2] == ("tab", "list"):
            return {"tabs": [{"tab_id": k, "label": v, "number": 1} for k, v in self.tab_labels.items()]}
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": k, "label": v, "number": 1}
                                   for k, v in self.workspace_labels.items()]}
        if args[:2] == ("agent", "get"):
            a = by_pane.get(args[2])
            return {"agent": a} if a else {}
        if args[:2] == ("pane", "get"):
            a = by_pane.get(args[2])
            if a is None:
                if check:
                    raise sleeper.SleeperError("no such pane")
                return {}
            pane = {"pane_id": args[2], "label": a.get("label"), "cwd": a.get("cwd"),
                    "terminal_id": a.get("terminal_id"), "tab_id": a.get("tab_id")}
            if a.get("agent"):
                pane["agent"] = a["agent"]
                pane["agent_session"] = a.get("agent_session")
            return {"pane": pane}
        if args[:2] == ("pane", "process-info"):
            pane = args[args.index("--pane") + 1]
            if self.process_info_broken:
                return {}
            return {"process_info": {"foreground_processes": self._procs(pane, by_pane.get(pane))}}
        if args[:2] == ("tab", "create"):
            self.tabs_made += 1
            ws = args[args.index("--workspace") + 1] if "--workspace" in args else "w1"
            n = 7 + self.tabs_made
            row = {"pane_id": f"{ws}:p{n}", "tab_id": f"{ws}:t{n}", "workspace_id": ws, "focused": False,
                   "cwd": args[args.index("--cwd") + 1], "terminal_id": f"term-new-{n}", "state_change_seq": 1,
                   "label": args[args.index("--label") + 1]}
            self.agents.append(row)
            return {"root_pane": dict(row), "tab": {"tab_id": row["tab_id"], "workspace_id": ws, "pane_count": 1}}
        if args[:2] == ("tab", "get"):
            return {"tab": {"tab_id": args[2], "pane_count": sum(a.get("tab_id") == args[2] for a in self.agents)}}
        if args[:2] in (("tab", "close"), ("pane", "close")):
            field = "tab_id" if args[0] == "tab" else "pane_id"
            for a in self.agents:
                if a.get(field) == args[2]:
                    self.resumes.pop(a["pane_id"], None)   # a closed pane is not restored, nor its command
            self.agents = [a for a in self.agents if a.get(field) != args[2]]
            return {}
        if args[:2] == ("agent", "start"):
            pane = args[args.index("--pane") + 1]
            if args[2] in self.taken_names:
                raise sleeper.SleeperError(f"herdr agent start failed: {{'code': 'agent_name_taken', 'name': '{args[2]}'}}")
            if self.start_errors:
                code = self.start_errors.pop(0)
                raise sleeper.HerdrError(f"herdr agent start failed: {{'code': '{code}'}}", code)
            if self.start_error:
                raise sleeper.HerdrError(f"herdr agent start failed: {{'code': '{self.start_error}'}}", self.start_error)
            uuid = self.start_uuid or (args[args.index("--resume") + 1] if "--resume" in args else self.spawn_uuid)
            kind = args[args.index("--kind") + 1]
            a = by_pane[pane]
            if "--resume" not in args:
                a["name"] = args[2]
                a["agent_status"] = "idle"
            a.update(agent=kind, agent_session={"value": uuid} if self.start_session_appears else None)
            if not self.start_session_appears:
                a.pop("agent_session")
            a["started_name"] = args[2]
            self.stub_panes.discard(pane)
            return {"agent": {"agent_session": {"value": uuid}}} if self.start_reports_session else {"agent": {}}
        if args[:2] == ("agent", "rename"):
            by_pane[args[2]]["name"] = args[3]
            return {"agent": {"name": args[3]}}
        if args[:2] == ("pane", "report-agent"):
            resume = list(args[args.index("--") + 1:]) if "--" in args else None
            if resume is not None and self.reject_resume:
                if check:
                    raise sleeper.HerdrError(f"herdr pane report-agent failed: {{'code': '{self.reject_resume}'}}",
                                             self.reject_resume)
                return {}
            by_pane[args[2]]["agent"] = args[args.index("--agent") + 1] if "--agent" in args else sleeper.CLAIM_AGENT
            if args[args.index("--agent") + 1:][:1] == [sleeper.ASK_AGENT]:
                by_pane[args[2]]["agent_status"] = args[args.index("--state") + 1]
            if resume is not None:
                self.resumes[args[2]] = resume
            return {}
        if args[:2] == ("pane", "release-agent"):
            a = by_pane.get(args[2])
            if a is None:   # a closed pane: real herdr answers an error (callers pass check=False)
                return {}
            if a.get("agent") == sleeper.ASK_AGENT:
                a.pop("agent", None)
            if a.get("agent") == sleeper.CLAIM_AGENT:
                # Herdr forgets the releasing reporter's resume command, but only from a holder: a
                # release with no claim (none survives a restart) leaves the saved command (lab, v0.9.5)
                self.resumes.pop(args[2], None)
                a.pop("agent", None)
            return {}
        if args[:2] in (("pane", "run"), ("pane", "rename"), ("pane", "report-metadata"),
                        ("pane", "send-keys"), ("notification", "show"), ("agent", "prompt")):
            if args[:2] == ("pane", "rename"):
                assert len(args) > 3, "bare `pane rename <pane>` is a usage error in real herdr"
                by_pane[args[2]]["label"] = None if args[3] == "--clear" else args[3]
            if args[:2] == ("pane", "send-keys") and args[2] in self.stub_panes and self.stub_execs_on_enter:
                by_pane[args[2]].update(agent="claude", agent_session={"value": self.stub_uuid})
                self.stub_panes.discard(args[2])
            return {}
        raise AssertionError(f"unexpected herdr call {args}")


@pytest.fixture
def fake(state: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHerdr:
    f = FakeHerdr([agent()])
    monkeypatch.setattr(sleeper, "herdr", f)
    monkeypatch.setattr(sleeper, "herdr_screen", lambda pane: f.screen)
    monkeypatch.setattr(sleeper, "herdr_matched_rule", lambda pane: None)  # never a real `herdr agent explain`
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: None)
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(sleeper, "EXIT_WAIT_SECONDS", 2)
    monkeypatch.setattr(sleeper.os, "kill", f.kill)
    monkeypatch.setattr(sleeper, "process_table", f.process_table)
    monkeypatch.setattr(sleeper, "memory_pressure", lambda: 1)
    transcript(state, UUID, 30)
    return f


def journal() -> dict[str, Any]:
    return sleeper.read_json(sleeper.JOURNAL)


def events() -> list[str]:
    p = sleeper.EVENTS
    return [json.loads(l)["event"] for l in p.read_text().splitlines()] if p.exists() else []


def stub_run(fake: FakeHerdr) -> str:
    return next(c[3] for c in fake.calls if c[:2] == ("pane", "run"))


# ------------------------------------------------------------- pure helpers

def test_replayable_argv_strips_session_selectors() -> None:
    ok = sleeper.replayable_argv
    assert ok(["--dangerously-skip-permissions", "--resume", "abc"]) == (["--dangerously-skip-permissions"], None)
    assert ok(["-r", "abc", "--model", "opus"]) == (["--model", "opus"], None)
    assert ok(["--resume=abc", "-c", "--continue"]) == ([], None)
    assert ok(["--resume", "--model", "opus"]) == (["--model", "opus"], None)  # bare --resume opens a picker
    assert ok([]) == ([], None)
    assert ok(["--add-dir", "/x", "--verbose"]) == (["--add-dir", "/x", "--verbose"], None)
    assert ok(["--dangerously-skip-permissions", "-n", "ts-x"]) == (["--dangerously-skip-permissions", "-n", "ts-x"], None)
    assert ok(["--name=ts-x"]) == (["--name=ts-x"], None)
    assert ok(["--unknown-flag", "value"])[0] is None and "unrecognised" in ok(["--unknown-flag", "value"])[1]
    assert ok(["--model"])[0] is None  # value option missing its value
    # every option our start paths pass (herdr-sleeper spawn, Herdr panes, chrome-agents)
    assert ok(["--dangerously-skip-permissions", "--chrome", "-n", "chrome-agents"])[1] is None
    assert ok(["--permission-mode", "auto", "--effort", "high", "--no-chrome"]) == \
        (["--permission-mode", "auto", "--effort", "high", "--no-chrome"], None)


@pytest.mark.parametrize(
    "args, needle",
    [
        (["--fork-session"], "--fork-session"),
        (["-p", "hi"], "-p"),
        (["--session-id", "x"], "--session-id"),
        (["--verbose", "publish the release"], "positional"),
        (["--dangerously-skip-permissions", "--", "x"], "`--`"),
    ],
)
def test_replayable_argv_refuses_dangerous(args: list[str], needle: str) -> None:
    argv, why = sleeper.replayable_argv(args)
    assert argv is None and why and needle in why


def test_replayable_argv_reasons_never_echo_values() -> None:
    for args in (["--api-key=sk-secret-1"], ["--print=sk-secret-1"], ["--verbose", "sk-secret-1 do the thing"]):
        argv, why = sleeper.replayable_argv(args)
        assert argv is None and why and "sk-secret-1" not in why
    assert sleeper.replayable_argv(["--env", "TOKEN=sk-secret-1"])[0] is None  # values here were secrets


def test_composer_detail_never_carries_the_draft() -> None:
    for composer in ("my password is hunter2", ""):
        screen = CLAUDE_SCREEN.format(composer=composer)
        if not composer:
            screen = screen.replace("────────\n  ◤", "  hunter2 on the second line\n────────\n  ◤", 1)
        state, detail = sleeper.composer_state(screen)
        assert state == "draft" and "hunter2" not in detail


def test_session_id_shape() -> None:
    ok = sleeper.valid_session_id
    assert ok(UUID) and ok("ses_3f9a1bc2") and ok("u")
    for bad in ("", "../x", "a/b", "*", "a b", "-r", "x;y", None, 5, "a" * 129):
        assert not ok(bad)


def test_parse_duration() -> None:
    assert sleeper.parse_duration("12h") == 12
    assert sleeper.parse_duration("90m") == 1.5
    assert sleeper.parse_duration("1d") == 24
    assert sleeper.parse_duration("3600s") == 1
    assert sleeper.parse_duration("2") == 2
    with pytest.raises(sleeper.ConfigError):
        sleeper.parse_duration("soon")


CLAUDE_SCREEN = """
❯ earlier question
⏺ pong
✻ Worked for 1s · done 5:23 PM
────────
❯ {composer}
────────
  ◤ graft · 40 nodes / 63 edges · ✓ synced
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""


def test_composer_state_fails_closed() -> None:
    cs = sleeper.composer_state
    assert cs(CLAUDE_SCREEN.format(composer=""))[0] == "empty"
    assert cs(CLAUDE_SCREEN.format(composer="half-typed thought")) == (
        "draft", "text on the prompt line (a draft or an open question menu), 18 chars")
    multiline = CLAUDE_SCREEN.format(composer="") .replace("────────\n  ◤", "  second line of a draft\n────────\n  ◤", 1)
    assert cs(multiline) == ("draft", "text below the prompt line (a multi-line draft or an open menu), 1 line")
    menu = CLAUDE_SCREEN.format(composer="1. Yes, deploy").replace(
        "────────\n  ◤", "  2. No\n  3. Type something\n────────\n  ◤", 1)
    assert cs(menu) == ("draft", "text on the prompt line (a draft or an open question menu), 14 chars")
    assert cs("❯ \n  ?\n────────\n")[0] == "draft"          # one-character draft is still a draft
    assert cs("❯ \n")[0] == "unknown"                    # bare glyph, nothing rendered below it
    assert cs(None)[0] == "unknown"                       # unreadable screen
    assert cs("djbclark@mac:~$ \n")[0] == "unknown"       # bare shell: no composer to vouch for
    assert cs("⏺ done\n✻ Worked\n")[0] == "unknown"       # transcript visible, composer scrolled off
    assert cs("⏺ done\n> quoted line\n")[0] == "unknown"  # a bare ">" is not the composer
    assert cs("❯ \n─── a rule I am drafting\n────────\n  ⏵⏵ bypass\n")[0] == "draft"


def test_manual_command_is_shell_safe() -> None:
    cmd = sleeper.manual_command({"cwd": "/tmp/my dir", "argv": ["--model", "it's"], "uuid": "u", "kind": "claude"})
    assert cmd == "cd '/tmp/my dir' && claude --model 'it'\"'\"'s' --resume u"


def test_manual_command_never_prints_refused_argv() -> None:
    cmd = sleeper.manual_command({"cwd": "/p", "argv": ["--fork-session", "--model", "x"], "uuid": "u", "kind": "claude"})
    assert cmd == "cd /p && claude --resume u"


def test_wake_name_falls_back_to_pane() -> None:
    assert sleeper.wake_name({"name": None, "pane_id": "w26:p2"}) == "wake-w26-p2"
    assert sleeper.wake_name({"name": "lichess", "pane_id": "w24:p1"}) == "lichess"


# ---------------------------------------------------------------- config

def test_load_config_precedence(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    for var in ("HERDR_SLEEPER_IDLE_HOURS", "HERDR_SLEEPER_IDLE", "HERDR_SLEEPER_EXCLUDE",
                "HERDR_SLEEPER_AGENTS", "HERDR_SLEEPER_POLL_SECONDS", "HERDR_SLEEPER_NOTIFY",
                "HERDR_SLEEPER_WAKE_ON_FOCUS", "HERDR_SLEEPER_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    values, source = sleeper.load_config()
    assert values == {"idle_hours": 12.0, "agents": ["claude", "opencode"], "exclude": [],
                      "poll_seconds": 60, "notify": True, "wake_on_focus": True, "dry_run": False,
                      "watcher": "plugin", "max_live": 0, "pressure_idle_hours": 0.5, "wake_refuse_from": [],
                      "restart_outdated": False, "machine": "", "machine_aliases": [],
                      "answer_dialogs": True, "limit_monitor": ""}
    assert set(source.values()) == {"default"}

    cfg.write_text('idle = "90m"\nexclude = "orc"\npoll_seconds = 30\nnotify = false\n')
    values, source = sleeper.load_config()
    assert values["idle_hours"] == 1.5 and values["exclude"] == ["orc"]   # a string is one name, not letters
    assert values["poll_seconds"] == 30 and values["notify"] is False
    assert source["idle_hours"] == str(cfg)

    monkeypatch.setenv("HERDR_SLEEPER_IDLE", "2d")
    monkeypatch.setenv("HERDR_SLEEPER_EXCLUDE", "a b")
    values, source = sleeper.load_config()
    assert values["idle_hours"] == 48 and values["exclude"] == ["a", "b"]
    assert source["idle_hours"] == "$HERDR_SLEEPER_IDLE"


@pytest.mark.parametrize(
    "text",
    ["idle_hours = = 3\n", "idle_hours = nan\n", "idle_hours = -1\n", "exclude = [1, 2]\n", "poll_seconds = 0\n",
     "bogus = 1\n", "idle_hours = false\n", "poll_seconds = true\n", 'agents = ["codex"]\n', "notify = 1\n"],
)
def test_load_config_rejects_bad_values(state: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    cfg = state / "config.toml"
    cfg.write_text(text)
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_env_alias_conflict_and_units(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "CONFIG_FILE", state / "absent.toml")
    monkeypatch.setenv("HERDR_SLEEPER_IDLE_HOURS", "90m")
    monkeypatch.delenv("HERDR_SLEEPER_IDLE", raising=False)
    assert sleeper.load_config()[0]["idle_hours"] == 1.5
    monkeypatch.setenv("HERDR_SLEEPER_IDLE", "2h")
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_cli_exclude_adds_to_config_exclude(fake: FakeHerdr, state: Path,
                                            monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = state / "config.toml"
    cfg.write_text('exclude = ["a"]\nidle_hours = 0.0\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    assert sleeper.main(["scan", "--exclude", "zzz"]) == 0
    assert "excluded" in capsys.readouterr().out and journal() == {}


def test_scan_refuses_with_broken_config(state: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    cfg = state / "config.toml"
    cfg.write_text("idle_hours = = 3\n")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    assert sleeper.main(["scan"]) == 2
    assert "broken config" in capsys.readouterr().err


# ------------------------------------------------------------- assessment

def test_assess_sleeps_when_window_elapsed(state: Path) -> None:
    transcript(state, UUID, 30)
    since = time.time() - 13 * 3600
    ok, _reason, idle = sleeper.assess(agent(), [agent()], 12, {}, idle_since=since)
    assert ok and idle is not None and idle > 12


def test_transcript_for_picks_newest_copy(state: Path) -> None:
    old = transcript(state, UUID, 40, proj="older")
    new = transcript(state, UUID, 0.1, proj="newer")
    assert sleeper.transcript_for(UUID) == new and sleeper.transcript_for(UUID) != old


def test_transcript_for_finds_other_account_slots(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-11-10 (mini2): gpu-dev ran on slot b (~/.claude-accounts/b), the caller on slot a;
    # deliver gpu-dev failed although ListAgents named it, because the name lives in slot b's transcript.
    slot_b = state / "accounts" / "b"
    (slot_b / "projects" / "proj").mkdir(parents=True)
    other = slot_b / "projects" / "proj" / "other-slot-uuid.jsonl"
    other.write_text('{"type":"agent-name","agentName":"gpu-dev"}\n')
    monkeypatch.setattr(sleeper, "ACCOUNT_CONFIG_DIRS", [slot_b])
    assert sleeper.transcript_for("other-slot-uuid") == other
    assert sleeper.claude_session_name(other) == "gpu-dev"


def test_assess_requires_transcript_for_claude(state: Path) -> None:
    ok, reason, _ = sleeper.assess(agent(), [agent()], 12, {}, idle_since=time.time() - 100 * 3600)
    assert not ok and "no transcript" in reason


def test_assess_opencode_needs_no_transcript(state: Path) -> None:
    since = time.time() - 13 * 3600
    a = agent(kind="opencode", uuid="sess-1")
    ok, _reason, _ = sleeper.assess(a, [a], 12, {}, idle_since=since)
    assert ok


@pytest.mark.parametrize(
    "kw, needle",
    [
        ({"status": "working"}, "status working"),
        ({"status": "blocked"}, "status blocked"),
        ({"focused": True}, "focused"),
        ({"focused": None}, "focus unknown"),
        ({"uuid": None}, "no session uuid"),
        ({"kind": "codex"}, "not supported"),
    ],
)
def test_assess_rejections(state: Path, kw: dict[str, Any], needle: str) -> None:
    transcript(state, UUID, 30)
    a = agent(**kw)
    ok, reason, _ = sleeper.assess(a, [a], 12, {}, idle_since=time.time() - 100 * 3600)
    assert not ok and needle in reason


def test_assess_scan_rows_and_gates(state: Path) -> None:
    transcript(state, UUID, 30)
    a, b = agent(pane="w1:p1"), agent(pane="w2:p1", status="working")
    assert "w2:p1" in sleeper.assess(a, [a, b], 12, {})[1]
    assert sleeper.assess(a, [a], 12, {}, {"a"})[1] == "excluded"
    assert sleeper.assess(a, [a], 12, {}, {"w1:p1"})[1] == "excluded"
    unnamed = agent(pane="w1:p1", name=None)
    assert sleeper.assess(unnamed, [unnamed], 12, {}, {"Title"})[1] == "excluded"
    transcript(state, UUID, 30).write_text(json.dumps({"type": "agent-name", "agentName": "keeper"}) + "\n")
    assert sleeper.assess(unnamed, [unnamed], 12, {}, {"keeper"})[1] == "excluded"
    assert sleeper.assess(unnamed, [unnamed], 12, {}, {"other"})[1] != "excluded"
    transcript(state, UUID, 30)
    assert "journal" in sleeper.assess(a, [a], 12, {"w1:p1": {"phase": "asleep"}})[1]
    assert "no transcript" in sleeper.assess(agent(uuid="other"), [agent(uuid="other")], 12, {})[1]


# ------------------------------------------------------ sleep state machine

def test_sleep_happy_path_signals_and_leaves_stub(fake: FakeHerdr) -> None:
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert slept and outcome == "slept"
    assert fake.signals == [(101, signal.SIGTERM)]                   # the exit is a signal, not a typed command
    j = journal()["w1:p1"]
    assert j["uuid"] == UUID and j["kind"] == "claude" and j["phase"] == "asleep"
    assert j["argv"] == ["--dangerously-skip-permissions"] and j["terminal_id"] == "term-1"
    assert ("agent", "prompt", "w1:p1", "/exit") not in fake.calls
    run = stub_run(fake)
    assert str(SCRIPT) in run and "stub" in run and "w1:p1" in run and UUID in run
    assert "exec " not in run                                        # the pane's shell must survive beneath the stub
    assert any(c[:2] == ("pane", "rename") and c[3].startswith("💤") for c in fake.calls)
    assert any(c[:2] == ("pane", "report-agent") for c in fake.calls)  # sidebar claim
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    assert any(c[:2] == ("notification", "show") for c in fake.calls)
    assert events() == ["slept"]


def test_sleep_keeps_a_draft(fake: FakeHerdr) -> None:
    """SIGTERM would discard a draft: unsent work. Claude panes with one stay awake."""
    fake.screen = CLAUDE_SCREEN.format(composer="unsent")
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "composer draft" in outcome and fake.signals == []
    assert "w1:p1" not in journal()


def test_sleep_ignores_the_dim_prompt_suggestion(fake: FakeHerdr) -> None:
    # Claude Code 2.1.306 as Herdr renders it with --format ansi: the suggestion is SGR 2
    fake.screen = ("⏺ done\n❯\xa0\x1b[0m\x1b[2mcommit the status file\x1b[0m\n" + "─" * 40
                   + "\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · install gh for PR status\n")
    assert sleeper.composer_state(fake.screen)[0] == "empty"
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]


@pytest.mark.parametrize("footer, needle", [
    ("  ⏵⏵ bypass permissions on · 1 shell", "1 shell"),
    ("  ⏵⏵ bypass permissions on · 2 monitors, 1 shell", "2 monitors, 1 shell"),
    ("  ◯ general-purpose  Reading files  18s · ↓ 140.1k tokens", "subagent"),
])
def test_sleep_waits_for_background_work(fake: FakeHerdr, footer: str, needle: str) -> None:
    fake.screen = "⏺ started\n❯ \n" + "─" * 40 + "\n" + footer + "\n"
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and needle in outcome and fake.signals == []


def test_background_counts_only_below_the_last_rule() -> None:
    screen = "⏺ 1 shell\n❯ 2 monitors\n" + "─" * 20 + "\n  ⏵⏵ bypass permissions on\n"
    assert sleeper.background_work(screen) is None


def test_sleep_waits_while_a_shell_runs_under_the_agent(fake: FakeHerdr) -> None:
    fake.helpers = {300: "zsh", 301: "python"}   # a run_in_background Bash task, an MCP server
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "shell process" in outcome and fake.signals == []


def _line(**rec: Any) -> str:
    return json.dumps(rec) + "\n"


def _tool(name: str, at: str, **inp: Any) -> str:
    return _line(type="assistant", timestamp=at,
                 message={"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]})


def _prompt(at: str) -> str:
    return _line(type="user", timestamp=at, message={"role": "user", "content": "go on"})


def _iso(seconds_ago: float) -> str:
    return sleeper.dt.datetime.fromtimestamp(time.time() - seconds_ago, sleeper.dt.timezone.utc).isoformat()


def test_scheduled_work_from_the_transcript(fake: FakeHerdr, state: Path) -> None:
    path = transcript(state, UUID, 0)
    path.write_text(_prompt(_iso(900)) + _tool("ScheduleWakeup", _iso(600), delaySeconds=1200))
    assert sleeper.scheduled_work(UUID) == "a scheduled wakeup is pending"
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "wakeup is pending" in outcome
    path.write_text(_tool("ScheduleWakeup", _iso(600), delaySeconds=60) + _prompt(_iso(500)))
    assert sleeper.scheduled_work(UUID) is None                          # it fired; the loop did not re-arm
    path.write_text(_tool("ScheduleWakeup", _iso(900), delaySeconds=60) + _tool("ScheduleWakeup", _iso(600), stop=True))
    assert sleeper.scheduled_work(UUID) is None                          # stopped
    path.write_text(_tool("ScheduleWakeup", _iso(3 * 3600), delaySeconds=60))
    assert sleeper.scheduled_work(UUID) is None                          # spent long ago
    path.write_text(_tool("CronCreate", _iso(3600)) + _tool("CronCreate", _iso(3000)) + _tool("CronDelete", _iso(60)))
    assert sleeper.scheduled_work(UUID) == "1 session cron job(s) scheduled"
    path.write_text(_tool("CronCreate", _iso(8 * 86400)))
    assert sleeper.scheduled_work(UUID) is None                          # expired


def test_sleep_refuses_when_state_moved_since_scan(fake: FakeHerdr) -> None:
    fake.agents[0]["state_change_seq"] = 6  # scan saw 5
    slept, outcome = sleeper.sleep_agent(agent(seq=5), 12, set(), dry_run=False)
    assert not slept and "state changed" in outcome
    assert not fake.signals and journal() == {}


def test_sleep_refuses_unreplayable_argv(fake: FakeHerdr) -> None:
    fake.argv = ["--fork-session"]
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "--fork-session" in outcome and journal() == {} and not fake.signals


def test_sleep_refuses_when_process_unidentifiable(fake: FakeHerdr) -> None:
    fake.pidless = True  # process-info answers, argv is readable, but no pid to signal
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "not identifiable" in outcome and journal() == {} and not fake.signals


def test_dry_run_runs_every_check_but_acts_on_nothing(fake: FakeHerdr) -> None:
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=True)
    assert not slept and outcome.startswith("would sleep")
    assert not fake.signals and journal() == {}
    fake.agents[0]["state_change_seq"] = 6
    assert "state changed" in sleeper.sleep_agent(agent(seq=5), 12, set(), dry_run=True)[1]


def test_exit_timeout_keeps_handle_as_exit_requested(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False  # SIGTERM delivered but the agent lingers
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "uncertain" in outcome
    assert journal()["w1:p1"]["phase"] == "exit-requested"
    assert events() == ["exit-uncertain"]


def test_exit_with_unreadable_process_info_stays_uncertain(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    # process-info works until the signal is sent, then becomes unreadable: Herdr drops the agent
    # but we cannot positively see the process go -> exit-requested, no pane run, handle kept
    real_kill = fake.kill

    def flaky(pid: int, sig: int) -> None:
        real_kill(pid, sig)
        fake.process_info_broken = True

    monkeypatch.setattr(sleeper.os, "kill", flaky)
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "uncertain" in outcome
    assert journal()["w1:p1"]["phase"] == "exit-requested"
    assert not any(c[:2] == ("pane", "run") for c in fake.calls)


def test_reconcile_settles_exit_requested(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    # case 1: the agent is still there next tick → first sighting only counts, second drops
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["seen_running"] == 1
    sleeper.reconcile(fake.agents)
    assert journal() == {}
    # case 2: it did leave after the timeout → becomes asleep
    fake.exit_leaves_agent = False
    fake.agents = [agent()]
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "asleep"


def test_reconcile_drops_entry_when_pane_runs_another_session(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].update(agent="claude", agent_session={"value": "other"})
    sleeper.reconcile(fake.agents)
    assert journal() == {} and events()[-1] == "reconciled"


# ------------------------------------------------------- wake state machine

def slept_entry(fake: FakeHerdr) -> dict[str, Any]:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    return journal()["w1:p1"]


def test_wake_happy_path_restores_label(fake: FakeHerdr) -> None:
    fake.agents[0]["label"] = "mylabel"
    entry = slept_entry(fake)
    assert fake.agents[0]["label"] == "💤 mylabel"
    assert sleeper.wake_entry(entry)
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert start[-2:] == ("--resume", UUID) and "--dangerously-skip-permissions" in start
    assert any(c[:2] == ("pane", "release-agent") for c in fake.calls)   # claim dropped before the start
    assert fake.agents[0]["label"] == "mylabel" and fake.agents[0]["agent"] == "claude"
    assert journal() == {} and events()[-1] == "woke"


def test_wake_clears_label_that_did_not_exist_before_sleep(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    assert fake.agents[0]["label"] == "💤 Title" and entry["label"] is None
    assert sleeper.wake_entry(entry)
    assert fake.agents[0]["label"] is None


def test_wake_filters_snapshot_argv(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    entry["argv"] = ["--fork-session"]  # a raw snapshot argv that sleep never vetted
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_read_json_rejects_non_object_root(state: Path) -> None:
    sleeper.STATE_DIR.mkdir(parents=True)
    sleeper.JOURNAL.write_text("[]\n")
    with pytest.raises(sleeper.DamagedState):
        sleeper.read_json(sleeper.JOURNAL)


def test_wake_quarantines_damaged_journal_before_snapshot_recovery(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    sleeper.JOURNAL.write_text("[]\n")
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 0
    damaged = list(sleeper.STATE_DIR.glob("sleeping.json.damaged-*"))
    assert len(damaged) == 1 and damaged[0].read_text() == "[]\n"
    assert "damaged journal moved to" in capsys.readouterr().err


def test_reconcile_needs_a_real_process_before_dropping(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.process_override = False  # agent list says present, process-info says no process
    sleeper.reconcile(fake.agents); sleeper.reconcile(fake.agents); sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "exit-requested" and "seen_running" not in journal()["w1:p1"]


def test_reconcile_sightings_must_be_consecutive(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    sleeper.reconcile(fake.agents)                      # True
    fake.process_override = None; fake.process_info_broken = True
    sleeper.reconcile(fake.agents)                      # unknown -> streak resets
    fake.process_info_broken = False
    sleeper.reconcile(fake.agents)                      # True again: only one in a row
    assert "w1:p1" in journal()


def test_displaced_session_survives_reconcile(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.start_uuid = "fresh-uuid"
    assert not sleeper.wake_entry(entry)                # pane now runs fresh-uuid; entry kept with wake_got
    sleeper.merge_snapshot(fake.agents)
    sleeper.reconcile(fake.agents)
    j = journal()["w1:p1"]
    assert j["uuid"] == UUID and j["phase"] == "displaced" and j["argv"] == ["--dangerously-skip-permissions"]
    assert not sleeper.wake_entry(j)                    # manual-only from here


def test_wake_after_server_restart_wakes_the_restored_pane(fake: FakeHerdr) -> None:
    """A1: every Herdr restart re-allocates terminal ids; the restored pane keeps its 💤 label
    (restore re-applies manual labels), so same pane id + 💤 label + bare shell is the same pane."""
    entry = slept_entry(fake)
    fake.agents[0]["terminal_id"] = "term-2"   # restored by a restarted server: new terminal, same pane, same cwd
    fake.agents[0].pop("agent", None)          # restart cleared our claim; a bare shell is left
    assert fake.agents[0]["label"].startswith(sleeper.SLEEP_MARK)
    assert sleeper.wake_entry(entry) and journal() == {}
    assert "identity re-established" in sleeper.LOGFILE.read_text()


def test_restamp_is_written_only_when_the_wake_succeeds(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: a refused wake must not leave the new terminal id on record (the same-terminal drop rule
    would then fire on a terminal that was never proven ours)."""
    entry = slept_entry(fake)
    fake.agents[0]["terminal_id"] = "term-2"
    fake.agents[0].pop("agent", None)
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "pid 42")
    assert not sleeper.wake_entry(entry)
    assert journal()["w1:p1"]["terminal_id"] == "term-1"


def test_new_terminal_without_the_sleep_label_is_reuse(fake: FakeHerdr) -> None:
    """F1: a fresh pane that got a recycled pane id (workspace ids restart at max(restored)+1) in the
    same cwd carries no 💤 label: orphan, never resume into it."""
    entry = slept_entry(fake)
    fake.agents[0].update(terminal_id="term-2", label=None)   # same cwd, new terminal, no label
    fake.agents[0].pop("agent", None)
    assert not sleeper.wake_entry(entry)
    assert ORPHAN in journal() and not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_restored_pane_moved_to_another_cwd_is_refused_not_orphaned(fake: FakeHerdr,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """F1: cwd is a wake precondition, not identity evidence: a restored pane cd'd elsewhere says "cd back"."""
    entry = slept_entry(fake)
    fake.agents[0].update(terminal_id="term-2", cwd="/somewhere/else")   # 💤 label kept by restore
    fake.agents[0].pop("agent", None)
    assert not sleeper.wake_entry(entry)
    sleeper.reconcile(fake.agents)
    assert "w1:p1" in journal() and "cd back" in sleeper.LOGFILE.read_text()


def test_legacy_entry_without_terminal_id_needs_the_sleep_label(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    j = journal(); j["w1:p1"]["terminal_id"] = None; sleeper.write_json(sleeper.JOURNAL, j)
    fake.agents[0].pop("agent", None)
    fake.agents[0]["label"] = "something the user set"
    assert not sleeper.wake_entry({**entry, "terminal_id": None})
    assert ORPHAN in journal()
    fake.agents[0]["label"] = "💤 Title"
    j = journal(); j["w1:p1"] = {**j.pop(ORPHAN), "phase": "asleep"}; sleeper.write_json(sleeper.JOURNAL, j)
    assert sleeper.wake_entry(journal()["w1:p1"]) and journal() == {}


def test_wake_after_restart_compares_cwd_by_realpath(fake: FakeHerdr, state: Path) -> None:
    real = state / "real"; real.mkdir()
    link = state / "link"; link.symlink_to(real)
    fake.agents[0]["cwd"] = str(real)
    sleeper.sleep_agent(agent(cwd=str(real)), 12, set(), dry_run=False)
    entry = journal()["w1:p1"]
    fake.agents[0].update(terminal_id="term-2", cwd=str(link))   # the precondition compares realpaths
    fake.agents[0].pop("agent", None)
    assert sleeper.wake_entry(entry) and journal() == {}


def test_wake_on_new_terminal_in_other_cwd_orphans_entry(fake: FakeHerdr) -> None:
    """A1/A2: a new terminal that does not carry our 💤 label -> a reused pane id; re-keyed, never dropped."""
    entry = slept_entry(fake)
    fake.agents[0].update(terminal_id="term-2", cwd="/somewhere/else", label=None)
    fake.agents[0].pop("agent", None)
    assert not sleeper.wake_entry(entry)
    j = journal()
    assert "w1:p1" not in j and j[ORPHAN]["phase"] == "orphaned" and j[ORPHAN]["uuid"] == UUID
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    assert events().count("orphaned") == 1


def test_wake_refuses_recycled_pane_by_cwd(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0]["cwd"] = "/somewhere/else"
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_wake_refuses_when_pane_gone_and_keeps_entry(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents = []
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_refuses_when_argv_unknown(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    entry["argv"] = None
    assert not sleeper.wake_entry(entry)
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_plain_launched_session_wakes(fake: FakeHerdr) -> None:
    """A session started without flags has nothing to replay: that is known, not unknown."""
    fake.argv = []
    entry = slept_entry(fake)
    assert entry["argv"] == []
    assert sleeper.wake_entry(entry)
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert "--resume" in start and "--dangerously-skip-permissions" not in start


def test_wake_without_flags_resumes_unknown_argv(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    rows = journal()
    rows["w1:p1"]["argv"] = None  # what sleeps before the fix wrote for a plain `claude`
    sleeper.write_json(sleeper.JOURNAL, rows)
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    assert sleeper.main(["wake", "w1:p1"]) != 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    assert sleeper.main(["wake", "w1:p1", "--without-flags"]) == 0
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert "--resume" in start and "--dangerously-skip-permissions" not in start


def test_wake_refuses_when_session_live_elsewhere(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "pid 42")
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_refuses_when_liveness_unknown(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = slept_entry(fake)

    def boom(uuid: str, except_pane: str | None = None, cwd: str | None = None) -> None:
        raise sleeper.SleeperError("ps failed")

    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", boom)
    with pytest.raises(sleeper.SleeperError):
        sleeper.wake_entry(entry)
    assert "w1:p1" in journal()


def test_wake_clears_entry_when_same_session_already_running(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})  # came back by other means
    assert sleeper.wake_entry(entry) and journal() == {}


def test_wake_keeps_entry_when_agent_shown_but_no_process(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})
    fake.process_info_broken = True
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_keeps_entry_on_session_mismatch(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.start_uuid = "fresh-uuid"
    assert not sleeper.wake_entry(entry)
    assert journal()["w1:p1"]["wake_got"] == "fresh-uuid" and events()[-1] == "wake-mismatch"


def test_wake_with_stub_running_sends_enter_instead_of_starting(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    assert sleeper.wake_entry(entry)
    assert any(c[:3] == ("pane", "send-keys", "w1:p1") and "enter" in c for c in fake.calls)
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)   # the stub execs the agent itself


def test_wake_from_snapshot_persists_before_trying(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")   # asleep, but no journal (crash)
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 0
    assert (fake.agents[0].get("agent_session") or {}).get("value") == UUID
    assert "recovering" in capsys.readouterr().out


def test_snapshot_keeps_sleeping_panes(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")   # now asleep: not in agent list
    sleeper.merge_snapshot([])
    assert "w1:p1" in sleeper.read_json(sleeper.SNAPSHOT)
    fake.agents[0].update(agent="claude", agent_session={"value": "different"})
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["uuid"] == "different"


def test_snapshot_keeps_only_the_replayable_argv(fake: FakeHerdr) -> None:
    fake.argv = ["--dangerously-skip-permissions", "--model", "opus", "sk-secret-1 initial prompt"]
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] is None     # refused argv: nothing kept
    fake.argv = ["--dangerously-skip-permissions", "--resume", UUID, "--model", "opus"]
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == ["--dangerously-skip-permissions", "--model", "opus"]
    assert "sk-secret-1" not in sleeper.SNAPSHOT.read_text()


def test_snapshot_purges_raw_argv_of_earlier_versions(fake: FakeHerdr) -> None:
    sleeper.write_json(sleeper.SNAPSHOT, {
        "w9:p1": {"kind": "claude", "uuid": "old-session", "argv": ["--print", "sk-secret-1"]},
        "w9:p2": {"kind": "opencode", "uuid": "ses_2", "argv": ["--token", "sk-secret-2"]},
        "w9:p3": {"kind": "claude", "uuid": "kept-session", "argv": ["--verbose", "--resume", "x"]}})
    sleeper.merge_snapshot(fake.agents)
    snap = sleeper.read_json(sleeper.SNAPSHOT)
    assert snap["w9:p1"]["argv"] is None and snap["w9:p2"]["argv"] is None
    assert snap["w9:p3"]["argv"] == ["--verbose"]
    assert "sk-secret" not in sleeper.SNAPSHOT.read_text()


def test_state_files_are_private(fake: FakeHerdr) -> None:
    sleeper.STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    sleeper.STATE_DIR.chmod(0o755)
    sleeper.EVENTS.write_text("")
    sleeper.EVENTS.chmod(0o644)                         # as an earlier version left it
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]
    with sleeper.locked():
        pass
    assert sleeper.STATE_DIR.stat().st_mode & 0o777 == 0o700
    for path in (sleeper.JOURNAL, sleeper.EVENTS, sleeper.LOGFILE, sleeper.LOCK, sleeper.SNAPSHOT):
        if path.exists():
            assert path.stat().st_mode & 0o777 == 0o600, path.name


def test_logs_rotate(state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper, "LOG_MAX_BYTES", 200)
    for i in range(20):
        sleeper.event("tick", n=i)
    rotated = sleeper.EVENTS.with_name(sleeper.EVENTS.name + ".1")
    assert rotated.exists() and sleeper.EVENTS.stat().st_size <= 300
    assert rotated.stat().st_mode & 0o777 == 0o600
    sleeper.cmd_log(types.SimpleNamespace(tail=None))
    shown = [json.loads(l)["n"] for l in capsys.readouterr().out.splitlines()]
    assert shown == sorted(shown) and shown[-1] == 19   # rotated half first, newest last


def test_sleep_refuses_a_session_id_of_unexpected_shape(fake: FakeHerdr) -> None:
    fake.agents[0]["agent_session"] = {"value": "../../etc"}
    ok, reason, _ = sleeper.assess(fake.agents[0], fake.agents, 0, {}, idle_since=0)
    assert not ok and "unexpected shape" in reason
    slept, outcome = sleeper.sleep_agent(fake.agents[0], 0, set(), dry_run=False, allow_focused=True)
    assert not slept and "unexpected shape" in outcome and fake.signals == []


def test_external_watcher_is_never_spawned(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"; cfg.write_text('watcher = "external"\n'); monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a watcher"))
    assert "supervised externally" in sleeper.ensure_watcher()
    cfg.write_text('watcher = "cron"\n')
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_stop_keeps_the_watcher_stopped(state: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a watcher"))
    assert sleeper.cmd_stop(types.SimpleNamespace()) == 0
    assert sleeper.STOPPED.exists() and "stopped by `stop`" in sleeper.ensure_watcher()
    REAL_HEAL()                                    # a focus hook must not respawn it either
    spawned: list[Any] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: spawned.append(a) or types.SimpleNamespace(pid=1))
    monkeypatch.setattr(sleeper, "watcher_lock_held", lambda: False)
    assert sleeper.cmd_start(types.SimpleNamespace()) == 0
    assert not sleeper.STOPPED.exists() and spawned


def test_uninstall_wakes_everything_and_keeps_orphans(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    j = journal(); j[ORPHAN] = {**j["w1:p1"], "phase": "orphaned", "pane_id": "w9:p9"}; sleeper.write_json(sleeper.JOURNAL, j)
    assert sleeper.cmd_uninstall(types.SimpleNamespace()) == 1
    assert list(journal()) == [ORPHAN] and sleeper.STOPPED.exists()
    assert "manual resume" in capsys.readouterr().out


def test_dry_run_watcher_sleeps_nothing_and_writes_no_events(fake: FakeHerdr) -> None:
    config = {**sleeper.DEFAULTS, "idle_hours": 0.0, "dry_run": True}
    sleeper.watch_tick({}, config)
    sleeper.watch_tick({}, config)
    assert fake.signals == [] and journal() == {} and "refused" not in events()
    assert "would sleep (dry run)" in sleeper.LOGFILE.read_text()


def deliver(monkeypatch: pytest.MonkeyPatch, target: str, body: str = "please rebase", sender: str = "orc",
            msg_id: str | None = None) -> int:
    import io
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO(body))
    return sleeper.cmd_deliver(types.SimpleNamespace(target=target, sender=sender, id=msg_id))


def prompts(fake: FakeHerdr) -> list[tuple[str, ...]]:
    return [c for c in fake.calls if c[:2] == ("agent", "prompt")]


def test_deliver_to_a_live_idle_agent(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert deliver(monkeypatch, "a") == 0
    (call,) = prompts(fake)
    assert call[2] == "w1:p1" and call[3].startswith(f"[relayed from {sleeper.this_machine()}/orc via herdr-sleeper, msg ")
    assert "not from the owner]" in call[3] and call[3].endswith("please rebase")
    assert sleeper.pending_mail(UUID) == []
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines()]
    assert [r["event"] for r in rec] == ["deliver-queued", "delivered"]
    assert all("please rebase" not in json.dumps(r) for r in rec)      # the audit log holds no message body


def test_deliver_wakes_a_sleeper_then_types_the_message(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    assert deliver(monkeypatch, UUID) == 0                                # addressed by session id
    assert any(c[:2] == ("agent", "start") for c in fake.calls)
    assert "w1:p1" not in journal() and prompts(fake)[-1][3].endswith("please rebase")


def test_deliver_reaches_the_live_session_in_a_pane_with_a_displaced_row(fake: FakeHerdr,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    other = "7d1c2a9e-5b3f-4e8a-9c1d-2f6b8a4e0c13"
    fake.agents[0] = agent(uuid=other, name=None)                        # another session runs there now
    j = journal(); j["w1:p1"]["phase"] = "displaced"; sleeper.write_json(sleeper.JOURNAL, j)
    assert deliver(monkeypatch, other) == 0                               # by session id
    assert prompts(fake)[-1][2] == "w1:p1" and prompts(fake)[-1][3].endswith("please rebase")
    assert journal()["w1:p1"]["phase"] == "displaced"                     # the old handle stays
    fake.agents[0] = agent(uuid=other, name=None)
    assert deliver(monkeypatch, "w1:p1", msg_id="m-2") == 0               # by pane id


def test_deliver_holds_mail_when_the_target_is_busy(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "blocked"                            # a permission prompt: never type into it
    assert deliver(monkeypatch, "w1:p1") == 3
    assert prompts(fake) == [] and len(sleeper.pending_mail(UUID)) == 1
    ok, reason, _ = sleeper.assess({**fake.agents[0], "agent_status": "idle"}, fake.agents, 0, {}, idle_since=0)
    assert not ok and "mail pending" in reason                            # pending mail keeps it awake
    fake.agents[0]["agent_status"] = "idle"
    sleeper.flush_mail(fake.agents)                                       # the watcher types it in later
    assert len(prompts(fake)) == 1 and sleeper.pending_mail(UUID) == []


def test_deliver_dedups_by_id(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert deliver(monkeypatch, "a", msg_id="m-1") == 0
    assert deliver(monkeypatch, "a", msg_id="m-1") == 0
    assert len(prompts(fake)) == 1


def test_deliver_refused_sender_does_not_wake(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"; cfg.write_text('wake_refuse_from = ["web-reader"]\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    slept_entry(fake)
    assert deliver(monkeypatch, "a", sender="web-reader") == 3
    assert not any(c[:2] == ("agent", "start") for c in fake.calls) and "w1:p1" in journal()
    assert len(sleeper.pending_mail(UUID)) == 1


def test_deliver_validates_input(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert deliver(monkeypatch, "a", body="  \n") == 2
    assert deliver(monkeypatch, "a", body="x" * (sleeper.DELIVER_MAX_BYTES + 1)) == 2
    assert deliver(monkeypatch, "a", sender="evil; rm") == 2
    assert deliver(monkeypatch, "a", msg_id="../x") == 2
    assert deliver(monkeypatch, "nobody") == 1
    assert prompts(fake) == []


def test_mail_files_are_private(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a") == 3
    (path,) = sleeper.pending_mail(UUID)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


def named_transcript(state: Path, uuid: str, name: str) -> None:
    transcript(state, uuid, 30).write_text(json.dumps({"type": "agent-name", "agentName": "old-name"}) + "\n"
                                           + json.dumps({"type": "agent-name", "agentName": name}) + "\n")


def test_sleep_records_the_claude_session_name(fake: FakeHerdr, state: Path) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    assert slept_entry(fake)["claude_name"] == "ts-api-worker"


def test_presence_lists_live_and_sleeping(fake: FakeHerdr, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    fake.agents.append({**agent(pane="w2:p1", uuid="u2-session", name="orc", terminal="t2"), "pid_hint": 102})
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    by_state = {r["state"]: r for r in rows}
    assert by_state["asleep"]["claude_name"] == "ts-api-worker" and by_state["asleep"]["session"] == UUID
    assert by_state["idle"]["name"] == "orc"
    assert all("argv" not in r for r in rows)


def test_presence_names_the_place_the_owner_sees(fake: FakeHerdr, state: Path,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents.append({**agent(pane="w5:p1R", uuid="u2-session", name="unblocker", terminal="t2"),
                        "tab_id": "w5:t1R", "workspace_id": "w5", "pid_hint": 102})
    fake.agents.append({**agent(pane="w5:p2", uuid="u3-session", name="nolabel", terminal="t3"),
                        "tab_id": "w5:t2", "workspace_id": "w5", "pid_hint": 103})
    fake.tab_labels = {"w5:t1R": "unblocker", "w5:t2": "\x1b[31m7"}
    fake.workspace_labels = {"w5": "ao"}
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True)) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["unblocker"]["place"] == "ao › unblocker"
    assert rows["nolabel"]["place"] == "ao › [31m7"            # control characters dropped
    assert rows["a"]["place"] is None                           # no tab id known: no place
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False)) == 0
    out = capsys.readouterr().out
    me = sleeper.this_machine()
    assert f"{me} › ao › unblocker" in out and "never by its pane id" in out


def run_hook(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict) -> dict | None:
    import io
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    assert sleeper.cmd_claude_hook(types.SimpleNamespace()) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_hook_adds_sleepers_to_list_agents(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    assert run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"}) is None
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    out = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    assert "ts-api-worker" in out["hookSpecificOutput"]["additionalContext"]
    assert "in Herdr" not in out["hookSpecificOutput"]["additionalContext"]   # no labels known: no place
    fake.agents[0].update(tab_id="w1:t1", workspace_id="w1")
    fake.tab_labels, fake.workspace_labels = {"w1:t1": "api"}, {"w1": "ts"}
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse",
                                         "tool_name": "ListAgents"})["hookSpecificOutput"]["additionalContext"]
    assert f"in Herdr: {sleeper.this_machine()} › ts › api" in ctx and "never by its pane id" in ctx


def test_hook_routes_send_message_to_a_sleeper(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    sender_log = state / "sender.jsonl"
    sender_log.write_text(json.dumps({"type": "agent-name", "agentName": "orchestrator"}) + "\n")
    spawned: list[list[str]] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    out = run_hook(monkeypatch, capsys, {
        "hook_event_name": "PreToolUse", "tool_name": "SendMessage", "transcript_path": str(sender_log),
        "tool_input": {"to": "ts-api-worker [3fa9c1]", "message": "please rebase"}})
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "queued as msg" in hso["permissionDecisionReason"]
    assert len(sleeper.pending_mail(UUID)) == 1
    assert spawned and spawned[0][-4:] == ["deliver-pending", UUID, "--from", "orchestrator"]
    # the spawned process: wake, then type the message in with the envelope
    assert sleeper.cmd_deliver_pending(types.SimpleNamespace(uuid=UUID, sender="orchestrator")) == 0
    assert prompts(fake)[-1][3].startswith(f"[relayed from {sleeper.this_machine()}/orchestrator via herdr-sleeper")
    assert sleeper.pending_mail(UUID) == []


def test_hook_leaves_live_peers_and_errors_alone(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"hook_event_name": "PreToolUse", "tool_name": "SendMessage",
               "tool_input": {"to": "a", "message": "hi"}}
    assert run_hook(monkeypatch, capsys, payload) is None             # live agent: SendMessage handles it
    monkeypatch.setattr(sleeper, "read_json", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run_hook(monkeypatch, capsys, payload) is None             # fails open


# ------------------------------------------------------------------ stub

def test_stub_wakes_by_exec_after_enter(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    fake.agents[0].pop("agent", None)  # claimed placeholder is not a real agent for this path
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(sleeper.os, "chdir", lambda _p: None)  # cwd check compares realpaths; entry cwd is /tmp/proj
    monkeypatch.setattr(sleeper.os, "getcwd", lambda: entry["cwd"])
    monkeypatch.setattr(sleeper.shutil, "which", lambda b: f"/opt/bin/{b}")
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: pytest.fail("no helper process carries the uuid"))
    execvps: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: execvps.append((b, a)))
    flushed: list[int] = []
    monkeypatch.setattr(sleeper.termios, "tcflush", lambda fd, how: flushed.append(how))
    monkeypatch.setattr(sleeper.sys.stdin, "fileno", lambda: 0, raising=False)
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert execvps == [("/opt/bin/claude", ["claude", "--dangerously-skip-permissions", "--resume", UUID])]
    assert flushed == [sleeper.termios.TCIFLUSH]   # type-ahead dropped before the agent owns the tty
    row = journal()["w1:p1"]   # G1: kept as `waking` (same pid after exec) until reconcile sees the session
    assert row["phase"] == "waking" and row["waking_pid"] == os.getpid() and events()[-1] == "woke"
    assert any(c[:2] == ("pane", "release-agent") for c in fake.calls)


@pytest.mark.parametrize("pid", [0, 1, -1, True, None, "101"])
def test_signalable_refuses_implausible_pids(pid: object) -> None:
    assert sleeper.signalable(pid, "claude", {101: (50, "claude")})


def test_signalable_wants_the_agent_name_in_the_process_table() -> None:
    assert sleeper.signalable(101, "claude", {101: (50, "claude")}) is None
    assert sleeper.signalable(101, "claude", {101: (50, "node")}) is None          # npm install
    assert "not in the process table" in sleeper.signalable(101, "claude", {})
    assert "not claude" in sleeper.signalable(101, "claude", {101: (50, "vim")})
    assert sleeper.signalable(sleeper.os.getpid(), "claude", {sleeper.os.getpid(): (1, "claude")})


def test_sleep_refuses_when_the_pid_runs_something_else(fake: FakeHerdr) -> None:
    fake.comm = "vim"   # the pid process-info named was reused by another program
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "not claude" in outcome and fake.signals == []
    assert "w1:p1" not in journal()


def test_sleep_reaps_helpers_left_behind(fake: FakeHerdr) -> None:
    fake.helpers = {201: "python", 202: "node"}   # MCP servers the agent did not stop
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]
    assert (201, signal.SIGTERM) in fake.signals and (202, signal.SIGTERM) in fake.signals
    rec = json.loads(sleeper.EVENTS.read_text().splitlines()[-1])
    assert rec["event"] == "slept" and sorted(rec["helpers_stopped"]) == ["node", "python"]


def test_sleep_without_a_bare_shell_types_no_stub(fake: FakeHerdr) -> None:
    fake.foreground = "vim"   # after the exit, something other than the shell owns the tty
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]
    assert not any(c[:2] == ("pane", "run") for c in fake.calls)
    assert journal()["w1:p1"]["stub"] is False and journal()["w1:p1"]["phase"] == "asleep"


def test_wake_refuses_agent_start_off_the_shell(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.foreground = "less"
    assert sleeper.wake_entry(entry) is False
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    assert journal()["w1:p1"]["uuid"] == UUID and events()[-1] == "wake-refused"


def test_stub_leaves_shell_on_interrupt(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(KeyboardInterrupt()))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "w1:p1" in journal()  # entry kept; the printed manual command still points at it
    assert "resume with:" in capsys.readouterr().out


def test_stub_refuses_when_session_live_elsewhere(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(sleeper.os, "getcwd", lambda: entry["cwd"])   # the liveness check is what refuses here
    monkeypatch.setattr(sleeper.shutil, "which", lambda b: f"/opt/bin/{b}")
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("double resume"))
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "pane w9:p9")
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "w9:p9" in capsys.readouterr().out and "w1:p1" in journal()


# ------------------------------------------------------------- focus hook

def focus_env(pane: str) -> None:
    os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
        {"event": "pane_focused", "data": {"type": "pane_focused", "pane_id": pane, "workspace_id": "w1"}})


def test_on_focus_wakes_sleeping_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert any(c[:2] == ("agent", "start") for c in fake.calls) and journal() == {}


def test_on_focus_ignores_ordinary_panes(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    focus_env("w9:p9")  # nothing of ours there: no wake, no lock contention, fast exit
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_on_focus_debounces_one_click(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    focus_env("w1:p1")
    lock = sleeper.STATE_DIR / ".w1_p1.wakelock"  # pane.replace(":", "_") inside the leading dot
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held")  # a sibling hook from the same click holds the per-pane lock
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_on_focus_respects_config_optout(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    cfg = state / "config.toml"
    cfg.write_text("wake_on_focus = false\n")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


# ---------------------------------------------------------------- watcher

def test_watch_tick_sleeps_only_after_window(fake: FakeHerdr, state: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('idle = "1h"\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    clocks: dict[str, Any] = {}
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    config, _src = sleeper.load_config()

    sleeper.watch_tick(clocks, config)
    assert journal() == {} and clocks["w1:p1"]["since"] == 1_000_000.0   # clock starts, nothing sleeps yet

    clock["t"] += 1800
    sleeper.watch_tick(clocks, config)
    assert journal() == {}                                       # half an hour is not an hour

    fake.agents[0]["state_change_seq"] = 6                        # activity between polls restarts the clock
    clock["t"] += 1800
    sleeper.watch_tick(clocks, config)
    assert journal() == {} and clocks["w1:p1"]["since"] == 1_003_600.0

    clock["t"] += 3700
    sleeper.watch_tick(clocks, config)
    assert journal()["w1:p1"]["phase"] == "asleep"                # now a full quiet hour has passed


def _three_idle_agents(fake: FakeHerdr, state: Path) -> dict[str, Any]:
    uuids = {"w1:p1": UUID, "w2:p1": "u2-session", "w3:p1": "u3-session"}
    fake.agents = [{**agent(pane=p, uuid=u, name=f"n{i}", terminal=f"term-{i}"), "pid_hint": 101 + i}
                   for i, (p, u) in enumerate(uuids.items())]
    for u in uuids.values():
        transcript(state, u, 30)
    now = 1_000_000.0
    # idle 3 h, 2 h, 10 min; none has reached the 12 h window
    return {p: {**sleeper.clock_key(a), "since": now - age}
            for (p, a), age in zip(((a["pane_id"], a) for a in fake.agents), (3 * 3600, 2 * 3600, 600))}


def test_watch_tick_sleeps_longest_idle_over_the_cap(fake: FakeHerdr, state: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    clocks = _three_idle_agents(fake, state)
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "max_live": 2})
    assert list(journal()) == ["w1:p1"]                                  # one over the cap: the longest idle
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "max_live": 0})
    assert list(journal()) == ["w1:p1"]                                  # no cap, no pressure: windows only


def test_watch_tick_sleeps_one_per_tick_under_pressure(fake: FakeHerdr, state: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    clocks = _three_idle_agents(fake, state)
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    monkeypatch.setattr(sleeper, "memory_pressure", lambda: 2)
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert list(journal()) == ["w1:p1"]
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert sorted(journal()) == ["w1:p1", "w2:p1"]
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert "w3:p1" not in journal()                                      # 10 min idle: under pressure_idle


def test_pressure_idle_must_fit_inside_idle(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"; monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    cfg.write_text('idle = "90m"\npressure_idle = "20m"\nmax_live = 8\n')
    values, _ = sleeper.load_config()
    assert values["pressure_idle_hours"] == pytest.approx(1 / 3) and values["max_live"] == 8
    cfg.write_text('idle = "1h"\npressure_idle = "2h"\n')
    assert sleeper.load_config()[0]["pressure_idle_hours"] == 1.0         # clamped: never later than idle
    for bad in ('pressure_idle = "0m"\n', "max_live = -1\n", "max_live = 2.5\n"):
        cfg.write_text(bad)
        with pytest.raises(sleeper.ConfigError):
            sleeper.load_config()


def test_restart_outdated_sleeps_and_wakes_onto_the_new_build(fake: FakeHerdr, state: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    builds = {"running": "/v/2.1.305"}
    monkeypatch.setattr(sleeper, "running_build", lambda pid: builds["running"])
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 3600}}
    config = {**sleeper.DEFAULTS, "restart_outdated": True, "exclude": ["a"]}   # keep-listed: restarts anyway
    sleeper.watch_tick(clocks, config)
    assert fake.signals == [(101, signal.SIGTERM)]
    assert any(c[:2] == ("agent", "start") for c in fake.calls) and "w1:p1" not in journal()
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if '"restarted"' in l][-1]
    assert rec["old"] == "2.1.305" and rec["new"] == "2.1.306" and rec["woke"] is True


def test_restart_outdated_leaves_current_young_and_dry_run_sessions(fake: FakeHerdr, state: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 3600}}
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.306")
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == []                                            # already current
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.305")
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True, "dry_run": True})
    assert fake.signals == [] and "would restart onto 2.1.306" in sleeper.LOGFILE.read_text()
    young = {"w1:p1": {**clocks["w1:p1"], "since": 1_000_000.0 - 60}}
    sleeper.watch_tick(young, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == []                                            # idle a minute: not yet


def test_watch_tick_honors_excludes_and_kinds(fake: FakeHerdr, state: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('idle = "0s"\nexclude = ["a"]\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    config, _src = sleeper.load_config()
    clocks: dict[str, Any] = {}
    sleeper.watch_tick(clocks, config)
    assert journal() == {} and "w1:p1" in clocks                  # clocked (for restart_outdated), never slept

    fake.agents[0].pop("name")
    sleeper.watch_tick(clocks, config)
    assert journal()["w1:p1"]["phase"] == "asleep"


# -------------------------------------------------------------- migration

def test_migration_adopts_legacy_journal(state: Path) -> None:
    legacy = state / "legacy"
    (legacy).mkdir(parents=True, exist_ok=True)
    (legacy / "sleeping.json").write_text(json.dumps({
        "w9:p1": {"pane_id": "w9:p1", "uuid": "legacy-uuid", "cwd": "/x",
                  "argv": ["--dangerously-skip-permissions"], "phase": "asleep"},
        "bad": "not an entry",
    }))
    sleeper.migrate_legacy()
    j = journal()
    assert j["w9:p1"]["uuid"] == "legacy-uuid" and j["w9:p1"]["kind"] == "claude"
    assert "bad" not in j and events() == ["migrated"]


def test_migration_is_once_and_opt_in(fake: FakeHerdr) -> None:
    (sleeper.STATE_DIR).mkdir(parents=True, exist_ok=True)
    sleeper.JOURNAL.write_text(json.dumps({"w1:p1": {"pane_id": "w1:p1", "uuid": "u", "kind": "claude"}}))
    sleeper.migrate_legacy()  # journal already exists → no touch
    assert journal()["w1:p1"]["uuid"] == "u" and events() == []


# ===================================================================== v0.1.1
# One block per fix-list item (A = session loss / double resume / silent stop,
# B = broken or misleading, C = low). Each test failed against v0.1.0.

class Ps:
    """A scripted `ps -eo pid=,args=` / `ps -p PID -o args=` for liveness and watcher checks."""

    def __init__(self, lines: list[str], fail: bool = False) -> None:
        self.lines, self.fail = lines, fail

    def __call__(self, cmd: list[str], **kw: Any) -> Any:
        if self.fail:
            raise OSError("ps: no such file")
        return types.SimpleNamespace(stdout="\n".join(self.lines) + "\n", returncode=0, stderr="")


def hold_flock(path: Path) -> Any:
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


# ------------------------------------------------------------- A2 orphans

def test_reconcile_orphans_reused_pane_id_running_another_session(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].update(agent="claude", agent_session={"value": "other"}, terminal_id="term-2")
    sleeper.reconcile(fake.agents)
    j = journal()
    assert "w1:p1" not in j and j[ORPHAN]["phase"] == "orphaned" and j[ORPHAN]["uuid"] == UUID
    assert j[ORPHAN]["orphaned_from"] == "w1:p1"
    sleeper.reconcile(fake.agents); sleeper.reconcile(fake.agents)
    assert events().count("orphaned") == 1 and ORPHAN in journal()   # one event, then silence


def test_reconcile_orphans_reused_bare_pane_in_other_cwd(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].pop("agent"); fake.agents[0].update(terminal_id="term-2", cwd="/elsewhere", label=None)
    sleeper.reconcile(fake.agents)
    assert ORPHAN in journal() and "w1:p1" not in journal()


def test_reconcile_keeps_entry_when_restart_kept_cwd(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].pop("agent"); fake.agents[0]["terminal_id"] = "term-2"   # restart: same pane, same cwd
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "asleep"


def test_legacy_entry_without_terminal_id_is_orphaned_not_dropped(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    j = journal(); j["w1:p1"]["terminal_id"] = None; sleeper.write_json(sleeper.JOURNAL, j)
    fake.agents[0].update(agent="claude", agent_session={"value": "other"})
    sleeper.reconcile(fake.agents)
    assert ORPHAN in journal()  # same terminal cannot be proven: keep the handle


def test_orphans_are_never_auto_woken(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].update(agent="claude", agent_session={"value": "other"}, terminal_id="term-2")
    sleeper.reconcile(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=True)) == 0
    assert sleeper.cmd_wake(types.SimpleNamespace(target=ORPHAN, all=False)) == 1
    assert not any(c[:2] == ("agent", "start") for c in fake.calls) and ORPHAN in journal()
    capsys.readouterr()
    assert sleeper.cmd_list(types.SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert ORPHAN in out and "orphaned" in out and f"--resume {UUID}" in out


def test_hand_made_orphan_key_is_treated_as_orphan(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sleeper.write_json(sleeper.JOURNAL, {ORPHAN: {"pane_id": ORPHAN, "uuid": UUID, "kind": "claude",
                                                  "phase": "asleep", "cwd": "/p", "argv": []}})
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=True)) == 0
    sleeper.reconcile(fake.agents)
    assert not any(c[:2] in (("agent", "start"), ("pane", "get")) for c in fake.calls) and ORPHAN in journal()


def test_snapshot_keeps_an_orphan_copy_of_a_reused_pane(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].update(agent_session={"value": "different"}, terminal_id="term-2")
    sleeper.merge_snapshot(fake.agents)
    snap = sleeper.read_json(sleeper.SNAPSHOT)
    assert snap["w1:p1"]["uuid"] == "different" and snap[ORPHAN]["uuid"] == UUID


# ----------------------------------------------------- A3 / A4 watcher survives

def test_watch_tick_skips_claude_without_session(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"; cfg.write_text('idle = "0s"\n'); monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    fake.agents[0].pop("agent_session")
    fake.agents.append({**agent(pane="w2:p1", name="b"), "agent_session": None})
    config, _ = sleeper.load_config()
    sleeper.watch_tick({}, config)  # v0.1.0: KeyError / AttributeError -> watcher gone
    assert journal() == {}


def run_loop(monkeypatch: pytest.MonkeyPatch, tick: Any, ticks: int) -> list[float]:
    """Drive watch_loop for `ticks` ticks of a scripted watch_tick; returns the requested sleeps."""
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_tick(clocks: dict[str, Any], config: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] > ticks:
            sleeper.STOP["requested"] = True
            return
        tick(calls["n"])

    monkeypatch.setattr(sleeper, "watch_tick", fake_tick)
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(sleeper.signal, "signal", lambda *a: None)
    sleeper.watch_loop()
    assert calls["n"] == ticks + 1
    return sleeps


def test_watch_loop_survives_an_arbitrary_exception(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    def tick(n: int) -> None:
        if n == 1:
            raise KeyError("agent_session")

    run_loop(monkeypatch, tick, 3)
    assert "tick-error: KeyError" in sleeper.LOGFILE.read_text()


def test_watch_loop_never_exits_and_backs_off(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    def tick(n: int) -> None:
        raise sleeper.SleeperError("herdr agent list timed out")

    sleeps = run_loop(monkeypatch, tick, 15)  # v0.1.0 exited for good after 10
    assert sum(sleeps) > 15 * 60 and max(sleeps) <= 1.0     # interruptible 1s steps ...
    assert "backing off" in sleeper.LOGFILE.read_text()


def test_watch_loop_waits_for_the_watcher_lock(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    """C/F20: one watcher per state dir, enforced by a lock rather than a pid read-back race."""
    fh = hold_flock(sleeper.WATCHER_LOCK)
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(sleeper, "watch_tick", lambda *a: pytest.fail("a second watcher ticked"))
    sleeper.watch_loop()
    fh.close()
    assert "another watcher" in capsys.readouterr().out


def test_heal_spawns_a_watcher_only_when_none_runs(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[dict[str, str]] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(kw["env"]) or types.SimpleNamespace(pid=4242))
    monkeypatch.setenv("HERDR_SLEEPER_IDLE", "0s")
    real_heal = REAL_HEAL
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([]))   # no recorded watcher process
    real_heal()
    assert len(spawned) == 1 and "HERDR_SLEEPER_IDLE" not in spawned[0]   # a heal never inherits test overrides
    fh = hold_flock(sleeper.WATCHER_LOCK)
    real_heal()
    fh.close()
    assert len(spawned) == 1


def test_list_scan_sleep_pane_and_focus_heal_the_watcher(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    healed: list[int] = []
    monkeypatch.setattr(sleeper, "heal_watcher", lambda: healed.append(1))
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    sleeper.cmd_list(types.SimpleNamespace())
    sleeper.cmd_scan(types.SimpleNamespace(dry_run=True, exclude=None))
    sleeper.cmd_sleep_pane(types.SimpleNamespace(pane="w1:p1", dry_run=True))
    focus_env("w9:p9"); sleeper.cmd_on_focus()
    assert len(healed) == 4


# ---------------------------------------------------- A5 structural stub match

def test_uuid_live_elsewhere_skips_only_a_real_stub(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "list_agents", lambda: [])
    stub = f"123 /usr/bin/python3 /x/plugins/herdr-sleeper/herdr-sleeper stub w1:p1 {UUID} /state"
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([stub]))
    assert REAL_UUID_LIVE(UUID, except_pane="w1:p1") is None
    sneaky = f"124 claude --resume {UUID} --add-dir /x/plugins/herdr-sleeper stub"
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([stub, sneaky]))
    assert REAL_UUID_LIVE(UUID, except_pane="w1:p1") == "pid 124"
    prompt = f"125 claude --resume {UUID} -- look at the herdr-sleeper stub bug"
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([prompt]))
    assert REAL_UUID_LIVE(UUID) == "pid 125"


def test_uuid_live_elsewhere_continue_cwd_and_ps_failure(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "list_agents", lambda: [])
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["7 claude --continue"]))
    monkeypatch.setattr(sleeper, "process_cwd", lambda pid: "/tmp/proj")
    assert "--continue" in REAL_UUID_LIVE(UUID, cwd="/tmp/proj")
    monkeypatch.setattr(sleeper, "process_cwd", lambda pid: None)
    with pytest.raises(sleeper.SleeperError):
        REAL_UUID_LIVE(UUID, cwd="/tmp/proj")
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([], fail=True))
    with pytest.raises(sleeper.SleeperError):
        REAL_UUID_LIVE(UUID)
    monkeypatch.setattr(sleeper, "list_agents", lambda: [agent(pane="w5:p5")])
    assert REAL_UUID_LIVE(UUID, except_pane="w1:p1") == "pane w5:p5"


def test_stub_running_is_structural(fake: FakeHerdr) -> None:
    fake.stub_panes.add("w1:p1")
    assert sleeper.stub_running("w1:p1", UUID) is True
    assert sleeper.stub_running("w1:p1", "another-uuid") is False
    fake.stub_panes.clear()
    fake.argv = ["--add-dir", "/p/herdr-sleeper", "stub"]   # a claude whose argv merely mentions us
    assert sleeper.stub_running("w1:p1", UUID) is False


# --------------------------------------------------------- A6 wake target

def test_wake_action_without_target_wakes_the_context_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents = [agent(pane="w1:p1", name=None), {**agent(pane="w2:p1", name="b", uuid="u2", terminal="term-b"),
                                                     "pid_hint": 2}]
    transcript(Path(str(sleeper.TRANSCRIPTS.parent)), "u2", 30)
    sleeper.sleep_agent(fake.agents[0], 12, set(), dry_run=False)
    sleeper.sleep_agent(fake.agents[1], 12, set(), dry_run=False)
    assert sleeper.find_journal_entry(None) is None
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"focused_pane_id": "w2:p1"}))
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=False)) == 0
    starts = [c for c in fake.calls if c[:2] == ("agent", "start")]
    assert len(starts) == 1 and "w2:p1" in starts[0] and "w1:p1" in journal()


def test_wake_without_target_or_context_refuses(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=False)) == 2
    assert "w1:p1" in journal()


# ------------------------------------------------------------ A7 stub order

def stub_ready(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, lines: list[str] | None = None) -> dict[str, Any]:
    entry = slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    feed = iter(lines if lines is not None else [""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(feed))
    monkeypatch.setattr(sleeper.os, "getcwd", lambda: entry["cwd"])
    monkeypatch.setattr(sleeper.shutil, "which", lambda b: f"/opt/bin/{b}")
    return entry


def test_stub_exec_failure_reinserts_the_entry_and_reclaims(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    stub_ready(fake, monkeypatch)

    def boom(b: str, a: list[str]) -> None:
        raise FileNotFoundError(b)

    monkeypatch.setattr(sleeper.os, "execvp", boom)
    fake.calls.clear()
    assert sleeper.cmd_stub("w1:p1", UUID) == 1
    row = journal()["w1:p1"]
    assert row["uuid"] == UUID and row["phase"] == "asleep" and "waking_pid" not in row
    assert events()[-1] == "wake-failed"
    assert any(c[:2] == ("pane", "report-agent") for c in fake.calls)   # the sidebar row is claimed back
    assert "manual" in capsys.readouterr().out


def test_stub_refuses_when_agent_not_on_path(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    stub_ready(fake, monkeypatch)
    monkeypatch.setattr(sleeper.shutil, "which", lambda b: None)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("exec without a binary"))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "w1:p1" in journal() and "PATH" in capsys.readouterr().out


def test_stub_filters_recovered_argv(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_ready(fake, monkeypatch)
    j = journal(); j["w1:p1"]["argv"] = ["--resume", "other-uuid", "--fork-session"]; sleeper.write_json(sleeper.JOURNAL, j)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("exec of unvetted argv"))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0 and "w1:p1" in journal()


def test_stub_rechecks_under_the_lock(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_ready(fake, monkeypatch)
    calls: list[str | None] = []

    def live(uuid: str, except_pane: str | None = None, cwd: str | None = None) -> str | None:
        calls.append(except_pane)
        return "pane w9:p9" if len(calls) > 1 else None   # a focus-wake raced us after the first look

    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", live)
    real_locked = sleeper.locked

    class spy(real_locked):  # type: ignore[misc,valid-type]
        def __enter__(self) -> Any:
            calls.append("lock")
            return super().__enter__()

    monkeypatch.setattr(sleeper, "locked", spy)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("double resume"))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "lock" in calls and calls.index("lock") < len(calls) - 1 and "w1:p1" in journal()


# ---------------------------------------------------- A8 exit-requested focus

def test_focus_during_exit_requested_does_not_clear_on_one_sighting(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    entry = journal()["w1:p1"]
    assert entry["phase"] == "exit-requested"
    assert not sleeper.wake_entry(entry, via="focus")
    assert journal()["w1:p1"]["phase"] == "exit-requested"


# ---------------------------------------------------------- A9 idle clocks

def test_idle_clock_survives_a_watcher_restart(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"; cfg.write_text('idle = "1h"\n'); monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    config, _ = sleeper.load_config()
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick(sleeper.load_clocks(), config)
    clock["t"] += 3000
    sleeper.watch_tick(sleeper.load_clocks(), config)   # a new watcher process: clocks come from idle.json
    clock["t"] += 700
    sleeper.watch_tick(sleeper.load_clocks(), config)
    assert journal()["w1:p1"]["phase"] == "asleep"


def test_idle_clock_restarts_when_terminal_or_seq_differs(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {**sleeper.DEFAULTS, "idle_hours": 1.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    for rec in ({"terminal_id": "term-OLD", "seq": 5, "since": 1.0},   # server restart / reused pane id
                {"terminal_id": "term-1", "seq": 4, "since": 1.0},      # activity
                {"terminal_id": "term-1", "seq": 5, "since": 2e9}):     # clock in the future
        clocks = {"w1:p1": dict(rec)}
        sleeper.watch_tick(clocks, config)
        assert clocks["w1:p1"]["since"] == 1_000_000.0 and journal() == {}


def test_damaged_idle_file_starts_clocks_now(state: Path) -> None:
    sleeper.STATE_DIR.mkdir(parents=True)
    sleeper.IDLE_FILE.write_text("[]\n")
    assert sleeper.load_clocks() == {}
    assert list(sleeper.STATE_DIR.glob("idle.json.damaged-*"))
    sleeper.IDLE_FILE.write_text(json.dumps({"w1:p1": {"since": "yesterday"}, "w2:p1": {"since": 5.0, "seq": 1,
                                                                                         "terminal_id": "t"}}))
    assert list(sleeper.load_clocks()) == ["w2:p1"]


# ----------------------------------------------------------- A10 watcher id

def test_watcher_alive_matches_argv_tokens(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["cargo watch -x test"]))
    assert not sleeper.watcher_alive({"pid": 77})
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["/usr/bin/python3 /x/herdr-sleeper watch"]))
    assert sleeper.watcher_alive({"pid": 77})   # a pre-v0.1.1 record (no lock) is judged by argv alone


def test_watcher_alive_requires_the_lock_for_a_v011_record(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F5: a reused pid that is another session's watcher must not pass for ours."""
    monkeypatch.setattr(sleeper.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["/usr/bin/python3 /x/herdr-sleeper watch"]))
    assert not sleeper.watcher_alive({"pid": 77, "lock": True})
    fh = hold_flock(sleeper.WATCHER_LOCK)
    assert sleeper.watcher_alive({"pid": 77, "lock": True})
    fh.close()


def test_heal_replaces_a_pre_v011_watcher(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """G3: v0.1.0 holds no watcher.lock; a heal (any focus, list, scan) replaces it, as startup would."""
    sleeper.STATE_DIR.mkdir(parents=True)
    sleeper.WATCHER_PID.write_text(json.dumps({"pid": 77, "stamp": "v0.1.0-stamp"}))
    signals: list[tuple[int, int]] = []
    gone = {"v": False}

    def kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            gone["v"] = True
        if sig == 0 and gone["v"]:
            raise ProcessLookupError(pid)
        signals.append((pid, sig))

    monkeypatch.setattr(sleeper.os, "kill", kill)
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["/usr/bin/python3 /orca/herdr-sleeper watch"]))
    spawned: list[int] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: spawned.append(1) or types.SimpleNamespace(pid=88))
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: None)
    REAL_HEAL()
    assert gone["v"] and spawned == [1]


def test_ensure_watcher_never_signals_an_unverified_pid(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeper.STATE_DIR.mkdir(parents=True)
    sleeper.WATCHER_PID.write_text(json.dumps({"pid": 77, "stamp": "old"}))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(sleeper.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(sleeper.subprocess, "run", Ps(["fswatch /tmp"]))   # pid 77 was reused
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: types.SimpleNamespace(pid=88))
    assert "spawned" in sleeper.ensure_watcher()
    assert (77, signal.SIGTERM) not in signals


# --------------------------------------------------------------- B items

def test_sleep_pane_on_the_focused_pane_sleeps(fake: FakeHerdr) -> None:
    fake.agents[0]["focused"] = True
    slept, outcome = sleeper.sleep_agent(agent(focused=True), 12, set(), dry_run=False, allow_focused=True)
    assert slept and outcome == "slept"
    assert not sleeper.sleep_agent(agent(pane="w1:p1", focused=True), 12, set(), dry_run=True)[0]


def test_stub_line_carries_no_control_bytes(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/s/herdr.sock")
    monkeypatch.setenv("HERDR_CLIENT_SOCKET_PATH", "/s/herdr-client.sock")
    slept_entry(fake)
    run = stub_run(fake)
    assert all(ord(c) >= 32 and ord(c) != 127 for c in run)            # nothing a line editor reads as keys
    assert "\\033[?1049l" in run and " env HERDR_SOCKET_PATH=/s/herdr.sock " in f" {run} "
    assert "HERDR_CLIENT_SOCKET_PATH" not in run                       # C/F19: the CLI routes by HERDR_SOCKET_PATH


def test_stub_ignores_a_typed_line_and_waits_for_bare_enter(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    stub_ready(fake, monkeypatch, ["do the thing", "", ""])
    execs: list[str] = []
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: execs.append(b))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert execs == ["/opt/bin/claude"] and "ignored" in capsys.readouterr().out


def test_stub_wake_restores_label_and_reconcile_hands_the_name_back(fake: FakeHerdr,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """B4 + F7: the name comes back when reconcile finalises the `waking` row; no detached helper
    whose argv carries the uuid (it read as a live session to every wake for 90 s)."""
    fake.agents[0]["label"] = "mylabel"
    stub_ready(fake, monkeypatch)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: None)
    sleeper.cmd_stub("w1:p1", UUID)
    assert fake.agents[0]["label"] == "mylabel"
    fake.stub_panes.discard("w1:p1")                                   # the stub's process became claude (exec)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID}, name=None)   # the exec'd agent, unnamed
    sleeper.reconcile(fake.agents)
    assert fake.agents[0]["name"] == "a" and journal() == {} and events()[-1] == "reconciled"


def test_reconcile_logs_a_name_it_could_not_give_back(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_ready(fake, monkeypatch)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: None)
    sleeper.cmd_stub("w1:p1", UUID)
    fake.stub_panes.discard("w1:p1")                                   # the stub's process became claude (exec)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID}, name=None)
    real = fake.__call__
    def rename_refused(*a: str, **k: Any) -> Any:
        if a[:2] == ("agent", "rename"):
            raise sleeper.SleeperError("herdr agent rename failed: agent_name_taken")
        return real(*a, **k)

    monkeypatch.setattr(sleeper, "herdr", rename_refused)
    sleeper.reconcile(fake.agents)
    assert journal() == {} and "could not give the name" in sleeper.LOGFILE.read_text()


def test_already_running_clear_path_restores_label(fake: FakeHerdr) -> None:
    fake.agents[0]["label"] = "mylabel"
    entry = slept_entry(fake)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})
    assert sleeper.wake_entry(entry) and fake.agents[0]["label"] == "mylabel"


def test_wake_via_stub_is_confirmed_before_reporting_success(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    fake.agents[0].pop("agent", None)
    fake.stub_panes.add("w1:p1")
    assert sleeper.cmd_wake(types.SimpleNamespace(target="w1:p1", all=False)) == 1   # stub refused: not a success
    assert "wake-delegated-unconfirmed" in events()
    fake.stub_execs_on_enter = True
    assert sleeper.cmd_wake(types.SimpleNamespace(target="w1:p1", all=False)) == 0
    assert "woke 1/1" in capsys.readouterr().out


def test_boolean_env_overrides(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_SLEEPER_NOTIFY", "false")
    monkeypatch.setenv("HERDR_SLEEPER_WAKE_ON_FOCUS", "0")
    values, _ = sleeper.load_config()
    assert values["notify"] is False and values["wake_on_focus"] is False
    monkeypatch.setenv("HERDR_SLEEPER_NOTIFY", "Yes")
    assert sleeper.load_config()[0]["notify"] is True
    monkeypatch.setenv("HERDR_SLEEPER_NOTIFY", "maybe")
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_wake_retries_once_with_the_pane_name_when_the_name_is_taken(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.taken_names = {"a"}
    assert sleeper.wake_entry(entry) and journal() == {}
    assert fake.agents[0]["started_name"] == "wake-w1-p1"
    fake.taken_names = {"a", "wake-w1-p1"}
    entry = slept_entry(fake)
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal() and events()[-1] == "wake-failed"


def test_start_answering_before_the_session_is_known_is_not_a_mismatch(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.start_reports_session = False
    assert sleeper.wake_entry(entry) and journal() == {}             # learned from `pane get` shortly after
    entry = slept_entry(fake)
    fake.start_session_appears = False
    assert not sleeper.wake_entry(entry)
    assert "wake-mismatch" not in events() and events()[-1] == "wake-unconfirmed"
    assert journal()["w1:p1"]["wake_pending"] is True
    fake.agents[0]["agent_session"] = {"value": "fresh"}              # it turned out to be another session
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "displaced"                 # kept, by hand only


def test_sigterm_lets_the_transaction_finish(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeper.on_term(signal.SIGTERM, None)          # v0.1.0: sys.exit(0) right here, mid-transaction
    assert sleeper.STOP["requested"] is True
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert journal()["w1:p1"]["phase"] == "asleep"
    config = {**sleeper.DEFAULTS, "idle_hours": 0.0}
    fake.agents.append({**agent(pane="w2:p1", name="b", terminal="term-b"), "pid_hint": 2})
    sleeper.watch_tick({}, config)                 # a stopping watcher starts no new transaction
    assert "w2:p1" not in journal()


def test_agent_process_without_readable_argv_is_unknown(fake: FakeHerdr) -> None:
    fake.empty_argv = True
    assert sleeper.agent_process("w1:p1", "claude")[0] is None


def test_legacy_config_is_translated_once(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = state / "legacy-config.toml"
    legacy.write_text('idle_hours = 12\nexclude = ["orc", "orc-meta"]   # orchestrators\ninterval_minutes = 30\n')
    cfg = state / "cfg" / "config.toml"
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    sleeper.migrate_legacy_config()
    values, _ = sleeper.load_config()
    assert values["idle_hours"] == 12 and values["exclude"] == ["orc", "orc-meta"]
    assert "interval_minutes =" not in cfg.read_text() and events() == ["config-migrated"]
    cfg.write_text('idle = "2h"\n')
    sleeper.migrate_legacy_config()                 # an existing config is never rewritten ...
    assert cfg.read_text() == 'idle = "2h"\n'
    assert "is not read" in sleeper.LOGFILE.read_text()   # ... but the stray file is pointed out


def test_legacy_config_with_unknown_keys_is_not_migrated(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / "legacy-config.toml").write_text("idle_hours = 12\nsurprise = 1\n")
    cfg = state / "cfg" / "config.toml"
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    sleeper.migrate_legacy_config()
    assert not cfg.exists()


# --------------------------------------------------------------- C items

@pytest.fixture
def no_tomllib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "tomllib", None)   # the 3.9 path, on every interpreter


def test_toml_subset_parses_the_documented_config(state: Path, monkeypatch: pytest.MonkeyPatch, no_tomllib: None) -> None:
    cfg = state / "config.toml"
    cfg.write_text('# comment\nidle = "90m"  # trailing\nexclude = ["pane#1", "orc"]\npoll_seconds = 30\n'
                   'notify = false\nagents = []\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    values, _ = sleeper.load_config()
    assert values["idle_hours"] == 1.5 and values["exclude"] == ["pane#1", "orc"] and values["notify"] is False


@pytest.mark.parametrize("text", ['idle = "1h"\nidle = "2h"\n', 'exclude = ["a\\"b"]\n', 'exclude = ["a", 1]\n',
                                  "[table]\n", 'idle = "1h\n', "exclude = [\n"])
def test_toml_subset_refuses_what_it_cannot_parse_exactly(state: Path, monkeypatch: pytest.MonkeyPatch,
                                                          no_tomllib: None, text: str) -> None:
    cfg = state / "config.toml"
    cfg.write_text(text)
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_context_pane_ignores_a_leaked_pane_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_PANE_ID", "w3:p1")                       # inherited from an outer session
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"workspace_id": "w1"}))
    assert sleeper.context_pane() is None
    monkeypatch.delenv("HERDR_PLUGIN_CONTEXT_JSON")
    assert sleeper.context_pane() == "w3:p1"                           # a manual run has no context to prefer


def test_ambiguous_name_refuses_with_candidates(fake: FakeHerdr) -> None:
    fake.agents = [agent(pane="w1:p1"), {**agent(pane="w2:p1", uuid="u2", terminal="term-b"), "pid_hint": 2}]
    transcript(Path(str(sleeper.TRANSCRIPTS.parent)), "u2", 30)
    sleeper.sleep_agent(fake.agents[0], 12, set(), dry_run=False)
    sleeper.sleep_agent(fake.agents[1], 12, set(), dry_run=False)
    with pytest.raises(sleeper.SleeperError, match="w1:p1.*w2:p1"):
        sleeper.find_journal_entry("a")
    assert sleeper.find_journal_entry("w2:p1")[0] == "w2:p1"


def test_done_status_survives_into_the_sleeping_label(fake: FakeHerdr) -> None:
    fake.agents[0]["agent_status"] = "done"
    sleeper.sleep_agent(agent(status="done"), 12, set(), dry_run=False)
    assert any(c[:2] == ("pane", "report-metadata") and "claude · sleeping · done" in c for c in fake.calls)


def test_focus_hook_gives_up_on_a_long_held_lock(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    fh = hold_flock(sleeper.LOCK)
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0                  # bounded wait, then the next focus retries
    fh.close()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    assert "busy" in sleeper.LOGFILE.read_text()


# ================================================================ fix pass
# Second adversarial round on v0.1.1: F = review2-fable.md, G = review2-grok.md.

def test_cmd_wake_refuses_a_hand_made_orphan_key_with_a_pane_body(fake: FakeHerdr,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    """F3: the key decides, not the body: copying an entry under `orphan:` must not wake it into
    whatever pane now has the body's pane id."""
    entry = slept_entry(fake)
    j = journal(); j[ORPHAN] = j.pop("w1:p1"); sleeper.write_json(sleeper.JOURNAL, j)   # pane_id still w1:p1
    fake.agents[0].pop("agent", None)
    assert sleeper.cmd_wake(types.SimpleNamespace(target=ORPHAN, all=False)) == 1
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 1
    assert not sleeper.wake_entry(entry, key=ORPHAN)
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    assert f"--resume {UUID}" in capsys.readouterr().out


def test_sleep_refuses_to_overwrite_a_kept_handle(fake: FakeHerdr) -> None:
    """F4: sleep-pane skips assess; it must still never write over another session's handle."""
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    kept = {"pane_id": "w1:p1", "uuid": "kept-uuid", "kind": "claude", "phase": "displaced", "wake_got": UUID}
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": kept})
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False, allow_focused=True)
    assert not slept and "kept handle" in outcome and journal()["w1:p1"] == kept and not fake.signals


def test_watch_tick_survives_an_exception_in_one_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: one pane raising a non-SleeperError must not starve the panes after it or the clock write."""
    fake.agents = [agent(pane="w1:p1"), {**agent(pane="w2:p1", name="b", terminal="term-b", uuid="u2"), "pid_hint": 2}]
    transcript(Path(str(sleeper.TRANSCRIPTS.parent)), "u2", 30)
    real = sleeper.sleep_agent

    def flaky(a: dict[str, Any], *args: Any, **kw: Any) -> Any:
        if a["pane_id"] == "w1:p1":
            raise TypeError("pane get returned a list")
        return real(a, *args, **kw)

    monkeypatch.setattr(sleeper, "sleep_agent", flaky)
    sleeper.watch_tick({}, {**sleeper.DEFAULTS, "idle_hours": 0.0})
    assert journal()["w2:p1"]["phase"] == "asleep" and "TypeError" in sleeper.LOGFILE.read_text()
    assert "w1:p1" in json.loads(sleeper.IDLE_FILE.read_text())


def test_stub_refuses_without_a_transcript(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    """F8: same gate as wake_entry: nothing to resume -> no exec, entry kept."""
    stub_ready(fake, monkeypatch)
    for t in (state / "projects").glob("*/*.jsonl"):
        t.unlink()
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("exec without a transcript"))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert journal()["w1:p1"]["phase"] == "asleep" and "transcript" in capsys.readouterr().out


def test_scan_honours_the_persisted_idle_clock(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    """F9: `scan` (no --dry-run) used to sleep everything at once, window ignored."""
    cfg = state / "config.toml"; cfg.write_text('idle = "1h"\n'); monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    sleeper.cmd_scan(types.SimpleNamespace(dry_run=False, exclude=None))
    assert journal() == {} and "idle 0.0h < 1h" in capsys.readouterr().out   # no clock: it starts now
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sleeper.write_json(sleeper.IDLE_FILE, {"w1:p1": {"terminal_id": "term-1", "seq": 5, "uuid": UUID,
                                                     "since": time.time() - 2 * 3600}})
    sleeper.cmd_scan(types.SimpleNamespace(dry_run=False, exclude=None))
    assert journal()["w1:p1"]["phase"] == "asleep"


def test_scan_honours_the_configured_dry_run(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    """`dry_run = true` binds a manual tick too: a plain `scan` slept two sessions for real."""
    cfg = state / "config.toml"
    cfg.write_text('idle = "1h"\ndry_run = true\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sleeper.write_json(sleeper.IDLE_FILE, {"w1:p1": {"terminal_id": "term-1", "seq": 5, "uuid": UUID,
                                                     "since": time.time() - 2 * 3600}})
    sleeper.cmd_scan(types.SimpleNamespace(dry_run=False, exclude=None))
    out = capsys.readouterr().out
    assert journal() == {} and "would sleep" in out and "(dry run)" in out


def test_idle_clock_restarts_when_the_session_differs(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """F9: the clock record carries the uuid: a claude replaced by another claude does not inherit it."""
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    clocks = {"w1:p1": {"terminal_id": "term-1", "seq": 5, "uuid": "previous-session", "since": 1.0}}
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "idle_hours": 1.0})
    assert clocks["w1:p1"]["since"] == 1_000_000.0 and clocks["w1:p1"]["uuid"] == UUID and journal() == {}


def test_clock_reset_is_persisted_before_any_sleep(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """G6: a focus pops the clock; a crash before the end-of-tick write used to replay the old `since`."""
    fake.agents = [agent(pane="w1:p1", focused=True), {**agent(pane="w2:p1", name="b", terminal="term-b",
                                                              uuid="u2"), "pid_hint": 2}]
    transcript(Path(str(sleeper.TRANSCRIPTS.parent)), "u2", 30)
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    old = {"terminal_id": "term-1", "seq": 5, "uuid": UUID, "since": 1.0}
    seen: list[dict[str, Any]] = []

    def watch(a: dict[str, Any], *args: Any, **kw: Any) -> Any:
        seen.append(json.loads(sleeper.IDLE_FILE.read_text()))
        return False, "refused: test"

    monkeypatch.setattr(sleeper, "sleep_agent", watch)
    sleeper.watch_tick({"w1:p1": dict(old)}, {**sleeper.DEFAULTS, "idle_hours": 0.0})
    assert seen and "w1:p1" not in seen[0]   # the reset was on disk before the first sleep transaction


def test_focus_on_a_displaced_pane_is_silent(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """F10: the user keeps clicking into a pane running another session; no lock, no log line per click."""
    sleeper.STATE_DIR.mkdir(parents=True, exist_ok=True)
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {"pane_id": "w1:p1", "uuid": "kept", "kind": "claude",
                                                   "phase": "displaced", "wake_got": UUID}})
    monkeypatch.setattr(sleeper, "load_config", lambda: pytest.fail("a displaced pane took the slow path"))
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0 and not sleeper.LOGFILE.exists()


def test_await_stub_wake_reports_a_slow_session_as_started(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """F11: the stub exec'd (row `waking`, its pid alive) but Herdr has not attached the session yet."""
    slept_entry(fake)
    j = journal(); j["w1:p1"].update(phase="waking", waking_pid=4242); sleeper.write_json(sleeper.JOURNAL, j)
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: True)
    assert sleeper.await_stub_wake(journal()["w1:p1"]) is True
    assert "not yet reported" in sleeper.LOGFILE.read_text() and "wake-delegated-unconfirmed" not in events()


# ------------------------------------------------------ G1/G2/G4 stub window

def test_ctrl_c_after_enter_puts_the_row_back(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """G1: SIGINT between the journal write and the exec used to drop the only listed handle."""
    stub_ready(fake, monkeypatch)
    real = fake.__call__

    def interrupted(*a: str, **kw: Any) -> Any:
        if a[:2] == ("pane", "release-agent"):
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(sleeper, "herdr", interrupted)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: pytest.fail("exec after ctrl-c"))
    assert sleeper.cmd_stub("w1:p1", UUID) == 1
    row = journal()["w1:p1"]
    assert row["phase"] == "asleep" and "waking_pid" not in row and "manual" in capsys.readouterr().out


def test_a_waking_row_with_a_live_pid_is_never_woken_again(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """G1/G2: focus, wake, wake-all and snapshot recovery all go through wake_entry or see the row."""
    slept_entry(fake)
    fake.agents[0].pop("agent", None)
    j = journal(); j["w1:p1"].update(phase="waking", waking_pid=4242); sleeper.write_json(sleeper.JOURNAL, j)
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: True)
    assert not sleeper.wake_entry(journal()["w1:p1"])
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=True)) == 1
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 1
    focus_env("w1:p1"); sleeper.cmd_on_focus()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: None)   # cannot tell -> same refusal
    assert not sleeper.wake_entry(journal()["w1:p1"])
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: False)  # the stub died: an ordinary asleep row
    assert sleeper.wake_entry(journal()["w1:p1"]) and journal() == {}


def test_reconcile_restores_asleep_when_the_waking_stub_died(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    fake.agents[0].pop("agent", None)
    j = journal(); j["w1:p1"].update(phase="waking", waking_pid=4242); sleeper.write_json(sleeper.JOURNAL, j)
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: True)
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "waking"           # still going: leave it
    monkeypatch.setattr(sleeper, "waking_alive", lambda e: False)
    fake.calls.clear()
    sleeper.reconcile(fake.agents)
    row = journal()["w1:p1"]
    assert row["phase"] == "asleep" and "waking_pid" not in row
    assert any(c[:2] == ("pane", "report-agent") for c in fake.calls)   # the sidebar row is claimed back


def test_reconcile_marks_a_waking_row_displaced_by_another_session(fake: FakeHerdr) -> None:
    slept_entry(fake)
    j = journal(); j["w1:p1"].update(phase="waking", waking_pid=4242); sleeper.write_json(sleeper.JOURNAL, j)
    fake.agents[0].update(agent="claude", agent_session={"value": "someone-else"})
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "displaced" and journal()["w1:p1"]["wake_got"] == "someone-else"


def test_waking_alive_matches_the_stub_or_the_resumed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    e = {"pane_id": "w1:p1", "uuid": UUID, "waking_pid": 4242}
    monkeypatch.setattr(sleeper.os, "kill", lambda pid, sig: None)
    for line, want in ((f"/usr/bin/python3 /x/herdr-sleeper stub w1:p1 {UUID} /s", True),
                       (f"claude --dangerously-skip-permissions --resume {UUID}", True),   # after the exec
                       ("vim notes.txt", False)):                                         # pid reused
        monkeypatch.setattr(sleeper.subprocess, "run", Ps([line]))
        assert sleeper.waking_alive(e) is want
    monkeypatch.setattr(sleeper.subprocess, "run", Ps([], fail=True))
    assert sleeper.waking_alive(e) is None

    def dead(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(sleeper.os, "kill", dead)
    assert sleeper.waking_alive(e) is False
    assert sleeper.waking_alive({**e, "waking_pid": None}) is False


def test_empty_foreground_list_is_unknown(fake: FakeHerdr) -> None:
    """G2: Herdr answers [] when it could not see the job; an idle shell still lists the shell."""
    entry = slept_entry(fake)
    fake.agents[0].pop("agent", None)
    fake.empty_foreground = True
    assert sleeper.agent_process("w1:p1", "claude")[0] is None
    assert sleeper.stub_running("w1:p1", UUID) is None
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_exec_failure_after_the_row_was_refilled_orphans_the_session(fake: FakeHerdr,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """G4: never `setdefault`: if the pane's row now names another session, ours goes to an orphan key."""
    stub_ready(fake, monkeypatch)

    def boom(b: str, a: list[str]) -> None:
        j = journal(); j["w1:p1"] = {"pane_id": "w1:p1", "uuid": "newer", "kind": "claude", "phase": "asleep"}
        sleeper.write_json(sleeper.JOURNAL, j)
        raise PermissionError(b)

    monkeypatch.setattr(sleeper.os, "execvp", boom)
    assert sleeper.cmd_stub("w1:p1", UUID) == 1
    j = journal()
    assert j["w1:p1"]["uuid"] == "newer" and j[ORPHAN]["uuid"] == UUID and j[ORPHAN]["phase"] == "orphaned"


# --------------------------------------------------------------- G5 config

def test_plugin_config_without_exclude_refuses_while_a_legacy_one_has_it(state: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "cfg" / "config.toml"; cfg.parent.mkdir(); cfg.write_text('idle = "12h"\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    (state / "legacy-config.toml").write_text('exclude = ["orc", "orc-meta"]\ninterval_minutes = 30\n')
    with pytest.raises(sleeper.ConfigError, match="merge"):
        sleeper.load_config()
    monkeypatch.setenv("HERDR_SLEEPER_EXCLUDE", "orc")   # an explicit exclude settles it
    assert sleeper.load_config()[0]["exclude"] == ["orc"]
    monkeypatch.delenv("HERDR_SLEEPER_EXCLUDE")
    cfg.write_text('idle = "12h"\nexclude = []\n')        # so does an explicit empty list
    assert sleeper.load_config()[0]["exclude"] == []
    (state / "legacy-config.toml").write_text("interval_minutes = 30\n")
    cfg.write_text('idle = "12h"\n')                      # a legacy file without excludes is no reason to refuse
    assert sleeper.load_config()[0]["exclude"] == []


def test_focus_delegated_to_the_stub_finishes_the_row(fake: FakeHerdr) -> None:
    """A focus wake handed to the pane's stub confirms it like `wake` does (outside the global lock):
    the `waking` row is finished and the name handed back now, not a watcher tick later."""
    slept_entry(fake)
    fake.agents[0].pop("agent", None)
    fake.stub_panes.add("w1:p1")
    fake.stub_execs_on_enter = True
    j = journal(); j["w1:p1"].update(phase="waking", waking_pid=os.getpid())   # what the stub writes on Enter
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {**j["w1:p1"], "phase": "asleep"}})
    real = fake.__call__

    def stub_marks_waking(*a: str, **kw: Any) -> Any:
        if a[:2] == ("pane", "send-keys"):
            sleeper.write_json(sleeper.JOURNAL, j)
        return real(*a, **kw)

    sleeper.herdr = stub_marks_waking  # restored by the fixture's monkeypatch of `herdr`
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert journal() == {} and fake.agents[0]["name"] == "a"


# ------------------------------------------------------------ review fixes

@pytest.mark.parametrize("styled, state", [
    ("\x1b[2mrun the tests\x1b[0m", "empty"),                 # the dim suggestion
    ("\x1b[38;2;200;2;2mtyped in red\x1b[0m", "draft"),        # truecolour: the 2 is not dim
    ("\x1b[38;5;2mtyped in green\x1b[0m", "draft"),            # palette colour 2
    ("\x1b[48;2;1;2;3;2msuggested\x1b[0m", "empty"),          # dim after a background colour
    ("\x1b[38:2::1:2:3mcolon form\x1b[0m", "draft"),
])
def test_composer_parses_sgr_colour_arguments(styled: str, state: str) -> None:
    screen = "⏺ done\n❯\xa0" + styled + "\n" + "─" * 40 + "\n  ⏵⏵ bypass permissions on\n"
    assert sleeper.composer_state(screen)[0] == state


def test_sleep_yields_to_a_delivery_in_progress(fake: FakeHerdr) -> None:
    with sleeper.mailbox_lock(UUID):
        slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "being delivered" in outcome and fake.signals == [] and journal() == {}
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]


def test_mail_is_never_typed_into_a_draft_or_a_journaled_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.screen = CLAUDE_SCREEN.format(composer="half a thought")
    assert deliver(monkeypatch, "a") == 3 and prompts(fake) == []
    fake.screen = EMPTY_SCREEN
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {**agent(), "uuid": UUID, "kind": "claude", "phase": "exit-requested"}})
    sleeper.flush_mail(fake.agents)
    assert prompts(fake) == [] and len(sleeper.pending_mail(UUID)) == 1
    sleeper.write_json(sleeper.JOURNAL, {})
    sleeper.flush_mail(fake.agents)
    assert len(prompts(fake)) == 1 and sleeper.pending_mail(UUID) == []


@pytest.mark.parametrize("body", ["stop\x1b[2J", "line\rsubmit early", "nul\x00", "csi\x9b"])
def test_deliver_rejects_control_characters(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    assert deliver(monkeypatch, "a", body=body) == 2 and prompts(fake) == []


def test_deliver_types_newlines_and_tabs(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert deliver(monkeypatch, "a", body="one\n\ttwo") == 0 and prompts(fake)[0][3].endswith("one\n\ttwo")


def test_prompt_failure_keeps_the_message_out_of_the_log(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: str, **kw: Any) -> dict[str, Any]:
        if args[:2] == ("agent", "prompt"):
            raise sleeper.SleeperError(f"herdr agent prompt failed: cannot type {args[3]!r}")
        return fake(*args, **kw)
    monkeypatch.setattr(sleeper, "herdr", failing)
    assert deliver(monkeypatch, "a", body="the secret plan") == 3
    for path in (sleeper.EVENTS, sleeper.LOGFILE):
        assert "secret plan" not in (path.read_text() if path.exists() else "")
    assert len(sleeper.pending_mail(UUID)) == 1


def test_deliver_retry_of_a_queued_id_tries_again(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", msg_id="m-2") == 3
    fake.agents[0]["agent_status"] = "idle"
    assert deliver(monkeypatch, "a", msg_id="m-2") == 0 and len(prompts(fake)) == 1
    assert deliver(monkeypatch, "a", msg_id="m-2") == 0 and len(prompts(fake)) == 1


def test_deliver_pending_validates_its_arguments(fake: FakeHerdr) -> None:
    assert sleeper.cmd_deliver_pending(types.SimpleNamespace(uuid="../x", sender="orc")) == 2


def test_hook_matches_claude_names_only_and_yields_to_live_ones(fake: FakeHerdr, state: Path,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda argv, **kw: None)
    send = {"hook_event_name": "PreToolUse", "tool_name": "SendMessage"}
    assert run_hook(monkeypatch, capsys, {**send, "tool_input": {"to": "a", "message": "hi"}}) is None
    assert run_hook(monkeypatch, capsys, {**send, "tool_input": {"to": UUID, "message": "hi"}}) is None
    fake.agents.append({**agent(pane="w2:p1", uuid="u2-session", name="other", terminal="t2"), "pid_hint": 102})
    named_transcript(state, "u2-session", "ts-api-worker")             # a live session took the name
    assert run_hook(monkeypatch, capsys, {**send, "tool_input": {"to": "ts-api-worker", "message": "hi"}}) is None
    assert sleeper.pending_mail(UUID) == []


def test_hook_refuses_to_relay_control_characters(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda argv, **kw: pytest.fail("woke for a bad message"))
    out = run_hook(monkeypatch, capsys, {"hook_event_name": "PreToolUse", "tool_name": "SendMessage",
                                         "tool_input": {"to": "ts-api-worker", "message": "x\x1b[2J"}})
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "control characters" in hso["permissionDecisionReason"]
    assert sleeper.pending_mail(UUID) == []


def test_wake_keeps_a_row_that_now_names_another_session(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {**entry, "uuid": "u-later"}})   # slept again meanwhile
    assert sleeper.wake_entry(entry) is True
    assert journal()["w1:p1"]["uuid"] == "u-later"


def test_restart_skips_runtimes_and_does_not_loop(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 3600}}
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/opt/homebrew/bin/node")
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == []                                            # an npm install: build unknown
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.305")   # and it stays old after the wake
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert len(fake.signals) == 1 and "restart-failed" in events()
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 3600}}
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert len(fake.signals) == 1                                        # cooldown: no restart loop


def test_crons_count_over_the_whole_transcript(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "TRANSCRIPT_TAIL_BYTES", 500)
    path = transcript(state, UUID, 0)
    path.write_text(_tool("CronCreate", _iso(3 * 86400)) + _prompt(_iso(600)) * 50)
    assert sleeper.scheduled_work(UUID) == "1 session cron job(s) scheduled"   # long before the tail
    with path.open("a") as fh:
        fh.write(_tool("CronDelete", _iso(60)))
    assert sleeper.scheduled_work(UUID) is None                                # read incrementally
    path.write_text(_tool("CronCreate", _iso(60)) + _prompt(_iso(30)) * 50)     # rewritten, not appended
    assert sleeper.scheduled_work(UUID) == "1 session cron job(s) scheduled"


def test_early_sleep_passes_over_a_refusing_pane(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clocks = _three_idle_agents(fake, state)
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    fake.helpers = {300: "zsh"}                    # the longest idle runs a background shell
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "max_live": 2})
    assert list(journal()) == ["w2:p1"]


# ------------------------------------------- B-274: surviving a Herdr restart

def test_claim_reports_stub_resume_argv(fake: FakeHerdr) -> None:
    """The stub's argv is reported as the pane's resume command, so Herdr types it back after a restart."""
    slept_entry(fake)
    want = sleeper.stub_argv("w1:p1", UUID)
    assert fake.resumes["w1:p1"] == want
    assert want[0] == "env" and str(SCRIPT) in want                  # a plain command first, the script by path
    assert want[-4:] == ["stub", "w1:p1", UUID, str(sleeper.STATE_DIR)]
    assert stub_run(fake).endswith(shlex.join(want))                 # what is typed and what is reported agree
    assert sleeper.resume_argv_problem(want) is None and "resume-not-reported" not in events()


def test_no_resume_when_stub_not_typed(fake: FakeHerdr) -> None:
    fake.foreground = "vim"   # no bare shell after the exit: no stub, so a restart must not start one
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]
    reports = [c for c in fake.calls if c[:2] == ("pane", "report-agent")]
    assert reports and all("--" not in c for c in reports) and fake.resumes == {}
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT


@pytest.mark.parametrize("argv, needle", [
    ([], "empty"),
    (["/usr/bin/env", "x"], "plain command"),
    (["-env"], "plain command"),
    (["env", "a\x07b"], "control"),
    (["env", "a\x85b"], "control"),                     # C1 controls count too (Rust char::is_control)
    (["env", *["a"] * 64], "arguments"),
    (["env", "a" * 8200], "bytes"),
    (["env", "/tmp/it's"], "apostrophe"),
])
def test_resume_argv_problem_mirrors_herdr(argv: list[str], needle: str) -> None:
    why = sleeper.resume_argv_problem(argv)
    assert why and needle in why
    ok = ["env", "HERDR_SESSION=default", "/x/herdr-sleeper", "stub", "w1:p1", UUID]
    assert sleeper.resume_argv_problem(ok) is None


def test_resume_argv_validation_falls_back(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A state dir with an apostrophe makes a resume command Herdr refuses: never sent, plain claim instead."""
    monkeypatch.setattr(sleeper, "STATE_DIR", state / "it's state")
    assert slept_entry(fake)["phase"] == "asleep"
    reports = [c for c in fake.calls if c[:2] == ("pane", "report-agent")]
    assert len(reports) == 1 and "--" not in reports[0]
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT and fake.resumes == {}
    assert "resume-not-reported" in events() and "apostrophe" in sleeper.LOGFILE.read_text()


@pytest.mark.parametrize("code", ["resume_not_accepted", "invalid_resume_argv"])
def test_claim_retries_without_resume_on_resume_not_accepted(fake: FakeHerdr, code: str) -> None:
    fake.reject_resume = code
    slept_entry(fake)
    reports = [c for c in fake.calls if c[:2] == ("pane", "report-agent")]
    assert len(reports) == 2 and "--" in reports[0] and "--" not in reports[1]
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT and fake.resumes == {}   # the row is claimed anyway
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if "resume-not-reported" in l][-1]
    assert code in rec["reason"]


def test_reconcile_reclaims_restarted_pane_with_running_stub(fake: FakeHerdr) -> None:
    slept_entry(fake)
    fake.restart()
    assert fake.stub_panes == {"w1:p1"} and "agent" not in fake.agents[0]   # stub typed back, claim gone
    assert sleeper.identity(journal()["w1:p1"], fake.agents[0]) == "restarted"
    sleeper.reconcile(fake.agents)
    j = journal()["w1:p1"]
    assert j["terminal_id"] == "term-1-r" and j["phase"] == "asleep"       # stamped together with the claim
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    assert fake.resumes["w1:p1"] == sleeper.stub_argv("w1:p1", UUID)     # reported again for the next restart
    rec = json.loads(sleeper.EVENTS.read_text().splitlines()[-1])
    assert rec["event"] == "reconciled" and rec["outcome"] == "reclaimed-after-restart"
    assert rec["terminal_id"] == "term-1-r"
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)                                          # settled: nothing more to do
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:])
    fake.stub_execs_on_enter = True                                         # and Enter goes to the stub
    assert sleeper.wake_entry(journal()["w1:p1"]) == sleeper.DELEGATED
    assert "w1:p1" not in fake.resumes                                      # released: Herdr drops the command


def test_reconcile_reclaims_a_restarted_pane_listed_under_our_placeholder(fake: FakeHerdr) -> None:
    """Should Herdr list the restored pane with the resume command's agent label, it is still re-claimed."""
    slept_entry(fake)
    fake.restart()
    fake.agents[0]["agent"] = sleeper.CLAIM_AGENT
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["terminal_id"] == "term-1-r" and events()[-1] == "reconciled"


def test_reconcile_leaves_restarted_pane_without_stub(fake: FakeHerdr) -> None:
    fake.foreground = "vim"
    slept_entry(fake)                                  # asleep with stub=false: no resume reported
    fake.foreground = None
    fake.restart()
    assert fake.stub_panes == set()
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["terminal_id"] == "term-1" and "agent" not in fake.agents[0]
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:])
    assert sleeper.wake_entry(journal()["w1:p1"]) is True     # focus/`wake` start it into the bare shell


def test_restart_without_a_saved_resume_leaves_a_shell_that_wakes(fake: FakeHerdr) -> None:
    """An older Herdr (or a refused resume): the stub does not come back; nothing is re-claimed or
    re-stamped, and the pane still wakes with `agent start`."""
    slept_entry(fake)
    fake.restart(keep_resume=False)
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["terminal_id"] == "term-1" and "agent" not in fake.agents[0]
    assert sleeper.wake_entry(journal()["w1:p1"]) is True and journal() == {}


def test_merge_snapshot_keeps_flags_after_native_resume(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == ["--dangerously-skip-permissions"]
    fake.argv = ["--resume", UUID]               # Herdr's native restore: `claude --resume <uuid>`, no flags
    fake.agents[0]["terminal_id"] = "term-1-r"
    sleeper.merge_snapshot(fake.agents)
    row = sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]
    assert row["argv"] == ["--dangerously-skip-permissions"] and row["terminal_id"] == "term-1-r"
    assert slept_entry(fake)["argv"] == ["--dangerously-skip-permissions"]   # and its wake gets them back


def test_reconcile_reports_resume_for_a_row_slept_without_one(fake: FakeHerdr) -> None:
    """A row slept by v0.1.1: claimed, stub running, but no resume command reported (and no
    `resume_reported` on the row). Reconcile reports it once, so a restart brings the stub back."""
    slept_entry(fake)
    j = journal()
    j["w1:p1"].pop("resume_reported")
    sleeper.write_json(sleeper.JOURNAL, j)
    fake.resumes.clear()
    fake.stub_panes = {"w1:p1"}
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    sleeper.reconcile(fake.agents)
    assert fake.resumes["w1:p1"] == sleeper.stub_argv("w1:p1", UUID)
    row = journal()["w1:p1"]
    assert row["resume_reported"] is True and row["terminal_id"] == "term-1" and row["phase"] == "asleep"
    rec = json.loads(sleeper.EVENTS.read_text().splitlines()[-1])
    assert rec["event"] == "reconciled" and rec["outcome"] == "resume-reported"
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)                                  # settled: no claim every tick
    assert not any(c[:2] in (("pane", "report-agent"), ("pane", "process-info")) for c in fake.calls[n:])
    fake.restart()                                                  # and the stub now survives a restart
    assert fake.stub_panes == {"w1:p1"}
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["terminal_id"] == "term-1-r" and fake.agents[0]["agent"] == sleeper.CLAIM_AGENT


def test_sleep_records_whether_the_resume_command_was_accepted(fake: FakeHerdr) -> None:
    assert slept_entry(fake)["resume_reported"] is True


def test_reconcile_leaves_a_slept_row_without_stub_process_alone(fake: FakeHerdr) -> None:
    """No stub in the foreground (ctrl-c'd, or never typed): nothing to report as a resume command."""
    slept_entry(fake)
    j = journal()
    j["w1:p1"].pop("resume_reported")
    sleeper.write_json(sleeper.JOURNAL, j)
    fake.resumes.clear()
    fake.stub_panes = set()
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:]) and fake.resumes == {}
    assert "resume_reported" not in journal()["w1:p1"]


def test_refused_wake_gets_its_claim_and_resume_back(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    """wake_entry releases our claim before its checks; Herdr drops the resume command with it. A
    refusal after that leaves the stub without either, until reconcile claims it again."""
    slept_entry(fake)
    fake.stub_panes = {"w1:p1"}
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "w9:p9")
    assert sleeper.wake_entry(journal()["w1:p1"]) is False
    assert "agent" not in fake.agents[0] and "w1:p1" not in fake.resumes   # released, command dropped
    sleeper.reconcile(fake.agents)
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    assert fake.resumes["w1:p1"] == sleeper.stub_argv("w1:p1", UUID)
    assert journal()["w1:p1"]["resume_reported"] is True


def test_refused_resume_is_not_retried_every_tick(fake: FakeHerdr) -> None:
    fake.reject_resume = "resume_not_accepted"
    assert slept_entry(fake)["resume_reported"] is False
    fake.stub_panes = {"w1:p1"}
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)                     # claimed, refusal on record: nothing to do
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:])
    sleeper.release("w1:p1")                           # a refused wake released it: one more attempt
    sleeper.reconcile(fake.agents)
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT and journal()["w1:p1"]["resume_reported"] is False
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:])


def test_put_back_drops_the_resume_marker(fake: FakeHerdr) -> None:
    """A waking stub released the pane (Herdr dropped the command); the row it returns to says so."""
    row = {**slept_entry(fake), "phase": "waking", "waking_pid": 1, "waking_at": "x"}
    j: dict[str, Any] = {}
    sleeper.put_back(j, "w1:p1", row, "test")
    assert j["w1:p1"]["phase"] == "asleep" and "resume_reported" not in j["w1:p1"]


def test_merge_snapshot_drops_flags_on_a_manual_bare_resume(fake: FakeHerdr) -> None:
    """`claude --resume <uuid>` typed by hand in the terminal that ran the session is not Herdr's
    restore: the flags the user left out (here --dangerously-skip-permissions) stay out."""
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == ["--dangerously-skip-permissions"]
    fake.argv = ["--resume", UUID]                       # same terminal: a manual restart without flags
    fake.agents[0]["pid_hint"] = 202
    sleeper.merge_snapshot(fake.agents)
    row = sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]
    assert row["argv"] == [] and "flags_inherited" not in row
    entry = slept_entry(fake)
    assert entry["argv"] == []                           # and its wake does not bring them back
    assert sleeper.wake_entry(entry)
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert "--dangerously-skip-permissions" not in start


def test_sleep_ignores_snapshot_flags_for_a_manual_bare_resume(fake: FakeHerdr) -> None:
    """sleep-pane can run before the watcher refreshed panes.json: the same rule applies there."""
    sleeper.merge_snapshot(fake.agents)
    fake.argv = ["--resume", UUID]
    assert slept_entry(fake)["argv"] == []


def test_inherited_flags_stay_until_another_process_resumes_by_hand(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.argv = ["--resume", UUID]
    fake.agents[0]["terminal_id"] = "term-1-r"           # Herdr's restore: a new terminal
    sleeper.merge_snapshot(fake.agents)
    row = sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]
    assert row["argv"] == ["--dangerously-skip-permissions"] and row["flags_inherited"] is True
    assert row["inherited_pid"] == 101
    sleeper.merge_snapshot(fake.agents)                  # later ticks, same process: kept
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == ["--dangerously-skip-permissions"]
    fake.process_info_broken = True                      # a hiccup keeps the row and its marker
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["flags_inherited"] is True
    fake.process_info_broken = False
    fake.agents[0]["pid_hint"] = 303                     # exited and resumed by hand in that terminal
    sleeper.merge_snapshot(fake.agents)
    row = sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]
    assert row["argv"] == [] and "flags_inherited" not in row


def test_merge_snapshot_still_updates_on_real_argv_change(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.argv = ["--model", "opus", "--resume", UUID]       # flags of its own: what runs is what counts
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == ["--model", "opus"]
    fake.argv = []                                           # restarted by hand without flags
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["argv"] == []
    fake.agents[0]["agent_session"] = {"value": "other-session"}   # a bare --resume of another session
    fake.argv = ["--resume", "other-session"]                       # inherits nothing
    sleeper.merge_snapshot(fake.agents)
    row = sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]
    assert row["uuid"] == "other-session" and row["argv"] == []


def external_watcher(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('watcher = "external"\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)


def test_external_watcher_missing_warns_rate_limited(state: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    spawned: list[Any] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda argv, **kw: spawned.append(argv) or types.SimpleNamespace(pid=1))
    REAL_HEAL()
    assert "watcher-missing" not in events() and spawned   # plugin mode heals instead (spawns, never for real)
    external_watcher(state, monkeypatch)
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a watcher"))
    REAL_HEAL(); REAL_HEAL()
    assert events().count("watcher-missing") == 1             # once an hour, not on every focus hook
    old = time.time() - 2 * 3600
    os.utime(sleeper.WATCHER_MISSING, (old, old))
    REAL_HEAL()
    assert events().count("watcher-missing") == 2
    assert sleeper.cmd_startup() == 0                        # server start says so every time
    assert events().count("watcher-missing") == 3
    assert "WARN the external herdr-sleeper watcher is not running" in sleeper.LOGFILE.read_text()
    fh = hold_flock(sleeper.WATCHER_LOCK)                     # the service is back
    os.utime(sleeper.WATCHER_MISSING, (old, old))
    REAL_HEAL(); sleeper.cmd_startup()
    fh.close()
    sleeper.cmd_stop(types.SimpleNamespace())                 # `stop` is deliberate, not missing
    os.utime(sleeper.WATCHER_MISSING, (old, old))
    REAL_HEAL(); sleeper.cmd_startup()
    assert events().count("watcher-missing") == 3


def test_list_shows_watcher_missing_header(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    capsys.readouterr()
    sleeper.cmd_list(types.SimpleNamespace())
    assert "WARNING" not in capsys.readouterr().out           # plugin mode: no header
    external_watcher(state, monkeypatch)
    sleeper.cmd_list(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("WARNING: the external herdr-sleeper watcher is not running")
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True)) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out) and "WARNING" in cap.err      # --json output stays parseable
    sleeper.cmd_presence(types.SimpleNamespace(json=False))
    assert capsys.readouterr().out.startswith("WARNING")
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    assert "watcher is not running" in ctx["hookSpecificOutput"]["additionalContext"]
    fh = hold_flock(sleeper.WATCHER_LOCK)
    sleeper.cmd_list(types.SimpleNamespace())
    fh.close()
    assert "WARNING" not in capsys.readouterr().out


def test_hook_notes_a_missing_watcher_with_nobody_asleep(fake: FakeHerdr, state: Path,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    external_watcher(state, monkeypatch)
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    text = ctx["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("Note: herdr-sleeper's watcher is not running") and "Sleeping sessions" not in text


def test_server_down_retries_at_poll_interval(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    def tick(n: int) -> None:
        if n <= 12:   # the watcher came up before the Herdr server (a reboot)
            raise sleeper.HerdrError("herdr agent list failed: no herdr server is running", "server_not_running")

    sleeps = run_loop(monkeypatch, tick, 14)
    log = sleeper.LOGFILE.read_text()
    assert sum(sleeps) == 14 * 60                    # every retry at the 60 s poll, no exponential backoff
    assert "backing off" not in log and log.count("herdr server not reachable") == 1
    assert "herdr server reachable again after 12 tick(s)" in log


@pytest.mark.parametrize("exc, down", [
    (sleeper.HerdrError("x", "server_not_running"), True),
    (sleeper.HerdrError("x", "server_unavailable"), True),     # lab: a tick during `server stop`
    (sleeper.SleeperError('herdr agent list failed: {"error":{"code":"server_unavailable",'
                          '"message":"server is shutting down"}}'), True),
    (sleeper.SleeperError("herdr agent list failed: Connection refused (os error 61)"), True),
    (sleeper.SleeperError("herdr agent list failed: No such file or directory (os error 2)"), True),
    (sleeper.HerdrError("herdr pane get failed", "pane_not_found"), False),
    (sleeper.SleeperError("herdr agent list timed out after 30.0s"), False),
])
def test_server_down_classifies_errors(exc: Exception, down: bool) -> None:
    assert sleeper.server_down(exc) is down


def test_herdr_error_carries_the_code(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = json.dumps({"id": "1", "error": {"code": "server_not_running", "message": "no herdr server is running"}})
    monkeypatch.setattr(sleeper.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="", stderr=err + "\n", returncode=1))
    with pytest.raises(sleeper.HerdrError) as info:
        sleeper.herdr("agent", "list")
    assert info.value.code == "server_not_running" and sleeper.server_down(info.value)
    assert sleeper.herdr("agent", "list", check=False) == {}


@pytest.mark.parametrize("full", [True, False], ids=["F_FULLFSYNC", "fsync-fallback"])
def test_write_json_syncs_to_the_drive(state: Path, monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    """File, then replace, then directory. On macOS fsync stops at the drive's write cache, so the
    sync is F_FULLFSYNC; where that is missing or refused, plain fsync."""
    order: list[str] = []
    real_replace = os.replace

    def kind(fd: int) -> str:
        return "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"

    def fsync(fd: int) -> None:
        order.append("fsync:" + kind(fd))

    def fcntl_call(fd: int, cmd: int, *rest: Any) -> int:
        assert cmd == 51                                # F_FULLFSYNC (sys/fcntl.h)
        if not full:
            raise OSError(45, "Operation not supported")
        order.append("full:" + kind(fd))
        return 0

    def replace(a: Any, b: Any) -> None:
        order.append("replace")
        real_replace(a, b)

    monkeypatch.setattr(sleeper.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(sleeper.fcntl, "fcntl", fcntl_call)
    monkeypatch.setattr(sleeper.os, "fsync", fsync)
    monkeypatch.setattr(sleeper.os, "replace", replace)
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {"uuid": UUID}})
    how = "full" if full else "fsync"
    assert order == [f"{how}:file", "replace", f"{how}:dir"]   # data on disk before it replaces the old file
    assert sleeper.read_json(sleeper.JOURNAL) == {"w1:p1": {"uuid": UUID}}


def test_write_json_without_f_fullfsync_uses_fsync(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.delattr(sleeper.fcntl, "F_FULLFSYNC", raising=False)   # another platform
    monkeypatch.setattr(sleeper.os, "fsync", calls.append)
    sleeper.write_json(sleeper.JOURNAL, {"a": 1})
    assert len(calls) == 2 and sleeper.read_json(sleeper.JOURNAL) == {"a": 1}


# ------------------------------------------- the real CLI's output shapes (lab finding, v0.1.2)

class CliShape:
    """subprocess.run as the real Herdr v0.9.5 CLI answers: `send_ok_request` commands (report-agent,
    release-agent, report-metadata, send-keys …) print nothing and exit 0 on success, or print the
    error JSON on stderr and exit 1; everything else prints a `{"result": …}` envelope."""

    def __init__(self, reject_resume: str | None = None) -> None:
        self.reject_resume = reject_resume
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kw: Any) -> types.SimpleNamespace:
        args = list(cmd[1:])
        self.calls.append(args)
        if tuple(args[:2]) in sleeper.OK_ONLY_COMMANDS:
            if args[:2] == ["pane", "report-agent"] and "--" in args and self.reject_resume:
                err = json.dumps({"id": "cli:request", "error": {"code": self.reject_resume, "message": "no"}})
                return types.SimpleNamespace(stdout="", stderr=err + "\n", returncode=1)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)
        if args[:2] == ["pane", "get"]:
            envelope = {"id": "cli:pane:get",
                        "result": {"type": "pane_info", "pane": {"pane_id": args[2], "agent": sleeper.CLAIM_AGENT}}}
            return types.SimpleNamespace(stdout=json.dumps(envelope) + "\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)


RESUME = ["env", "HERDR_SESSION=default", "/x/herdr-sleeper", "stub", "w1:p1", UUID]
REPORT = ("pane", "report-agent", "w1:p1", "--source", "x", "--agent", "y", "--state", "unknown")


def test_ok_only_commands_succeed_without_output(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.subprocess, "run", CliShape())
    assert sleeper.herdr(*REPORT, "--", *RESUME) == {}
    assert sleeper.herdr("pane", "release-agent", "w1:p1", "--source", "x", "--agent", "y") == {}
    with pytest.raises(sleeper.SleeperError):   # a command that answers with an envelope still needs one
        sleeper.herdr("agent", "list")


def test_ok_only_command_error_on_stderr_raises_with_code(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.subprocess, "run", CliShape(reject_resume="resume_not_accepted"))
    with pytest.raises(sleeper.HerdrError) as info:
        sleeper.herdr(*REPORT, "--", *RESUME)
    assert info.value.code == "resume_not_accepted"


def test_claim_reports_resume_against_the_real_cli_shape(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lab, Herdr v0.9.5: the resume report succeeded but was logged `resume-not-reported` (exit 0 with
    no stdout read as a failure), and the claim was sent a second time without it."""
    cli = CliShape()
    monkeypatch.setattr(sleeper.subprocess, "run", cli)
    assert sleeper.claim("w1:p1", "claude", resume=RESUME)
    reports = [c for c in cli.calls if c[:2] == ["pane", "report-agent"]]
    assert len(reports) == 1 and reports[0][reports[0].index("--") + 1:] == RESUME
    assert "resume-not-reported" not in events()


def test_claim_falls_back_on_a_refusal_from_the_real_cli(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = CliShape(reject_resume="resume_not_accepted")
    monkeypatch.setattr(sleeper.subprocess, "run", cli)
    assert sleeper.claim("w1:p1", "claude", resume=RESUME)
    reports = [c for c in cli.calls if c[:2] == ["pane", "report-agent"]]
    assert len(reports) == 2 and "--" in reports[0] and "--" not in reports[1]
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if "resume-not-reported" in l][-1]
    assert rec["reason"].startswith("resume_not_accepted")


def test_reconcile_labels_and_claims_a_late_exit(fake: FakeHerdr) -> None:
    """Lab, Herdr v0.9.5: an agent that took longer than EXIT_WAIT_SECONDS to leave was settled to
    `asleep` with no 💤 label and no claim, so the next Herdr restart orphaned its own pane."""
    fake.exit_leaves_agent = False
    fake.agents[0]["label"] = "mylabel"
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert journal()["w1:p1"]["phase"] == "exit-requested" and fake.agents[0]["label"] == "mylabel"
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    sleeper.reconcile(fake.agents)
    j = journal()["w1:p1"]
    assert j["phase"] == "asleep" and j["stub"] is False
    assert fake.agents[0]["label"] == "💤 mylabel" and fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    assert fake.resumes == {}                       # no stub in the pane, so nothing for a restart to start
    fake.restart()
    assert sleeper.identity(journal()["w1:p1"], fake.agents[0]) == "restarted"   # not "reused"
    sleeper.reconcile(fake.agents)
    assert not any(k.startswith("orphan:") for k in journal())
    assert sleeper.wake_entry(journal()["w1:p1"]) is True                        # focus/`wake` start it


def test_reconcile_does_not_label_a_late_exit_in_another_terminal(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    fake.agents[0]["terminal_id"] = "term-other"    # the pane id now names another terminal
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)
    assert not any(c[:2] in (("pane", "rename"), ("pane", "report-agent")) for c in fake.calls[n:])


def test_reconcile_clears_the_label_when_herdr_resumed_the_session(fake: FakeHerdr) -> None:
    """Lab, Herdr v0.9.5: a pane slept without a stub keeps Herdr's own claude resume plan, so a
    restart brings the session back awake under the 💤 label; dropping the row must clear the label."""
    fake.agents[0]["label"] = "mylabel"
    fake.foreground = "sleep"                     # the shell never came back after the exit: no stub
    assert sleeper.sleep_agent(agent(), 12, set(), dry_run=False)[0]
    assert journal()["w1:p1"]["stub"] is False and fake.agents[0]["label"] == "💤 mylabel"
    fake.agents[0].update(agent="claude", agent_session={"value": UUID}, terminal_id="term-1-r")  # native resume
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["seen_running"] == 1 and fake.agents[0]["label"] == "💤 mylabel"
    sleeper.reconcile(fake.agents)
    assert journal() == {} and fake.agents[0]["label"] == "mylabel"


def test_orphaned_restarted_stub_drops_its_resume_command(fake: FakeHerdr) -> None:
    """Lab, Herdr v0.9.5: a slept pane whose 💤 label was removed comes back from a restart with our
    stub (the reported resume command) but no label: orphaned, and the stale command is dropped so
    the next restart does not type a stub for an orphan again."""
    slept_entry(fake)
    fake.agents[0]["label"] = None                # the owner renamed the pane
    fake.restart()
    assert fake.stub_panes == {"w1:p1"} and "w1:p1" in fake.resumes
    sleeper.reconcile(fake.agents)
    assert list(journal()) == ["orphan:" + UUID[:8]] and fake.resumes == {}
    assert "agent" not in fake.agents[0]          # claimed only to release it
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)                # orphans are by hand only: nothing more happens
    assert not any(c[:2] in (("pane", "report-agent"), ("pane", "release-agent")) for c in fake.calls[n:])


def test_orphaned_pane_drops_the_resume_even_when_the_stub_already_left(fake: FakeHerdr) -> None:
    """Lab race: the watcher orphaned the row before the restored stub read the journal; the stub
    found no row and left a shell, but Herdr still had the command saved for the next restart."""
    slept_entry(fake)
    fake.agents[0]["label"] = None
    fake.restart()
    fake.stub_panes.clear()                       # the stub already exited back to the shell
    assert "w1:p1" in fake.resumes
    sleeper.reconcile(fake.agents)
    assert list(journal()) == ["orphan:" + UUID[:8]] and fake.resumes == {}


def test_orphan_slept_without_a_stub_is_not_claimed(fake: FakeHerdr) -> None:
    fake.foreground = "sleep"                     # no stub, so no resume command was ever reported
    slept_entry(fake)
    fake.foreground = None
    fake.agents[0]["label"] = None
    fake.restart()
    n = len(fake.calls)
    sleeper.reconcile(fake.agents)
    assert list(journal()) == ["orphan:" + UUID[:8]]
    assert not any(c[:2] == ("pane", "report-agent") for c in fake.calls[n:])


# ------------------------------------------------------- B-244: insomnia

def _claim(monkeypatch: pytest.MonkeyPatch, reason: str | None = "ssh master", duration: str | None = None,
           pane: str = "w1:p1", parent_chain: list[int] | None = None) -> int:
    """`insomnia claim` from inside the agent in `pane` (its pid 101 is our ancestor unless told otherwise)."""
    monkeypatch.setenv("HERDR_PANE_ID", pane)
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101, 50, 1] if parent_chain is None else parent_chain)
    return sleeper.cmd_insomnia(types.SimpleNamespace(action="claim", reason=reason, duration=duration))


def _release(monkeypatch: pytest.MonkeyPatch, pane: str | None = None, force: bool = False,
             caller: str | None = "w1:p1") -> int:
    if caller:
        monkeypatch.setenv("HERDR_PANE_ID", caller)
    else:
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101, 50, 1])
    return sleeper.cmd_insomnia(types.SimpleNamespace(action="release", pane=pane, force=force))


def _event_rows(name: str) -> list[dict[str, Any]]:
    return [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if f'"{name}"' in l] \
        if sleeper.EVENTS.exists() else []


def test_ancestors_walks_up_and_stops_on_cycles() -> None:
    table = {900: (800, "zsh"), 800: (101, "bash"), 101: (50, "claude"), 50: (1, "zsh"), 1: (0, "launchd")}
    assert sleeper.ancestors(table, 900) == [800, 101, 50, 1]
    assert sleeper.ancestors({5: (6, "a"), 6: (5, "b")}, 5) == [6, 5]
    assert sleeper.ancestors({}, 900) == []


def test_insomnia_claim_writes_a_claim_keyed_by_session(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch, duration="4h") == 0
    rec = sleeper.read_json(sleeper.INSOMNIA)[UUID]
    assert rec["reason"] == "ssh master" and rec["until"] == 1_000_000.0 + 4 * 3600 and rec["pane"] == "w1:p1"
    assert oct(sleeper.INSOMNIA.stat().st_mode & 0o777) == "0o600"
    assert _event_rows("insomnia-claimed")[-1]["renewed"] is False
    token = [c for c in fake.calls if c[:2] == ("pane", "report-metadata") and sleeper.INSOMNIA_SOURCE in c]
    assert token and "insomnia=ssh master" in token[-1]
    assert _claim(monkeypatch, reason=None) == 0                         # renewing keeps the start, drops --for
    rec2 = sleeper.read_json(sleeper.INSOMNIA)[UUID]
    assert rec2["at"] == rec["at"] and rec2["until"] is None and _event_rows("insomnia-claimed")[-1]["renewed"]


@pytest.mark.parametrize("kw, code, needle", [
    ({"reason": "a\x1b[2Jb"}, 2, "control characters"),
    ({"reason": "x" * 121}, 2, "longer than"),
    ({"reason": "  "}, 2, "empty"),
    ({"duration": "soon"}, 2, "not a duration"),
    ({"duration": "0m"}, 2, "longer than zero"),
    ({"parent_chain": [7, 1]}, 1, "does not run under the agent"),
    ({"pane": "w9:p9"}, 1, "runs no agent"),
])
def test_insomnia_claim_refusals(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                 kw: dict[str, Any], code: int, needle: str) -> None:
    assert _claim(monkeypatch, **kw) == code
    assert needle in capsys.readouterr().err and not sleeper.INSOMNIA.exists()


def test_insomnia_claim_needs_a_herdr_pane_and_a_session(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101])
    assert sleeper.cmd_insomnia(types.SimpleNamespace(action="claim", reason=None, duration=None)) == 1
    assert "no HERDR_PANE_ID" in capsys.readouterr().err
    fake.agents[0].pop("agent_session")
    assert _claim(monkeypatch) == 1 and "no session id" in capsys.readouterr().err
    fake.agents[0]["agent"] = "codex"
    assert _claim(monkeypatch) == 1 and "never slept" in capsys.readouterr().err
    assert not sleeper.INSOMNIA.exists()


def test_insomnia_keeps_a_due_session_awake_until_released(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch) == 0
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 13 * 3600}}
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert journal() == {} and fake.signals == []
    ok, reason, _ = sleeper.assess(fake.agents[0], fake.agents, 12, {}, idle_since=0.0)
    assert not ok and reason == "insomnia (ssh master)"
    assert sleeper.sleep_agent(fake.agents[0], 12, set(), dry_run=False, allow_focused=True)[1] == \
        "refused: insomnia (ssh master)"                                 # sleep-pane honours it too
    assert _release(monkeypatch) == 0 and _event_rows("insomnia-released")[-1]["forced"] is False
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert list(journal()) == ["w1:p1"]


def test_insomnia_is_passed_over_by_max_live_and_pressure(fake: FakeHerdr, state: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    clocks = _three_idle_agents(fake, state)                             # w1 longest idle, then w2, then w3
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch) == 0                                      # w1 holds insomnia
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "max_live": 1})
    assert sorted(journal()) == ["w2:p1"]                                # w3 is under pressure_idle
    monkeypatch.setattr(sleeper, "memory_pressure", lambda: 4)
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert "w1:p1" not in journal() and (101, signal.SIGTERM) not in fake.signals


def test_insomnia_claim_survives_a_herdr_restart(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch) == 0
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    tokens = len([c for c in fake.calls if sleeper.INSOMNIA_SOURCE in c])
    fake.restart()                                                       # new terminal id, the name is gone
    fake.agents[0].pop("name")
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 13 * 3600}}
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert journal() == {}
    assert len([c for c in fake.calls if sleeper.INSOMNIA_SOURCE in c]) == tokens + 1   # the token comes back
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert len([c for c in fake.calls if sleeper.INSOMNIA_SOURCE in c]) == tokens + 1   # once, not every tick


def test_damaged_insomnia_file_holds_every_session_and_stays(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.ensure_state_dir()
    for text in ("{not json", '{"' + UUID + '": "x"}', '{"../x": {"at": 1}}', '{"' + UUID + '": {"at": 1, "until": "soon"}}'):
        sleeper.INSOMNIA.write_text(text)
        ok, reason, _ = sleeper.assess(fake.agents[0], fake.agents, 12, {}, idle_since=0.0)
        assert not ok and reason.startswith("insomnia claims unreadable")
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": time.time() - 13 * 3600}}
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert journal() == {} and fake.signals == []
    assert _claim(monkeypatch) == 1 and _release(monkeypatch) == 1      # never written over
    assert sleeper.INSOMNIA.read_text() == '{"' + UUID + '": {"at": 1, "until": "soon"}}'
    assert not list(sleeper.STATE_DIR.glob("insomnia.json.damaged-*"))  # never quarantined
    assert sleeper.cmd_insomnia(types.SimpleNamespace(action="list", json=False)) == 2
    capsys.readouterr()
    sleeper.cmd_presence(types.SimpleNamespace(json=False))
    assert "malformed claim" in capsys.readouterr().out


def test_insomnia_expires_with_an_event(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: now["t"])
    assert _claim(monkeypatch, duration="1h") == 0
    now["t"] += 2 * 3600
    assert sleeper.insomnia_reason(UUID) is None                         # expired: no longer holds
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert sleeper.read_json(sleeper.INSOMNIA) == {} and _event_rows("insomnia-expired")
    clears = [c for c in fake.calls if "--clear-token" in c]
    assert clears and clears[-1][2] == "w1:p1"


def test_stale_insomnia_is_reported_daily_and_kept(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: now["t"])
    assert _claim(monkeypatch) == 0
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": now["t"] - 23 * 3600}}
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert not _event_rows("insomnia-stale")
    now["t"] += 2 * 3600
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert [r["state"] for r in _event_rows("insomnia-stale")] == ["idle 25h"]
    now["t"] += 24 * 3600
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert len(_event_rows("insomnia-stale")) == 2 and UUID in sleeper.read_json(sleeper.INSOMNIA)
    assert journal() == {}


def test_insomnia_of_a_session_that_is_gone_is_reported_stale(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: now["t"])
    assert _claim(monkeypatch) == 0
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")    # the user quit the agent
    now["t"] += 25 * 3600
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert [r["state"] for r in _event_rows("insomnia-stale")] == ["session not running"]
    assert _release(monkeypatch, pane=UUID) == 1                          # not the caller's own: needs --force
    assert _release(monkeypatch, pane=UUID, force=True, caller=None) == 0
    assert sleeper.read_json(sleeper.INSOMNIA) == {} and _event_rows("insomnia-released")[-1]["forced"] is True


def test_insomnia_release_of_another_session_needs_force(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents.append({**agent(pane="w2:p1", uuid="u2-session", name="orc", terminal="term-2"), "pid_hint": 102})
    assert _claim(monkeypatch) == 0                                      # w1 ("a") claims
    assert _release(monkeypatch, pane="a", caller="w2:p1") == 1
    assert "--force" in capsys.readouterr().err and UUID in sleeper.read_json(sleeper.INSOMNIA)
    assert _release(monkeypatch, pane="a", force=True, caller="w2:p1") == 0
    assert sleeper.read_json(sleeper.INSOMNIA) == {}
    assert _event_rows("insomnia-released")[-1]["by"] == "orc"
    assert "released by orc (--force)" in sleeper.LOGFILE.read_text()
    assert _release(monkeypatch) == 0 and "nothing to release" in capsys.readouterr().out


def test_restart_outdated_still_restarts_an_insomniac(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.305")
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch) == 0
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 13 * 3600}}  # due, too
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == [(101, signal.SIGTERM)] and "w1:p1" not in journal()
    assert _event_rows("restarted")[-1]["woke"] is True and UUID in sleeper.read_json(sleeper.INSOMNIA)


def test_scan_presence_and_list_show_the_claim(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    assert _claim(monkeypatch, reason="tunnel to db") == 0
    capsys.readouterr()
    sleeper.cmd_scan(types.SimpleNamespace(dry_run=False, exclude=None))
    out = capsys.readouterr().out
    assert "keep  w1:p1" in out and "insomnia (tunnel to db)" in out and journal() == {}
    sleeper.cmd_presence(types.SimpleNamespace(json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["insomnia"] == "tunnel to db"
    assert sleeper.cmd_insomnia(types.SimpleNamespace(action="list", json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["session"] == UUID and listed[0]["state"] == "idle" and listed[0]["reason"] == "tunnel to db"


# ------------------------------------------------- B-244: spawn and close

SPAWNED_UUID = "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f"


@pytest.fixture
def checkout(state: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = state / "development"
    repo = root / "proj-wt"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    (root / "not-a-repo").mkdir()
    monkeypatch.setattr(sleeper, "SPAWN_ROOT", root)
    return repo


def _spawn(monkeypatch: pytest.MonkeyPatch, cwd: Path | str, name: str = "impl-1", prompt: str = "build B-1",
           extra: list[str] | None = None, kind: str = "claude", sender: str | None = "orc", json_out: bool = False) -> int:
    import io
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO(prompt))
    return sleeper.cmd_spawn(types.SimpleNamespace(cwd=str(cwd), name=name, kind=kind, sender=sender, workspace=None,
                                                   json=json_out, agent_args=extra))


def _close(monkeypatch: pytest.MonkeyPatch, target: str, force: bool = False, caller: str | None = None) -> int:
    if caller:
        monkeypatch.setenv("HERDR_PANE_ID", caller)
        monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101, 50, 1])
    return sleeper.cmd_close(types.SimpleNamespace(target=target, force=force, sender="orc"))


def test_spawn_starts_a_named_session_and_types_its_first_prompt(fake: FakeHerdr, checkout: Path,
                                                                  monkeypatch: pytest.MonkeyPatch,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    assert _spawn(monkeypatch, checkout / "sub", extra=["--", "--model", "haiku"], json_out=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"name": "impl-1", "pane": "w1:p8", "tab": "w1:t8", "session": SPAWNED_UUID, "prompt": "delivered"}
    create = next(c for c in fake.calls if c[:2] == ("tab", "create"))
    assert "--no-focus" in create and create[create.index("--label") + 1] == "impl-1"
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert start[2] == "impl-1" and list(start[start.index("--") + 1:]) == ["-n", "impl-1", "--model", "haiku"]
    typed = next(c for c in fake.calls if c[:2] == ("agent", "prompt"))
    assert typed[2] == "w1:p8" and typed[3].startswith(f"[relayed from {sleeper.this_machine()}/orc via herdr-sleeper") and typed[3].endswith("build B-1")
    rec = sleeper.read_json(sleeper.SPAWNED)["impl-1"]
    assert rec["by"] == "orc" and rec["uuid"] == SPAWNED_UUID and rec["cwd"] == os.path.realpath(checkout / "sub")
    assert "spawned" in events() and "delivered" in events()
    rows = sleeper.presence_rows()
    assert next(r for r in rows if r["session"] == SPAWNED_UUID)["spawned_by"] == "orc"


def test_spawn_codex_gets_no_session_name_flag(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _spawn(monkeypatch, checkout, kind="codex") == 0
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert start[start.index("--kind") + 1] == "codex" and start[-1] == "--"


@pytest.mark.parametrize("kw, needle", [
    ({"cwd": "/tmp"}, "checkout below"),
    ({"cwd": "ROOT"}, "checkout below"),
    ({"cwd": "ROOT/not-a-repo"}, "not inside a git checkout"),
    ({"cwd": "ROOT/missing"}, "not a directory"),
    ({"name": "-n"}, "--name must match"),
    ({"name": "a b"}, "--name must match"),
    ({"prompt": ""}, "first prompt: empty"),
    ({"prompt": "go\x1b[2J"}, "control characters"),
    ({"extra": ["--resume", UUID]}, "select a session"),
    ({"extra": ["--continue"]}, "select a session"),
    ({"extra": ["-n", "other"]}, "drop -n/--name"),
    ({"extra": ["--name=other"]}, "drop -n/--name"),
    ({"extra": ["-n=other"]}, "drop -n/--name"),
    ({"extra": ["--verbose", "do the thing"]}, "would not survive a sleep"),
    ({"extra": ["--print"]}, "would not survive a sleep"),
    ({"sender": "bad sender"}, "--from"),
])
def test_spawn_refusals(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str], kw: dict[str, Any], needle: str) -> None:
    cwd = str(kw.pop("cwd", checkout)).replace("ROOT", str(sleeper.SPAWN_ROOT))
    assert _spawn(monkeypatch, cwd, **kw) == 2
    assert needle in capsys.readouterr().err
    assert not any(c[:2] in (("tab", "create"), ("agent", "start")) for c in fake.calls)


def test_spawn_refuses_a_taken_name(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    assert _spawn(monkeypatch, checkout, name="a") == 1                 # the live agent's Herdr name
    assert "live in w1:p1" in capsys.readouterr().err
    slept_entry(fake)
    assert _spawn(monkeypatch, checkout, name="a") == 1                 # now asleep under that name
    assert "asleep in w1:p1" in capsys.readouterr().err
    assert not any(c[:2] == ("tab", "create") for c in fake.calls)


def test_spawn_that_does_not_start_closes_its_tab(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    fake.start_error = "agent_not_ready"
    assert _spawn(monkeypatch, checkout) == 1
    err = capsys.readouterr().err
    assert "folder-trust" in err and "tab w1:t8 closed" in err
    assert ("tab", "close", "w1:t8") in fake.calls and not any(a["pane_id"] == "w1:p8" for a in fake.agents)
    assert "spawn-failed" in events() and not sleeper.SPAWNED.exists()


def test_spawn_retries_a_pane_that_is_busy_for_a_moment(fake: FakeHerdr, checkout: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    fake.start_errors = ["agent_pane_busy", "agent_pane_busy"]
    assert _spawn(monkeypatch, checkout) == 0
    assert sum(1 for c in fake.calls if c[:2] == ("agent", "start")) == 3
    assert "spawn-failed" not in events() and sleeper.SPAWNED.exists()


def test_spawn_gives_up_on_a_pane_that_stays_busy(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    fake.start_error = "agent_pane_busy"
    assert _spawn(monkeypatch, checkout) == 1
    assert sum(1 for c in fake.calls if c[:2] == ("agent", "start")) == sleeper.SPAWN_BUSY_RETRIES + 1
    assert "still busy after" in capsys.readouterr().err and ("tab", "close", "w1:t8") in fake.calls


def test_spawn_does_not_retry_other_refusals(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.start_error = "agent_not_ready"
    assert _spawn(monkeypatch, checkout) == 1
    assert sum(1 for c in fake.calls if c[:2] == ("agent", "start")) == 1


def test_close_shuts_an_idle_spawned_session_down_and_closes_its_tab(fake: FakeHerdr, checkout: Path,
                                                                      monkeypatch: pytest.MonkeyPatch,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    assert _spawn(monkeypatch, checkout) == 0
    fake.agents[-1]["pid_hint"] = 202
    capsys.readouterr()
    assert _close(monkeypatch, "impl-1") == 0
    assert (202, signal.SIGTERM) in fake.signals and ("tab", "close", "w1:t8") in fake.calls
    assert "closed impl-1 (tab w1:t8)" in capsys.readouterr().out
    assert sleeper.read_json(sleeper.SPAWNED) == {}
    rec = _event_rows("closed")[-1]
    assert rec["by"] == "orc" and rec["forced"] is False and rec["exited"] is True


@pytest.mark.parametrize("setup, needle", [
    (lambda f: f.agents[0].update(agent_status="working"), "is busy (status working)"),
    (lambda f: setattr(f, "screen", CLAUDE_SCREEN.format(composer="half a thought")), "composer draft"),
    (lambda f: setattr(f, "helpers", {300: "zsh"}), "shell process"),
])
def test_close_refuses_what_would_lose_work_unless_forced(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str], setup: Any,
                                                         needle: str) -> None:
    setup(fake)
    assert _close(monkeypatch, "a") == 1
    assert needle in capsys.readouterr().err and fake.signals == [] and len(fake.agents) == 1
    assert _close(monkeypatch, "a", force=True) == 0
    assert fake.agents == [] and _event_rows("closed")[-1]["forced"] is True
    assert "forced by orc" in sleeper.LOGFILE.read_text()


def test_close_refuses_insomnia_and_mail_and_force_drops_the_claim(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    assert _claim(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    with sleeper.mailbox_lock(UUID):
        sleeper.queue_message(UUID, "orc", "later", None)
    assert _close(monkeypatch, "a") == 1
    err = capsys.readouterr().err
    assert "holds insomnia (ssh master)" in err and "undelivered mail" in err
    assert _close(monkeypatch, "a", force=True) == 0
    assert sleeper.read_json(sleeper.INSOMNIA) == {} and _event_rows("insomnia-released")[-1]["via"] == "close"


def test_close_refuses_the_calling_session(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    assert _close(monkeypatch, "a", force=True, caller="w1:p1") == 1
    assert "calling session" in capsys.readouterr().err and fake.agents


def test_close_of_one_pane_in_a_shared_tab_closes_only_the_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents.append({"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1", "terminal_id": "t2"})
    assert _close(monkeypatch, "a") == 0
    assert ("pane", "close", "w1:p1") in fake.calls and not any(c[:2] == ("tab", "close") for c in fake.calls)
    assert [a["pane_id"] for a in fake.agents] == ["w1:p2"]


def test_close_of_a_sleeping_session_releases_closes_and_drops_the_row(fake: FakeHerdr,
                                                                        monkeypatch: pytest.MonkeyPatch,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    fake.stub_panes.add("w1:p1")                                       # the stub waits for Enter
    assert "w1:p1" in fake.resumes
    assert _close(monkeypatch, "a") == 0
    assert journal() == {} and "w1:p1" not in fake.resumes            # no stub typed back after a restart
    assert ("tab", "close", "w1:t1") in fake.calls
    assert f"--resume {UUID}" in capsys.readouterr().out


def test_close_never_touches_a_reused_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    fake.agents[0]["terminal_id"] = "term-reused"                      # same pane id, new terminal, no label
    fake.agents[0]["label"] = None
    assert _close(monkeypatch, UUID, force=True) == 1
    assert "is asleep and its pane is not provably its own" in capsys.readouterr().err
    assert not any(c[:2] in (("tab", "close"), ("pane", "close")) for c in fake.calls) and journal()


def test_close_never_touches_a_pane_whose_row_is_orphaned(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    rows = journal()
    sleeper.write_json(sleeper.JOURNAL, {ORPHAN: {**rows["w1:p1"], "phase": "orphaned"}})
    assert _close(monkeypatch, UUID, force=True) == 1
    assert "neither live nor asleep" in capsys.readouterr().err   # an orphan is no session's handle to act on
    assert not any(c[:2] in (("tab", "close"), ("pane", "close")) for c in fake.calls) and ORPHAN in journal()


def test_close_refuses_a_slept_pane_that_now_runs_someone_elses_work(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    fake.agents[0].pop("agent")                                        # ctrl-c in the stub: claim released
    fake.foreground = "ssh"                                            # ... and the owner works in the pane
    assert _close(monkeypatch, "a", force=True) == 1
    assert "other than its wake stub" in capsys.readouterr().err
    assert not any(c[:2] in (("tab", "close"), ("pane", "close")) for c in fake.calls) and journal()
    fake.foreground = None                                             # a bare shell again: nothing to lose
    assert _close(monkeypatch, "a") == 0 and journal() == {}


def test_close_waits_for_a_waking_row_to_settle(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    rows = journal()
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {**rows["w1:p1"], "phase": "waking", "waking_pid": 4242}})
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})   # the woken agent is back already
    before = list(fake.signals)
    assert _close(monkeypatch, "a", force=True) == 1
    assert "still being settled" in capsys.readouterr().err and fake.signals == before and journal()


def test_insomnia_follows_a_new_session_id_in_the_same_terminal(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _claim(monkeypatch) == 0
    new = "9a9a9a9a-0000-4000-8000-000000000001"
    fake.agents[0]["agent_session"] = {"value": new}                   # /clear: same process, new session id
    transcript(Path(str(sleeper.TRANSCRIPTS.parent)), new, 30)
    ok, reason, _ = sleeper.assess(fake.agents[0], fake.agents, 12, {}, idle_since=0.0)
    assert not ok and reason == "insomnia (ssh master)"               # holds before the watcher re-keys it
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 13 * 3600}}
    sleeper.watch_tick(clocks, dict(sleeper.DEFAULTS))
    assert journal() == {} and list(sleeper.read_json(sleeper.INSOMNIA)) == [new]
    assert _event_rows("insomnia-carried")[-1]["old"] == UUID
    fake.agents[0]["terminal_id"] = "term-elsewhere"                   # another terminal: no carry-over
    assert sleeper.insomnia_reason("other-uuid", "w1:p1", "term-elsewhere") is None
    assert _release(monkeypatch) == 0 and sleeper.read_json(sleeper.INSOMNIA) == {}


def test_spawn_never_types_into_a_pane_that_shows_no_shell(fake: FakeHerdr, checkout: Path,
                                                          monkeypatch: pytest.MonkeyPatch,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    fake.foreground = "zsh-config-wizard"
    assert _spawn(monkeypatch, checkout) == 1
    assert "never showed a bare shell" in capsys.readouterr().err
    assert not any(c[:2] == ("agent", "start") for c in fake.calls) and ("tab", "close", "w1:t8") in fake.calls


def test_damaged_bytes_in_a_state_file_are_damage_not_a_crash(state: Path) -> None:
    sleeper.ensure_state_dir()
    sleeper.INSOMNIA.write_bytes(b"\xff\xfe{}")
    with pytest.raises(sleeper.DamagedState):
        sleeper.read_json(sleeper.INSOMNIA)
    assert sleeper.insomnia_reason(UUID).startswith("insomnia claims unreadable")


def test_watcher_prunes_spawn_records_of_sessions_that_are_gone(fake: FakeHerdr, checkout: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    assert _spawn(monkeypatch, checkout) == 0
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert "impl-1" in sleeper.read_json(sleeper.SPAWNED)               # live: kept
    fake.agents = [a for a in fake.agents if a["pane_id"] != "w1:p8"]   # its tab closed by hand
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert "impl-1" in sleeper.read_json(sleeper.SPAWNED)               # within the grace period
    later = time.time() + 2 * 3600
    monkeypatch.setattr(sleeper.time, "time", lambda: later)
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert sleeper.read_json(sleeper.SPAWNED) == {} and "spawn-record-dropped" in events()


def test_spawn_record_follows_a_cleared_session(fake: FakeHerdr, checkout: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    assert _spawn(monkeypatch, checkout) == 0
    new = "9a9a9a9a-0000-4000-8000-000000000002"
    fake.agents[-1]["agent_session"] = {"value": new}                  # /clear in the spawned session
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert sleeper.read_json(sleeper.SPAWNED)["impl-1"]["uuid"] == new and "spawn-record-moved" in events()
    sleeper.record_spawned("impl-1", {**sleeper.read_json(sleeper.SPAWNED)["impl-1"], "uuid": SPAWNED_UUID})
    assert _close(monkeypatch, "impl-1") == 0                          # a stale record still closes by name
    assert "closed impl-1" in capsys.readouterr().out


# ------------------------------------- the name back after a stub wake (B-244 follow-up)

def _rename_not_found(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """`agent rename` answers agent_not_found the first `times` calls (Herdr has not listed the agent yet)."""
    real, calls = fake.__call__, [0]

    def herdr(*a: str, **k: Any) -> Any:
        if a[:2] == ("agent", "rename"):
            calls[0] += 1
            if calls[0] <= times:
                raise sleeper.HerdrError("herdr agent rename failed: agent_not_found", "agent_not_found")
        return real(*a, **k)
    monkeypatch.setattr(sleeper, "herdr", herdr)
    return calls


def _woken_unnamed(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_ready(fake, monkeypatch)
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: None)
    sleeper.cmd_stub("w1:p1", UUID)
    fake.stub_panes.discard("w1:p1")
    fake.agents[0].update(agent="claude", agent_session={"value": UUID}, name=None)


def test_wake_retries_the_rename_until_herdr_lists_the_agent(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    _woken_unnamed(fake, monkeypatch)
    calls = _rename_not_found(fake, monkeypatch, times=3)
    assert sleeper.finish_waking("w1:p1", UUID, None)
    assert calls[0] == 4 and fake.agents[0]["name"] == "a" and not sleeper.NAMES_OWED.exists()
    assert "could not give the name" not in (sleeper.LOGFILE.read_text() if sleeper.LOGFILE.exists() else "")


def test_a_name_herdr_cannot_take_yet_is_owed_and_the_watcher_hands_it_over(fake: FakeHerdr,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    _woken_unnamed(fake, monkeypatch)
    _rename_not_found(fake, monkeypatch, times=1)
    sleeper.reconcile(fake.agents)                                     # under the lock: no waiting there
    assert journal() == {} and fake.agents[0].get("name") is None
    assert sleeper.read_json(sleeper.NAMES_OWED)["w1:p1"]["name"] == "a"
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert fake.agents[0]["name"] == "a" and sleeper.read_json(sleeper.NAMES_OWED) == {}
    assert _event_rows("renamed")[-1]["via"] == "watcher"


def test_an_owed_name_is_dropped_for_another_session_or_after_an_hour(fake: FakeHerdr,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: now["t"])
    _rename_not_found(fake, monkeypatch, times=10_000)                # Herdr never lists it
    fake.agents[0]["name"] = None
    sleeper.give_name_back("w1:p1", {"name": "a", "uuid": UUID}, None)
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert "w1:p1" in sleeper.read_json(sleeper.NAMES_OWED)            # still trying
    now["t"] += 2 * 3600
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert sleeper.read_json(sleeper.NAMES_OWED) == {} and "rename it by hand" in sleeper.LOGFILE.read_text()
    sleeper.give_name_back("w1:p1", {"name": "a", "uuid": UUID}, None)
    fake.agents[0]["agent_session"] = {"value": "another-session"}     # the pane moved on
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert sleeper.read_json(sleeper.NAMES_OWED) == {} and fake.agents[0]["name"] is None


# ------------------------------------------------------------ other machines (v0.1.5)

FAKE_SSH = '''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_SSH_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
reply = json.load(open(os.environ["FAKE_SSH_REPLY"]))
sys.stdout.write(reply.get("stdout", ""))
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("rc", 0))
'''


class FakeSsh:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.bin, self.log, self.reply_file = tmp / "ssh", tmp / "ssh.jsonl", tmp / "ssh-reply.json"
        self.bin.write_text(FAKE_SSH)
        self.bin.chmod(0o755)
        monkeypatch.setattr(sleeper, "SSH_BIN", str(self.bin))
        monkeypatch.setenv("FAKE_SSH_LOG", str(self.log))
        monkeypatch.setenv("FAKE_SSH_REPLY", str(self.reply_file))
        self.reply()

    def reply(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self.reply_file.write_text(json.dumps({"stdout": stdout, "stderr": stderr, "rc": rc}))

    def calls(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def remote_argv(self, i: int = -1) -> list[str]:
        """What the remote shell runs, split back the way it would."""
        return shlex.split(self.calls()[i]["argv"][-1])


def write_catalog(rows: list[dict] | str) -> None:
    sleeper.MACHINES_CATALOG.parent.mkdir(parents=True, exist_ok=True)
    sleeper.MACHINES_CATALOG.write_text(rows if isinstance(rows, str) else json.dumps({"version": 1, "ssh": rows}))


def machine_row(label: str = "dev2", target: str = "herdr-dev2", enabled: bool = True) -> dict:
    return {"id": "0" * 32, "label": label, "target": target, "session": "default", "enabled": enabled}


@pytest.fixture
def ssh(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSsh:
    """dev1 (this machine) with dev2 saved; the real Popen, so the fake ssh actually runs."""
    write_catalog([machine_row()])
    cfg = tmp_path / "machine.toml"
    cfg.write_text('machine = "dev1"\nexclude = []\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    return FakeSsh(tmp_path, monkeypatch)


def test_saved_machines_reads_only_safe_enabled_rows(state: Path) -> None:
    assert sleeper.saved_machines() == {}                                 # no catalog: no machines
    write_catalog([machine_row(), machine_row("off", enabled=False), machine_row("Dev 3"),
                   machine_row("evil", "-oProxyCommand=touch /tmp/x"), machine_row("dev4", "dev@10.8.0.9")])
    assert sleeper.saved_machines() == {"dev2": "herdr-dev2", "dev4": "dev@10.8.0.9"}
    for broken in ("{not json", json.dumps({"version": 1}), json.dumps([1])):
        write_catalog(broken)
        with pytest.raises(sleeper.SleeperError, match="catalog"):
            sleeper.saved_machines()                                      # never a guessed target


def test_route_sends_only_saved_machine_labels_away(ssh: FakeSsh) -> None:
    assert sleeper.route("impl-1", None) == (None, "impl-1")
    assert sleeper.route("dev2/impl-1", None) == ("dev2", "impl-1")
    assert sleeper.route("dev1/impl-1", None) == (None, "impl-1")         # this machine's own label
    assert sleeper.route("dev9/impl-1", None) == (None, "dev9/impl-1")    # not saved: a local name
    assert sleeper.route("impl-1", "dev2") == ("dev2", "impl-1")
    assert sleeper.route("impl-1", "dev1") == (None, "impl-1")


def test_machine_aliases_route_former_labels(ssh: FakeSsh, tmp_path: Path) -> None:
    (tmp_path / "machine.toml").write_text('machine = "dev1"\nexclude = []\n'
                                           'machine_aliases = ["orch=dev2", "impl=dev1"]\n')
    assert sleeper.route("orch/impl-1", None) == ("dev2", "impl-1")       # a saved machine's old label
    assert sleeper.route("impl/impl-1", None) == (None, "impl-1")         # this machine's old label
    assert sleeper.route("impl-1", "orch") == ("dev2", "impl-1")
    assert sleeper.route("impl-1", "impl") == (None, "impl-1")
    assert sleeper.machine_label("orch") == "dev2"
    assert sleeper.machine_label("impl") is None
    assert sleeper.route("dev9/impl-1", None) == (None, "dev9/impl-1")    # unaliased, unsaved: still local


@pytest.mark.parametrize("bad", ['"orch"', '"orch=orch"', '"Orch=dev2"', '"orch=dev 2"', "1"])
def test_machine_aliases_are_validated(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       bad: str) -> None:
    cfg = tmp_path / "m.toml"
    cfg.write_text(f'exclude = []\nmachine_aliases = [{bad}]\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    with pytest.raises(sleeper.ConfigError, match="machine_aliases"):
        sleeper.load_config()


def test_machine_label_comes_from_config_and_is_validated(state: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "m.toml"
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    cfg.write_text('machine = "dev1"\n')
    assert sleeper.this_machine() == "dev1"
    cfg.write_text('exclude = []\n')
    assert sleeper.MACHINE_RE.fullmatch(sleeper.this_machine())          # no label: the short hostname
    cfg.write_text('machine = "Dev 1"\n')
    with pytest.raises(sleeper.ConfigError, match="machine"):
        sleeper.load_config()
    with pytest.raises(sleeper.ConfigError):
        sleeper.this_machine()                                            # never a guessed label


def test_remote_command_is_locked_down_and_quoted(ssh: FakeSsh) -> None:
    ssh.reply(stdout="ok\n")
    done = sleeper.remote_sleeper("dev2", ["spawn", "--cwd", "/Users/dev/development/my app", "--", "--model",
                                           "opus"], "body")
    argv = ssh.calls()[0]["argv"]
    assert done.stdout == "ok\n" and ssh.calls()[0]["stdin"] == "body"
    assert argv[:-2] == ["-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                         "-o", "ClearAllForwardings=yes"]
    assert argv[-2] == "herdr-dev2"
    assert ssh.remote_argv() == ["herdr-sleeper", "spawn", "--cwd", "/Users/dev/development/my app", "--",
                                 "--model", "opus"]
    # what could escape the quoting in some shell never leaves this machine
    for bad in ("x; rm -rf ~", "$(id)", "`id`", "a'b", 'a"b', "a\\b", "a\nb", "-"[:0]):
        with pytest.raises(sleeper.SleeperError, match="cannot be sent"):
            sleeper.remote_sleeper("dev2", ["deliver", bad])
    assert len(ssh.calls()) == 1


def test_remote_failures_raise(ssh: FakeSsh, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(sleeper.SleeperError, match="no saved Herdr machine 'dev7'.*saved: dev2"):
        sleeper.remote_sleeper("dev7", ["presence"])
    assert ssh.calls() == []
    ssh.reply(stderr="ssh: connect to host 10.8.0.101 port 22: Operation timed out\n", rc=255)
    with pytest.raises(sleeper.SleeperError, match="SSH to herdr-dev2 failed: .*timed out"):
        sleeper.remote_sleeper("dev2", ["presence"])
    ssh.bin.write_text("#!/bin/sh\nsleep 5\n")
    with pytest.raises(sleeper.SleeperError, match="no answer within 1s"):
        sleeper.remote_sleeper("dev2", ["presence"], timeout=1)


def test_deliver_to_another_machine_carries_sender_machine_and_a_fixed_id(
        ssh: FakeSsh, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    ssh.reply(stdout="queued msg x for impl-1: not ready yet (busy); typed in when it is\n", rc=3)
    assert deliver(monkeypatch, "dev2/impl-1", body="review MR !4") == 3   # the remote exit code passes through
    call = ssh.calls()[0]
    remote = ssh.remote_argv()
    assert remote[:5] == ["herdr-sleeper", "deliver", "--here", "--from", "dev1/orc"]
    assert remote[5] == "--id" and re.fullmatch(r"[0-9a-f]{16}", remote[6]) and remote[7:] == ["--", "impl-1"]
    assert call["stdin"] == "review MR !4" and "queued msg" in capsys.readouterr().out
    assert prompts(fake) == []                                            # nothing typed on this machine
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines()]
    assert [r["event"] for r in rec] == ["deliver-remote", "deliver-remote-done"]
    assert rec[1]["code"] == 3 and all("review MR" not in json.dumps(r) for r in rec)
    assert rec[1]["reason"] == "queued msg x for impl-1: not ready yet (busy); typed in when it is"
    import io
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO("again"))
    assert sleeper.cmd_deliver(types.SimpleNamespace(target="impl-1", sender="orc", id="m-1", machine="dev2")) == 3
    assert ssh.remote_argv()[-4:] == ["--id", "m-1", "--", "impl-1"]    # a retry keeps the caller's id


def test_deliver_from_another_machine_shows_it_in_the_envelope(fake: FakeHerdr,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    assert deliver(monkeypatch, "a", sender="dev2/impl-1") == 0
    assert prompts(fake)[0][3].startswith("[relayed from dev2/impl-1 via herdr-sleeper, msg ")
    assert "not from the owner]" in prompts(fake)[0][3]
    for bad in ("dev2/x/y", "Dev2/x", "dev2/", "/x"):
        assert not sleeper.sender_ok(bad)
        assert deliver(monkeypatch, "a", sender=bad) == 2


def test_a_local_sender_carries_this_machine_in_the_envelope(fake: FakeHerdr, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "machine.toml"
    cfg.write_text('machine = "mini2"\nexclude = []\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    assert deliver(monkeypatch, "a", sender="md-verteidigung") == 0
    assert prompts(fake)[0][3].startswith("[relayed from mini2/md-verteidigung via herdr-sleeper, msg ")
    assert deliver(monkeypatch, "a", sender="mbp/guide", body="next") == 0   # already qualified: kept
    assert prompts(fake)[1][3].startswith("[relayed from mbp/guide via herdr-sleeper, msg ")


def test_wake_close_and_insomnia_list_run_on_the_named_machine(ssh: FakeSsh, fake: FakeHerdr,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    ssh.reply(stdout="woke impl-1\n")
    assert sleeper.cmd_wake(types.SimpleNamespace(target="dev2/impl-1", all=False, without_flags=False,
                                                  machine=None)) == 0
    assert ssh.remote_argv() == ["herdr-sleeper", "wake", "--here", "--", "impl-1"]
    assert sleeper.cmd_wake(types.SimpleNamespace(target=None, all=False, without_flags=False, machine="dev2")) == 2
    assert sleeper.cmd_close(types.SimpleNamespace(target="impl-1", force=True, sender="orc", machine="dev2")) == 0
    assert ssh.remote_argv() == ["herdr-sleeper", "close", "--here", "--from", "dev1/orc", "--force", "--", "impl-1"]
    assert sleeper.cmd_insomnia(types.SimpleNamespace(action="list", json=True, machine="dev2")) == 0
    assert ssh.remote_argv() == ["herdr-sleeper", "insomnia", "list", "--json"]
    assert "woke impl-1" in capsys.readouterr().out
    assert not any(c[:2] in (("agent", "start"), ("pane", "close"), ("tab", "close")) for c in fake.calls)


def test_spawn_on_another_machine_leaves_the_checks_to_it(ssh: FakeSsh, fake: FakeHerdr,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    ssh.reply(stdout='{"name": "impl-1"}\n')
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO("build B-1"))
    args = types.SimpleNamespace(cwd="/Users/dev/development/klinikon/x", name="impl-1", kind="claude",
                                 sender="orc", workspace=None, json=True, agent_args=["--", "--model", "opus"],
                                 machine="dev2")
    assert sleeper.cmd_spawn(args) == 0
    assert ssh.remote_argv() == ["herdr-sleeper", "spawn", "--cwd", "/Users/dev/development/klinikon/x",
                                 "--name", "impl-1", "--kind", "claude", "--from", "dev1/orc", "--json",
                                 "--", "--model", "opus"]
    assert ssh.calls()[0]["stdin"] == "build B-1"
    assert not any(c[:2] == ("tab", "create") for c in fake.calls)       # nothing created here


def test_presence_all_merges_machines_and_warns_for_an_unreachable_one(
        ssh: FakeSsh, fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    remote = [{"state": "asleep", "name": "impl-1", "claude_name": "impl-1", "pane": "w2:p1", "kind": "claude",
               "cwd": "/Users/dev/development/klinikon", "session": "s", "slept_at": "2026-11-07T10:00:00",
               "insomnia": None, "spawned_by": "dev1/orc", "place": "klinikon › impl"}]
    ssh.reply(stdout=json.dumps(remote))
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine=None, all=True)) == 0
    out = capsys.readouterr().out
    assert "dev1/a " in out and "dev2/impl-1" in out and "deliver <machine>/<name>" in out
    assert "dev2 › klinikon › impl" in out
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True, machine=None, all=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {(r["machine"], r["name"]) for r in rows} == {("dev1", "a"), ("dev2", "impl-1")}
    ssh.reply(stderr="Connection refused\n", rc=255)
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine=None, all=True)) == 0
    out = capsys.readouterr().out
    assert "WARNING: dev2: SSH to herdr-dev2 failed: Connection refused" in out and "dev1/a " in out
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine="dev2", all=False)) == 1


def test_hook_lists_other_machines_in_list_agents(ssh: FakeSsh, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    ssh.reply(stdout=json.dumps([{"state": "idle", "name": "impl-2", "pane": "w2:p2", "cwd": "/k"},
                                 {"state": "asleep", "name": "wake-w2-p1", "claude_name": "impl-1", "pane": "w2:p1",
                                  "cwd": "/k", "slept_at": "2026-11-07T10:00:00"}]))
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    text = ctx["hookSpecificOutput"]["additionalContext"]
    assert "- dev2/impl-2 · idle · /k" in text and "- dev2/impl-1 · asleep since 2026-11-07T10:00:00 · /k" in text
    assert "never the owner's" in text
    ssh.reply(stderr="No route to host\n", rc=255)
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    assert "- unreachable: dev2: SSH to herdr-dev2 failed: No route to host" in ctx["hookSpecificOutput"][
        "additionalContext"]


def test_hook_relays_send_message_to_another_machine(ssh: FakeSsh, fake: FakeHerdr, state: Path,
                                                     monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    sender_log = state / "sender.jsonl"
    sender_log.write_text(json.dumps({"type": "agent-name", "agentName": "orchestrator"}) + "\n")
    started: list[tuple[list[str], str]] = []

    class Child:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            assert kw["start_new_session"] and kw["stdout"] is sleeper.subprocess.DEVNULL
            self.argv, self.stdin = argv, self

        def write(self, text: str) -> None:
            started.append((self.argv, text))

        def close(self) -> None:
            pass
    monkeypatch.setattr(sleeper.subprocess, "Popen", Child)
    out = run_hook(monkeypatch, capsys, {
        "hook_event_name": "PreToolUse", "tool_name": "SendMessage", "transcript_path": str(sender_log),
        "tool_input": {"to": "dev2/impl-1 [3fa9c1]", "message": "please rebase"}})
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "relayed from dev1/orchestrator" in hso["permissionDecisionReason"]
    [(argv, body)] = started
    assert argv[2:8] == ["deliver", "impl-1", "--machine", "dev2", "--from", "orchestrator"]
    assert argv[8] == "--id" and argv[9] in hso["permissionDecisionReason"] and body == "please rebase"
    assert sleeper.pending_mail(UUID) == [] and prompts(fake) == []
    out = run_hook(monkeypatch, capsys, {"hook_event_name": "PreToolUse", "tool_name": "SendMessage",
                                         "tool_input": {"to": "dev2/impl-1", "message": "bad\x07"}})
    assert "cannot be relayed there" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(started) == 1


def test_own_label_targets_resolve_here(ssh: FakeSsh, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    # presence --all prints this machine's rows as dev1/<name>; that address must work here too
    assert deliver(monkeypatch, "dev1/a") == 0 and prompts(fake)[0][2] == "w1:p1"
    args = types.SimpleNamespace(target="dev1/a", all=False, without_flags=False, machine=None, here=False)
    assert sleeper.routed("wake", args) is None and args.target == "a"
    assert ssh.calls() == []


def test_remote_end_never_routes_again(ssh: FakeSsh, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO("hi"))
    # dev2 sends `deliver --here -- dev1/x`: resolved here as given, never sent back over SSH
    assert sleeper.cmd_deliver(types.SimpleNamespace(target="dev2/a", sender="dev2/impl", id=None, machine=None,
                                                     here=True)) == 1
    assert ssh.calls() == []
    args = types.SimpleNamespace(target="dev2/x", all=False, without_flags=False, machine=None, here=True)
    assert sleeper.routed("wake", args) is None and args.target == "dev2/x"


def test_targets_that_could_turn_into_options_stay_here(ssh: FakeSsh, fake: FakeHerdr,
                                                        monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    for target in ("dev2/-h", "dev2/--all", "dev2/x y", "dev2/$(id)"):
        assert deliver(monkeypatch, target) == 2
        assert sleeper.cmd_close(types.SimpleNamespace(target=target, force=False, sender="orc", machine=None,
                                                       here=False)) == 2
    assert ssh.calls() == [] and "cannot be sent to another machine" in capsys.readouterr().err
    # and the remote parser reads what follows `--` as the target, whatever it looks like
    seen: list[str] = []
    monkeypatch.setattr(sleeper, "cmd_deliver", lambda a: seen.append(a.target) or 0)
    assert sleeper.main(["deliver", "--here", "--from", "dev1/orc", "--", "-h"]) == 0 and seen == ["-h"]


def test_a_lost_connection_prints_the_id_to_retry_with(ssh: FakeSsh, fake: FakeHerdr,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    ssh.reply(stderr="client_loop: send disconnect: Broken pipe\n", rc=255)
    assert deliver(monkeypatch, "dev2/impl-1") == 1
    err = capsys.readouterr().err
    sent_id = ssh.remote_argv()[6]
    assert f"Whether msg {sent_id} arrived is unknown: retry with `--id {sent_id}`" in err
    done = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines()][-1]
    assert done["event"] == "deliver-remote-done" and done["code"] is None and "Broken pipe" in done["error"]
    ssh.reply(stdout=f"msg {sent_id} was delivered already; not sent twice\n")
    assert deliver(monkeypatch, "dev2/impl-1", msg_id=sent_id) == 0       # the retry the message suggests
    assert ssh.remote_argv()[6] == sent_id


def test_remote_paths_refuse_without_a_sender_or_with_a_broken_config(
        ssh: FakeSsh, fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    assert sleeper.cmd_close(types.SimpleNamespace(target="dev2/x", force=False, sender=None, machine=None,
                                                   here=False)) == 2
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO("go"))
    spawn = types.SimpleNamespace(cwd="/Users/dev/development/k", name="impl-1", kind="claude", sender=None,
                                  workspace=None, json=False, agent_args=[], machine="dev2")
    assert sleeper.cmd_spawn(spawn) == 2
    for bad_name, flags in (("bad name", []), ("impl-1", ["--", "--append-system-prompt", "it's"])):
        monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO("go"))
        rc = sleeper.cmd_spawn(types.SimpleNamespace(**{**vars(spawn), "sender": "orc", "name": bad_name,
                                                        "agent_args": flags}))
        assert rc in (1, 2)
    assert ssh.calls() == []
    sleeper.CONFIG_FILE.write_text('machine = "Not A Label"\n')
    assert deliver(monkeypatch, "dev2/impl-1") == 1                       # no label we trust: nothing sent
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine=None, all=True)) == 1
    assert ssh.calls() == []


def test_odd_remote_output_never_crashes(ssh: FakeSsh, fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    ssh.reply(stdout=json.dumps([{"name": "impl-1"}, {"state": "idle", "pane": None, "kind": 7}, "junk"]))
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine=None, all=True)) == 0
    out = capsys.readouterr().out
    assert "dev2/impl-1" in out and "unknown" in out
    ssh.bin.write_text("#!/bin/sh\nprintf '\\377\\376 not utf-8\\n'\n")
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine="dev2", all=False)) == 0
    assert "not utf-8" in capsys.readouterr().out
    ssh.bin.write_text("#!/bin/sh\nexit 127\n")
    with pytest.raises(sleeper.SleeperError, match="not on the remote PATH"):
        sleeper.remote_sleeper("dev2", ["presence"])


def test_hook_waits_for_all_machines_together_and_keeps_local_sleepers(
        ssh: FakeSsh, fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    write_catalog([machine_row("dev2", "herdr-dev2"), machine_row("dev3", "herdr-dev3")])
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    ssh.bin.write_text("#!/bin/sh\nsleep 30\n")
    monkeypatch.setattr(sleeper, "REMOTE_HOOK_SECONDS", 3)
    began = time.monotonic()
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    assert time.monotonic() - began < 6                                   # not 3 s per machine, one after another
    text = ctx["hookSpecificOutput"]["additionalContext"]
    assert "ts-api-worker" in text
    assert "- unreachable: dev2: no answer within 3s" in text and "- unreachable: dev3: no answer" in text
    monkeypatch.setattr(sleeper, "machines_presence", lambda *a, **k: 1 / 0)
    ctx = run_hook(monkeypatch, capsys, {"hook_event_name": "PostToolUse", "tool_name": "ListAgents"})
    assert "ts-api-worker" in ctx["hookSpecificOutput"]["additionalContext"]   # an error there costs nothing here


def test_local_sleepers_keep_send_message_with_machines_saved(ssh: FakeSsh, fake: FakeHerdr, state: Path,
                                                              monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    named_transcript(state, UUID, "ts-api-worker")
    slept_entry(fake)
    spawned: list[list[str]] = []
    monkeypatch.setattr(sleeper.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    for to in ("ts-api-worker", "dev1/ts-api-worker"):
        out = run_hook(monkeypatch, capsys, {"hook_event_name": "PreToolUse", "tool_name": "SendMessage",
                                             "tool_input": {"to": to, "message": "rebase"}})
        assert "queued as msg" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert [a[-4] for a in spawned] == ["deliver-pending", "deliver-pending"] and ssh.calls() == []


def test_an_unreadable_catalog_keeps_slashed_local_names_local(state: Path, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "m.toml"
    cfg.write_text('machine = "dev1"\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    write_catalog("{broken")
    assert sleeper.route("feat/x", None) == (None, "feat/x")
    with pytest.raises(sleeper.SleeperError, match="unreadable"):
        sleeper.remote_sleeper("dev2", ["presence"])                      # an explicit --machine still refuses


# ------------------------------------------------------------------ claude-account slots

def accounts(tmp_path: Path, active: str | None, records: dict[int, str] | None = None,
             written: float = 1_000.0) -> Path:
    d = sleeper.ACCOUNTS_DIR
    (d / "run").mkdir(parents=True, exist_ok=True)
    if active is not None:
        (d / "active").write_text(active + "\n")
    for pid, slot in (records or {}).items():
        rec = d / "run" / str(pid)
        rec.write_text(slot + "\n")
        os.utime(rec, (written, written))
    return d


def test_installed_build_follows_the_account_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build = tmp_path / "versions" / "2.1.306"
    build.parent.mkdir()
    build.write_text("")
    real = tmp_path / "bin" / "claude"
    real.parent.mkdir()
    real.symlink_to(build)
    launcher = tmp_path / "local" / "claude"
    launcher.parent.mkdir()
    launcher.write_text(f"#!/bin/zsh -f\n# claude-account launcher\n# real-binary: {real}\nexec -a claude $real\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(sleeper.shutil, "which", lambda kind, path=None: str(launcher))
    assert sleeper.installed_build("claude") == os.path.realpath(build)
    launcher.write_text("#!/bin/sh\nexec /elsewhere \"$@\"\n")             # no marker: the script itself
    assert sleeper.installed_build("claude") == os.path.realpath(launcher)


def test_session_account_reads_the_launcher_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sleeper.active_account() is None                                # no launcher installed
    accounts(tmp_path, None, {101: "b", 102: "explicit"})
    assert sleeper.active_account() == "a"                                 # installed, never switched
    (sleeper.ACCOUNTS_DIR / "active").write_text("b\n")
    assert sleeper.active_account() == "b"
    monkeypatch.setattr(sleeper, "process_start", lambda pid: 999.5)
    assert sleeper.session_account(101) == "b"                             # written as it started
    assert sleeper.session_account(102) is None                            # a hand-set config dir
    assert sleeper.session_account(103) == "a"                             # started before the launcher
    monkeypatch.setattr(sleeper, "process_start", lambda pid: 1_100.0)
    assert sleeper.session_account(101) == "a"                             # a reused pid
    monkeypatch.setattr(sleeper, "process_start", lambda pid: None)
    assert sleeper.session_account(101) is None                            # gone or unreadable


def test_restart_follows_an_account_switch(fake: FakeHerdr, state: Path, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.306")
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    monkeypatch.setattr(sleeper, "process_start", lambda pid: 999.0)
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    accounts(tmp_path, "b", {101: "a"})
    young = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 60}}
    sleeper.watch_tick(young, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == []                                            # idle a minute: not yet
    clocks = {"w1:p1": {**sleeper.clock_key(fake.agents[0]), "since": 1_000_000.0 - 180}}
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True, "dry_run": True})
    assert fake.signals == [] and "would restart onto account b" in sleeper.LOGFILE.read_text()
    sleeper.watch_tick(clocks, {**sleeper.DEFAULTS, "restart_outdated": True})
    assert fake.signals == [(101, signal.SIGTERM)]                       # 3 min idle: well before pressure_idle
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if '"restarted"' in l][-1]
    assert rec["old"] == "account a" and rec["new"] == "account b" and rec["kind"] == "account"


def test_account_command_restarts_idle_and_leaves_busy(fake: FakeHerdr, state: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper, "process_start", lambda pid: 999.0)
    monkeypatch.setattr(sleeper, "heal_watcher", lambda: None)
    monkeypatch.setattr(sleeper, "load_config", lambda: ({**sleeper.DEFAULTS, "restart_outdated": True}, "test"))
    accounts(tmp_path, "b", {101: "a"})
    args = types.SimpleNamespace(restart=False, json=True)
    assert sleeper.cmd_account(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["active"] == "b" and out["sessions"][0]["slot"] == "a" and fake.signals == []
    fake.agents[0]["agent_status"] = "working"
    assert sleeper.cmd_account(types.SimpleNamespace(restart=True, json=True)) == 0
    row = json.loads(capsys.readouterr().out)["sessions"][0]
    assert row["action"].startswith("pending: switches at its next idle") and fake.signals == []
    fake.agents[0]["agent_status"] = "idle"

    def start_via_launcher(*argv: Any, **kw: Any) -> Any:   # the woken claude went through the launcher
        if argv[:2] == ("agent", "start"):
            (sleeper.ACCOUNTS_DIR / "run" / "101").write_text("b\n")
        return fake(*argv, **kw)
    monkeypatch.setattr(sleeper, "herdr", start_via_launcher)
    assert sleeper.cmd_account(types.SimpleNamespace(restart=True, json=True)) == 0
    row = json.loads(capsys.readouterr().out)["sessions"][0]
    assert row["action"] == "restarted" and fake.signals == [(101, signal.SIGTERM)]


# ------------------------------------------------- v0.1.8: why mail waits, and saying so

def held() -> dict | None:
    return sleeper.read_mail_hold(UUID)


def mail_events() -> list[dict]:
    p = sleeper.EVENTS
    return [r for r in (json.loads(l) for l in p.read_text().splitlines()) if r["event"] == "mail-held"] \
        if p.exists() else []


def test_deliver_is_not_delivered_when_only_older_mail_went_in(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", body="older one", msg_id="older") == 3
    fake.agents[0]["agent_status"] = "idle"
    monkeypatch.setattr(sleeper, "MAIL_BATCH_MAX_BYTES", 10)  # one message per prompt: the batch splits here
    answers = iter([None, None, "status working"])  # ready, `older` may go, then the agent is busy for `mine`
    monkeypatch.setattr(sleeper, "mail_refusal", lambda pane, uuid: next(answers))
    capsys.readouterr()
    assert deliver(monkeypatch, "a", body="my one", msg_id="mine") == 3
    out = capsys.readouterr().out
    assert "typed 1 older message(s) first; mine waits (status working)" in out and "delivered" not in out
    assert [p.stem.split("-", 1)[1] for p in sleeper.pending_mail(UUID)] == ["mine"]
    assert "older" in sleeper.recently_delivered() and "mine" not in sleeper.recently_delivered()
    assert held()["reason"] == "status working"


def test_queued_mail_goes_in_as_one_prompt_once_the_agent_is_ready(fake: FakeHerdr,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    for i in range(3):
        assert deliver(monkeypatch, "a", body=f"note {i}", msg_id=f"m{i}") == 3
    fake.agents[0]["agent_status"] = "idle"
    checks = []
    monkeypatch.setattr(sleeper, "mail_refusal", lambda pane, uuid: checks.append(pane))  # always ready
    sleeper.flush_mail(sleeper.list_agents())
    (call,) = prompts(fake)                                 # one prompt, not one per message and tick
    text = call[3]
    assert text.index("msg m0;") < text.index("msg m1;") < text.index("msg m2;") and text.endswith("note 2")
    assert len(checks) == 1 and sleeper.pending_mail(UUID) == [] and held() is None
    assert {"m0", "m1", "m2"} <= set(sleeper.recently_delivered())


def test_mail_is_retried_between_ticks_only_while_some_waits(fake: FakeHerdr,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    flushes = []
    monkeypatch.setattr(sleeper, "pause", lambda s: None)
    monkeypatch.setattr(sleeper, "flush_mail_safely", lambda agents: flushes.append(1))
    sleeper.pause_watching_mail(60)
    assert flushes == []                                    # nothing queued: no extra Herdr calls
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", body="later") == 3
    sleeper.pause_watching_mail(60)
    assert len(flushes) == 60 // sleeper.MAIL_POLL_SECONDS - 1  # every 5 s; the last slot is the tick itself


def test_deliver_names_the_real_refusal(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a") == 3
    out = capsys.readouterr().out
    assert "not ready yet (status working)" in out and "not idle" not in out
    fake.screen = CLAUDE_SCREEN.format(composer="half a thought")
    fake.agents[0]["agent_status"] = "idle"
    assert deliver(monkeypatch, "a") == 3
    out = capsys.readouterr().out
    assert "composer draft: text on the prompt line" in out and "half a thought" not in out
    # the session is gone between the caller's look and the lock: "no live agent", not "not idle"
    monkeypatch.setattr(sleeper, "session_state", lambda uuid: ("live", agent(), None))
    fake.agents.clear()
    code, text = sleeper.finish_delivery(UUID, "m-x", "orc", None, dict(sleeper.DEFAULTS))
    assert code == 3 and "no live agent runs the session" in text
    assert held()["reason"] == sleeper.NO_LIVE_AGENT


def test_deliver_pending_succeeds_only_when_the_mailbox_is_empty(fake: FakeHerdr,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", msg_id="one") == 3 and deliver(monkeypatch, "a", msg_id="two") == 3
    fake.agents[0]["agent_status"] = "idle"
    assert sleeper.cmd_deliver_pending(types.SimpleNamespace(uuid=UUID, sender="orc")) == 0
    assert len(prompts(fake)) == 1 and sleeper.pending_mail(UUID) == [] and held() is None  # both in one prompt


def test_hold_reason_is_recorded_shown_and_cleared(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents.append({**agent(pane="w2:p1", uuid="u2-session", name="orc", terminal="t2"), "pid_hint": 102})
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", body="secret plan") == 3 and deliver(monkeypatch, "a", body="b") == 3
    hold_file = sleeper.MAILBOX / UUID / sleeper.HELD_NAME
    assert hold_file.stat().st_mode & 0o777 == 0o600 and len(sleeper.pending_mail(UUID)) == 2  # .held is not mail
    sleeper.flush_mail(fake.agents)                                    # the watcher keeps the reason current
    assert held()["reason"] == "status working" and prompts(fake) == []
    capsys.readouterr()
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True)) == 0
    rows = {r["name"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["a"]["mail_queued"] == 2 and rows["a"]["mail_held"] == "status working"
    assert isinstance(rows["a"]["mail_oldest_s"], int)
    assert rows["orc"]["mail_queued"] == 0 and rows["orc"]["mail_oldest_s"] is None and rows["orc"]["mail_held"] is None
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False)) == 0
    text = capsys.readouterr().out
    assert "[2 queued 0m: status working]" in text and "secret plan" not in text + hold_file.read_text()
    fake.agents[0]["agent_status"] = "idle"
    sleeper.flush_mail(fake.agents)
    assert len(prompts(fake)) == 1 and held() is None and not hold_file.exists()  # both in one prompt


def test_mail_held_event_escalates_once_per_reason(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    rules: list[str] = []
    monkeypatch.setattr(sleeper, "herdr_matched_rule", lambda pane: rules.append(pane) or "live_turn_working (working)")
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", body="the secret plan") == 3
    t0 = time.time()
    sleeper.flush_mail(fake.agents, now=t0 + 100)
    sleeper.flush_mail(fake.agents, now=t0 + 590)
    assert mail_events() == []                                        # not before MAIL_HELD_EVENT_SECONDS
    sleeper.flush_mail(fake.agents, now=t0 + 700)
    sleeper.flush_mail(fake.agents, now=t0 + 800)
    (ev,) = mail_events()
    assert ev["reason"] == "status working" and ev["queued"] == 1 and ev["oldest_age_s"] >= 690
    assert ev["to"] == "a" and ev["pane"] == "w1:p1" and ev["uuid"] == UUID
    assert ev["herdr_rule"] == "live_turn_working (working)" and rules == ["w1:p1"]
    fake.agents[0]["agent_status"] = "idle"                           # a new reason: a draft
    fake.screen = CLAUDE_SCREEN.format(composer="the secret draft")
    sleeper.flush_mail(fake.agents, now=t0 + 900)
    fake.screen = CLAUDE_SCREEN.format(composer="the secret draft, longer")   # same hold, other length
    sleeper.flush_mail(fake.agents, now=t0 + 1000)
    evs = mail_events()
    assert len(evs) == 2 and evs[1]["reason"].startswith("composer draft:") and "herdr_rule" not in evs[1]
    sleeper.flush_mail(fake.agents, now=t0 + 900 + sleeper.MAIL_HELD_REPEAT_SECONDS + 1)
    assert len(mail_events()) == 3                                    # the same hold again after 30 min
    assert prompts(fake) == [] and len(sleeper.pending_mail(UUID)) == 1
    logged = sleeper.EVENTS.read_text() + (sleeper.LOGFILE.read_text() if sleeper.LOGFILE.exists() else "")
    assert "secret" not in logged


def test_mail_for_an_absent_session_says_so(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a") == 3
    sleeper.flush_mail([])
    assert held()["reason"] == sleeper.NO_LIVE_AGENT
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": {**agent(), "uuid": UUID, "kind": "claude", "phase": "asleep"}})
    sleeper.flush_mail([])
    assert held()["reason"].startswith(sleeper.NO_LIVE_AGENT + ": asleep") and prompts(fake) == []


def test_matched_rule_keeps_only_the_rule_never_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    out = json.dumps({"matched_rule": {"id": "live_turn_working", "priority": 970, "state": "working",
                                       "region": "bottom_non_empty_lines(12)"},
                      "evaluated_rules": [{"evidence": {"region_preview": "❯ my secret draft"}}]})
    runs: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> Any:
        runs.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(sleeper.subprocess, "run", run)
    rule = sleeper.herdr_matched_rule("w7:pP")
    assert rule == "live_turn_working (working, bottom_non_empty_lines(12), priority 970)"
    assert runs[0][1:] == ["agent", "explain", "w7:pP", "--json"]
    for bad in ("not json", json.dumps({"matched_rule": {"id": "x; rm -rf / my secret draft"}}), json.dumps([1])):
        out = bad
        assert sleeper.herdr_matched_rule("w7:pP") is None

    def boom(argv: list[str], **kw: Any) -> Any:
        raise OSError("no herdr")
    monkeypatch.setattr(sleeper.subprocess, "run", boom)
    assert sleeper.herdr_matched_rule("w7:pP") is None


def test_resolve_by_claude_name_when_herdr_dropped_the_agent_name(fake: FakeHerdr, state: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    named_transcript(state, UUID, "taxi-orch")
    fake.agents[0].pop("name")
    where, hit, _ = sleeper.resolve_target("taxi-orch", fake.agents, {})
    assert where == "live" and hit["pane_id"] == "w1:p1"
    assert deliver(monkeypatch, "taxi-orch") == 0 and len(prompts(fake)) == 1
    other = "6b1e7d2a-0c4f-4e8b-9a3d-5f6e7a8b9c0d"
    named_transcript(state, other, "taxi-orch")
    fake.agents.append({**agent(pane="w2:p1", uuid=other, name=None, terminal="t2"), "pid_hint": 102})
    with pytest.raises(sleeper.SleeperError, match="matches 2 live agents by Claude name"):
        sleeper.resolve_target("taxi-orch", fake.agents, {})
    fake.agents.pop()
    sleeping = {"w3:p1": {**agent(pane="w3:p1"), "uuid": other, "kind": "claude", "claude_name": "taxi-orch"}}
    with pytest.raises(sleeper.SleeperError, match="by Claude name and sleeping agents"):
        sleeper.resolve_target("taxi-orch", fake.agents, sleeping)


def test_a_malformed_mail_file_is_moved_aside_and_the_rest_still_goes(fake: FakeHerdr,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a", msg_id="good") == 3
    box = sleeper.MAILBOX / UUID
    (box / f"{time.time_ns() - 10**9}-bad.json").write_text(json.dumps({"x": 1}))   # older than `good`
    other = "6b1e7d2a-0c4f-4e8b-9a3d-5f6e7a8b9c0d"
    fake.agents.append({**agent(pane="w2:p1", uuid=other, name="b", terminal="t2"), "pid_hint": 102})
    assert deliver(monkeypatch, "b", msg_id="later") == 0                # b is idle
    fake.agents[1]["agent_status"] = "working"
    assert deliver(monkeypatch, "b", msg_id="later2") == 3
    fake.agents[0]["agent_status"] = fake.agents[1]["agent_status"] = "idle"
    real = sleeper.type_mail

    def flaky(pane: str, uuid: str, name: str) -> Any:
        if uuid == UUID and not (box / "boom").exists():
            (box / "boom").write_text("")
            raise RuntimeError("a bug in one mailbox")
        return real(pane, uuid, name)
    monkeypatch.setattr(sleeper, "type_mail", flaky)
    sleeper.flush_mail(fake.agents)                                     # one mailbox's bug costs only that one
    assert sleeper.pending_mail(other) == [] and len(sleeper.pending_mail(UUID)) == 2
    assert "RuntimeError: a bug in one mailbox" in sleeper.LOGFILE.read_text()
    sleeper.flush_mail(fake.agents)
    assert sleeper.pending_mail(UUID) == [] and sleeper.pending_mail(other) == []
    assert any(p.suffix == ".damaged" for p in box.iterdir())
    typed = [p[3] for p in prompts(fake)]
    assert sum("msg good;" in t for t in typed) == 1 and sum("msg later2;" in t for t in typed) == 1
    assert "malformed" in sleeper.LOGFILE.read_text()


def test_mail_flush_runs_when_the_sleep_pass_fails(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert deliver(monkeypatch, "a") == 3
    fake.agents[0]["agent_status"] = "idle"

    def broken(agents: list[dict]) -> None:
        raise RuntimeError("reconcile bug")
    monkeypatch.setattr(sleeper, "reconcile", broken)
    with pytest.raises(RuntimeError, match="reconcile bug"):
        sleeper.watch_tick({}, dict(sleeper.DEFAULTS))                 # the tick still fails (and backs off) ...
    assert len(prompts(fake)) == 1 and sleeper.pending_mail(UUID) == []  # ... but the mail went in


def test_remote_presence_carries_mail_fields_and_tolerates_old_rows(
        ssh: FakeSsh, fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    new = {"state": "idle", "name": "taxi-orch", "pane": "w7:pP", "kind": "claude", "cwd": "/x", "session": "s",
           "mail_queued": 3, "mail_oldest_s": 7320, "mail_held": "composer draft: text on the prompt line"}
    old = {"state": "asleep", "name": "impl-1", "pane": "w2:p1", "kind": "claude", "cwd": "/y", "session": "t"}
    ssh.reply(stdout=json.dumps([new, old]))
    assert sleeper.cmd_presence(types.SimpleNamespace(json=False, machine=None, all=True)) == 0
    out = capsys.readouterr().out
    assert "[3 queued 122m: composer draft: text on the prompt line]" in out
    impl = next(line for line in out.splitlines() if "dev2/impl-1" in line)
    assert "queued" not in impl
    lines, _problems = sleeper.remote_listing()
    assert any("dev2/taxi-orch" in l and "3 queued 122m" in l for l in lines)


# ------------------------------------------------------------ close --self

def _self_close(monkeypatch: pytest.MonkeyPatch, reason: str | None = None, cancel: bool = False,
                pane: str | None = "w1:p1", parent_chain: list[int] | None = None, **kw: Any) -> int:
    """`close --self` from inside the agent in `pane` (its pid 101 is our ancestor unless told otherwise)."""
    if pane:
        monkeypatch.setenv("HERDR_PANE_ID", pane)
    else:
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101, 50, 1] if parent_chain is None else parent_chain)
    args = {"target": None, "force": False, "sender": None, "machine": None, "self_close": True,
            "reason": reason, "cancel": cancel, **kw}
    return sleeper.cmd_close(types.SimpleNamespace(**args))


def _closing() -> dict[str, Any]:
    return sleeper.read_json(sleeper.SELF_CLOSE)


def test_close_self_records_the_request_and_acts_on_nothing(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents[0]["agent_status"] = "working"                          # the turn that asks is still running
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_000.0)
    assert _self_close(monkeypatch, reason="B-17 done") == 0
    out = capsys.readouterr().out
    assert "a closes after this turn ends; a message delivered before then cancels it" in out
    rec = _closing()[UUID]
    assert rec["at"] == 1_000_000.0 and rec["reason"] == "B-17 done" and rec["pane"] == "w1:p1"
    assert rec["name"] == "a" and rec["by"] == "a"
    assert oct(sleeper.SELF_CLOSE.stat().st_mode & 0o777) == "0o600"
    assert _event_rows("self-close-requested")[-1]["renewed"] is False
    monkeypatch.setattr(sleeper.time, "time", lambda: 1_000_100.0)
    assert _self_close(monkeypatch) == 0                                 # idempotent: same wish, same start
    assert _closing()[UUID]["at"] == 1_000_000.0 and _closing()[UUID]["reason"] == "B-17 done"
    assert list(_closing()) == [UUID] and _event_rows("self-close-requested")[-1]["renewed"] is True
    assert fake.signals == [] and not any(c[:2] in (("tab", "close"), ("pane", "close")) for c in fake.calls)


def test_close_self_names_insomnia_that_would_hold_it(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    assert _claim(monkeypatch) == 0
    assert _self_close(monkeypatch) == 0
    assert "holds insomnia (ssh master): release it, or the close waits" in capsys.readouterr().out


@pytest.mark.parametrize("kw, code, needle", [
    ({"pane": None}, 1, "no HERDR_PANE_ID"),
    ({"parent_chain": [7, 1]}, 1, "does not run under the agent"),
    ({"pane": "w9:p9"}, 1, "runs no agent"),
    ({"target": "a"}, 2, "--self takes no target"),
    ({"force": True}, 2, "--self takes no target"),
    ({"machine": "dev2"}, 2, "on this machine"),
    ({"reason": "a\x1b[2Jb"}, 2, "control characters"),
])
def test_close_self_refusals(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                             kw: dict[str, Any], code: int, needle: str) -> None:
    assert _self_close(monkeypatch, **kw) == code
    assert needle in capsys.readouterr().err and not sleeper.SELF_CLOSE.exists()


def test_close_self_needs_a_claude_session_with_an_id(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents[0]["agent"] = "opencode"
    assert _self_close(monkeypatch) == 1 and "composer cannot be checked" in capsys.readouterr().err
    fake.agents[0]["agent"] = "claude"
    fake.agents[0].pop("agent_session")
    assert _self_close(monkeypatch) == 1 and "no session id" in capsys.readouterr().err
    assert not sleeper.SELF_CLOSE.exists()


def test_close_flags_that_need_self_and_a_missing_target(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert sleeper.cmd_close(types.SimpleNamespace(target="a", force=False, sender="orc", cancel=True)) == 2
    assert "go with --self" in capsys.readouterr().err
    assert sleeper.cmd_close(types.SimpleNamespace(target=None, force=False, sender="orc")) == 2
    assert "name the session to close" in capsys.readouterr().err and fake.agents


def test_watcher_closes_a_self_close_only_when_idle_and_nothing_refuses(fake: FakeHerdr,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert _self_close(monkeypatch, reason="done") == 0
    monkeypatch.delenv("HERDR_PANE_ID")                                 # the watcher runs in no pane
    cfg = dict(sleeper.DEFAULTS)
    sleeper.watch_tick({}, cfg)                                         # the turn still runs
    assert fake.signals == [] and UUID in _closing()
    fake.agents[0]["agent_status"] = "idle"
    fake.screen = CLAUDE_SCREEN.format(composer="half a thought")      # the owner types into it
    sleeper.watch_tick({}, cfg)
    assert fake.signals == [] and UUID in _closing() and _event_rows("self-close-held") == []  # quiet: a draft
    fake.screen = EMPTY_SCREEN
    assert _claim(monkeypatch) == 0                                     # insomnia is a refusal, never forced
    monkeypatch.delenv("HERDR_PANE_ID")
    sleeper.watch_tick({}, cfg)
    sleeper.watch_tick({}, cfg)
    held = _event_rows("self-close-held")
    assert fake.signals == [] and UUID in _closing() and len(held) == 1  # logged once, kept pending
    assert "holds insomnia (ssh master)" in held[0]["why"]
    assert _release(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    sleeper.watch_tick({}, cfg)
    assert fake.signals == [(101, signal.SIGTERM)] and ("tab", "close", "w1:t1") in fake.calls
    assert fake.agents == [] and _closing() == {}
    rec = _event_rows("closed")[-1]
    assert rec["by"] == "self" and rec["forced"] is False and rec["via"] == "close --self" and rec["reason"] == "done"


def test_watcher_closes_a_self_close_that_fell_asleep(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _self_close(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    sleeper.tend_self_closes()
    assert journal() == {} and ("tab", "close", "w1:t1") in fake.calls and _closing() == {}
    assert _event_rows("closed")[-1]["state"] == "asleep"


def test_delivery_cancels_a_pending_self_close_and_says_so(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _self_close(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    assert deliver(monkeypatch, "a") == 0
    (call,) = prompts(fake)
    assert call[3].endswith("please rebase\n\n" + sleeper.SELF_CLOSE_CANCELLED)
    assert sleeper.SELF_CLOSE_CANCELLED == ("[herdr-sleeper: your pending close --self was cancelled by this "
                                            "message; run it again when you are done]")
    assert _closing() == {}
    rec = _event_rows("self-close-cancelled")[-1]
    assert rec["via"] == "deliver" and rec["by"] == ["orc"]
    assert deliver(monkeypatch, "a", body="again") == 0                 # nothing pending: nothing appended
    assert prompts(fake)[-1][3].endswith("again")
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))
    assert fake.signals == [] and fake.agents


def test_queued_mail_flushed_by_the_watcher_cancels_too(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    assert _self_close(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    assert deliver(monkeypatch, "a") == 3                               # queued: the turn still runs
    assert UUID in _closing()
    fake.agents[0]["agent_status"] = "idle"
    sleeper.watch_tick({}, dict(sleeper.DEFAULTS))                      # the flush types it in, then no close
    assert prompts(fake)[-1][3].endswith(sleeper.SELF_CLOSE_CANCELLED)
    assert _closing() == {} and fake.signals == [] and fake.agents


def test_close_self_cancel(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    assert _self_close(monkeypatch) == 0
    capsys.readouterr()
    assert _self_close(monkeypatch, cancel=True) == 0
    assert "close --self cancelled for a" in capsys.readouterr().out and _closing() == {}
    assert _event_rows("self-close-cancelled")[-1]["via"] == "cancel"
    assert _self_close(monkeypatch, cancel=True) == 0
    assert "nothing to cancel" in capsys.readouterr().out


def test_pending_self_close_expires_after_a_day(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    fake.agents[0]["agent_status"] = "working"
    assert _self_close(monkeypatch, reason="done") == 0
    clock["t"] += 24 * 3600 - 1
    sleeper.tend_self_closes()
    assert UUID in _closing()
    clock["t"] += 1
    fake.agents[0]["agent_status"] = "idle"                             # too late: the wish is gone, not acted on
    sleeper.tend_self_closes()
    assert _closing() == {} and fake.signals == [] and fake.agents
    assert _event_rows("self-close-expired")[-1]["reason"] == "done"


def test_presence_shows_a_pending_self_close(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.cmd_presence(types.SimpleNamespace(json=True))
    assert json.loads(capsys.readouterr().out)[0]["closing"] is None
    assert _self_close(monkeypatch, reason="B-17 done") == 0
    capsys.readouterr()
    sleeper.cmd_presence(types.SimpleNamespace(json=True))
    assert json.loads(capsys.readouterr().out)[0]["closing"] == "B-17 done"
    sleeper.cmd_presence(types.SimpleNamespace(json=False))
    assert "[closing: B-17 done]" in capsys.readouterr().out
    assert _self_close(monkeypatch, cancel=True) == 0 and _self_close(monkeypatch, reason=None) == 0
    capsys.readouterr()
    sleeper.cmd_presence(types.SimpleNamespace(json=True))
    assert json.loads(capsys.readouterr().out)[0]["closing"] == "pending"


def test_watcher_never_closes_a_wish_cancelled_since_it_looked(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _self_close(monkeypatch) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    real = sleeper.close_session

    def deliver_first(*a: Any, **k: Any) -> Any:
        assert deliver(monkeypatch, "a") == 0                          # typed in; Herdr still shows `idle`
        return real(*a, **k)
    monkeypatch.setattr(sleeper, "close_session", deliver_first)
    sleeper.tend_self_closes()
    assert fake.signals == [] and fake.agents and _closing() == {} and _event_rows("self-close-held") == []


# ------------------------------------------- v0.1.15: the limit menu and one-time dialogs

LIMIT_MENU = """⏺ Running the migration tests
  ⎿  Error: rate limited
────────────────────────────────
 You've hit your limit · resets Fri 6am
 /usage-credits to finish what you're working on.

 What do you want to do?

 ❯ 1. Stop and wait for limit to reset
   2. Wait here, then continue automatically at Fri 6am
   3. Switch to usage credits

 Enter to confirm · Esc to cancel
"""
AUTO_MODE = """ Make auto mode your default permission mode?

 Auto mode lets Claude decide when to ask.

 {a}Yes, set auto mode as my default permission mode
 {b}No, keep bypass permissions
"""
QUOTED = ("⏺ The runbook says: Claude shows a menu\n"
          "  ❯ 1. Stop and wait for limit to reset\n  3. Switch to usage credits, /usage-credits\n" + EMPTY_SCREEN)
PERMISSION = """ Bash command: rm -rf build
 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently
"""


def test_match_dialog_only_the_live_menu() -> None:
    d, sel = sleeper.match_dialog(LIMIT_MENU)
    assert d and d.kind == "limit" and sel == "Stop and wait for limit to reset"
    assert sleeper.match_dialog(QUOTED)[0] is None                       # quoted above a composer
    assert sleeper.match_dialog(PERMISSION)[0] is None                   # looks alike, is not
    moved = LIMIT_MENU.replace("❯ 1.", "  1.").replace("  3. Switch", "❯ 3. Switch")
    d, sel = sleeper.match_dialog(moved)
    assert d and d.kind == "limit" and sel.startswith("Switch to usage credits")
    d, sel = sleeper.match_dialog(AUTO_MODE.format(a="❯ ", b="  "))
    assert d and d.kind == "auto-mode-default" and sel.startswith("Yes")
    tip = " Selected text auto-copies to your clipboard\n ❯ 1. Yes, try it\n   2. Not now\n"
    assert sleeper.match_dialog(tip)[0].kind == "feature-tip"
    intro = " Claude in Chrome · https://code.claude.com/docs/en/chrome\n Enter to confirm · Esc to cancel\n"
    assert sleeper.match_dialog(intro) == (sleeper.DIALOGS[3], None)
    assert sleeper.match_dialog(None) == (None, "screen unreadable")


def test_slot_room_waits_for_the_reset_after_a_hit() -> None:
    now = 1_000_000.0
    usage = {"a": {"seven_day": {"used_percentage": 93, "resets_at": now + 3600},
                   "five_hour": {"used_percentage": 40, "resets_at": now + 600}, "seen_at": now - 900},
             "b": {"seven_day": {"used_percentage": 96, "resets_at": now + 7200}}}
    assert sleeper.slot_room("a", usage, {}, now)                       # 93 %: room by the numbers
    assert not sleeper.slot_room("a", usage, {"a": now - 60}, now)      # ... but a session just hit it
    assert sleeper.slot_room("a", usage, {"a": now - 60}, now + 3700)   # its fullest window reset since
    assert not sleeper.slot_room("b", usage, {}, now)                   # 96 %
    assert sleeper.slot_room("c", usage, {}, now)                       # never seen: someone chose it
    stale = {"a": {"seven_day": {"used_percentage": 50, "resets_at": now - 100}, "seen_at": now - 99}}
    assert not sleeper.slot_room("a", stale, {"a": now - 60}, now)      # no reset known after the hit
    stale["a"]["seen_at"] = now - 1
    assert sleeper.slot_room("a", stale, {"a": now - 60}, now)          # a reading newer than the hit


def limit_setup(fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, screen: str,
                active: str = "b", usage: dict | None = None) -> dict[str, Any]:
    fake.agents[0].update(agent_status="blocked", state_change_seq=7)
    fake.screen = screen
    accounts(tmp_path, active, {101: "a"})
    if usage is not None:
        (sleeper.ACCOUNTS_DIR / "usage.json").write_text(json.dumps(usage))
    monkeypatch.setattr(sleeper, "process_start", lambda pid: 999.0)
    monkeypatch.setattr(sleeper, "running_build", lambda pid: "/v/2.1.306")
    monkeypatch.setattr(sleeper, "installed_build", lambda kind: "/v/2.1.306")
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(sleeper, "send_note", lambda target, body: notes.append((target, body)))
    real = fake.__call__

    def keys(*argv: Any, **kw: Any) -> Any:
        if argv[:2] == ("pane", "send-keys") and argv[3] == "enter" and fake.screen == LIMIT_MENU:
            fake.agents[0].update(agent_status="idle", state_change_seq=8)   # option 1: the pane goes idle
            fake.screen = EMPTY_SCREEN
        if argv[:2] == ("agent", "start"):                                   # resumed through the launcher
            (sleeper.ACCOUNTS_DIR / "run" / "101").write_text("b\n")
        return real(*argv, **kw)
    monkeypatch.setattr(sleeper, "herdr", keys)
    return {"notes": notes}


def mailbox_bodies(fake: FakeHerdr | None = None) -> list[str]:
    """Notes queued for UUID, and (with fake) the ones the mail flush typed in already."""
    box = sleeper.MAILBOX / UUID
    queued = [json.loads(p.read_text())["body"] for p in sorted(box.glob("*.json"))] if box.exists() else []
    typed = [c[3] for c in (fake.calls if fake else []) if c[:2] == ("agent", "prompt")]
    return typed + queued


def test_limit_menu_moves_the_session_onto_the_active_slot(fake: FakeHerdr, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = limit_setup(fake, tmp_path, monkeypatch, LIMIT_MENU,
                      usage={"b": {"seven_day": {"used_percentage": 20, "resets_at": 2e9}}})
    cfg = {**sleeper.DEFAULTS, "limit_monitor": "mbp/dev-mini-setup"}
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, cfg)
    assert not [c for c in fake.calls if c[:2] == ("pane", "send-keys")]   # first sighting: hands off
    clock["t"] += 60
    sleeper.watch_tick({}, cfg)
    sent = [c[3:] for c in fake.calls if c[:2] == ("pane", "send-keys")]
    assert sent[0] == ("enter",) and ("down",) not in sent                # option 1, nothing else
    assert fake.signals == [(101, signal.SIGTERM)]                       # restarted at once, on the MacBook too
    assert "dialog-answered" in events() and "limit-continue" in events()
    assert len(mailbox_bodies(fake)) == 1 and sleeper.CONTINUE_NOTE.format(slot="b") in mailbox_bodies(fake)[0]
    assert ctx["notes"] == [] and sleeper.read_limited()["sessions"] == {}
    assert sleeper.read_limited()["hits"]["a"] == clock["t"]


def test_limit_menu_never_presses_from_another_option(fake: FakeHerdr, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    moved = LIMIT_MENU.replace("❯ 1.", "  1.").replace("  3. Switch", "❯ 3. Switch")
    limit_setup(fake, tmp_path, monkeypatch, moved)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    for _ in range(3):
        sleeper.watch_tick({}, sleeper.DEFAULTS)
        clock["t"] += 60
    assert not [c for c in fake.calls if c[:2] == ("pane", "send-keys")]
    assert "unknown-dialog" in events() and fake.signals == []


def test_every_slot_used_up_waits_and_tells_the_monitor_once(fake: FakeHerdr, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    usage = {"a": {"seven_day": {"used_percentage": 100, "resets_at": now + 86400}},
             "b": {"seven_day": {"used_percentage": 97, "resets_at": now + 3600}}}
    ctx = limit_setup(fake, tmp_path, monkeypatch, LIMIT_MENU, active="b", usage=usage)
    cfg = {**sleeper.DEFAULTS, "limit_monitor": "mbp/dev-mini-setup"}
    clock = {"t": now}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, cfg)
    clock["t"] += 60
    sleeper.watch_tick({}, cfg)
    assert [c[3:] for c in fake.calls if c[:2] == ("pane", "send-keys")] == [("enter",)]
    assert fake.signals == [] and "limit-wait" in events()
    assert len(ctx["notes"]) == 1 and ctx["notes"][0][0] == "mbp/dev-mini-setup"
    assert "slot a: 100 %" in ctx["notes"][0][1] and "slot b: 97 %" in ctx["notes"][0][1]
    fake.agents[0].update(agent_status="blocked", state_change_seq=9)    # the next turn hits it again
    fake.screen = LIMIT_MENU
    for _ in range(2):
        clock["t"] += 60
        sleeper.watch_tick({}, cfg)
    assert len(ctx["notes"]) == 1                                        # once per reset window


def test_a_limited_session_goes_on_after_its_reset(fake: FakeHerdr, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000_000.0
    usage = {"a": {"five_hour": {"used_percentage": 100, "resets_at": now + 600}}}
    limit_setup(fake, tmp_path, monkeypatch, LIMIT_MENU, active="a", usage=usage)
    clock = {"t": now}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    clock["t"] += 60
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert fake.signals == [] and mailbox_bodies(fake) == []             # a is active and full: wait
    clock["t"] = now + 700
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert sleeper.RESET_NOTE.format(slot="a") in "".join(mailbox_bodies(fake))
    assert sleeper.read_limited()["sessions"] == {}


def test_one_time_dialogs_get_the_choice_that_changes_nothing(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0].update(agent_status="blocked", state_change_seq=7)
    screens = {"now": AUTO_MODE.format(a="❯ ", b="  ")}
    monkeypatch.setattr(sleeper, "herdr_screen", lambda pane: screens["now"])
    real = fake.__call__

    def keys(*argv: Any, **kw: Any) -> Any:
        if argv[:2] == ("pane", "send-keys") and argv[3] == "down":
            screens["now"] = AUTO_MODE.format(a="  ", b="❯ ")
        return real(*argv, **kw)
    monkeypatch.setattr(sleeper, "herdr", keys)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    clock["t"] += 60
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert [c[3:] for c in fake.calls if c[:2] == ("pane", "send-keys")] == [("down",), ("enter",)]
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if '"dialog-answered"' in l][-1]
    assert rec["kind"] == "auto-mode-default" and rec["choice"] == "No, keep bypass permissions"


def test_focused_and_unknown_screens_are_left_alone(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0].update(agent_status="blocked", state_change_seq=7, focused=True)
    fake.screen = LIMIT_MENU
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    clock["t"] += 120
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert not [c for c in fake.calls if c[:2] == ("pane", "send-keys")]  # someone may be looking: 5 min
    fake.agents[0]["focused"] = False
    fake.screen = PERMISSION
    fake.agents[0]["state_change_seq"] = 8
    for _ in range(2):
        clock["t"] += 60
        sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert not [c for c in fake.calls if c[:2] == ("pane", "send-keys")] and "unknown-dialog" in events()
    sleeper.watch_tick({}, {**sleeper.DEFAULTS, "answer_dialogs": False})


def test_limit_monitor_config_is_checked(state: Path) -> None:
    sleeper.CONFIG_FILE.write_text('limit_monitor = "mbp/dev-mini-setup"\nexclude = []\n')
    assert sleeper.load_config()[0]["limit_monitor"] == "mbp/dev-mini-setup"
    sleeper.CONFIG_FILE.write_text('limit_monitor = "-oProxyCommand=x"\nexclude = []\n')
    with pytest.raises(sleeper.ConfigError, match="limit_monitor"):
        sleeper.load_config()


def test_option_two_selected_is_never_answered(fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    two = LIMIT_MENU.replace("❯ 1.", "  1.").replace("   2. Wait", " ❯ 2. Wait")
    d, sel = sleeper.match_dialog(two)
    assert d and d.kind == "limit" and sel.startswith("Wait here")
    limit_setup(fake, tmp_path, monkeypatch, two)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    for _ in range(3):
        sleeper.watch_tick({}, sleeper.DEFAULTS)
        clock["t"] += 60
    assert not [c for c in fake.calls if c[:2] == ("pane", "send-keys")]


def test_limit_restart_in_a_pane_with_a_displaced_row(fake: FakeHerdr, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """mini2 w3:p3, 2026-11-10: the pane kept a displaced handle for another session; the limited
    live session is restarted in place onto b, and the displaced row stays exactly as it was."""
    other = "b28ffddd-0000-4000-8000-000000000000"
    displaced = {"pane_id": "w1:p1", "uuid": other, "kind": "claude", "phase": "displaced", "wake_got": UUID,
                 "argv": ["--dangerously-skip-permissions"], "cwd": "/tmp/proj", "terminal_id": "term-1",
                 "slept_at": "2026-11-09T10:00:00"}
    sleeper.write_json(sleeper.JOURNAL, {"w1:p1": displaced})
    ctx = limit_setup(fake, tmp_path, monkeypatch, LIMIT_MENU,
                      usage={"b": {"seven_day": {"used_percentage": 20, "resets_at": 2e9}}})
    monkeypatch.setattr(sleeper, "pane_at_shell", lambda pane: True)
    monkeypatch.setattr(sleeper, "reconcile", lambda agents: None)   # not what this test is about
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(sleeper.time, "time", lambda: clock["t"])
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    clock["t"] += 60
    sleeper.watch_tick({}, sleeper.DEFAULTS)
    assert fake.signals == [(101, signal.SIGTERM)]
    starts = [c for c in fake.calls if c[:2] == ("agent", "start")]
    assert len(starts) == 1 and "--resume" in starts[0] and UUID in starts[0]
    assert journal() == {"w1:p1": displaced}                                  # untouched
    assert sleeper.CONTINUE_NOTE.format(slot="b") in "".join(mailbox_bodies(fake))
    assert ctx["notes"] == []
    rec = [json.loads(l) for l in sleeper.EVENTS.read_text().splitlines() if '"restarted"' in l][-1]
    assert rec["in_place"] is True and rec["new"] == "account b"


# ------------------------------------------- v0.1.17: the cursor cell of a prompt suggestion

def test_suggestion_cursor_cell_is_not_a_draft() -> None:
    """mini2 mbeg-orch 2026-11-10: `❯ ok` suggestion, cursor (SGR 7) over its first letter."""
    raw = "❯\xa0\x1b[0m\x1b[7mo\x1b[0m\x1b[2mk\x1b[0m"
    assert sleeper.typed_text(raw).strip("❯\xa0 ") == ""
    assert sleeper.composer_state(f"⏺ done\n────────\n{raw}\n────────\n  footer\n") == ("empty", "")
    draft = "❯ ok\x1b[7m \x1b[0m"                                   # typed, cursor after it
    assert sleeper.typed_text(draft).strip("❯ ") == "ok"
    lone = "❯ \x1b[7my\x1b[0m"                                       # reverse with no dim run after: typed
    assert sleeper.typed_text(lone).strip("❯ ") == "y"
    assert sleeper.sgr_attrs("38;2;7;7;7", False, False) == (False, False)   # colour args are no attributes
    assert sleeper.sgr_attrs("7;2", False, False) == (True, True) and sleeper.sgr_attrs("27", True, True) == (True, False)


def test_unnamed_agent_sleeps_under_its_title_and_wakes_with_that_name(fake: FakeHerdr,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """dev-mini-setup 2026-11-10: a pane restarted outside Herdr has no agent name but its session's
    title; it sleeps and wakes under that title, so a deliver by the visible name finds it."""
    fake.agents[0].update(name=None, terminal_title_stripped="ansible-orch-arana")
    sleeper.sleep_agent(dict(fake.agents[0]), 12, set(), dry_run=False)
    assert journal()["w1:p1"]["name"] == "ansible-orch-arana"
    assert sleeper.resolve_target("ansible-orch-arana", [], journal())[0] == "asleep"
    fake.stub_panes.add("w1:p1")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(sleeper.os, "getcwd", lambda: "/tmp/proj")
    monkeypatch.setattr(sleeper.shutil, "which", lambda b: f"/opt/bin/{b}")
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: None)
    sleeper.cmd_stub("w1:p1", UUID)
    fake.stub_panes.discard("w1:p1")
    fake.agents[0].update(agent="claude", agent_session={"value": UUID}, name=None)
    sleeper.reconcile(fake.agents)
    assert fake.agents[0]["name"] == "ansible-orch-arana" and journal() == {}


@pytest.mark.parametrize("title, named, other", [
    ("two words", None, None),          # not a name
    ("claude", None, None),             # the agent kind, not a name
    ("busy-title", "a", None),          # has a Herdr name: that one wins
    ("shared", None, "shared"),         # another live pane shows the same title
])
def test_title_is_no_name_when_invalid_taken_or_already_named(title: str, named: str | None,
                                                              other: str | None) -> None:
    me = agent(name=named)
    me["terminal_title_stripped"] = title
    others = [me]
    if other:
        twin = agent(pane="w1:p2", uuid="30603f77-21a6-4a16-9f2b-77a87efbc665", name=None)
        twin["terminal_title_stripped"] = other
        others.append(twin)
    assert sleeper.title_name(me, others, {}) is None


def test_title_is_no_name_when_a_sleeping_session_goes_by_it() -> None:
    me = agent(name=None)
    me["terminal_title_stripped"] = "dev-mini-setup"
    row = {"pane_id": "w2:p1", "uuid": "30603f77-21a6-4a16-9f2b-77a87efbc665", "name": "dev-mini-setup",
           "kind": "claude", "argv": [], "cwd": "/tmp", "phase": "asleep"}
    journal_rows = {"w2:p1": row} if sleeper.valid_entry(row) else {}
    assert journal_rows, "fixture row must be a valid journal entry"
    assert sleeper.title_name(me, [me], journal_rows) is None
    assert sleeper.title_name(me, [me], {}) == "dev-mini-setup"


def test_sane_tty_restores_enter_and_ctrl_c_after_a_killed_agent() -> None:
    """2026-11-11: a claude killed by SIGTERM left icanon -icrnl -isig -iexten on the pane's tty;
    Enter (CR) never ended input() and ctrl-c was a literal ^C, so the slept pane could not wake."""
    import pty
    import select
    import termios
    leader, follower = pty.openpty()
    try:
        attrs = termios.tcgetattr(follower)
        attrs[0] &= ~termios.ICRNL
        attrs[0] |= termios.IGNCR
        attrs[3] &= ~(termios.ISIG | termios.IEXTEN | termios.ECHO)
        attrs[3] |= termios.ICANON
        termios.tcsetattr(follower, termios.TCSANOW, attrs)
        sleeper.sane_tty(follower)
        iflag, _oflag, _cflag, lflag = termios.tcgetattr(follower)[:4]
        assert iflag & termios.ICRNL and not iflag & termios.IGNCR
        assert lflag & termios.ISIG and lflag & termios.ICANON and lflag & termios.ECHO and lflag & termios.IEXTEN
        os.write(leader, b"\r")   # Enter on such a terminal is CR: it now ends the line as \n
        ready, _w, _x = select.select([follower], [], [], 2)
        assert ready and os.read(follower, 16) == b"\n"
    finally:
        os.close(leader)
        os.close(follower)


def test_sane_tty_ignores_a_non_tty(tmp_path: Path) -> None:
    with open(tmp_path / "f", "w") as fh:
        sleeper.sane_tty(fh.fileno())   # no exception
    sleeper.sane_tty(-1)


# Claude Code 2.1 once the turn is over while a background agent and a workflow still run
# (md-verteidigung, mini2 2026-11-11): Herdr says `working`, the prompt takes input.
BG_WAIT_SCREEN = (
    "⏺ Runde 2 schicke ich, sobald die Kritikpunkte umgesetzt sind.\n\n"
    "\x1b[38;2;153;153;153m✻ Waiting for \x1b[1m1\x1b[0m background agent and \x1b[1m1\x1b[0m dynamic workflow to finish\n\n"
    + "─" * 60 + " md-verteidigung ─\n❯ {composer}\n" + "─" * 80 + "\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n\n"
    "  ⏺ main\n"
    "  ◯ general-purpose        Fixing assertions        32m 18s · ↓ 214.1k tokens\n"
    "  ◯ kritikrunde-1-analyse  ▰▰▰▰▱▱  6/7 · 17m27s · ↓ 1.7m tokens · /workflows to stop\n")


def test_background_wait_reads_only_the_line_over_the_composer() -> None:
    assert sleeper.background_wait(BG_WAIT_SCREEN.format(composer=""))
    assert sleeper.composer_state(BG_WAIT_SCREEN.format(composer="")) == ("empty", "")   # the task panel is footer
    busy = BG_WAIT_SCREEN.replace("Waiting for", "Thinking… (esc to interrupt) Waiting for")
    assert not sleeper.background_wait(busy.format(composer=""))
    quoted = ("✻ Waiting for 1 background agent to finish\n⏺ later output\n✻ Clauding… (esc to interrupt)\n"
              + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n")
    assert not sleeper.background_wait(quoted)                     # transcript text further up never counts
    assert not sleeper.background_wait(None)


def test_composer_with_a_task_panel_below_still_sees_a_draft() -> None:
    state, _ = sleeper.composer_state(BG_WAIT_SCREEN.format(composer="half a thought"))
    assert state == "draft"
    # a prompt whose box is not the last one on screen is no composer we can vouch for
    odd = "❯ \n" + "─" * 40 + "\n⏺ more\n" + "─" * 40 + "\n  footer\n"
    assert sleeper.composer_state(odd)[0] == "unknown"


def test_mail_goes_into_a_claude_pane_waiting_on_background_work(fake: FakeHerdr,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    fake.screen = BG_WAIT_SCREEN.format(composer="")
    assert deliver(monkeypatch, "a") == 0
    assert prompts(fake)[-1][3].endswith("please rebase") and sleeper.pending_mail(UUID) == []


def test_mail_waits_while_the_turn_itself_runs_or_a_draft_is_open(fake: FakeHerdr,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    fake.agents[0]["agent_status"] = "working"
    fake.screen = BG_WAIT_SCREEN.replace("Waiting for", "Thinking… (esc to interrupt)").format(composer="")
    assert deliver(monkeypatch, "a") == 3 and prompts(fake) == []
    fake.screen = BG_WAIT_SCREEN.format(composer="unsent words")
    sleeper.flush_mail(fake.agents)
    assert prompts(fake) == [] and len(sleeper.pending_mail(UUID)) == 1
    fake.screen = BG_WAIT_SCREEN.format(composer="")
    sleeper.flush_mail(fake.agents)                                # the watcher types it in once the prompt waits
    assert len(prompts(fake)) == 1 and sleeper.pending_mail(UUID) == []


def test_background_wait_is_claude_only(fake: FakeHerdr) -> None:
    other = {**fake.agents[0], "agent": "opencode", "agent_status": "working"}
    assert not sleeper.mail_ready_status(other, BG_WAIT_SCREEN.format(composer=""), read_screen=False)
    blocked = {**fake.agents[0], "agent_status": "blocked"}         # a permission prompt never takes mail
    assert not sleeper.mail_ready_status(blocked, BG_WAIT_SCREEN.format(composer=""), read_screen=False)


def test_a_displaced_row_answers_to_its_session_id_only(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """mini2 wake-w3-p3, 2026-11-10..13: deliveries by the pane's wake name went into a displaced
    row's mailbox while the pane's newcomer, which senders meant, never saw them."""
    slept_entry(fake)
    fake.agents[0] = agent(uuid="7d1c2a9e-5b3f-4e8a-9c1d-2f6b8a4e0c13", name="taxi-lying")  # the newcomer
    j = journal(); j["w1:p1"]["phase"] = "displaced"; sleeper.write_json(sleeper.JOURNAL, j)
    assert deliver(monkeypatch, "wake-w1-p1") == 1                   # no silent queue for a dead handle
    assert "no live or sleeping agent matches 'wake-w1-p1'" in capsys.readouterr().err
    assert sleeper.pending_mail(UUID) == []
    assert deliver(monkeypatch, UUID, msg_id="m-uuid") == 3           # by session id it still queues
    assert len(sleeper.pending_mail(UUID)) == 1


def test_close_drops_a_displaced_row_whose_pane_is_gone_and_sets_its_mail_aside(
        fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    slept_entry(fake)
    j = journal(); j["w1:p1"]["phase"] = "displaced"; sleeper.write_json(sleeper.JOURNAL, j)
    fake.agents[0] = agent(uuid="7d1c2a9e-5b3f-4e8a-9c1d-2f6b8a4e0c13", name="taxi-lying")
    assert deliver(monkeypatch, UUID, msg_id="m-old") == 3
    # while the pane is there (someone else's now), the row is still not ours to drop
    assert _close(monkeypatch, UUID) == 1
    assert "not provably its own" in capsys.readouterr().err and "w1:p1" in journal()
    fake.agents.clear()                                                # the pane was closed
    assert _close(monkeypatch, UUID) == 0
    out = capsys.readouterr().out
    assert "dropped the displaced sleep row" in out and "1 queued message(s) set aside" in out
    assert journal() == {} and sleeper.pending_mail(UUID) == []
    aside = list((sleeper.MAILBOX / UUID).glob("retired-*/*.json"))
    assert len(aside) == 1 and "displaced-retired" in events()
    assert not any(c[:2] in (("tab", "close"), ("pane", "close")) for c in fake.calls)



# ------------------------------------------------------------- ask the owner

def _ask(monkeypatch: pytest.MonkeyPatch, context: str = "", **kw: Any) -> int:
    """`ask` from inside the agent in w1:p1 (its pid 101 is our ancestor)."""
    import io
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [101, 50, 1])
    monkeypatch.setattr(sleeper.sys, "stdin", io.StringIO(context))
    args = dict(question="Deploy now or tonight?", option=["now", "tonight"], recommend=2, withdraw=None,
                list=False, all=False, json=False)
    args.update(kw)
    return sleeper.cmd_ask(types.SimpleNamespace(**args))


def test_ask_opens_a_tab_next_to_the_asker(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    fake.agents[0].update(tab_id="w1:t1", workspace_id="w1")
    fake.tab_labels, fake.workspace_labels = {"w1:t1": "api"}, {"w1": "ts"}
    assert _ask(monkeypatch, context="The canary is green.\n") == 0
    out = capsys.readouterr().out
    (ask_id, rec), = sleeper.read_json(sleeper.ASKS).items()
    assert rec["state"] == "open" and rec["uuid"] == UUID and rec["options"] == ["now", "tonight"]
    assert rec["context"] == "The canary is green." and rec["asker"] == "a"
    create = next(c for c in fake.calls if c[:2] == ("tab", "create"))
    assert "❓ a" in create and create[create.index("--workspace") + 1] == "w1"
    run = next(c for c in fake.calls if c[:2] == ("pane", "run"))
    assert run[2] == rec["pane"] and f"ask-pane {ask_id}" in run[3]
    assert f"ask {ask_id} is open in {sleeper.this_machine()} › ts › ❓ a" in out
    assert _ask(monkeypatch, list=True) == 0 and ask_id in capsys.readouterr().out


@pytest.mark.parametrize("kw, needle", [
    ({"question": ""}, "--question is empty"),
    ({"question": "two\nlines"}, "one line"),
    ({"option": ["x"] * 10, "recommend": None}, "at most 9"),
    ({"recommend": 3}, "--recommend names an option"),
])
def test_ask_refusals(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                      kw: dict[str, Any], needle: str) -> None:
    assert _ask(monkeypatch, **kw) == 2
    assert needle in capsys.readouterr().err and not sleeper.ASKS.exists()


def test_ask_needs_the_calling_agent(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sleeper, "ancestors", lambda table, pid: [7])
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    args = types.SimpleNamespace(question="q?", option=None, recommend=None, withdraw=None, list=False,
                                 all=False, json=False)
    assert sleeper.cmd_ask(args) == 1 and "does not run under the agent" in capsys.readouterr().err


def _answer(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch, typed: list[str], state: Path) -> dict:
    ask_id, rec = next(iter(sleeper.read_json(sleeper.ASKS).items()))
    monkeypatch.setenv("HERDR_PANE_ID", rec["pane"])
    monkeypatch.setattr(sleeper, "sane_tty", lambda fd=None: None)
    feed = iter(typed)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(feed))
    assert sleeper.cmd_ask_pane(types.SimpleNamespace(ask_id=ask_id, state_dir=str(sleeper.STATE_DIR))) == 0
    return dict(sleeper.read_json(sleeper.ASKS)[ask_id], id=ask_id)


def test_ask_pane_shows_blocked_and_sends_the_pick_as_the_owners_answer(
        fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _ask(monkeypatch) == 0
    prompts: list[str] = []
    real = fake.__call__

    def spy(*args: str, **kw: Any) -> Any:
        if args[:2] == ("agent", "prompt"):
            prompts.append(args[3])
        if args[:2] == ("pane", "report-agent") and sleeper.ASK_AGENT in args:
            assert "blocked" in args
        return real(*args, **kw)
    monkeypatch.setattr(sleeper, "herdr", spy)
    rec = _answer(fake, monkeypatch, ["", "2"], state)
    assert rec["state"] == "answered" and rec["answer"] == "option 2: tonight" and rec["delivered"]
    assert prompts and prompts[-1].startswith(f"[the owner's answer to your ask {rec['id']}")
    assert "relayed from" not in prompts[-1] and "A: option 2: tonight" in prompts[-1]
    assert ("tab", "close", rec["tab"]) in fake.calls
    assert not any(a.get("agent") == sleeper.ASK_AGENT for a in fake.agents)
    assert "(recommended)" in capsys.readouterr().out


def test_ask_pane_takes_free_text_and_discuss(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _ask(monkeypatch) == 0
    assert _answer(fake, monkeypatch, ["wait for Monday"], state)["answer"] == "wait for Monday"
    assert _ask(monkeypatch) == 0
    asks = sleeper.read_json(sleeper.ASKS)
    open_id = next(k for k, v in asks.items() if v["state"] == "open")
    sleeper.write_json(sleeper.ASKS, {open_id: asks[open_id]})
    assert _answer(fake, monkeypatch, ["D"], state)["state"] == "discuss"


def test_presence_does_not_list_ask_tabs(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert _ask(monkeypatch) == 0
    rec = next(iter(sleeper.read_json(sleeper.ASKS).values()))
    fake("pane", "report-agent", rec["pane"], "--source", sleeper.ASK_SOURCE, "--agent", sleeper.ASK_AGENT,
         "--state", "blocked")
    capsys.readouterr()
    assert sleeper.cmd_presence(types.SimpleNamespace(json=True)) == 0
    assert [r["pane"] for r in json.loads(capsys.readouterr().out)] == ["w1:p1"]


def test_ask_withdraw_is_the_askers_and_closes_the_tab(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    assert _ask(monkeypatch) == 0
    ask_id, rec = next(iter(sleeper.read_json(sleeper.ASKS).items()))
    other = dict(rec, uuid="someone-else")
    sleeper.write_json(sleeper.ASKS, {ask_id: other})
    assert _ask(monkeypatch, withdraw=ask_id) == 1 and "another session's ask" in capsys.readouterr().err
    sleeper.write_json(sleeper.ASKS, {ask_id: rec})
    assert _ask(monkeypatch, withdraw=ask_id) == 0
    assert sleeper.read_json(sleeper.ASKS)[ask_id]["state"] == "withdrawn" and ("tab", "close", rec["tab"]) in fake.calls
    # the tab's program, started again after a restart, sees it closed and closes the tab
    monkeypatch.setenv("HERDR_PANE_ID", rec["pane"])
    assert sleeper.cmd_ask_pane(types.SimpleNamespace(ask_id=ask_id, state_dir=str(sleeper.STATE_DIR))) == 0
    assert "no longer open" in capsys.readouterr().out
