"""Run one child process the way the runner runs every child (05, Processes and logs): in its own
process group, with its output streamed to files as it arrives and only a bounded tail kept in
memory, under the runner's own clock, and with token-shaped strings redacted before anything is
stored. Used for agents and for gate and check commands alike. Reports facts; decides nothing.
"""

import copy
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time

from . import record

TAIL_BYTES = 256 * 1024
LINE_LIMIT = 64 * 1024
# What the log and every stream observer see instead of a line longer than LINE_LIMIT.
OVERLONG_PLACEHOLDER = b"[overlong line omitted for safe redaction]"
# A caller that must parse a long line (an agent's terminal event) gets it whole, redacted, up to this.
RAW_LINE_LIMIT = 16 * 1024 * 1024
GRACE_S = 5.0

# The one list of what counts as a secret in stored output (04, rule 5; RUN-10).
REDACTIONS = [
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"gh[ousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(rb"(?i)(aws_secret_access_key\s*[=:]\s*)[A-Za-z0-9/+=]{40}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
]
REDACTED = b"[redacted]"


class Cancelled(Exception):
    """An `on_start` refusal of a call whose task code was not released yet. On its way out it
    carries `runs`, the commands that ran before it (`checks.run_commands` sets it), and
    `cleanup`, an open cleanup of its own group when stopping that group failed (`run_process`
    sets it): the caller settles both, never as "nothing ran" (PROC-25, PROC-26)."""
    runs = ()
    cleanup = None


def redact(data):
    for pattern in REDACTIONS:
        data = pattern.sub(lambda m: (m.group(1) if m.groups() else b"") + REDACTED, data)
    return data


class _Sink:
    """Redacts whole lines, writes them to the log at once, keeps a bounded tail. With `keep_raw`
    it also keeps the last non-blank line whole, and hands each overlong line whole to
    `on_overlong`, both redacted and up to RAW_LINE_LIMIT; the log still gets the placeholder."""

    def __init__(self, path, observer=None, keep_raw=False, on_overlong=None):
        self.observer, self.on_overlong = observer, on_overlong
        self.keep_raw = keep_raw or on_overlong is not None
        self.fh = open(path, "ab")
        self.pending = b""
        self.tail = b""
        self.total = 0
        self.overlong = False
        self.raw = bytearray()          # the current line; None once it passed RAW_LINE_LIMIT
        self.last = b""

    def feed(self, chunk):
        # A token can cross any read boundary. Keep complete bounded lines, and suppress
        # an overlong line in full rather than leak a token split at the buffer limit.
        while chunk:
            newline = chunk.find(b"\n")
            end = newline + 1 if newline >= 0 else len(chunk)
            part, chunk = chunk[:end], chunk[end:]
            if self.keep_raw and self.raw is not None:
                if len(self.raw) + len(part) > RAW_LINE_LIMIT:
                    self.raw = None
                else:
                    self.raw += part
            if not self.overlong:
                if len(self.pending) + len(part) > LINE_LIMIT:
                    self.pending = b""
                    self.overlong = True
                    self._emit(OVERLONG_PLACEHOLDER + b"\n")
                else:
                    self.pending += part
            if newline >= 0:
                if not self.overlong:
                    self._emit(self.pending)
                self._raw_line_done()
                self.pending = b""
                self.overlong = False

    def _raw_line_done(self):
        if not self.keep_raw:
            return
        raw, self.raw = self.raw, bytearray()
        if raw is None:
            self.last = b""                     # too long to keep: the last line is unknown
        elif raw.strip():
            self.last = bytes(raw)
            if self.overlong and self.on_overlong:
                self.on_overlong(redact(self.last))

    def last_line(self):
        return redact(self.last) if self.last else b""

    def _emit(self, data):
        data = redact(data)
        self.fh.write(data)
        self.fh.flush()
        self.total += len(data)
        self.tail = (self.tail + data)[-TAIL_BYTES:]
        if self.observer:
            self.observer(data)

    def close(self):
        if self.pending:
            self._emit(self.pending)
            self.pending = b""
        if self.raw is None or self.raw:
            self._raw_line_done()
        self.fh.close()


class ProcResult:
    def __init__(self, status, returncode, stdout_tail, stderr_tail, seconds, identity, error="",
                 stdout_last=b"", cleanup=None):
        self.status = status                  # exited | timed-out | not-started
        self.returncode = returncode
        self.stdout_tail, self.stderr_tail = stdout_tail, stderr_tail
        self.seconds, self.identity, self.error = seconds, identity, error
        self.stdout_last = stdout_last        # the last non-blank stdout line whole, with `last_line`
        self.cleanup = cleanup


