"""Unit tests for the herdr-sleeper plugin: pure decision logic plus the sleep/wake state
machine, the watcher tick, the focus hook, and the in-pane stub — against a fake `herdr`
(no Herdr, no Claude needed)."""

from __future__ import annotations

import json
import os
import signal
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


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state dir + transcript root, with a transcript for UUID on disk."""
    d = tmp_path / "state"
    for name in ("STATE_DIR", "JOURNAL", "SNAPSHOT", "EVENTS", "LOGFILE", "LOCK", "WATCHER_PID",
                 "IDLE_FILE", "WATCHER_LOCK"):
        base = getattr(sleeper, name, None)
        if base is None:
            continue
        monkeypatch.setattr(sleeper, name, d / base.name if name != "STATE_DIR" else d)
    monkeypatch.setattr(sleeper, "LEGACY_STATE_DIR", tmp_path / "legacy")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", tmp_path / "absent.toml")
    monkeypatch.setattr(sleeper, "LEGACY_CONFIG_FILES", [tmp_path / "legacy-config.toml"], raising=False)
    # never spawn a real watcher from a unit test; the heal path has its own tests
    monkeypatch.setattr(sleeper, "heal_watcher", lambda: None, raising=False)
    if hasattr(sleeper, "STOP"):
        monkeypatch.setitem(sleeper.STOP, "requested", False)
    for var in ("HERDR_PLUGIN_CONTEXT_JSON", "HERDR_PANE_ID", "HERDR_PLUGIN_EVENT_JSON"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    monkeypatch.setattr(sleeper, "TRANSCRIPTS", root)
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
                    "terminal_id": a.get("terminal_id")}
            if a.get("agent"):
                pane["agent"] = a["agent"]
                pane["agent_session"] = a.get("agent_session")
            return {"pane": pane}
        if args[:2] == ("pane", "process-info"):
            pane = args[args.index("--pane") + 1]
            if self.process_info_broken:
                return {}
            return {"process_info": {"foreground_processes": self._procs(pane, by_pane.get(pane))}}
        if args[:2] == ("agent", "start"):
            pane = args[args.index("--pane") + 1]
            if args[2] in self.taken_names:
                raise sleeper.SleeperError(f"herdr agent start failed: {{'code': 'agent_name_taken', 'name': '{args[2]}'}}")
            uuid = self.start_uuid or args[args.index("--resume") + 1]
            kind = args[args.index("--kind") + 1]
            a = by_pane[pane]
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
            by_pane[args[2]]["agent"] = sleeper.CLAIM_AGENT
            return {}
        if args[:2] == ("pane", "release-agent"):
            a = by_pane[args[2]]
            if a.get("agent") == sleeper.CLAIM_AGENT:
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
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: None)
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(sleeper, "EXIT_WAIT_SECONDS", 2)
    monkeypatch.setattr(sleeper.os, "kill", f.kill)
    monkeypatch.setattr(sleeper, "process_table", f.process_table)
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
    assert ok(["--unknown-flag", "value"])[0] is None and "unrecognised" in ok(["--unknown-flag", "value"])[1]
    assert ok(["--model"])[0] is None  # value option missing its value


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
    assert cs(CLAUDE_SCREEN.format(composer="half-typed thought")) == ("draft", "text in the composer")
    multiline = CLAUDE_SCREEN.format(composer="") .replace("────────\n  ◤", "  second line of a draft\n────────\n  ◤", 1)
    assert cs(multiline)[0] == "draft"
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
                "HERDR_SLEEPER_WAKE_ON_FOCUS"):
        monkeypatch.delenv(var, raising=False)
    values, source = sleeper.load_config()
    assert values == {"idle_hours": 12.0, "agents": ["claude", "opencode"], "exclude": [],
                      "poll_seconds": 60, "notify": True, "wake_on_focus": True}
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


def test_watch_tick_honors_excludes_and_kinds(fake: FakeHerdr, state: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('idle = "0s"\nexclude = ["a"]\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    config, _src = sleeper.load_config()
    clocks: dict[str, Any] = {}
    sleeper.watch_tick(clocks, config)
    assert journal() == {} and clocks == {}                       # excluded before the clock even starts

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
