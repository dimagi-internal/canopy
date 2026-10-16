#!/usr/bin/env python3
"""Rail: no foreground shell wait may sleep past the prompt-cache window.

The prompt cache expires 5 minutes after it was last used. A foreground Bash call that
sleeps longer than that wakes into a COLD cache: the next model call re-writes the whole
context (450-720k tokens on a long ACE phase agent) and is slower for it. Evidence,
ACE runs 2026-10-15/16 (noora-health, kaduna-graduation, smallholder-seasonal): phase
agents waited on children with `sleep 300`..`sleep 595` and
`perl -e 'select(undef,undef,undef,590)'` — the second spelling used because the harness
refuses a bare long `sleep` — and woke 7-9 times per wait, each wake a full re-cache.

So the cap is 270 s (5 min minus headroom for the call itself), counted the way the
wait actually runs:

  * every sleep the command line can reach is summed: `sleep N` (with s/m/h/d suffixes
    and several operands), `perl -e 'select(undef,undef,undef,N)'` / `sleep N`,
    `python -c 'time.sleep(N)'`, PowerShell `Start-Sleep`, and anything inside
    `bash|sh|zsh -c '...'`;
  * a `for` loop multiplies its body by its iteration count (`$(seq ...)`, `{a..b}`,
    a literal word list, C-style `for ((i=0;i<N;i++))`) — `for i in $(seq 1 115); do
    sleep 5; done` is 575 s, not 5;
  * a `while`/`until` loop that sleeps is unbounded;
  * `timeout D cmd` caps what `cmd` can wait;
  * and nothing can outlast the Bash tool's own `timeout` (default 120 s), so a wait is
    judged on min(estimate, tool timeout) — an unbounded poll loop under the default
    timeout is fine; the same loop under `timeout: 600000` is not.

Only waits are judged. A 10-minute test suite has no sleep in it and is never touched;
`run_in_background: true` commands are allowed (they don't hold the session).

The right patterns, named in the refusal: run the work in the background and wait on
its completion notification; poll in steps of <= 270 s; `gh pr checks <n> --watch` as a
background command. In a one-shot runner turn (see one_shot_turn_guard.py) background
is not available, so the refusal names only the <= 270 s chunk loop.

Override: CANOPY_SLEEP_CAP_SECONDS (a number; 0 or "off" disables the rail).
Fails OPEN on any unexpected input — a rail that wedges sessions is worse than the gap.
Stdlib only (system python3, like every canopy hook).
"""
import json
import math
import os
import re
import shlex
import sys

DEFAULT_CAP_S = 270
#: Claude Code's Bash default when the call names no `timeout` (ms).
DEFAULT_TOOL_TIMEOUT_MS = 120_000
SHELL_TOOLS = {"Bash", "PowerShell"}

_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SEPARATORS = {";", "&&", "||", "|", "&", ";;", "|&"}
_TRANSPARENT = {"(", ")", "{", "}", "!", "then", "else", "fi", "elif", "if", "in", "esac",
                "wait", "time"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}

INF = math.inf


# ---------------------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------------------

def _arith(m: "re.Match") -> str:
    expr = m.group(1)
    if re.fullmatch(r"[\d\s+\-*/()]+", expr):
        try:
            return str(int(eval(expr, {"__builtins__": {}}, {})))  # digits + operators only
        except Exception:
            return m.group(0)
    return m.group(0)


def _newlines_to_semicolons(cmd: str) -> str:
    """Unquoted newlines end a statement; shlex would otherwise treat them as spaces and
    glue `sleep 5` onto the `done` on the next line."""
    out, quote, esc = [], None, False
    for ch in cmd:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\" and quote != "'":
            esc = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            continue
        out.append(ch)
    return "".join(out)


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)


def _tokens(cmd: str):
    # A heredoc body is DATA (a doc being written, a commit message), not commands —
    # "sleep 600" in a README must not be read as a wait.
    cmd = _HEREDOC.sub(" ", cmd)
    cmd = re.sub(r"\$\(\((.*?)\)\)", _arith, cmd)
    # Redirections (`2>&1`, `&>file`, `>&2`) are not a background `&`.
    cmd = re.sub(r"\d*>&\d*-?|&>>?", " > ", cmd)
    cmd = _newlines_to_semicolons(cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = "#"
    return list(lex)


# ---------------------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------------------

def _duration(word: str):
    """`5`, `1.5m`, `2h` → seconds; `infinity` → inf; anything else (a $VAR) → None."""
    if word.lower() in ("inf", "infinity"):
        return INF
    m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)([smhd]?)", word)
    if not m:
        return None
    return float(m.group(1)) * _UNIT[m.group(2)]