# Captured at import: a task may edit this checkout before the next invocation.
_SUPERVISOR_SOURCE = r'''
import json
import os
import signal
import subprocess
import sys
import time


def _supervise(release_fd, result_fd, argv):
    """Outlive the CLI and its writers without importing the runner checkout."""
    linux = sys.platform.startswith("linux")
    if linux:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0):       # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "cannot track detached descendants")

    def processes():
        if linux:
            rows = []
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open("/proc/" + name + "/stat") as fh:
                        fields = fh.read().rpartition(")")[2].split()
                    rows.append((int(name), int(fields[1]), int(fields[2]), fields[0]))
                except (OSError, ValueError, IndexError):
                    continue
            return rows
        try:
            # The inspection process must not appear as a surviving group member itself.
            found = subprocess.run(["ps", "-e", "-o", "pid=,ppid=,pgid=,stat="],
                                   capture_output=True, text=True, start_new_session=True)
        except OSError as exc:
            found = subprocess.CompletedProcess([], 1, "", str(exc))
        if found.returncode and sys.platform == "darwin":
            import ctypes
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            size = 256
            while True:
                buf = (ctypes.c_int * size)()
                got = libc.proc_listpids(2, os.getpgrp(), buf, ctypes.sizeof(buf))
                if got < 0 or (got == 0 and ctypes.get_errno()):
                    raise OSError(ctypes.get_errno(), "cannot inspect invocation")
                if got < ctypes.sizeof(buf):
                    return [(pid, 0, os.getpgrp(), "R") for pid in list(buf)[:got // 4]
                            if pid > 0 and leader_identity(pid, live=True)]
                size *= 2
        if found.returncode:
            raise RuntimeError("cannot inspect invocation: " + found.stderr.strip())
        return [(int(r[0]), int(r[1]), int(r[2]), r[3])
                for line in found.stdout.splitlines() if len(r := line.split()) == 4]

    def members():
        rows = processes()
        descendants = {os.getpid()}
        if linux:
            while True:
                more = {pid for pid, parent, _group, _state in rows if parent in descendants}
                if more <= descendants:
                    break
                descendants.update(more)
        return [pid for pid, _parent, group, state in rows
                if pid != os.getpid() and state[0] not in ("Z", "X")
                and (group == os.getpgrp() or pid in descendants)]

    def leader_identity(pid, live=False):
        if linux:
            with open(f"/proc/{pid}/stat") as fh:
                ticks = int(fh.read().rpartition(")")[2].split()[19])
            with open("/proc/sys/kernel/random/boot_id") as fh:
                boot = fh.read().strip()
            return {"pid": pid, "pgid": os.getpgrp(), "start_ticks": ticks, "boot_id": boot}
        if sys.platform == "darwin":
            import ctypes
            import struct
            libc = ctypes.CDLL("libc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(136)
            got = libc.proc_pidinfo(ctypes.c_int(pid), 3, ctypes.c_uint64(0), buf, 136)
            if got == 136:
                if live and struct.unpack_from("I", buf.raw, 4)[0] == 5:
                    return None
                sec, usec = struct.unpack_from("QQ", buf.raw, 120)
                boot, size = ctypes.create_string_buffer(64), ctypes.c_size_t(64)
                libc.sysctlbyname(b"kern.bootsessionuuid", boot, ctypes.byref(size), None, 0)
                return {"pid": pid, "pgid": os.getpgrp(), "start_ticks": sec * 1000000 + usec,
                        "boot_id": boot.value.decode("ascii", "replace")}
            return None                               # a fast CLI may already have exited
        # Older platforms retain their ps start identity.
        found = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                               capture_output=True, text=True)
        return {"pid": pid, "pgid": os.getpgrp(), "lstart": found.stdout.strip()}

    listening = True
    def report(value):
        nonlocal listening
        if listening:
            try:
                os.write(result_fd, json.dumps(value).encode() + b"\n")
            except BrokenPipeError:
                listening = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)
    token = os.read(release_fd, 1)
    if token != b"G":
        os.close(release_fd)
        os.close(result_fd)
        return
    go_read, go_write = os.pipe()
    error_read, error_write = os.pipe()
    try:
        child = os.fork()
    except OSError as exc:
        for fd in (go_read, go_write, error_read, error_write, release_fd):
            os.close(fd)
        result = {"status": "not-started", "returncode": None, "error": str(exc)}
    else:
        if child == 0:
            os.close(go_write)
            os.close(error_read)
            os.close(result_fd)
            os.close(release_fd)
            try:
                if os.read(go_read, 1) != b"G":
                    os._exit(127)
                os.close(go_read)
                for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                    if hasattr(signal, name):
                        signal.signal(getattr(signal, name), signal.SIG_DFL)
                os.execvp(argv[0], argv)                # error_write closes on successful exec
            except OSError as exc:
                os.write(error_write, (str(exc) + ": " + repr(argv[0])).encode()[:4096])
                os._exit(127)
        os.close(go_read)
        os.close(error_write)
        # Hold even an instant command until its start identity has been captured.
        leader = leader_identity(child)
        if not leader:
            raise RuntimeError("cannot identify the waiting CLI leader")
        report({"leader": leader})
        token = os.read(release_fd, 1)                  # A acknowledges the durable leader identity
        if token == b"A":
            token = os.read(release_fd, 1)
            if token in (b"G", b""):                   # abrupt runner death leaves a recorded orphan
                os.write(go_write, b"G")
        os.close(release_fd)
        os.close(go_write)
        error = os.read(error_read, 4096).decode("utf-8", errors="replace")
        os.close(error_read)
        _pid, status = os.waitpid(child, 0)
        result = ({"status": "not-started", "returncode": None, "error": error} if error else
                  {"status": "exited", "returncode": os.waitstatus_to_exitcode(status)})
    report(result)
    os.close(result_fd)
    while members():
        time.sleep(0.05 if listening else 1.0)
'''
_SUPERVISOR_SOURCE += "\n_supervise(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:])\n"