def _sleep_operands(args) -> float:
    total = 0.0
    for a in args:
        d = _duration(a)
        if d is not None:
            total += d
    return total


def _perl_seconds(code: str) -> float:
    total = 0.0
    for m in re.finditer(r"select\s*\(\s*undef\s*,\s*undef\s*,\s*undef\s*,\s*([\d.]+)\s*\)", code):
        total += float(m.group(1))
    for m in re.finditer(r"\b(?:usleep|sleep)\s*\(?\s*([\d.]+)", code):
        n = float(m.group(1))
        total += n / 1e6 if code[m.start():m.start() + 6] == "usleep" else n
    return total


def _python_seconds(code: str) -> float:
    return sum(float(m.group(1)) for m in re.finditer(r"\bsleep\s*\(\s*([\d.]+)\s*\)", code))


def _powershell_seconds(cmd: str) -> float:
    total = 0.0
    for m in re.finditer(r"(?i)\b(?:Start-Sleep|sleep)\b([^;|\n]*)", cmd):
        rest = m.group(1)
        ms = re.search(r"(?i)-(?:m|milliseconds)\s+([\d.]+)", rest)
        if ms:
            total += float(ms.group(1)) / 1000
            continue
        s = re.search(r"(?i)(?:-(?:s|seconds)\s+)?([\d.]+)", rest)
        if s:
            total += float(s.group(1))
    return total


def _loop_count(words):
    """Iterations of `for v in WORDS`, or None when it can't be known."""
    text = " ".join(words)
    m = re.search(r"(?:\$\s*\(|`)\s*seq\s+(-?\d+)(?:\s+(-?\d+))?(?:\s+(-?\d+))?\s*(?:\)|`)", text)
    if m:
        nums = [int(x) for x in m.groups() if x is not None]
        if len(nums) == 1:
            first, step, last = 1, 1, nums[0]
        elif len(nums) == 2:
            first, step, last = nums[0], 1, nums[1]
        else:
            first, step, last = nums
        if step == 0:
            return None
        return max(0, (last - first) // step + 1)
    m = re.fullmatch(r"\{(-?\d+)\.\.(-?\d+)(?:\.\.(\d+))?\}", text.strip())
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        step = int(m.group(3) or 1) or 1
        return abs(b - a) // step + 1
    if any(ch in text for ch in "$`*?"):
        return None
    return len(words)


# ---------------------------------------------------------------------------------------
# A small recursive walk over the token stream
# ---------------------------------------------------------------------------------------

class _Walk:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def seq(self, stop=()):
        """Seconds of a statement list, up to (not past) a token in `stop`."""
        total = 0.0
        while self.peek() is not None and self.peek() not in stop:
            tok = self.peek()
            if tok in _SEPARATORS:
                self.take()
                continue
            total += self.statement(stop)
        return total

    def statement(self, stop):
        tok = self.peek()
        if tok == "for":
            return self.for_loop()
        if tok in ("while", "until"):
            return self.cond_loop()
        if tok in _TRANSPARENT or tok in ("do", "done"):
            self.take()
            return 0.0
        words = []
        while self.peek() is not None and self.peek() not in _SEPARATORS \
                and self.peek() not in stop and self.peek() not in ("(", ")", "do", "done"):
            words.append(self.take())
        backgrounded = self.peek() == "&"
        secs = _simple(words)
        return 0.0 if backgrounded else secs

    def for_loop(self):
        self.take()  # for
        words = []
        while self.peek() is not None and self.peek() != "do":
            words.append(self.take())
        if self.peek() == "do":
            self.take()
        body = self.seq(stop=("done",))
        if self.peek() == "done":
            self.take()
        if not body:
            return 0.0
        joined = " ".join(words)
        c = re.search(r"\(\s*\(\s*\w+\s*=\s*(-?\d+)\s*;\s*\w+\s*(<=|<)\s*(-?\d+)", joined)
        if c:
            first, op, last = int(c.group(1)), c.group(2), int(c.group(3))
            count = max(0, last - first + (1 if op == "<=" else 0))
        else:
            items = [w for w in words if w not in (";",)]
            if items and items[0] not in ("(",):
                items = items[1:]  # the loop variable
            if items and items[0] == "in":
                items = items[1:]
            count = _loop_count(items) if items else None
        return INF if count is None else body * count

    def cond_loop(self):
        self.take()  # while / until
        cond = self.seq(stop=("do",))
        if self.peek() == "do":
            self.take()
        body = self.seq(stop=("done",))
        if self.peek() == "done":
            self.take()
        return INF if (cond or body) else 0.0


def _simple(words) -> float:
    """Seconds one simple command can wait."""
    while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
        words = words[1:]  # VAR=value prefixes
    if not words:
        return 0.0
    cmd = os.path.basename(words[0])
    args = words[1:]
    if cmd in ("sleep", "gsleep"):
        return _sleep_operands(args)
    if cmd in ("timeout", "gtimeout"):
        rest = list(args)
        while rest and rest[0].startswith("-"):
            opt = rest.pop(0)
            if opt in ("-s", "-k", "--signal", "--kill-after") and rest:
                rest.pop(0)
        if not rest:
            return 0.0
        cap = _duration(rest[0])
        inner = _simple(rest[1:])
        return inner if cap is None else min(cap, inner)
    if cmd in ("nohup", "exec", "command", "env", "nice", "caffeinate"):
        return _simple([a for a in args if not a.startswith("-")])
    if cmd in _SHELLS and "-c" in args:
        idx = args.index("-c")
        if idx + 1 < len(args):
            return wait_seconds(args[idx + 1])
    if cmd.startswith("perl"):
        return sum(_perl_seconds(args[i + 1]) for i, a in enumerate(args)
                   if a in ("-e", "-E") and i + 1 < len(args))
    if cmd.startswith("python"):
        return sum(_python_seconds(args[i + 1]) for i, a in enumerate(args)
                   if a == "-c" and i + 1 < len(args))
    return 0.0


def wait_seconds(cmd: str) -> float:
    """Upper bound on the seconds a shell command line spends SLEEPING (inf = unbounded)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return 0.0
    w = _Walk(_tokens(cmd))
    total = w.seq()
    return total


# ---------------------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------------------

def cap_seconds(env=None):
    env = os.environ if env is None else env
    raw = (env.get("CANOPY_SLEEP_CAP_SECONDS") or "").strip().lower()
    if not raw:
        return DEFAULT_CAP_S
    if raw in ("0", "off", "false", "no", "none"):
        return None
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_CAP_S


def _tool_timeout_s(tool_input, env) -> float:
    t = tool_input.get("timeout")
    if isinstance(t, (int, float)) and t > 0:
        return t / 1000
    try:
        return float(env.get("BASH_DEFAULT_TIMEOUT_MS") or DEFAULT_TOOL_TIMEOUT_MS) / 1000
    except ValueError:
        return DEFAULT_TOOL_TIMEOUT_MS / 1000


def _one_shot(env) -> bool:
    flag = (env.get("CANOPY_ONE_SHOT_TURN") or "").strip().lower()
    if flag:
        return flag not in ("0", "false", "no", "off")
    return "CANOPY_REQUESTED_BY" in env


def decide(tool: str, tool_input: dict, env=None):
    """None to allow, else the refusal text."""
    env = os.environ if env is None else env
    if tool not in SHELL_TOOLS or not isinstance(tool_input, dict):
        return None
    cap = cap_seconds(env)
    if cap is None:
        return None
    bg = tool_input.get("run_in_background")
    if bg is True or (isinstance(bg, str) and bg.strip().lower() in ("true", "1", "yes")):
        return None
    cmd = tool_input.get("command") or ""
    try:
        slept = _powershell_seconds(cmd) if tool == "PowerShell" else wait_seconds(cmd)
    except Exception:
        return None  # unparseable (e.g. an unterminated quote): fail open
    if slept <= 0:
        return None
    effective = min(slept, _tool_timeout_s(tool_input, env))
    if effective <= cap:
        return None
    shown = "an unbounded loop" if slept == INF else f"~{int(slept)} s"
    if _one_shot(env):
        fix = (f"poll in foreground steps of at most {int(cap)} s — one short check per Bash "
               f"call (e.g. `sleep {int(cap) - 30}; gh pr view <n> --json state`), then decide "
               f"and call again")
    else:
        fix = ("run the work itself with run_in_background: true and wait for its completion "
               "notification (no sleep at all); or `gh pr checks <n> --watch` as a background "
               f"command; or, if you must poll, poll in steps of at most {int(cap)} s per Bash call")
    return (f"canopy: blocked a foreground wait of {shown} (counted to {int(effective)} s "
            f"against the Bash timeout). The prompt cache expires after 5 minutes idle, so a "
            f"wait over {int(cap)} s wakes into a cold cache and re-writes the whole context "
            f"on the next call — slower and wasteful, and agents were doing it 7-9 times per "
            f"wait. Instead, {fix}.")


def main() -> int:
    try:
        data = json.load(sys.stdin)
        reason = decide(data.get("tool_name", ""), data.get("tool_input") or {})
    except Exception:
        return 0  # fail open
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