def run_process(argv, *, cwd, env, stdin_data=b"", stdout_path, stderr_path=None,
                timeout_s=None, on_start=None, grace_s=GRACE_S, on_stdout=None, last_line=False,
                on_stdout_overlong=None):
    """Run `argv` to the end or to the deadline. `stderr_path=None` sends both streams to one log.
    `on_start(identity)` must durably record the waiting supervisor before task code is released.
    It receives independent snapshots and is called again with the CLI identity after spawn.
    It may return a callback to run once after the leader identity is acknowledged. A caught
    interruption cancels the waiting CLI; abrupt runner death leaves a recorded orphan.
    `last_line` and `on_stdout_overlong` are for parsing a stdout line the log withholds (_Sink)."""
    started = time.monotonic()
    release_read, release_write = os.pipe()
    result_read, result_write = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-c", _SUPERVISOR_SOURCE,
                                 str(release_read), str(result_write), *argv],
                                cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if stderr_path else subprocess.STDOUT,
                                pass_fds=(release_read, result_write), start_new_session=True)
    except OSError as exc:
        os.close(release_write)
        os.close(result_read)
        return ProcResult("not-started", None, b"", b"", 0.0, None, str(exc))
    finally:
        os.close(release_read)
        os.close(result_write)
    identity, after_release = None, None
    out, err = None, None
    sel = selectors.DefaultSelector()
    result_pipe = os.fdopen(result_read, "rb", buffering=0)
    result_data, outcome = b"", None
    timed_out, interrupted, raised = False, False, None
    cleanup_error = ""
    deadline = started + timeout_s if timeout_s else None
    try:
        out = _Sink(stdout_path, on_stdout, last_line, on_stdout_overlong)
        err = _Sink(stderr_path) if stderr_path else None
        identity = record.process_identity(proc.pid)
        if identity and (identity.get("start_ticks") or identity.get("lstart")):
            identity["group"] = {"pgid": proc.pid, "supervisor": dict(identity)}
            if on_start:
                after_release = on_start(copy.deepcopy(identity))
            try:
                os.write(release_write, b"G")
            except BrokenPipeError:
                pass                                  # drain the startup error below
        elif proc.poll() is None:
            raise record.RecordError("cannot identify the waiting invocation supervisor")
        os.set_blocking(result_read, False)
        sel.register(result_pipe, selectors.EVENT_READ, "result")
        os.set_blocking(proc.stdout.fileno(), False)
        sel.register(proc.stdout, selectors.EVENT_READ, out)
        if err:
            os.set_blocking(proc.stderr.fileno(), False)
            sel.register(proc.stderr, selectors.EVENT_READ, err)
        os.set_blocking(proc.stdin.fileno(), False)
        todo = memoryview(stdin_data)
        if len(todo):
            sel.register(proc.stdin, selectors.EVENT_WRITE, None)
        else:
            proc.stdin.close()
        exited_at = None
        while any(k.data is not None for k in sel.get_map().values()):
            if outcome is not None or proc.poll() is not None:
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at > 0.5:
                    break
            wait = 0.2
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out = True
                    break
                wait = min(wait, left)
            for key, _mask in sel.select(wait):
                if key.fileobj is result_pipe:
                    chunk = os.read(result_read, 65536)
                    result_data += chunk
                    while b"\n" in result_data:
                        line, result_data = result_data.split(b"\n", 1)
                        message = json.loads(line)
                        if "leader" in message:
                            leader = message["leader"]
                            now = record.process_identity(leader["pid"])
                            if now and record.is_alive(leader):
                                leader = now
                            identity["group"]["leader"] = leader
                            members = [record.process_identity(pid)
                                       for pid in record.invocation_pids(identity)]
                            identity["group"]["members"] = [member for member in members if member]
                            if on_start:
                                on_start(copy.deepcopy(identity))
                            try:
                                os.write(release_write, b"A")
                            except BrokenPipeError:
                                pass
                            else:
                                if callable(after_release):
                                    after_release()
                                try:
                                    os.write(release_write, b"G")
                                except BrokenPipeError:
                                    pass
                            os.close(release_write)
                            release_write = None
                        else:
                            outcome = message
                    if not chunk:
                        sel.unregister(result_pipe)
                    continue
                if key.fileobj is proc.stdin:
                    try:
                        sent = os.write(proc.stdin.fileno(), todo[:65536])
                        todo = todo[sent:]
                    except BrokenPipeError:
                        todo = todo[len(todo):]
                    except BlockingIOError:
                        pass
                    if not len(todo):
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if chunk:
                    key.data.feed(chunk)
                else:
                    sel.unregister(key.fileobj)
        if not timed_out and outcome is None:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=left)
            except subprocess.TimeoutExpired:
                timed_out = True
    except BaseException as exc:
        interrupted, raised = True, exc
        raise
    finally:
        if release_write is not None:
            try:
                os.write(release_write, b"C")           # cancellation differs from an abrupt EOF
            except BrokenPipeError:
                pass
            os.close(release_write)
        try:
            if identity and identity.get("group"):
                stop_group(proc, identity, grace_s, polite=timed_out or interrupted)
            else:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
        except (record.RecordError, OSError, subprocess.TimeoutExpired) as exc:
            cleanup_error = str(exc)
            if outcome is None and not interrupted:
                raise
            if isinstance(raised, Cancelled):
                # Its group may outlive the call: the caller keeps it for `resume` (PROC-25).
                raised.cleanup = {"status": "open", "error": cleanup_error, "group": identity}
        finally:
            for key in list(sel.get_map().values()):
                if key.data is not None and key.fileobj is not result_pipe:
                    try:
                        rest = os.read(key.fileobj.fileno(), 1 << 20)
                        if rest:
                            key.data.feed(rest)
                    except (BlockingIOError, OSError):
                        pass
            sel.close()
            result_pipe.close()
            if cleanup_error and (err or out):
                (err or out).feed(("\n[invocation cleanup: " + cleanup_error + "]\n").encode())
            if out:
                out.close()
            if err:
                err.close()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
    seconds = time.monotonic() - started
    if outcome is None:
        detail = (err or out).tail.decode("utf-8", errors="replace")
        ran = bool(identity and identity.get("group", {}).get("leader"))
        outcome = {"status": "exited" if ran else "not-started",
                   "returncode": proc.returncode if ran else None,
                   "error": "supervisor exited without a result: " + detail}
    error = "; ".join(filter(None, (outcome.get("error", ""), cleanup_error)))
    cleanup = {"status": "open", "error": cleanup_error, "group": identity} if cleanup_error else None
    return ProcResult("timed-out" if timed_out else outcome["status"], outcome["returncode"], out.tail,
                      err.tail if err else b"", seconds, identity, error=error,
                      stdout_last=out.last_line(), cleanup=cleanup)


def stop_group(proc, identity, grace_s, polite=True):
    """Stop verified group members and tracked descendants, including a dead supervisor's group."""
    stopped = record.stop_process_group(identity, grace_s if polite else 2.0, polite=polite)
    proc.wait(timeout=2)
    if not stopped:
        raise record.RecordError(f"could not stop invocation group {identity['pgid']}")
