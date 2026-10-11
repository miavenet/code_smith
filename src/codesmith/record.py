"""The run directory: create it, keep its state durably, record intents and outcomes, reconcile
after a crash, and regenerate the files that are written for readers (04, 05 Crash recovery).

`state.json` is the only thing read back. STATUS.md, STATUS.html and index.json are derived:
deleting them loses nothing. Write-once is enforced by hashes (`integrity.json`), never by file modes.
"""

import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
import uuid

from . import __version__, budgets, findings, gitops, invariants, schema

from .status import (
    HEARTBEAT_S, OUTCOME_KEPT, PROTOCOL_BLOCK, _parse_at, counted, recent_events, window_phrases,
    spend_by_model, run_status_model, markdown_status, render_run_status,
    record_links, html_status, review_churn, rulings_phrase,
    churn_lines, owner_ruling_lines, render_task_status, render_index
)

RUNS_DIR = ".runs"
RUNS_DIR_ENV = "CODE_SMITH_RUNS_DIR"         # set by `runner --runs-dir DIR`, or exported by the owner
LOCK_FILE = "lock"
STATE_VERSION = 1
FINISHED_STATUSES = ("done",)
TASK_TERMINAL = ("accepted", "objected", "blocked", "failed", "skipped")
# Decision-bearing files of a finished directory (04, rule 4). state.json and findings.json are
# covered from the moment they exist, as is inputs.json.
DECISION_FILES = ("result.json", "verdict.json", "verification.json", "outputs.json",
                  "decision.json", "inputs.json")


class RecordError(Exception):
    pass


class LockHeld(RecordError):
    def __init__(self, holder, path):
        self.holder, self.path = holder, path
        who = (f"run {holder.get('run_id')} (pid {(holder.get('process') or {}).get('pid')})"
               if holder else "an unreadable lock")
        if holder and holder.get("runs_dir"):            # the work tree's lock (TreeLock)
            who += f", recording in {holder['runs_dir']}"
        super().__init__(f"another runner holds this repository: {who}. Lock file: {path}")


class ReconcileError(RecordError):
    """The repository or the record is not what the state expects. Stop; never guess."""


class CleanupOpen(ReconcileError):
    """An invocation's cleanup is still open and `resume` did not close it (PROC-23)."""
    def __init__(self, run, why=""):
        super().__init__((why + ": " if why else "") + cleanup_refusal(run))


class OrphanAlive(ReconcileError):
    def __init__(self, identity, task, invocation=""):
        self.identity, self.task = identity, task
        pids = ", ".join(map(str, invocation_pids(identity)))
        super().__init__(f"the child process of task '{task}' from the previous runner is still running "
                         f"(invocation {invocation or task}; pids {pids}). Refusing to start a second one beside it; "
                         "`resume --stop-orphans` stops it first")


def _no_crash(point):
    return None


# -- durable files ------------------------------------------------------------------------------

def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


TEMP_TRIES = 16


def _temp_beside(path):
    """(fd, name) of a new file in `path`'s directory, created exclusively (`O_CREAT|O_EXCL`,
    which neither opens an existing file nor follows a link there) under a name no project file
    has: `.<name>.<random>.tmp`. Never `path + ".tmp"`, which may be a deliverable or a link
    (REC-63)."""
    directory, base = os.path.split(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(TEMP_TRIES):
        tmp = os.path.join(directory, f".{base}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            return os.open(tmp, flags, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no unused temporary name", path)


def write_durable(path, data, crash=_no_crash):
    """Temporary file, flush, sync, rename, sync the directory. A crash leaves the old file intact.
    The temporary file is new and uniquely named (`_temp_beside`); a failure before the rename
    removes it, and one a kill leaves is never reused."""
    path = os.fspath(path)
    fd, tmp = _temp_beside(path)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        crash("state:before-rename")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(os.path.dirname(path))


def dump_json(obj):
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def read_json(path):
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def _read_bytes(path):
    """The bytes of a file, or None when there is no file to read."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


# -- process identity ----------------------------------------------------------------------

def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        pass
    if sys.platform == "darwin":
        return _darwin_boot_session()
    return ""


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        libc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p,
                                      ctypes.c_int]
        _LIBC = libc
    return _LIBC


def _darwin_boot_session():
    """macOS names each boot with a UUID: sysctl kern.bootsessionuuid."""
    import ctypes
    try:
        buf, size = ctypes.create_string_buffer(64), ctypes.c_size_t(64)
        if _libc().sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, 0):
            return ""
        return buf.value.decode("ascii", "replace")
    except (OSError, AttributeError):
        return ""


def _darwin_start(pid):
    """(status, start time in microseconds) from libproc's struct proc_bsdinfo, or None. The
    status is at offset 4 (5 is a zombie); the start time is the two uint64 at 120 and 128."""
    import ctypes
    import struct
    buf = ctypes.create_string_buffer(136)
    try:
        ctypes.set_errno(0)
        got = _libc().proc_pidinfo(ctypes.c_int(pid), 3, ctypes.c_uint64(0), buf, 136)  # PIDTBSDINFO
    except (OSError, AttributeError):
        return None
    if got != 136:
        return None
    status = struct.unpack_from("I", buf.raw, 4)[0]
    sec, usec = struct.unpack_from("QQ", buf.raw, 120)
    return status, sec * 1_000_000 + usec


def _proc_stat(pid):
    """(state, start ticks) from /proc/<pid>/stat. The command name may hold spaces and
    parentheses, so the fields are read after the last ')'. Field 3 is the state, 22 the start."""
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
        rest = fh.read().rpartition(")")[2].split()
    return rest[0], int(rest[19])


def process_identity(pid):
    """What identifies a process beyond its pid, which is reused: start ticks and the boot id on
    Linux; on macOS the start time in microseconds and the boot session; elsewhere the weaker
    `ps` start time. None if there is no such live process."""
    if os.path.isdir("/proc/self"):
        try:
            state, ticks = _proc_stat(pid)
        except (OSError, ValueError, IndexError):
            return None
        if state in ("Z", "X"):
            return None
        ident = {"pid": pid, "start_ticks": ticks, "boot_id": boot_id()}
    elif sys.platform == "darwin" and _darwin_start(os.getpid()):
        import ctypes
        found = _darwin_start(pid)
        if found is None and ctypes.get_errno() == errno.ESRCH:
            return None
        if found is None and _pid_exists(pid):
            # libproc refuses to describe a process of another user (EPERM); `ps` still can.
            lstart = _ps_lstart(pid)
            if not lstart:
                return None
            ident = {"pid": pid, "lstart": lstart}
        elif found is None or found[0] == 5:                 # SZOMB
            return None
        else:
            ident = {"pid": pid, "start_ticks": found[1], "boot_id": boot_id()}
    else:
        lstart = _ps_lstart(pid)
        if not lstart:
            return None
        ident = {"pid": pid, "lstart": lstart}
    try:
        ident["pgid"] = os.getpgid(pid)
    except OSError:
        return None
    return ident


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                              # there, but another user's
        return True
    return True


def is_alive(identity):
    """Is the recorded process (or supervised invocation group) still live? Exclude reused pids."""
    if identity and identity.get("group"):
        return bool(invocation_pids(identity))
    if not identity or "pid" not in identity:
        return False
    if "start_ticks" not in identity:                        # recorded by `ps` (or an older runner)
        return bool(identity.get("lstart")) and _ps_lstart(identity["pid"]) == identity["lstart"]
    now = process_identity(identity["pid"])
    if now is None:
        return False
    return all(now.get(k) == identity.get(k) for k in ("start_ticks", "boot_id"))


def group_pids(pgid):
    """Live members only: zombies cannot write. Inspection failure must never mean empty."""
    if os.path.isdir("/proc/self"):
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    elif sys.platform == "darwin":
        import ctypes
        libc = _libc()
        # PROC_PGRP_ONLY; grow if the group filled the buffer during enumeration.
        size = 256
        while True:
            buf = (ctypes.c_int * size)()
            ctypes.set_errno(0)
            got = libc.proc_listpids(2, pgid, buf, ctypes.sizeof(buf))
            if got < 0 or (got == 0 and ctypes.get_errno()):
                raise RecordError(f"cannot inspect process group {pgid}")
            if got < ctypes.sizeof(buf):
                pids = list(buf)[:got // ctypes.sizeof(ctypes.c_int)]
                break
            size *= 2
    else:
        res = subprocess.run(["ps", "-e", "-o", "pid=,pgid="], capture_output=True, text=True)
        if res.returncode:
            raise RecordError(f"cannot inspect process group {pgid}: {res.stderr.strip()}")
        pids = [int(row[0]) for line in res.stdout.splitlines()
                if len(row := line.split()) == 2 and int(row[1]) == pgid]
    members = []
    for pid in pids:
        try:
            if pid > 0 and os.getpgid(pid) == pgid and process_identity(pid):
                members.append(pid)
        except ProcessLookupError:
            pass
    return sorted(members)


def descendant_pids(parent):
    """Linux ancestry includes adopted children that left the invocation's process group."""
    if not os.path.isdir("/proc/self"):
        return []
    rows = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as fh:
                fields = fh.read().rpartition(")")[2].split()
            rows.append((int(name), int(fields[1]), fields[0]))
        except (OSError, ValueError, IndexError):
            continue
    found = {parent}
    while True:
        more = {pid for pid, ppid, _state in rows if ppid in found}
        if more <= found:
            break
        found.update(more)
    return sorted(pid for pid, _ppid, state in rows
                  if pid != parent and pid in found and state not in ("Z", "X"))


def invocation_pids(identity):
    """New invocations retain a supervisor identity; old records remain leader-only."""
    if not identity:
        return []
    group = identity.get("group")
    if not group:
        return [identity["pid"]] if is_alive(identity) else []
    anchor = group["supervisor"]
    if anchor.get("boot_id") and anchor["boot_id"] != boot_id():
        return []
    known = {member["pid"] for member in group.get("members", []) if is_alive(member)}
    if not is_alive(anchor) and process_identity(anchor["pid"]) is not None:
        return sorted(known)                           # the numeric group may now be unrelated
    members = set(group_pids(group["pgid"]))
    if is_alive(anchor):
        members.update(descendant_pids(anchor["pid"]))
    return sorted(members | known)


def _ps_lstart(pid):
    res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def stop_process_group(identity, grace_s=5.0, polite=True):
    """Stop verified members, retaining their start identities across supervisor death."""
    group = identity.get("group")
    pgid = group["pgid"] if group else identity.get("pgid") or identity["pid"]
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL) if polite else (signal.SIGKILL,)
    for sig in signals:
        if not is_alive(identity):
            return True
        if group:
            anchor = group["supervisor"]
            anchored = pgid == anchor["pid"] and is_alive(anchor)
            leader = group.get("leader")
            witnesses = group.get("members", []) + ([leader] if leader else [])
            live = [member for member in witnesses if is_alive(member)]
            if not anchored:
                in_group = any((process_identity(member["pid"]) or {}).get("pgid") == pgid
                               for member in live)
                if not live or (not in_group and group_pids(pgid)):
                    return False                      # a detached witness cannot vouch for a group
            if not anchored and (pgid != anchor["pid"] or process_identity(anchor["pid"])):
                return False
            # A surviving recorded member prevents group-id reuse. Snapshot all members before
            # signalling; their start identities survive supervisor death during escalation.
            members = [process_identity(pid) for pid in invocation_pids(identity)]
            group["members"] = [member for member in members if member]
            if anchored:
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass
            for member in group["members"]:
                if (not anchored or member.get("pgid") != pgid) and is_alive(member):
                    try:
                        os.kill(member["pid"], sig)
                    except ProcessLookupError:
                        pass
        else:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return True
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not is_alive(identity):
                return True
            time.sleep(0.05)
    return not is_alive(identity)


# -- the repository lock ------------------------------------------------------------------------

class Lock:
    """One run at a time per repository. The lock file holds the run id and the identity of the
    process that took it, for readers; what excludes a second runner is an flock on that file,
    held open for as long as the lock is. The kernel drops it when the process dies, so a lock
    whose process is gone is stale and is taken over, and two runners that find the same stale
    lock cannot both take it. A file that names a live process is still respected (a runner
    without the flock may hold it). Holding the flock is what owns the lock: every
    runner of this format writes the file only under it, so a file that is empty or unreadable
    while we hold its flock is nobody's write in progress (a takeover killed half-way by an older
    runner) and is taken over like a stale one. A takeover never writes into the locked file: it
    links a fresh, already locked and written file over it, so no kill leaves a torn holder."""

    def __init__(self, runs_dir, tree=None, top=None):
        home = lock_home(top) if top else ""
        # With `top` (the repository the record belongs to), the lock file is in that repository's
        # git directory, keyed by the runs directory, where `git clean -fdx` and `rm -rf .runs`
        # cannot remove it while it is held (W-02). The file in the runs directory is still taken
        # beside it (`legacy`): an older runner and an older reader look only there, and a runner
        # is refused if either is held.
        self.path = run_lock_path(home, runs_dir) if home else os.path.join(runs_dir, LOCK_FILE)
        self.legacy = Lock(runs_dir) if home else None
        self.held = False
        self._fd = None
        # With `tree` (the work tree's lock file, `tree_lock_path`), a runner that changes a run or
        # dispatches work also excludes every other runner on the same checkout, whatever runs
        # directory that one uses. The runs directory's lock comes first, so its holder and its
        # messages are what a second runner of the same record sees, as before.
        self.tree = TreeLock(tree, runs_dir) if tree else None

    def acquire(self, run_id):
        if self.legacy:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._acquire(run_id)
        try:
            if self.legacy and os.path.isdir(os.path.dirname(self.legacy.path)):
                self.legacy._acquire(run_id)
            if self.tree:
                self.tree.acquire(run_id)
        except BaseException:
            self.release()
            raise
        return self

    def holder(self):
        try:
            return read_json(self.path)
        except FileNotFoundError:
            return self.legacy.holder() if self.legacy else None
        except (OSError, ValueError):
            return {}

    def _acquire(self, run_id):
        for _ in range(3):
            if self._create(run_id):
                return self
            try:
                fd = os.open(self.path, os.O_RDWR)
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:                              # a live runner holds it
                os.close(fd)
                raise LockHeld(self.holder() or {}, self.path) from None
            try:
                # The file locked must still be the one at the path: a releasing runner unlinks it.
                if not os.path.samestat(os.fstat(fd), os.stat(self.path)):
                    os.close(fd)
                    continue
            except FileNotFoundError:
                os.close(fd)
                continue
            holder = self.holder()
            if holder and is_alive(holder.get("process")):  # a runner without the flock
                os.close(fd)
                raise LockHeld(holder, self.path)
            # Stale (its process is gone), or empty or unreadable under our flock: ours.
            try:
                self._create(run_id, replace=True)
            finally:
                os.close(fd)                             # the replaced file's flock
            return self
        raise LockHeld(self.holder() or {}, self.path)

    def _create(self, run_id, replace=False):
        """Take a lock where there is none, or, with `replace`, put ours in place of a stale one
        whose flock the caller holds. The file is locked and written under a name of its own and
        only then linked (or renamed) to the lock path, so no runner ever finds the lock file
        before its flock is held and its holder written. False if a lock file is already there."""
        tmp = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, dump_json({"run_id": run_id, "process": process_identity(os.getpid())}))
            os.fsync(fd)
            if replace:
                os.replace(tmp, self.path)
            else:
                os.link(tmp, self.path)
        except FileExistsError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:                    # renamed into place
                pass
        self._fd, self.held = fd, True
        return True

    def release(self):
        """Unlink the lock file only if it is still the one this lock holds, then let go."""
        if self.tree:
            self.tree.release()
        if self.legacy:
            self.legacy.release()
        if self.held:
            try:
                if os.path.samestat(os.fstat(self._fd), os.stat(self.path)):
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
            os.close(self._fd)
            self._fd, self.held = None, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


TREE_LOCK_FILE = "code-smith.lock"
LOCK_HOME = "code-smith"                         # the run locks, in the repository's common git directory
_lock_homes = {}


def lock_home(top):
    """`code-smith/` in the common git directory of the repository at `top` (W-02): shared by its
    linked work trees, so two of them recording into one runs directory exclude each other, and
    out of reach of `git clean`. '' when `top` is not in a git repository."""
    if top not in _lock_homes:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            res = subprocess.run(["git", "-c", f"safe.directory={top}", "-C", top, "rev-parse",
                                  "--git-common-dir"], capture_output=True, text=True, env=env)
            found = res.stdout.strip() if res.returncode == 0 else ""
        except OSError:
            found = ""
        _lock_homes[top] = os.path.normpath(os.path.join(top, found, LOCK_HOME)) if found else ""
    return _lock_homes[top]


def run_lock_path(home, runs_dir):
    """One lock file per runs directory, named by a digest of its real path, which stays the same
    when the directory itself has been removed."""
    digest = hashlib.sha256(os.path.realpath(runs_dir).encode()).hexdigest()[:16]
    return os.path.join(home, f"run-{digest}.lock")


def tree_lock_path(git):
    """The work tree's lock file, in the work tree's own git directory: one per checkout (a linked
    worktree has its own), and out of reach of `git clean` and of the runs directory's location."""
    return git.git_path(TREE_LOCK_FILE)


def tree_lock_holder(git):
    """The live runner holding the work tree's lock, or None. Read without taking the flock: a
    holder writes the file under it and empties it on release, so a file naming a live process
    is a lock held (K9: named even when the runs directory is gone)."""
    try:
        holder = read_json(tree_lock_path(git))
    except (OSError, ValueError, gitops.GitError):
        return None
    return holder if isinstance(holder, dict) and is_alive(holder.get("process")) else None


class TreeLock:
    """One runner per work tree, whatever its runs directory (W-01). Only the flock excludes;
    the file's content names the holder for the message and is written under the flock. The file
    is never removed, so two runners can never lock two different inodes at one path; the kernel
    drops the flock when its process dies, so there is nothing stale to take over."""

    def __init__(self, path, runs_dir):
        self.path, self.runs_dir = path, runs_dir
        self._fd = None

    def acquire(self, run_id):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            try:
                holder = read_json(self.path)
            except (OSError, ValueError):
                holder = {}
            raise LockHeld(holder, self.path) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, dump_json({"run_id": run_id, "process": process_identity(os.getpid()),
                                 "runs_dir": self.runs_dir}), 0)
        self._fd = fd
        return self

    def release(self):
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            os.close(self._fd)
            self._fd = None


# -- .runs/-------------------------------------------------------------------------------------

README = """\
# .runs — the record of code_smith runs

This directory is written by code_smith. It is the record of how work in this repository came
to be: every run, every task, every attempt, every prompt, every answer, every review finding. The
deliverables themselves are **not** here; they are in the repository, on the run's git branch.

By default this directory is `.runs/` at the top of the repository. `runner --runs-dir DIR ...`,
the environment variable `CODE_SMITH_RUNS_DIR`, or `runs_dir` under the workflow's `[defaults]`
puts it elsewhere (relative paths are relative to the top of the repository). A location from the
workflow is remembered by `start` in the repository's git config (`codesmith.runsdir`); one from
the option or the variable is not, and every later command on these runs then needs it again.

Nothing in here is tracked by git (`.gitignore` holds `*`). Committing a run's record is the
owner's choice. A finished run can be deleted with `rm -rf`; `runner prune` then removes the git
refs it pinned under `refs/code-smith/`.

## How to read a run, cold

Open `<workflow>/<run>/STATUS.md`. It says, in words, what the run did, what it needs and what to
type next. Every directory also has an `index.json` that says what each file in it is. Start there
rather than guessing from file names.

    .runs/
      README.md                     this file
      lock                          present while a runner is working in this repository (its twin in
                                    .git/code-smith/ is the lock that counts)
      qualification-cache.json      what `doctor` established about each agent profile
      <workflow>/
        latest                      text file: the directory name of the newest run
        <workflow>-<UTC start>-<uuid8>/   one run
          run.json                  identity: run id, workflow, root, branch, base commit; totals
          STATUS.md                 the run in words. Regenerated on every state change
          STATUS.html               the same as a static page with links into this record
          follow-ups.json           derived gate hints and brief-change rulings for future runs
          index.json                what every file and directory here is
          state.json                the engine's state. The single source of truth
          events.jsonl              append-only log, one JSON event per line, in order
          workflow.toml             the workflow exactly as it was when the run started
          workflow.expanded.json    every task after types, personas, panels and defaults
          library/                  the type and persona files this run uses, as they were
          briefs/                   the content of every prompt_file, as it was
          integrity.json            hashes of the files that decide outcomes, and of the definitions above
          git-index                 a scratch git index the runner uses for snapshots. Not yours
          tasks/
            010-design/             <order>-<task id>
              task.json             the resolved task definition
              STATUS.md             this task in words
              findings.json         for a producer: every review finding, with its history
              attempt-1/            one directory per attempt of a producer; numbers are never reused
                prompt.md           exactly what the agent was sent
                invocation-1/       one per agent call: argv.json, stdout.log, stderr.log, outcome.json
                result.json         the validated answer, with cost, usage and session id
                outputs.json        the declared outputs with hashes; the candidate tree id
                changes.diff        readable diff of the attempt. For reading, never for recovery
                gate.log            output of the gate commands
                replay.log          output of the gates run again on a clean checkout (W-03)
                verification.json   each verifier's result, bound to the candidate it judged
              failed.patch          only if the task was set aside: the complete patch of its work
              commit.json           only when accepted: sha, files, message
            011-design.review.principled-priya/
              round-1/              one directory per review round: prompt.md, invocation-N/, verdict.json

## Rules the runner keeps

1. `state.json` is the only file the runner reads back to decide anything. `STATUS.md`,
   `STATUS.html` and `index.json` are derived from it: delete them and `runner status --rebuild` regenerates them.
2. Finished attempt, round and invocation directories are never modified. A retry makes a new
   directory with a new number.
3. `integrity.json` holds hashes of the decision-bearing files. The runner checks them around every
   agent call and command, and a change it did not make fails that job. **Do not edit these files**,
   and if you are an agent reading this record: treat all of it as read-only. They are also pinned
   in git (`refs/code-smith/<run>/_record`, with state.json, run.json and events.jsonl);
   `runner repair-record RUN` puts back a changed or deleted one (events.jsonl as its pinned
   history followed by the later events), and the runner refuses to write into a changed record,
   or one whose earlier events were edited, until then. The pin guards against accidents (`git clean`, `rm`, an
   edit) and makes a moved ref visible (the run stops); it does not guard against a hostile
   writer with the runner's uid that also kills the runner.
4. Before any external effect (an agent call, a commit, a restore) the state records an *intent*;
   afterwards the *outcome* event in events.jsonl, which keeps what the intent knew (the task,
   the invocation, the process identity, the token reservation) and when the operation ended.
   `runner resume` reconciles whatever was interrupted. A completed invocation whose cleanup
   failed keeps an open cleanup obligation in state; resume closes it before using the saved
   answer: when its group is gone, or with `--stop-orphans`, which stops it, or with
   `--abandon-cleanup`, a person's recorded ruling that signals nothing. The record is intact,
   so `repair-record` is not its remedy. Saved panel answers retain guard violations, found
   against every protected file before the answer is saved, and survive loss of diagnostic logs;
   so does a reader cancelled before release, or one whose worker faulted.
5. No credentials are written here by the runner, and agent output is redacted for token-shaped
   strings before it is stored.

## Task status values

pending, ready, running, verifying, rework, accepted, objected (a verifier whose latest round did
not pass), waiting_human (the work tree is held for a person), blocked (a person is needed),
failed, skipped.

## Exit codes of the runner

0: every task is accepted. 2: something failed, the budget ran out, or the workflow or environment
is wrong. 255: a person is needed.
"""


RUNS_DIR_CONFIG = "codesmith.runsdir"         # where `start` remembers a workflow's runs_dir


def resolve_runs_dir(top, where):
    """A runs directory as the owner wrote it, wherever they wrote it (`--runs-dir`,
    CODE_SMITH_RUNS_DIR, `[defaults] runs_dir`): `~` expanded, relative to the repository top."""
    return os.path.normpath(os.path.join(top, os.path.expanduser(where)))


def _git_config(top, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-c", f"safe.directory={top}", "-C", top, "config", "--local", *args],
                          capture_output=True, text=True, env=env)


def remembered_runs_dir(top):
    """The runs directory that the last `start` in this repository took from its workflow's
    `[defaults] runs_dir`, or ''."""
    try:
        res = _git_config(top, "--get", RUNS_DIR_CONFIG)
    except OSError:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def remember_runs_dir(top, path):
    """Record (or, with None, forget) where `start` put the record, in the repository's own
    git config, so that `status`, `resume` and the other commands on a RUN find it."""
    if path:
        _git_config(top, RUNS_DIR_CONFIG, path)
    elif remembered_runs_dir(top):
        _git_config(top, "--unset-all", RUNS_DIR_CONFIG)


def runs_dir_for(top):
    """Where the record of this repository's runs lives: the owner's override (`--runs-dir`, or
    CODE_SMITH_RUNS_DIR; a command on a workflow sets it from `[defaults] runs_dir`), else the
    location the last `start` remembered from its workflow, else `.runs/` at the top. Relative
    paths are relative to the top of the repository, whichever way they were given."""
    override = os.environ.get(RUNS_DIR_ENV, "")
    if override:
        return resolve_runs_dir(top, override)
    return remembered_runs_dir(top) or os.path.join(top, RUNS_DIR)


def ensure_runs_dir(top):
    """The runs directory (`.runs/` at the top of the repository by default) ignores itself and
    explains itself."""
    path = runs_dir_for(top)
    os.makedirs(path, exist_ok=True)
    dress_runs_dir(path)
    return path


def dress_runs_dir(path):
    """Write the runs directory's `.gitignore` and README where they are missing. Returns the
    names written. A save calls this too (W-02): an author who deletes the ignore file would
    otherwise put the whole record into the next snapshot as a change of its own."""
    written = []
    for name, text in ((".gitignore", "*\n"), ("README.md", README)):
        target = os.path.join(path, name)
        if not os.path.exists(target):
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            written.append(name)
    return written


def find_runs_dir(start):
    """The runs directory of the repository that holds `start`, or None."""
    top = gitops.find_toplevel(start)
    path = runs_dir_for(top) if top else ""
    return path if path and os.path.isdir(path) else None


def list_runs(runs_dir, workflow=None):
    """Every run directory, oldest first: a list of (workflow, name, path)."""
    found = []
    if not os.path.isdir(runs_dir):
        return found
    for wf in sorted(os.listdir(runs_dir)):
        wf_dir = os.path.join(runs_dir, wf)
        if not os.path.isdir(wf_dir) or (workflow and wf != workflow):
            continue
        for name in sorted(os.listdir(wf_dir)):
            path = os.path.join(wf_dir, name)
            if os.path.isfile(os.path.join(path, "run.json")):
                found.append((wf, name, path))
    return sorted(found, key=_run_order)


def _run_order(found):
    """Oldest first. The start time is in run.json; the directory name no longer begins with it."""
    wf, name, path = found
    try:
        started = read_json(os.path.join(path, "run.json")).get("started", "")
    except (OSError, ValueError):
        started = ""
    return (started, name, wf)


def resolve_run(runs_dir, ref="latest", unfinished_only=False):
    """RUN is a directory name, a UUID prefix, or `latest`."""
    runs = list_runs(runs_dir)
    if unfinished_only:
        runs = [r for r in runs if Run.load(r[2]).state["status"] not in FINISHED_STATUSES]
    if not runs:
        raise RecordError(f"no runs under {runs_dir}")
    if ref in (None, "", "latest"):
        return runs[-1][2]
    matches = [r for r in runs if r[1] == ref]
    if not matches:
        matches = [r for r in runs if read_json(os.path.join(r[2], "run.json"))["run_id"]
                   .startswith(ref.lower())]
    if not matches:
        raise RecordError(f"no run matches '{ref}'")
    if len(matches) > 1:
        raise RecordError(f"'{ref}' matches several runs: " + ", ".join(m[1] for m in matches))
    return matches[0][2]


# -- the pinned record (W-02) -------------------------------------------------------------------

def repair_hint(name):
    """The way back from a changed or removed record, as a stop reason names it."""
    return (f"`runner repair-record {name}` puts back the recorded copies from git, then "
            f"`runner resume {name}`")


def cleanup_hint(name):
    """The way on from an open cleanup (PROC-23), never `repair-record`: the record is intact."""
    return (f"`runner resume {name} --stop-orphans` stops the group and closes the cleanup; if "
            f"the stop keeps failing, stop the processes yourself, then `runner resume {name} "
            "--abandon-cleanup` records your ruling that they are gone")


def cleanup_refusal(run):
    """The refusal of a command that would write while a cleanup is open, or ''."""
    problems = run.cleanup_problems()
    if not problems:
        return ""
    return ("an invocation's cleanup is open: " + "; ".join(problems) + ". "
            + cleanup_hint(run.name))


# refs/code-smith/<run>/_record: the decision files as one tree. No task id begins with "_", so
# it never clashes with a task's refs (`<run>/<task>/base`).
RECORD_REF = "_record"
RUN_TOTALS = ("status", "spend", "seconds", "run_budget_usd", "run_budget_tokens")   # run.json's copied totals


class PinMoved(RecordError):
    """The pinned record's ref is not what this runner last made it, or holds another run's or a
    later state: something else moved it. The ref is left as it is and the run stops (K2)."""

    def __init__(self, ref, expected, found, why=""):
        self.ref, self.expected, self.found = ref, expected, found
        super().__init__(
            f"the pinned record {ref} was moved by something other than this runner (expected "
            f"{expected or 'no ref'}, found {found or 'no ref'}{'; ' + why if why else ''}). It was "
            f"left as it is. Find what moved it; if the move was yours, `git update-ref -d {ref}` "
            "lets `runner resume` pin the record afresh")


EVENTS = "events.jsonl"


class RecordPin:
    """The run's decision files kept as one git tree under `refs/code-smith/<run>/_record`, so
    `runner repair-record` can put back what an accident (`git clean`, `rm`, an edit) changed or
    deleted. The pinned set is `state.json`, `run.json`, `integrity.json`, `events.jsonl` and
    every file the manifest covers.

    Git writes the objects (`hash-object -w`, `mktree --batch`), so the repository's object
    format, `core.sharedRepository` and fsync policy apply (`core.fsync=committed` unless the
    owner set one), and the ref moves by compare-and-swap against the tree this pinner last saw
    (`update-ref ref new old`): at most three git processes per save. A file is pinned only with
    the bytes the runner knows to be its own: `state.json` as the save writes it, `integrity.json`
    as the runner last wrote it, a covered file only while its hash matches the manifest (else its
    earlier pinned copy stays), `run.json` once, `events.jsonl` while its pinned prefix is unchanged.
    A writer's edit therefore never becomes the copy that a repair would put back, and a forged move of the
    ref stops the run. It does not defend against a hostile writer with the runner's uid that also
    kills the runner: that one can rewrite the ref and the files alike."""

    def __init__(self, top, run_name):
        self.git = gitops.Git(top)
        self.ref = f"{gitops.REF_PREFIX}{run_name}/{RECORD_REF}"
        fmt = self.git.out("rev-parse", "--show-object-format", check=False)
        self.algo = fmt if fmt in ("sha1", "sha256") else "sha1"
        owner = self.git.run("config", "--get", "core.fsync", check=False).returncode == 0
        self.config = () if owner else ("-c", "core.fsync=committed")
        self.tree = self.git.out("rev-parse", "--verify", "-q", self.ref, check=False) or None
        entries = _ls_tree(self.git, self.tree) if self.tree else []
        self.pinned = {rel: oid for kind, oid, rel, _size in entries if kind == "blob"}
        self.known = {e[1] for e in entries} | ({self.tree} if self.tree else set())
        self.pending = {}                  # object id -> (kind, bytes), written at the next move
        self.covered = {}                  # rel -> (sha256, object id) of verified content
        self.fixed = {}                    # state.json, integrity.json, run.json -> object id
        self.run_json, self.identity = None, None    # run.json as last pinned, and its identity
        self.events = next(((size, oid) for kind, oid, rel, size in entries if rel == EVENTS),
                           (None, None))   # (size, object id) of events.jsonl as pinned
        self.moved = None                  # a PinMoved found at construction, raised at the first pin
        self.behind = False                # the pinned state is older than the disk's (a pin failed)
        self.pinned_state = None           # (state, generation) of the pinned state.json, once checked
        self.deleted = None                # the tree of a ref found deleted and pinned again

    def put(self, kind, data):
        """The object id of `data`; the object is written by git at the next `move`."""
        oid = hashlib.new(self.algo, f"{kind} {len(data)}\0".encode() + data).hexdigest()
        if oid not in self.known:
            self.pending[oid] = (kind, data)
        return oid

    def _hash_blobs(self, paths):
        """Write the pending blobs, and the files at `paths`, with one `git hash-object -w`.
        Returns the object ids of `paths`, in order."""
        blobs = [(oid, data) for oid, (kind, data) in self.pending.items() if kind == "blob"]
        if not blobs and not paths:
            return []
        argv = (*self.config, "hash-object", "-w", "--no-filters")
        if len(blobs) == 1 and not paths:
            found = [self.git.out(*argv, "--stdin", stdin=blobs[0][1])]
        else:
            with tempfile.TemporaryDirectory(prefix="code-smith-pin-") as tmp:
                names = []
                for oid, data in blobs:
                    names.append(os.path.join(tmp, oid))
                    with open(names[-1], "wb") as fh:
                        fh.write(data)
                stdin = "".join(p + "\n" for p in names + list(paths)).encode()
                found = self.git.out(*argv, "--stdin-paths", stdin=stdin).split()
        if found[:len(blobs)] != [oid for oid, _data in blobs]:
            raise ValueError("git hashed the record's blobs to other ids than the runner")
        for oid, _data in blobs:
            self.pending.pop(oid)
            self.known.add(oid)
        self.known.update(found[len(blobs):])
        return found[len(blobs):]

    def _make_trees(self):
        """Write the pending trees, children first, with one `git mktree --batch`."""
        trees = [(oid, entries) for oid, (kind, entries) in self.pending.items() if kind == "tree"]
        if not trees:
            return
        text = "\n".join("".join(f"{mode} {kind} {oid}\t{name}\n" for mode, kind, oid, name in entries)
                         for _oid, entries in trees)
        found = self.git.out(*self.config, "mktree", "--batch", stdin=text.encode()).split()
        if found != [oid for oid, _entries in trees]:
            raise ValueError("git wrote the record's trees with other ids than the runner")
        for oid, _entries in trees:
            self.pending.pop(oid)
            self.known.add(oid)

    def verified(self, rel, digest, path):
        """The object of a covered file whose bytes match `digest`, else its earlier pinned copy."""
        known = self.covered.get(rel)
        if known and known[0] == digest:
            return known[1]
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            data = None
        if data is not None and hashlib.sha256(data).hexdigest() == digest:
            self.covered[rel] = (digest, self.put("blob", data))
            return self.covered[rel][1]
        return self.pinned.get(rel)

    def put_tree(self, files):
        root = {}
        for rel, oid in files.items():
            node, parts = root, rel.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = oid

        def write(node):
            entries = []
            for name, value in node.items():
                if "\n" in name or name.startswith('"'):
                    raise ValueError(f"a record path git's tree text cannot hold: {name!r}")
                if isinstance(value, dict):
                    entries.append(((name + "/").encode(), ("40000", "tree", write(value), name)))
                else:
                    entries.append((name.encode(), ("100644", "blob", value, name)))
            entries = [e for _key, e in sorted(entries)]
            body = b"".join(b"%s %s\0" % (mode.encode(), name.encode()) + bytes.fromhex(oid)
                            for mode, _kind, oid, name in entries)
            oid = hashlib.new(self.algo, b"tree %d\0" % len(body) + body).hexdigest()
            if oid not in self.known:
                self.pending[oid] = ("tree", entries)
            return oid
        return write(root)

    def move(self, files, paths=None):
        """Point the ref at the tree of `files` (rel -> object id) and of `paths` (rel -> a file
        git hashes itself), by compare-and-swap. Raises PinMoved when the ref is not the tree this
        pinner last saw; any other failure leaves the pending objects for the next move."""
        paths = dict(paths or {})
        files = dict(files, **dict(zip(paths, self._hash_blobs(list(paths.values())))))
        tree = self.put_tree(files)
        if tree == self.tree:
            return files
        self._make_trees()
        old = self.tree or "0" * len(tree)
        res = self.git.run(*self.config, "update-ref", self.ref, tree, old, check=False)
        if res.returncode != 0:
            found = self.git.out("rev-parse", "--verify", "-q", self.ref, check=False) or None
            if found is None and self.tree:
                # Deleted, not moved (an author's habit, GIT-21): nothing forged is waiting to be
                # put back, so the runner's own copy is pinned again, still only where none is.
                res = self.git.run(*self.config, "update-ref", self.ref, tree, "0" * len(tree),
                                   check=False)
                if res.returncode == 0:
                    self.deleted, self.tree = self.tree, tree
                    return files
                found = self.git.out("rev-parse", "--verify", "-q", self.ref, check=False) or None
            if found != self.tree:
                raise PinMoved(self.ref, self.tree, found)
            raise gitops.GitError(["update-ref", self.ref, tree, old], res.returncode,
                                  res.stderr.decode("utf-8", "replace"))
        self.tree = tree
        return files


def _blob_id(algo, data):
    """The object id git gives `data` as a blob."""
    return hashlib.new(algo, b"blob %d\0" % len(data) + data).hexdigest()


EVENTS_CHUNK = 1 << 20          # bytes: the most of events.jsonl held in memory at once (REC-64)


def _blob_hasher(algo, size):
    """A hash that gives the blob id of `size` bytes once they are fed to it."""
    return hashlib.new(algo, b"blob %d\0" % size)


def _read_span(fh, size):
    """The first `size` bytes from `fh`'s position, in pieces of at most EVENTS_CHUNK; fewer when
    the file ends first."""
    left = size
    while left > 0:
        chunk = fh.read(min(left, EVENTS_CHUNK))
        if not chunk:
            return
        left -= len(chunk)
        yield chunk


def _ls_tree(git, tree):
    """(type, object id, rel, size or None) of every blob and tree under a pinned record tree."""
    raw = git.run("ls-tree", "-r", "-t", "-l", "-z", "--full-tree", tree).stdout
    found = []
    for entry in raw.split(b"\0"):
        if entry:
            meta, _tab, rel = entry.partition(b"\t")
            _mode, kind, oid, size = meta.decode().split()
            found.append((kind, oid, rel.decode("utf-8", "surrogateescape"),
                          int(size) if size.isdigit() else None))
    return found


def pinned_files(git, tree):
    """rel -> object id of every file in a pinned record tree."""
    return {rel: oid for kind, oid, rel, _size in _ls_tree(git, tree) if kind == "blob"}


def read_objects(git, oids):
    """The bytes of many objects in one `git cat-file --batch`."""
    if not oids:
        return {}
    out = git.run("cat-file", "--batch", stdin=("\n".join(oids) + "\n").encode()).stdout
    found, at = {}, 0
    for oid in oids:
        end = out.index(b"\n", at)
        head = out[at:end].split()
        if len(head) < 3:                                # "<oid> missing"
            at = end + 1
            continue
        size = int(head[2])
        found[oid] = out[end + 1:end + 1 + size]
        at = end + 1 + size + 1
    return found


def find_run(runs_dir, git, ref="latest"):
    """RUN as `resolve_run` takes it, among the runs on disk and the runs whose record is only
    pinned (a `git clean -fdx` removed its directory). Returns the run directory's path."""
    found = {}
    for _wf, name, path in list_runs(runs_dir):
        try:
            found[name] = (path, read_json(os.path.join(path, "run.json")))
        except (OSError, ValueError):
            continue
    suffix = "/" + RECORD_REF
    for pinned, tree in git.pins().items():
        name = pinned[len(gitops.REF_PREFIX):-len(suffix)] if pinned.endswith(suffix) else ""
        if not name or "/" in name or name in found:
            continue
        res = git.run("cat-file", "blob", f"{tree}:run.json", check=False)
        try:
            info = json.loads(res.stdout.decode("utf-8")) if res.returncode == 0 else None
        except ValueError:
            info = None
        if info and info.get("workflow"):
            found[name] = (os.path.join(runs_dir, info["workflow"], name), info)
    runs = sorted(found.items(), key=lambda kv: (kv[1][1].get("started", ""), kv[0]))
    if not runs:
        raise RecordError(f"no runs under {runs_dir}, and no pinned record of one")
    if ref in (None, "", "latest"):
        return runs[-1][1][0]
    matches = [r for r in runs if r[0] == ref] or [
        r for r in runs if str(r[1][1].get("run_id", "")).startswith(ref.lower())]
    if not matches:
        raise RecordError(f"no run matches '{ref}'")
    if len(matches) > 1:
        raise RecordError(f"'{ref}' matches several runs: " + ", ".join(m[0] for m in matches))
    return matches[0][1][0]


def run_json_for(info, state):
    """run.json for `state`: its identity, never changed, with the totals copied from the state.
    One function for the page a regeneration writes and the copy a save pins, so the pinned
    run.json describes the same checkpoint as the pinned state (K11)."""
    return dict(info, status=state["status"], spend=state["spend"], seconds=state["seconds"],
                run_budget_usd=state["run_budget_usd"],
                run_budget_tokens=state.get("run_budget_tokens", 0))


def _identity(data, also=()):
    """run.json without the totals a regeneration copies into it from the state (and `also`)."""
    info = json.loads(data.decode("utf-8")) if isinstance(data, bytes) else data
    return {k: v for k, v in info.items() if k not in RUN_TOTALS and k not in also}


def _run_part(rel):
    """The attempt or round directory a record path lies in (`tasks/<dir>/attempt-N`), or ''."""
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "tasks" and re.fullmatch(r"(attempt|round)-\d+", parts[2]):
        return "/".join(parts[:3])
    return ""


def _generation(state):
    """(save_seq, op_seq) of a run state: which of two copies is the later one. None when
    `state` is not a run state at all (truncated, or valid JSON of another shape)."""
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), dict):
        return None
    try:
        return int(state.get("save_seq", 0)), int(state.get("op_seq", 0))
    except (TypeError, ValueError):
        return None


def parse_state(data):
    """(state, generation) of the bytes of a state.json, or (None, None) when they are not one."""
    try:
        state = json.loads(data)
    except (TypeError, ValueError):
        return None, None
    found = _generation(state)
    return (state, found) if found else (None, None)


def record_ref(name):
    return f"{gitops.REF_PREFIX}{name}/{RECORD_REF}"


def pinned_record(git, name):
    """(files, data) of a run's pinned record: rel -> object id, and object id -> bytes. A tree
    or object git cannot read is a RecordError naming it; None when the run has no pin."""
    ref = record_ref(name)
    tree = git.pins(name).get(ref)
    if not tree:
        return None
    try:
        files = pinned_files(git, tree)
    except gitops.GitError as exc:
        raise RecordError(f"the pinned record {ref} (tree {tree}) cannot be read from git: "
                          f"{exc.stderr.strip()}. Nothing was changed") from exc
    oids = sorted(set(files.values()))
    try:
        data = read_objects(git, oids)
    except gitops.GitError:
        data = {}
        for oid in oids:
            res = git.run("cat-file", "blob", oid, check=False)
            if res.returncode == 0:
                data[oid] = res.stdout
    for rel, oid in sorted(files.items()):
        if oid not in data:
            raise RecordError(f"the pinned copy of {rel} (object {oid} under {ref}) is missing or "
                              "corrupt in git, so the pinned record cannot be trusted whole. "
                              "Nothing was changed")
    return files, data


def pinned_run_id(git, name):
    """The run id in a run's pinned run.json, or ''."""
    res = git.run("cat-file", "blob", f"{record_ref(name)}:run.json", check=False)
    try:
        info = json.loads(res.stdout) if res.returncode == 0 else {}
    except ValueError:
        info = {}
    return info.get("run_id", "") if isinstance(info, dict) else ""


def _events_repair(pinned, found):
    """The events.jsonl a repair writes: None when the file on disk already continues the pinned
    one; else the pinned history followed by what was logged after it was lost."""
    if found is not None and found.startswith(pinned):
        return None
    if found:
        # Event timestamps identify rows even if their contents were edited. Keep later rows,
        # not a second, corrupted version of the history we are restoring.
        stamps = set()
        for line in pinned.splitlines():
            try:
                stamps.add(json.loads(line)['at'])
            except (ValueError, KeyError, TypeError):
                pass
        later = []
        for line in found.splitlines(keepends=True):
            try:
                if json.loads(line).get('at') in stamps:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass
            later.append(line)
        found = b''.join(later)
    if found and pinned and not pinned.endswith(b"\n"):
        pinned += b"\n"
    return pinned + (found or b"")


def repair_record(path, git, dry_run=False, accept_older=False):
    """Compare the run record at `path` with its pinned copy (W-02). Every pinned file that is
    missing, or whose bytes differ (run.json: whose identity differs), is put back from git
    unless `dry_run`. Returns {"wrong": [...], "lost": [...], "rebuilt": [...], "problems": [...],
    "kept": [...]}: the files found wrong, with both hashes; the attempt and round directories
    that are gone with their logs, which nothing can bring back; the task.json files rebuilt from
    the frozen workflow; what the integrity check still finds afterwards; and what was kept
    although it differs from the pin.

    A `state.json` on disk that is a state of the same run with a later generation than the
    pinned one (the pin failed after it, K1) is kept, with its `integrity.json`, unless
    `accept_older`; the other files are put back only where they agree with that manifest.
    `events.jsonl` is put back by putting the pinned history in front of what was logged since."""
    name = os.path.basename(path)
    pinned = pinned_record(git, name)
    if pinned is None:
        raise RecordError(f"run {name} has no pinned record ({record_ref(name)}): "
                          "it was recorded by an older runner, or its refs were pruned. Nothing "
                          "was changed; the files named by the integrity check must be put back by hand")
    files, data = pinned
    restore = {rel: data[oid] for rel, oid in files.items()}
    pinned_state, pinned_gen = parse_state(restore.get("state.json"))
    if "state.json" in files and pinned_state is None:
        raise RecordError(f"the pinned state.json under {record_ref(name)} is not a run state. "
                          "Nothing was changed")
    disk = _read_bytes(os.path.join(path, "state.json"))
    disk_state, disk_gen = parse_state(disk) if disk is not None else (None, None)
    kept, keep, manifest = [], set(), None
    if (not accept_older and disk_state is not None and pinned_state is not None
            and disk_state.get("run_id") == pinned_state.get("run_id") and disk_gen > pinned_gen):
        keep = {"state.json", "integrity.json"}
        kept.append(f"state.json on disk is newer than the pin (save {disk_gen[0]}, op {disk_gen[1]} "
                    f"vs save {pinned_gen[0]}, op {pinned_gen[1]}); kept, with its integrity.json. "
                    "`--accept-older` puts the pinned ones back instead")
        try:
            manifest = read_json(os.path.join(path, "integrity.json"))["files"]
        except (OSError, ValueError, KeyError, TypeError):
            manifest = None
    wrong = []
    for rel in sorted(files):
        if rel in keep:
            continue
        target = os.path.join(path, rel)
        recorded = hashlib.sha256(restore[rel]).hexdigest()
        found = sha256_file(target) if os.path.isfile(target) else None
        if found == recorded:
            continue
        if rel == EVENTS:
            restore[rel] = _events_repair(restore[rel], _read_bytes(target))
            if restore[rel] is None:
                continue
        if rel == "run.json" and found:
            try:
                if _identity(read_json(target), ("agents",)) == _identity(restore[rel], ("agents",)):
                    continue
            except (OSError, ValueError, AttributeError):
                pass
        if isinstance(manifest, dict) and manifest.get(rel, recorded) != recorded:
            continue                    # older than the kept manifest: the integrity check names it
        wrong.append({"path": rel, "was": "missing" if found is None else "changed",
                      "recorded_sha256": recorded, "found_sha256": found})
    state = (disk_state if disk_state is not None and not any(w["path"] == "state.json" for w in wrong)
             else pinned_state) or {"tasks": {}}
    parts = {_run_part(rel) for rel in files} - {""}
    for st in state["tasks"].values():
        for key in ("attempt_dir", "pending_call"):
            if isinstance(st, dict) and isinstance(st.get(key), str):
                parts.add(_run_part(st[key]) or "")
    parts.discard("")
    lost = sorted(p for p in parts if not os.path.isdir(os.path.join(path, p)))
    report = {"wrong": wrong, "lost": lost, "rebuilt": [], "problems": [], "kept": kept}
    if dry_run:
        return report
    for w in wrong:
        target = os.path.join(path, w["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        write_durable(target, restore[w["path"]])
    run = Run.load(path)
    report["rebuilt"] = run._rebuild_task_dirs()
    if wrong or lost:
        run.event("record-repaired", paths=wrong, lost_logs=lost, rebuilt=report["rebuilt"])
    report["problems"] = run.integrity_check()
    if not report["problems"]:
        run.regenerate()
    return report


# -- a run --------------------------------------------------------------------------------------

def runner_source():
    """Which runner source made a record (RUN-49): its version, a SHA-256 over the package's
    Python files (`src_sha256`, always), and, when the package sits in a Git checkout, that
    checkout's `commit` and whether the package differs from it (`dirty`). `runner_version`
    alone did not say which of many commits ran a run."""
    here = os.path.dirname(os.path.abspath(__file__))
    digest = hashlib.sha256()
    for name in sorted(n for n in os.listdir(here) if n.endswith(".py")):
        with open(os.path.join(here, name), "rb") as fh:
            digest.update(name.encode() + b"\0" + fh.read() + b"\0")
    out = {"version": __version__, "src_sha256": digest.hexdigest()}
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        head = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10, env=env)
        if head.returncode == 0 and head.stdout.strip():
            changed = subprocess.run(["git", "-C", here, "status", "--porcelain", "--", "."],
                                     capture_output=True, text=True, timeout=10, env=env)
            out.update(commit=head.stdout.strip(), dirty=bool(changed.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass                                     # no git: the fingerprint is the identity
    return out


GAPS_KEPT = 3             # the operations named in `clock_gap.where`, latest last


def _kept_process(identity):
    """The process identity an outcome event keeps (REC-50): the group id, and the supervisor's
    and the leader's identities as recorded, without the member list. Historical: nothing reads
    it to find a live process."""
    if not isinstance(identity, dict):
        return identity
    group = identity.get("group") or {}
    kept = {"pgid": group.get("pgid", identity.get("pgid"))}
    for name in ("supervisor", "leader"):
        if group.get(name):
            kept[name] = {k: v for k, v in group[name].items() if k != "group"}
    if not group:
        kept["pid"] = identity.get("pid")
    return kept


def _count_span(state, began, ended):
    """Add one agent call's wall interval to `state.agent_spans` (REC-51): `summed_s` the sum of
    the intervals, `open` the union of those that a call still to finish can overlap, `folded_s`
    the length of the union of the rest. A later call begins after now, and a call in flight
    began at its intent, so an interval that ended before both can overlap nothing to come and is
    folded: the state keeps a few intervals, not one per call."""
    spans = state.setdefault("agent_spans", {"summed_s": 0.0, "folded_s": 0.0, "open": []})
    spans["summed_s"] = round(spans["summed_s"] + ended - began, 6)
    merged = []
    for b, e in sorted(spans["open"] + [[began, ended]]):
        if merged and b < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([b, e])
    starts = [_parse_at(it.get("at")) for it in state["intents"] if it.get("kind") == "agent"]
    edge = min([s.timestamp() for s in starts if s is not None] + [time.time()])
    spans["open"] = [[b, e] for b, e in merged if e > edge]
    spans["folded_s"] = round(spans["folded_s"] + sum(e - b for b, e in merged if e <= edge), 6)


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp(now):
    return now.strftime("%Y%m%dT%H%M%SZ")


def task_dir_names(tasks):
    """<order>-<id>. Written tasks step by ten, leaving gaps for replanned tasks; the generated
    members of a panel follow their producer one by one (010-design, 011-design.review.…)."""
    names, n = {}, 0
    for t in tasks:
        n = n + 1 if t.get("generated") else (n // 10 + 1) * 10
        names[t["id"]] = f"{n:03d}-{t['id']}"
    return names


class Run:
    def __init__(self, path, state):
        self.path = path
        self.state = state
        self.crash = _no_crash
        self._status_lock = threading.Lock()         # regenerate() and the heartbeat both write STATUS.md
        self.refresh_s = None                        # the workflow's status_refresh_s, read when first needed
        self._violations = []                        # what the last save's invariant check found
        self._defs, self._defs_stamp = None, None
        self._migrated = None                        # (from, to) until the migrated state is saved
        self._pin, self._pin_top = None, None        # the pinned record (W-02), made at the first save
        self._pin_lock = threading.Lock()
        self._pin_errors = set()                     # the pin failures already logged (K1)
        self._events_told = False                    # record-events-changed logged (REC-58)
        self._pin_moved = None                       # the PinMoved that stops this run (K2)
        self._saved = _generation(state) or (0, 0)   # the generation of state.json on disk
        self._wrote_state = False                    # this process has saved the state
        self._manifest_bytes = None                  # integrity.json as this process trusts it (K3)
        self._render_fingerprints = {}
        self._rendered_full = False
        self._ledger_fingerprints = {}
        self._render_failures = set()
        self._render_context = threading.local()

    @property
    def _render_target(self):
        return getattr(self._render_context, "target", {"page": "STATUS.md"})

    @_render_target.setter
    def _render_target(self, target):
        self._render_context.target = target

    # -- creation and loading ----------------------------------------------------------------

    @classmethod
    def create(cls, wf, git, branch, original_branch, now=None, run_id=None, branch_intent=False):
        """Make the run directory with a frozen copy of everything that defines the work. With
        `branch_intent`, the first state already holds the intent to create the run branch, so a
        run interrupted before its branch exists is resumed by creating it. run.json is written
        last: until it exists the directory is not a run (`list_runs`)."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        run_id = run_id or str(uuid.uuid4())
        runs = ensure_runs_dir(git.top)
        wf_dir = os.path.join(runs, wf.name)
        os.makedirs(wf_dir, exist_ok=True)
        name = f"{wf.name}-{_utc_stamp(now)}-{run_id[:8]}"
        path = os.path.join(wf_dir, name)
        os.mkdir(path)                                   # exclusive: never reuse a run directory

        with open(wf.workflow_file, "rb") as fh:
            frozen = fh.read()
        write_durable(os.path.join(path, "workflow.toml"), frozen)
        write_durable(os.path.join(path, "workflow.expanded.json"), dump_json(wf.expanded()))
        cls._freeze_library(wf, path)
        cls._freeze_briefs(wf, path)

        names = task_dir_names(wf.tasks)
        for t in wf.tasks:
            tdir = os.path.join(path, "tasks", names[t["id"]])
            os.makedirs(tdir)
            write_durable(os.path.join(tdir, "task.json"), dump_json(t))

        run_json = {
            "run_id": run_id, "name": name, "workflow": wf.name,
            "workflow_sha256": hashlib.sha256(frozen).hexdigest(),
            "workflow_file": wf.workflow_file, "root": wf.root, "git_toplevel": git.top,
            "library": list(wf.library_dirs),
            "branch": branch, "original_branch": original_branch,
            "branch_mode": wf.defaults["branch"], "base_commit": git.head(),
            "started": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "runner_version": __version__,
            "runner_source": runner_source(),
            "agents": {},                                # versions are recorded by doctor
        }

        state = {
            "version": STATE_VERSION, "schema_version": schema.SCHEMA_VERSION,
            "run_id": run_id, "status": "running",
            "op_seq": 0, "intents": [], "active_producer": None, "expect": None,
            "spend": {"known_usd": 0.0, "reserved_usd": 0.0,
                      "unpriced": {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                   "unknown_calls": 0}},
            "run_budget_usd": wf.defaults["run_budget_usd"],
            "run_budget_tokens": wf.defaults["run_budget_tokens"], "seconds": 0,
            # 0: the bound of an unpriced call's reservation follows the run (BUD-17)
            "unpriced_call_reserve": wf.defaults["unpriced_call_reserve"],
            "tasks": {t["id"]: {"status": "pending", "dir": names[t["id"]], "kind": t["kind"],
                                "type": t["type"], "attempts": 0, "cost_usd": 0.0,
                                "commit": None, "reason": "",
                                # the planned agent and model; `provider_current` says what ran
                                **({"agent": t["agent"], "model": t.get("model", "")}
                                   if t["kind"] in ("produce", "review") else {})} for t in wf.tasks},
            "order": [t["id"] for t in wf.tasks],
        }
        if branch_intent:
            state["op_seq"] = 1
            state["intents"].append({"op": f"op-0001-{uuid.uuid4().hex[:8]}", "kind": "branch",
                                     "at": _utc_now_iso(), "name": branch})
        run = cls(path, state)
        run._pin_top = git.top                           # run.json, which names it, comes last
        # The frozen execution inputs are under the manifest from the start: the engine
        # trusts them on every resume, and a producer could otherwise edit them inside .runs/.
        write_durable(os.path.join(path, "integrity.json"), dump_json(
            {"files": {run._rel(f): sha256_file(f) for f in run.definition_files()}}))
        run.save()
        write_durable(os.path.join(path, "run.json"), dump_json(run_json))
        run.event("run-created", workflow=wf.name, branch=branch, base_commit=run_json["base_commit"])
        for it in state["intents"]:
            run.event("intent", op=it["op"], kind=it["kind"])
        with open(os.path.join(wf_dir, "latest"), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        run.regenerate_safely(full=True)
        return run

    @staticmethod
    def _freeze_library(wf, path):
        used = {}
        for t in wf.tasks:
            tdef = wf.types.get(t["type"])
            if tdef and tdef.get("_where"):
                used[("types", t["type"])] = tdef["_where"]
            pdef = wf.personas.get(t.get("perspective") or "")
            if pdef and pdef.get("_where"):
                used[("personas", t["perspective"])] = pdef["_where"]
        for (sub, stem), source in sorted(used.items()):
            os.makedirs(os.path.join(path, "library", sub), exist_ok=True)
            shutil.copyfile(source, os.path.join(path, "library", sub, stem + ".toml"))

    @staticmethod
    def _freeze_briefs(wf, path):
        for t in wf.tasks:
            if t.get("prompt_file"):
                os.makedirs(os.path.join(path, "briefs"), exist_ok=True)
                shutil.copyfile(t["prompt_file"], os.path.join(path, "briefs", t["id"] + ".md"))
            if t.get("rules_file"):
                os.makedirs(os.path.join(path, "briefs"), exist_ok=True)
                shutil.copyfile(t["rules_file"], os.path.join(path, "briefs", t["id"] + ".rules.md"))

    @classmethod
    def load(cls, path):
        """Only `state.json` is read back. A leftover `.state.json.*.tmp` is ignored. An older shape
        is migrated in memory (`schema.migrate`); the first save makes it durable and records the
        `state-migrated` event, so a command that only reads writes nothing."""
        run = cls(path, read_json(os.path.join(path, "state.json")))
        try:
            run._migrated = schema.migrate(run.state, run)
        except schema.NewerState as exc:
            raise RecordError(str(exc)) from exc
        return run

    @property
    def info(self):
        return read_json(os.path.join(self.path, "run.json"))

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def index_file(self):
        return os.path.join(self.path, "git-index")

    def task_dir(self, task_id):
        return os.path.join(self.path, "tasks", self.state["tasks"][task_id]["dir"])

    # -- state, events, intents --------------------------------------------------------------

    def save(self):
        """Write the state durably, pinning it first so the pinned copy is never older. Every
        save counts in `save_seq`, which tells a repair whether the pin or the disk is later (K1).
        A pin that fails leaves `pin_stale` in the state written; a ref moved by someone else
        is left alone and stops the run once the state is written (PinMoved, K2)."""
        self._keep_home()
        self.state["save_seq"] = self.state.get("save_seq", 0) + 1
        stale = self.state.pop("pin_stale", None)
        data = dump_json(self.state)
        moved = self.pin_record(state=data, stale=stale)
        if "pin_stale" in self.state:
            data = dump_json(self.state)
        write_durable(os.path.join(self.path, "state.json"), data, self.crash)
        self._saved, self._wrote_state = _generation(self.state), True
        if self._migrated:
            self.event("state-migrated", **dict(zip(("from_version", "to_version"), self._migrated)))
            self._migrated = None
        self._check_invariants()
        if moved:
            raise moved

    def _check_invariants(self):
        """Report-only (W-04): a broken rule is an event, once per change of what is broken, and a
        line in STATUS.md. It never stops a real run; under the test suite it raises."""
        try:
            found = invariants.check(self.state, self._definitions())
        except Exception as exc:                            # noqa: BLE001 - never hurt the run
            if invariants.strict():
                raise
            found = [f"the invariant check itself failed: {exc}"]
        if found and invariants.strict():
            raise invariants.Violation("; ".join(found))
        if found != self._violations:
            self._violations = found
            if found:
                try:
                    self.event("invariant-violation", problems=found)
                except OSError:
                    pass

    def _definitions(self):
        """The frozen task definitions by id, read again only when the file changed (a replan)."""
        path = os.path.join(self.path, "workflow.expanded.json")
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            return None
        if self._defs_stamp != stamp:
            try:
                self._defs = {t["id"]: t for t in read_json(path)["tasks"]}
            except (OSError, ValueError, KeyError, TypeError):
                self._defs = None
            self._defs_stamp = stamp
        return self._defs

    def event(self, event, **data):
        if "at" in data:
            # The event's own stamp is the record's: a payload never shadows it (REC-60). Its
            # name (`event`) cannot be passed; it is this method's own argument.
            raise ValueError(f"event {event!r}: the payload key 'at' is reserved")
        line = json.dumps({"at": datetime.datetime.now(datetime.timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "event": event, **data},
                          sort_keys=True, ensure_ascii=False)
        with open(os.path.join(self.path, "events.jsonl"), "a+b") as fh:
            # A kill in the middle of an append leaves a torn last line: end it, so that it does
            # not swallow this event.
            if fh.seek(0, os.SEEK_END) and (fh.seek(-1, os.SEEK_END), fh.read(1))[1] != b"\n":
                fh.write(b"\n")
            fh.write((line + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())

    def begin(self, kind, **expect):
        """Record the intent of an external effect, durably, before the effect. Returns its id."""
        self.state["op_seq"] += 1
        op_id = f"op-{self.state['op_seq']:04d}-{uuid.uuid4().hex[:8]}"
        # Stamped to the microsecond, as events are: an intent's time starts the wall clock of
        # its outcome (`wall_s`) and its span among parallel calls (REC-51).
        self.state["intents"].append({"op": op_id, "kind": kind, "at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), **expect})
        self.save()
        self.event("intent", op=op_id, kind=kind)
        return op_id

    def amend(self, op_id, **more):
        """Add what is only known once the effect has started, such as an agent's process."""
        self.intent(op_id).update(more)
        self.save()

    def intent(self, op_id):
        for it in self.state["intents"]:
            if it["op"] == op_id:
                return it
        raise KeyError(op_id)

    def finish(self, op_id, ended=None, **outcome):
        """Record the outcome and clear the intent. The outcome event carries `wall_s`, the wall
        clock from the intent to `ended` (epoch seconds; now by default), and `ended_at`; a caller
        that measured the operation passes `seconds`. Where the wall clock ran a minute or more
        longer than the measured time, the difference is added to `state.clock_gap`, with the
        operation (RUN-50). The event keeps what only the intent knew (`OUTCOME_KEPT`: the task,
        the invocation, the process identity as recorded, the token reservation and remainder),
        since the intent is gone once the operation ends (REC-50); an agent call's interval is
        counted in `state.agent_spans` (REC-51). A reconciled operation has no times; one
        `cancelled` before release, which waited and did not run, keeps `ended_at` (the cancel
        time) and has no `wall_s` (PROC-26)."""
        it = self.intent(op_id)
        cleanup = outcome.get("cleanup")
        if cleanup and cleanup.get("status") == "open":
            self.state.setdefault("cleanup_obligations", []).append({
                "op": op_id, "task": it.get("task"),
                "invocation": it.get("invocation_dir", op_id), "cleanup": cleanup})
            self.state['stop_reason'] = cleanup_refusal(self)
        self.state["intents"].remove(it)
        ended = ended or time.time()
        for key in OUTCOME_KEPT:
            source = "invocation_dir" if key == "invocation" else key
            if source in it and key not in outcome:
                outcome[key] = _kept_process(it[source]) if key == "process" else it[source]
        if not it.get("reconciled"):
            outcome.setdefault("ended_at", datetime.datetime.fromtimestamp(
                ended, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        began = None if it.get("reconciled") or outcome.get("cancelled") else _parse_at(it.get("at"))
        if began is not None:
            outcome["wall_s"] = round(max(0.0, ended - began.timestamp()), 1)
            gap = outcome["wall_s"] - float(outcome.get("seconds", outcome["wall_s"]))
            if gap >= 60:
                clock = self.state.setdefault("clock_gap", {"operations": 0, "seconds": 0})
                clock.update(operations=clock["operations"] + 1, seconds=round(clock["seconds"] + gap))
                clock["where"] = (clock.get("where") or [])[-(GAPS_KEPT - 1):] + [
                    {"op": op_id, "kind": it["kind"], "task": it.get("task"), "seconds": round(gap)}]
            if it["kind"] == "agent":
                _count_span(self.state, began.timestamp(), max(ended, began.timestamp()))
        self.save()
        self.event("outcome", op=op_id, kind=it["kind"], **outcome)

    def set_status(self, task_id, status, reason=""):
        self.state["tasks"][task_id].update(status=status, reason=reason)

    def record_acceptance(self, task_id, commit, files, message):
        """A producer's work is committed: write commit.json, mark it accepted."""
        tdir = self.task_dir(task_id)
        self.write_decision(os.path.join(tdir, "commit.json"),
                            {"sha": commit, "files": files, "message": message})
        self.state["tasks"][task_id].update(status="accepted", commit=commit, reason="")
        self.state["last_tip"] = commit
        if self.state.get("active_producer") == task_id:
            self.state["active_producer"] = None

    # -- numbered directories -------------------------------------------------------

    @staticmethod
    def _next_numbered(parent, prefix):
        """`<prefix>-N`, where N was never used under `parent`. Created exclusively, so nothing
        stale is ever found in it; a lost race simply takes the next number."""
        os.makedirs(parent, exist_ok=True)
        used = [int(n[len(prefix) + 1:]) for n in os.listdir(parent)
                if n.startswith(prefix + "-") and n[len(prefix) + 1:].isdigit()]
        n = max(used, default=0) + 1
        while True:
            path = os.path.join(parent, f"{prefix}-{n}")
            try:
                os.mkdir(path)
                return n, path
            except FileExistsError:
                n += 1

    def new_attempt(self, task_id):
        n, path = self._next_numbered(self.task_dir(task_id), "attempt")
        self.state["tasks"][task_id]["attempts"] += 1
        return n, path

    def new_round(self, task_id):
        return self._next_numbered(self.task_dir(task_id), "round")

    def new_invocation(self, parent):
        return self._next_numbered(parent, "invocation")

    # -- integrity ------------------------------------------------------------------

    def _manifest_path(self):
        return os.path.join(self.path, "integrity.json")

    def _manifest(self):
        return read_json(self._manifest_path())

    def _rel(self, path):
        return os.path.relpath(path, self.path).replace(os.sep, "/")

    DEFINITION_FILES = ("workflow.toml", "workflow.expanded.json")
    DEFINITION_DIRS = ("library", "briefs")

    def definition_files(self):
        """The frozen copies that define the work: the workflow as started and expanded, and the
        library types, personas and briefs copied at start (or installed by a replan). The engine
        reads these, never the originals, so they are execution inputs and are protected."""
        found = [os.path.join(self.path, n) for n in self.DEFINITION_FILES
                 if os.path.isfile(os.path.join(self.path, n))]
        for d in self.DEFINITION_DIRS:
            for dirpath, dirnames, files in os.walk(os.path.join(self.path, d)):
                dirnames.sort()
                found += [os.path.join(dirpath, f) for f in sorted(files) if f != "index.json"]
        return found

    def protect_definitions(self, missing_only=False):
        """Bring the frozen definitions under the manifest: all of them after a replan installed
        new ones, or with `missing_only` those a run recorded before this protection lacks."""
        known = self._trusted_manifest()["files"]
        paths = [f for f in self.definition_files() if not (missing_only and self._rel(f) in known)]
        if paths:
            self.protect(*paths)
        return [self._rel(p) for p in paths]

    def _trusted_manifest(self):
        """integrity.json as the runner itself last made it (K3): as this process last wrote it,
        else the pinned copy, else (no pin, or a pin older than the state: one failed) the disk's.
        Never the disk's once this process or the pin knows better: an author who changes a frozen
        file and its hash together must not have the pair adopted by the next `protect`."""
        if self._manifest_bytes is None:
            try:
                pin = self._pinner()
            except (OSError, ValueError, KeyError, TypeError, gitops.GitError):
                pin = None
            with self._pin_lock:
                self._load_manifest(pin)
        return json.loads(self._manifest_bytes) if self._manifest_bytes else {"files": {}}

    def _load_manifest(self, pin):
        if self._manifest_bytes is not None:
            return
        if pin and "integrity.json" in pin.pinned and not pin.behind:
            try:
                self._manifest_bytes = read_objects(pin.git, [pin.pinned["integrity.json"]]).get(
                    pin.pinned["integrity.json"])
            except gitops.GitError:
                self._manifest_bytes = None
        if self._manifest_bytes is None:
            self._manifest_bytes = _read_bytes(self._manifest_path())

    def protect(self, *paths):
        """Put decision-bearing files under the manifest, with their current hashes. The entries
        are added to the trusted manifest, never to whatever is on disk: a disk manifest that
        differs is replaced, with an event, and the files it vouched for then fail the integrity
        check as the change they are."""
        manifest = self._trusted_manifest()
        disk = _read_bytes(self._manifest_path())
        if disk is not None and disk != self._manifest_bytes:
            try:
                found = json.loads(disk)["files"]
            except (ValueError, KeyError, TypeError):
                found = {}
            ours = manifest["files"]
            self.event("integrity-manifest-replaced", paths=sorted(
                rel for rel in set(found) | set(ours) if found.get(rel) != ours.get(rel)))
        for p in paths:
            manifest["files"][self._rel(p)] = sha256_file(p)
        self._manifest_bytes = dump_json(manifest)
        moved = self.pin_record()                          # first, as in `save`
        write_durable(self._manifest_path(), self._manifest_bytes)
        if moved:
            raise moved

    def write_findings(self):
        """Publish changed ledgers after saving state, independently of derived pages."""
        for task_id, task in self.state["tasks"].items():
            ledger = task.get("ledger")
            if ledger is None:
                continue
            digest = hashlib.sha256(dump_json(ledger)).hexdigest()
            if self._ledger_fingerprints.get(task_id) != digest:
                self.write_decision(os.path.join(self.task_dir(task_id), "findings.json"), ledger)
                self._ledger_fingerprints[task_id] = digest

    def write_decision(self, path, obj):
        """Write a decision-bearing file durably and bring the manifest up to date. A first write
        cannot strand the record: until its entry exists the file is not checked. A file already
        in the manifest is rewritten under an intent (`publish_decision`), since a kill between
        the file and its entry would leave every later integrity check failing; the same bytes
        again are left alone."""
        data = dump_json(obj)
        known = self._trusted_manifest()["files"].get(self._rel(path))
        if known is None:
            write_durable(path, data)
            self.protect(path)
        elif not (known == hashlib.sha256(data).hexdigest() and os.path.exists(path)
                  and sha256_file(path) == known):
            self.publish_decision(path, obj)

    def _write_decision_now(self, path, obj):
        write_durable(path, dump_json(obj))
        self.protect(path)

    def publish_decision(self, path, obj, crash=_no_crash):
        """write_decision under an intent: the complete payload is recorded first, so a crash between
        the file and its manifest entry is repaired by writing both again from the intent."""
        op = self.begin("decision", path=self._rel(path), payload=obj)
        write_durable(path, dump_json(obj))
        crash("decision:file-written")
        self.protect(path)
        self.finish(op, path=self._rel(path))

    def close_directory(self, directory):
        """A finished attempt, round or invocation directory: its decision files are now fixed.
        Hashing again gives the same result, so this is safe to repeat after a crash."""
        found = []
        for dirpath, _dirs, files in os.walk(directory):
            found += [os.path.join(dirpath, f) for f in files if f in DECISION_FILES]
        if found:
            self.protect(*sorted(found))
        return [self._rel(p) for p in sorted(found)]

    def accepted_inputs(self, task_id):
        """Acceptance evidence must exist even in records predating its integrity entry."""
        rel = self.state['tasks'][task_id].get('attempt_dir')
        try:
            data = read_json(os.path.join(self.path, rel, 'inputs.json')) if rel else None
            inputs = data['inputs']
            if (not isinstance(inputs, dict) or any(
                    not isinstance(tid, str) or not isinstance(files, dict) or any(
                        not isinstance(path, str) or not isinstance(sha, str)
                        for path, sha in files.items()) for tid, files in inputs.items())):
                raise ValueError('inputs must map tasks to path hashes')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RecordError(f"accepted task '{task_id}' has missing or malformed inputs.json") from exc
        return inputs

    def integrity_begin(self):
        """Call before a job. Returns a guard for `integrity_end`."""
        problems = self.integrity_check()
        if problems:
            raise RecordError("the run record was changed: " + "; ".join(problems) + ". "
                              + repair_hint(self.name))
        return {"state.json": sha256_file(os.path.join(self.path, "state.json")),
                "integrity.json": sha256_file(self._manifest_path())}

    def integrity_end(self, guard):
        """Call after a job, before the runner writes anything. Lists what the job changed."""
        return self.integrity_check() + self.guard_changes(guard)

    def guard_changes(self, guard):
        """The cheap half of `integrity_end`: has state.json or integrity.json changed?"""
        problems = []
        for name, digest in guard.items():
            path = os.path.join(self.path, name)
            if not os.path.exists(path):
                problems.append(f"{name} was removed")
            elif sha256_file(path) != digest:
                problems.append(f"{name} was changed")
        return problems

    # -- the pinned record and the record's home (W-02) ----------------------------------------

    def _pinner(self):
        """The run's RecordPin; None where the run is in no git repository, or while run.json,
        which names the repository, is not written yet. A construction that fails raises, and is
        tried again at the next call (K1). A pinned state of another run, or later than the disk's
        by more than the one save a kill between the pin and the write leaves, marks the pin moved
        (K2)."""
        with self._pin_lock:
            if self._pin is None:
                top = self._pin_top
                if top is None:
                    try:
                        top = self.info.get("git_toplevel")
                    except (OSError, ValueError):
                        return None
                if not top:
                    self._pin = False
                else:
                    pin = RecordPin(top, self.name)
                    self._check_pinned(pin)
                    self._pin = pin
            return self._pin or None

    def _check_pinned(self, pin):
        if not pin.tree:
            return
        oid = pin.pinned.get("state.json")
        pinned, found = parse_state(read_objects(pin.git, [oid]).get(oid)) if oid else (None, None)
        why = ""
        if pinned is None:
            why = "it holds no run state"
        elif pinned.get("run_id") != self.state.get("run_id"):
            why = f"it holds the state of run {pinned.get('run_id')}"
        elif found[0] > self._saved[0] + 1 or found[1] > self._saved[1] + 1:
            why = (f"its state is later than the one on disk (save {found[0]}, op {found[1]} vs "
                   f"save {self._saved[0]}, op {self._saved[1]})")
        else:
            pin.behind = found < self._saved
            pin.pinned_state = (pinned, found)
        if why:
            pin.moved = PinMoved(pin.ref, None, pin.tree, why)

    def pin_record(self, state=None, stale=None):
        """Bring `refs/code-smith/<run>/_record` up to date, with `state` the bytes the save is
        about to write. Called before state.json or integrity.json is written, so the pinned copy
        is never older than the file. A failure never fails the save: it is logged once per
        distinct error (`record-pin-failed`) and left in the state as `pin_stale` (`stale`, the
        one the save took out, keeps its time) until a pin succeeds. Returns the PinMoved that
        stops the run the first time the ref is found moved, else None; a moved ref is never
        written again by this process."""
        if self._pin_moved:
            self.state["pin_stale"] = stale or self.state.get("pin_stale")
            return None
        try:
            pin = self._pinner()
            if not pin:
                return None
            with self._pin_lock:
                if pin.moved:
                    raise pin.moved
                self._pin_now(pin, state)
        except PinMoved as exc:
            self._pin_moved = exc
            self.state["pin_stale"] = {"since": _utc_now_iso(), "error": str(exc)}
            self.event("record-pin-moved", ref=exc.ref, expected=exc.expected, found=exc.found,
                       message=str(exc))
            return exc
        except (OSError, ValueError, KeyError, TypeError, gitops.GitError) as exc:
            # A git failure is told by what git said: its argv carries the object ids of each save.
            said = (f"git {next((a for a in exc.argv if a[:1] != '-' and '=' not in a), '')}: "
                    f"{exc.stderr.strip()}" if isinstance(exc, gitops.GitError) else str(exc))
            message = f"{type(exc).__name__}: {said}"
            self.state["pin_stale"] = {"since": (stale or self.state.get("pin_stale") or {}).get(
                "since") or _utc_now_iso(), "error": message}
            if message not in self._pin_errors:
                self._pin_errors.add(message)
                self.event("record-pin-failed", exception=type(exc).__name__, message=said)
        return None

    def _pin_now(self, pin, state):
        if state is not None:
            pin.fixed["state.json"] = pin.put("blob", state)
        self._load_manifest(pin)
        if self._manifest_bytes is not None:
            pin.fixed["integrity.json"] = pin.put("blob", self._manifest_bytes)
        if "state.json" not in pin.fixed:                # a protect before this process's first save
            if "state.json" in pin.pinned and not pin.behind:
                pin.fixed["state.json"] = pin.pinned["state.json"]
            elif os.path.exists(os.path.join(self.path, "state.json")):
                with open(os.path.join(self.path, "state.json"), "rb") as fh:
                    pin.fixed["state.json"] = pin.put("blob", fh.read())
        self._pin_run_json(pin)
        files = dict(pin.fixed)
        manifest = json.loads(self._manifest_bytes) if self._manifest_bytes else {"files": {}}
        for rel, digest in manifest["files"].items():
            oid = pin.verified(rel, digest, os.path.join(self.path, rel))
            if oid:
                files[rel] = oid
        events, grown, spool = self._pin_events(pin)
        if events:
            files[EVENTS] = events
        try:
            moved = pin.move(files, paths={EVENTS: spool} if spool else None)
        finally:
            if spool:
                os.unlink(spool)
        if spool:
            pin.events = (grown, moved[EVENTS])
        if pin.deleted:
            self.event("record-pin-restored", ref=pin.ref, was=pin.deleted, now=pin.tree)
            pin.deleted = None

    def _pin_events(self, pin):
        """events.jsonl is pinned while it only grows (append-only) and still begins with its
        pinned copy. A file that grew is read once and pinned as read, so its size and object id
        agree even while an event is appended meanwhile. A file that shrank or is gone, or whose
        pinned prefix was edited, keeps the earlier pinned copy, and only that file does: the
        rest of the save is still pinned (REC-58). The edit is logged once per process
        (`record-events-changed`) and refused by `events_changed`. Memory is bounded: an
        unchanged file is only `stat`ed, a grown one streamed in `EVENTS_CHUNK` pieces, and git
        hashes the spooled copy itself (REC-64). Returns (the pinned object id to keep, or None;
        the grown size, or None; the spool file git hashes for it, or None)."""
        size, oid = pin.events
        path = os.path.join(self.path, EVENTS)
        try:
            now = os.stat(path).st_size
        except OSError:
            return oid, None, None
        if now == size or (size is not None and now < size):
            return oid, None, None                       # unchanged: nothing is read (REC-64)
        # One pass over the first `now` bytes, in bounded chunks: the pinned prefix is hashed as it
        # streams by, and all of it is spooled to a file of its own for git to hash. A line
        # appended meanwhile is past `now`, so the size and the object are one snapshot.
        prefix = _blob_hasher(pin.algo, size) if oid and size else None
        fd, spool = tempfile.mkstemp(prefix="code-smith-events-")
        try:
            with os.fdopen(fd, "wb") as out, open(path, "rb") as fh:
                copied = 0
                for chunk in _read_span(fh, now):
                    if prefix is not None and copied < size:
                        prefix.update(chunk[:size - copied])
                    out.write(chunk)
                    copied += len(chunk)
        except OSError:
            os.unlink(spool)
            return oid, None, None
        if copied < now or (prefix is not None and prefix.hexdigest() != oid):
            os.unlink(spool)
            if copied < now:
                return oid, None, None                   # it shrank under the read
            if not self._events_told:
                self._events_told = True
                self.event("record-events-changed", pinned_size=size, message=(
                    "an earlier line of events.jsonl was changed; the pinned history is kept and "
                    "`runner repair-record` puts it back, keeping the later events"))
            return oid, None, None
        return None, now, spool

    def events_changed(self):
        """["events.jsonl was changed"] when the file no longer begins with its pinned copy: an
        earlier line was edited, which neither the runner (it only appends) nor a kill does. A
        shorter file is not named: the next save keeps the pinned copy, and a repair puts it
        back in front of what was logged since."""
        try:
            pin = self._pinner()
        except (OSError, ValueError, KeyError, TypeError, gitops.GitError):
            pin = None
        size, oid = pin.events if pin else (None, None)
        if not (oid and size):
            return []
        digest, read = _blob_hasher(pin.algo, size), 0
        try:
            with open(os.path.join(self.path, EVENTS), "rb") as fh:
                for chunk in _read_span(fh, size):        # bounded, never the whole file (REC-64)
                    digest.update(chunk)
                    read += len(chunk)
        except OSError:
            return []
        if read == size and digest.hexdigest() != oid:
            return ["events.jsonl was changed"]
        return []

    def record_problems(self):
        """What makes the record unfit to be written into (K6): the integrity check, a
        state.json that differs from its pinned copy of the same save, and an events.jsonl whose
        pinned history was edited (REC-58). `repair-record` is their remedy. An open cleanup is
        not a changed record and has its own remedy (`cleanup_problems`, `cleanup_hint`)."""
        return self.integrity_check() + self.state_changed() + self.events_changed()

    def cleanup_problems(self):
        """One line per invocation whose cleanup is still open (PROC-21), counting an intent the
        first version-3 runner kept `cancelled`, which `reconcile` turns into one (PROC-25)."""
        return [f"cleanup open for '{entry['task']}' ({entry['invocation']}): "
                f"{entry['cleanup']['error']}"
                for entry in self.state.get("cleanup_obligations", [])
                if entry['cleanup']['status'] == 'open'] + [
            f"cleanup open for '{it['task']}' ({it.get('invocation_dir', it['op'])}): "
            f"{CARRIED_CANCELLED}" for it in self.state["intents"]
            if it.get("cancelled") and it["kind"] != "replay"]

    def state_changed(self):
        """["state.json was changed"] when the state on disk is not the bytes pinned for the same
        save, which only an edit makes; checked only on states this runner counted (`save_seq`)
        and while the pin holds that very save (not one later: a kill between pin and write).
        Only the state as this process found it is checked: once it saved, the disk is its own."""
        problems = []
        if self._wrote_state:
            return problems
        try:
            pin = self._pinner()
        except (OSError, ValueError, KeyError, TypeError, gitops.GitError):
            pin = None
        found = getattr(pin, "pinned_state", None)
        if pin and found and found[1] == self._saved and found[1][0] > 0:
            disk = _read_bytes(os.path.join(self.path, "state.json"))
            oid = pin.pinned.get("state.json")
            if disk is not None and _blob_id(pin.algo, disk) != oid:
                problems.append("state.json was changed")
        return problems

    def _pin_run_json(self, pin):
        """run.json changes after it is written (the totals, the agents' versions), but its
        identity never does: a new version is pinned only while its identity is the pinned one.
        Its totals are those of the state being pinned, not of the last page written."""
        if "run.json" not in pin.fixed and "run.json" in pin.pinned:
            pin.fixed["run.json"] = pin.pinned["run.json"]
        try:
            with open(os.path.join(self.path, "run.json"), "rb") as fh:
                data = dump_json(run_json_for(json.loads(fh.read()), self.state))
        except (FileNotFoundError, ValueError, TypeError, AttributeError):
            return                                   # gone or not run.json: the pinned copy stays
        if data == pin.run_json:
            return
        if pin.identity is None and "run.json" in pin.pinned:
            pin.identity = _identity(read_objects(pin.git, [pin.pinned["run.json"]])
                                     [pin.pinned["run.json"]], also=("agents",))
            pin.fixed["run.json"] = pin.pinned["run.json"]
        identity = _identity(data, also=("agents",))
        if pin.identity is None or identity == pin.identity:
            pin.identity, pin.run_json = identity, data
            pin.fixed["run.json"] = pin.put("blob", data)

    def _rebuild_task_dirs(self):
        """After a repair: every task's directory exists, and a missing task.json is written
        again from the frozen, protected workflow.expanded.json. Returns the files written."""
        try:
            defs = {t["id"]: t for t in read_json(os.path.join(self.path, "workflow.expanded.json"))["tasks"]}
        except (OSError, ValueError, KeyError):
            defs = {}
        written = []
        for task_id in self.state["order"]:
            tdir = self.task_dir(task_id)
            os.makedirs(tdir, exist_ok=True)
            target = os.path.join(tdir, "task.json")
            if task_id in defs and not os.path.exists(target):
                write_durable(target, dump_json(defs[task_id]))
                written.append(self._rel(target))
        return written

    def _keep_home(self):
        """Before a save: the run directory, and the runs directory's ignore file when the runs
        directory is inside the work tree, are put back if a writer removed them (W-02)."""
        os.makedirs(self.path, exist_ok=True)
        try:
            pin = self._pinner()
        except (OSError, ValueError, KeyError, TypeError, gitops.GitError):
            pin = None                                   # the pin reports it (`record-pin-failed`)
        if not pin:
            return
        runs = os.path.realpath(os.path.dirname(os.path.dirname(self.path)))
        if not runs.startswith(pin.git.top + os.sep):
            return
        if ".gitignore" in dress_runs_dir(runs):
            self.event("record-ignore-restored", was="missing", path=os.path.relpath(
                os.path.join(runs, ".gitignore"), pin.git.top).replace(os.sep, "/"))

    # -- pause requests -----------------------------------------------------------------------

    PAUSE_FILE = "pause.requested"

    def pause_path(self):
        return os.path.join(self.path, self.PAUSE_FILE)

    def request_pause(self, by="owner"):
        """`runner pause` leaves this file; the engine reads it before starting any call."""
        write_durable(self.pause_path(), dump_json({"requested_at": _utc_now_iso(), "by": by}))

    def pause_requested(self):
        return os.path.exists(self.pause_path())

    def clear_pause(self):
        try:
            os.remove(self.pause_path())
        except FileNotFoundError:
            pass

    def integrity_check(self):
        problems = []
        try:
            disk = _read_bytes(self._manifest_path())
            manifest = self._trusted_manifest()
        except (ValueError, KeyError, TypeError):
            return ["integrity.json is malformed"]
        if disk is None:
            return ["integrity.json was removed"]
        # A disk manifest that differs from the trusted one is not itself a problem: a kill in
        # `protect` between the pin and the write leaves exactly that (REC-59). Each covered file
        # is checked against the trusted entry, so a paired edit still names its file (REC-55),
        # and `reconcile` or the next `protect` writes the trusted manifest back.
        for rel, digest in sorted(manifest["files"].items()):
            path = os.path.join(self.path, rel)
            if not os.path.exists(path):
                problems.append(f"{rel} was removed")
            elif sha256_file(path) != digest:
                problems.append(f"{rel} was changed")
        return problems

    # -- derived files -----------------------------------------------------------------

    def regenerate_safely(self, full=False):
        """Rendering must not abort a saved decision. The CLI calls regenerate directly. A record
        whose run.json is gone (removed under the runner) is not rendered: the `record-lost` stop
        says what happened, and a page that cannot find run.json would only add a second fault."""
        if self.record_gone():
            return
        try:
            self.regenerate(full=full, guarded=True)
        except Exception as exc:                            # noqa: BLE001 - never hurt the run
            self._render_failed(exc, self._render_target)

    def record_gone(self):
        return not os.path.exists(os.path.join(self.path, "run.json"))

    def _render_failed(self, exc, target):
        page = target["page"]
        if page not in self._render_failures:
            self.event("render-failed", **target, exception=type(exc).__name__, message=str(exc))
            self._render_failures.add(page)

    def _render_file(self, path, renderer, *args, task=None, guarded=False):
        """Write one derived page. `guarded`: a page that raises is reported (`render-failed`,
        once until it renders again) and the others are still written; returns whether this one
        was (REC-44). Unguarded, the error reaches the caller (`status`)."""
        self._render_target = {"page": self._rel(path), **({"task": task} if task else {})}
        try:
            self._write(path, renderer(*args))
        except Exception as exc:                            # noqa: BLE001 - one page, not the rest
            if not guarded:
                raise
            self._render_failed(exc, self._render_target)
            return False
        self._render_failures.discard(self._rel(path))
        return True

    def regenerate(self, full=False, guarded=False):
        """Refresh run pages and changed task subtrees; full rebuilds every directory index.
        Fingerprints advance only after the task page and all its indexes were written.
        `guarded` (the engine's `regenerate_safely`): each task page and index is written under
        its own guard, so one that raises does not stop the others (REC-44)."""
        self._render_target = {"page": "run.json"}
        with open(os.path.join(self.path, "run.json"), "rb") as fh:
            was = fh.read()
        info = run_json_for(json.loads(was.decode("utf-8")), self.state)
        if dump_json(info) != was:
            write_durable(os.path.join(self.path, "run.json"), dump_json(info))
        self._render_failures.discard("run.json")
        self._render_target = {"page": "STATUS.md"}
        self.write_status(info, branch_disposition(info, self.state), self.runner_alive())
        self._render_file(os.path.join(self.path, 'follow-ups.json'), findings.followups_json,
                          self.state, guarded=guarded)
        rebuild = full or not self._rendered_full
        for task_id in self.state["order"]:
            task = self.state["tasks"][task_id]
            digest = hashlib.sha256(dump_json(task)).hexdigest()
            if not rebuild and self._render_fingerprints.get(task_id) == digest:
                continue
            tdir = self.task_dir(task_id)
            written = self._render_file(os.path.join(tdir, "STATUS.md"), render_task_status,
                                        task_id, task, tdir, self.name, task=task_id, guarded=guarded)
            for dirpath, dirnames, _files in os.walk(tdir):
                dirnames.sort()
                written = self._render_file(os.path.join(dirpath, "index.json"), self._index_text,
                                            dirpath, task=task_id, guarded=guarded) and written
            if written:                     # a task whose page failed is tried again next save
                self._render_fingerprints[task_id] = digest
        if rebuild:
            task_dirs = {self.task_dir(t) for t in self.state["order"]}
            all_written = True
            for dirpath, dirnames, _files in os.walk(self.path):
                # Task subtrees were handled above; retired tasks still need full rebuilds.
                dirnames[:] = sorted(d for d in dirnames if os.path.join(dirpath, d) not in task_dirs)
                all_written = self._render_file(os.path.join(dirpath, "index.json"), self._index_text,
                                                dirpath, guarded=guarded) and all_written
            self._rendered_full = all_written
        else:
            self._render_file(os.path.join(self.path, "index.json"), self._index_text, self.path,
                              guarded=guarded)

    def _index_text(self, directory):
        return dump_json(render_index(self.path, directory)).decode("utf-8")

    def runner_alive(self):
        """Is a runner working on this run right now: the runs directory's lock names this run and
        the process it names is alive. Read by the status page, which otherwise cannot tell an
        operation in flight from one a dead runner left behind (both are open intents)."""
        info = self.info
        holder = Lock(os.path.dirname(os.path.dirname(self.path)),
                      top=info.get("git_toplevel")).holder() or {}
        return holder.get("run_id") == info.get("run_id") and is_alive(holder.get("process"))

    def write_status(self, info, disposition, alive, now=None, tmp_suffix=".tmp"):
        """STATUS.md and STATUS.html from one model of the page, each replaced whole."""
        self._render_target = {"page": "STATUS.md"}
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if self.refresh_s is None:
            try:
                frozen = read_json(os.path.join(self.path, "workflow.expanded.json"))["defaults"]
                self.refresh_s = int(frozen.get("status_refresh_s", HEARTBEAT_S))
            except (OSError, ValueError, KeyError, TypeError):
                self.refresh_s = HEARTBEAT_S
        model = run_status_model(info, self.state, disposition, now=now, run_path=self.path,
                                 alive=alive, refresh_s=self.refresh_s)
        with self._status_lock:
            for name, renderer, args in (
                    ("STATUS.md", markdown_status, (model,)),
                    ("STATUS.html", html_status, (model, info, self.state, self.path, alive))):
                self._render_target = {"page": name}
                self._write(os.path.join(self.path, name), renderer(*args), tmp_suffix)
                self._render_failures.discard(name)

    def refresh_status(self, now=None):
        """The heartbeat: rewrite the run's STATUS.md and STATUS.html alone, with the age of what
        is in flight. Nothing changes in the state while one long agent call runs, so without
        this the files look dead for half an hour. Observational: it never touches the state, and
        a failure to render (the engine may be changing the state under us) records a failed beat."""
        if self.record_gone():
            return False
        try:
            self.write_status(self.info, None, self.runner_alive(), now=now, tmp_suffix=".beat")
        except Exception as exc:                            # noqa: BLE001 - never hurt the run
            self._render_failed(exc, self._render_target)
            return False
        return True

    @staticmethod
    def _write(path, text, tmp_suffix=".tmp"):
        """A derived file, replaced whole: a kill leaves the old one, never an empty one."""
        tmp = path + tmp_suffix
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)


# -- reconciliation --------------------------------------------------------------------

def reconcile(run, git, stop_orphans=False, crash=_no_crash, grace_s=5.0, abandon=None):
    """Settle every intent that has no outcome, before anything else happens. Returns what was
    done, in words. Raises ReconcileError when the repository is not what the state expects.
    An open cleanup is closed when its group is gone; a live group is stopped only with
    `stop_orphans`, and `abandon` (a person's ruling: `by` and how it was given) closes it
    without a signal (PROC-23). `abandon` may be a callable that returns that ruling or raises
    its refusal; it is called once the record has passed the integrity check, before any effect
    (PROC-27). With `abandon`, `stop_orphans` stops only the groups of open agent and command
    intents, never an abandoned obligation's; without `stop_orphans` such a live group refuses
    before the ruling is recorded, so a refusal records nothing (PROC-28)."""
    done = []
    found = run.integrity_check()
    # Publications may precede their manifest entries. A replan's installed files must match
    # its protected staging copies; unrelated edits still refuse before reconciliation writes.
    pending = {it['path'] for it in run.state['intents'] if it['kind'] == 'decision'}
    manifest = ({} if any(p.startswith('integrity.json ') for p in found)
                else run._trusted_manifest()['files'])
    for it in run.state['intents']:
        if it['kind'] != 'replan':
            continue
        prefix = it['directory'] + '/after/'
        for rel, digest in manifest.items():
            if not rel.startswith(prefix):
                continue
            target = rel[len(prefix):]
            if target not in ('workflow.toml', 'workflow.expanded.json') and not target.startswith(
                    ('library/', 'briefs/')):
                continue
            path = os.path.join(run.path, target)
            if not os.path.exists(path) or sha256_file(path) == digest:
                pending.add(target)
    problems = [p for p in found
                if not any(p in (f"{path} was changed", f"{path} was removed") for path in pending)]
    if problems:
        raise ReconcileError("the run record was changed: " + "; ".join(problems) + ". "
                             + repair_hint(run.name))
    if callable(abandon):
        abandon = abandon()
    if abandon is not None and not stop_orphans:
        # Checked before the ruling is recorded: the refusal changes nothing (PROC-28).
        for it in run.state["intents"]:
            if (it["kind"] in ("agent", "command") and (it.get("process") or {}).get("group")
                    and not it.get("cancelled")):
                _reconcile_orphan(it, False, grace_s)
    done += _carried_cancelled(run)
    disk = _read_bytes(run._manifest_path())
    if run._manifest_bytes is not None and disk is not None and disk != run._manifest_bytes:
        run.protect()          # the trusted manifest back, with `integrity-manifest-replaced` (REC-59)
        done.append("integrity.json: written again from the runner's own copy")
    for entry in run.state.get('cleanup_obligations', []):
        cleanup = entry['cleanup']
        if cleanup['status'] != 'open':
            continue
        if abandon is not None:
            # A person's ruling that the group is gone or may be left (PROC-23): nothing is
            # signalled, and the ruling is kept with the obligation it closed.
            cleanup['status'] = 'abandoned'
            entry['ruling'] = dict(abandon)
            _reset_commands(run, entry)
            run.save()
            # The ruling's time is `ruled_at`: the event's own `at` is the record's stamp (REC-60).
            run.event('cleanup-abandoned', task=entry['task'], invocation=entry['invocation'],
                      **{k: v for k, v in abandon.items() if k != 'at'}, ruled_at=abandon['at'])
            done.append(f"{entry['invocation']}: cleanup abandoned by {abandon.get('by', '?')}; its "
                        "group was not signalled" + ("; --stop-orphans stops only the groups of "
                                                     "interrupted calls" if stop_orphans else ""))
            continue
        identity = cleanup['group']
        try:
            alive = bool(identity) and is_alive(identity)
            if alive and not stop_orphans:
                raise CleanupOpen(run)            # signalled only with --stop-orphans (PROC-19/20)
            stopped = bool(identity) and (not alive or stop_process_group(identity, grace_s))
        except CleanupOpen:
            raise
        except (RecordError, OSError) as exc:
            raise CleanupOpen(run, "cleanup retry failed: " + str(exc)) from exc
        if not stopped:
            raise CleanupOpen(run, "could not close it")
        cleanup['status'] = 'closed'
        _reset_commands(run, entry)
        run.save()
        run.event('cleanup-closed', task=entry['task'], invocation=entry['invocation'])
        done.append(f"{entry['invocation']}: cleanup closed")
    for it in run.state["intents"]:
        if it["kind"] in ("agent", "command") and (it.get("process") or {}).get("group"):
            _reconcile_orphan(it, stop_orphans, grace_s)
    git.runs_dir = os.path.dirname(os.path.dirname(run.path))    # never restored (W-02)
    expect = run.state.get("expect")
    if expect:                                           # the run was paused holding the tree
        tip = git.head()
        if tip != expect["tip"] and not _export_committed(run, git, expect["tip"], tip):
            raise ReconcileError(f"while the run was paused the branch tip changed: expected "
                                 f"{expect['tip']}, found {tip}")
        tree = git.snapshot(run.index_file)
        if tree != expect["tree"]:
            changed = [p for _s, p, _o, _n in git.changed_paths(expect["tree"], tree)]
            raise ReconcileError("while the run was paused the work tree changed: "
                                 + ", ".join(changed))
    # A replay's checkout is removed last, once any command still running in it has been settled.
    for it in sorted(run.state["intents"], key=lambda it: it["kind"] == "replay"):
        handler = _RECONCILERS.get(it["kind"])
        if handler is None:
            raise ReconcileError(f"intent {it['op']} has unknown kind '{it['kind']}'")
        it["reconciled"] = True              # its wall-clock span includes the downtime: not timed
        done.append(handler(run, git, it, stop_orphans=stop_orphans, crash=crash, grace_s=grace_s))
    run.write_findings()
    run.regenerate_safely(full=True)
    return done


def _export_committed(run, git, paused, tip):
    """True when the branch moved since the stop only by the owner's commit of the rulings files
    `runner export-rulings` wrote (RUN-81): nothing in the run can change the tree, the tip
    descends from the one the stop left, and the commits since change only those files. The
    tree check that follows still holds the files as the export wrote them."""
    from .status import export_live
    if export_live(run.state):
        return False
    default = None
    if any(e.get("written") and not e.get("file") for e in run.state.get("standing_pending", [])):
        try:
            frozen = read_json(os.path.join(run.path, "workflow.expanded.json"))
            default = frozen.get("defaults", {}).get("rulings_file")
        except (OSError, ValueError):
            default = None
    files = findings.exported_files(run.state, default)
    if not files or git.run("merge-base", "--is-ancestor", paused, tip, check=False).returncode:
        return False
    changed = {p for _s, p, _o, _n in git.changed_paths(git.tree_of(paused), git.tree_of(tip))}
    return bool(changed) and changed <= files


def _reset_commands(run, entry):
    """A closed or abandoned cleanup of a command job: its commands may have stopped between two
    gates, so the job runs again."""
    for st in run.state['tasks'].values():
        for job in (st.get('panel') or {}).get('jobs', []):
            if job['task'] == entry['task'] and job['kind'] != 'review':
                job.pop('raw_outcome', None)
                job['result'] = None


def _reconcile_commit(run, git, it, crash, **_):
    op, task = it["op"], it["task"]
    tip = git.head()
    if git.find_operation(tip, op):
        if git.tree_of(tip) != it["candidate"]:
            raise ReconcileError(f"commit {tip} carries operation {op} but its tree is not the "
                                 f"candidate {it['candidate']}")
        git.sync_index()                                 # the crash may have come before this step
        commit, what = tip, "the commit had been made; recorded the acceptance"
    elif tip == it["parent"]:
        tree = git.snapshot(run.index_file)
        if tree != it["candidate"]:
            raise ReconcileError(f"the work tree is {tree}, not the candidate {it['candidate']} "
                                 f"that was about to be committed for '{task}'")
        commit = git.commit_candidate(it["candidate"], it["paths"], it["subject"],
                                      run.state["run_id"], task, op, it["parent"],
                                      it.get("extra_trailer", ""), crash=crash)
        what = "the commit had not been made; committed the verified candidate"
    else:
        raise ReconcileError(f"task '{task}' was about to be committed on {it['parent']}, but the "
                             f"branch tip is {tip}, which does not carry operation {op}. Someone "
                             "changed the branch")
    message = git.commit_message(it["subject"], run.state["run_id"], task, op,
                                 it.get("extra_trailer", ""))
    run.record_acceptance(task, commit, git.commit_files(commit), message)
    run.finish(op, commit=commit)
    return f"{op} commit of '{task}': {what} ({commit[:7]})"


def _reconcile_restore(run, git, it, crash, **_):
    git.restore(it["target"], it["paths"], expected_tree=it.get("expected", it["target"]),
                index_file=run.index_file, crash=crash)
    run.finish(it["op"], restored=len(it["paths"]))
    return f"{it['op']} restore: run again from the pinned target and verified"


def _reconcile_recover(run, git, it, crash, **_):
    """Put the set-aside work back again from the pinned candidate and open the transaction from
    the intent, whatever the state already shows: only the intent says the recovery is done."""
    task, head = it["task"], git.head()
    if head != it["head"]:
        raise ReconcileError(f"the set-aside work of '{task}' was being put back on commit "
                             f"{it['head']}, but the branch tip is now {head}. Nothing was restored")
    if run.state.get("expect"):
        # A run stopped by a failed restore expected the tree it left behind. `reconcile` has
        # checked it; the replay below changes the tree, so a crash in it must not leave that
        # expectation to refuse the next `resume`. The intent's `head` still guards the branch.
        run.state["expect"] = None
        run.save()
    git.restore(it["target"], it["paths"], expected_tree=it["expected"],
                index_file=run.index_file, crash=crash)
    from . import engine
    engine.open_transaction(run, git, task, it["base"],
                            {"record": {"attempt": it["attempt"]}, "paths": it["paths"]}, it["op"])
    return (f"{it['op']} recovery of '{task}': the set-aside work of attempt {it['attempt']} is "
            "back, verified, and the transaction is open")


def _reconcile_decision(run, git, it, **_):
    run._write_decision_now(os.path.join(run.path, it["path"]), it["payload"])
    run.finish(it["op"], path=it["path"])
    return f"{it['op']} decision {it['path']}: written again from the intent"


def _reconcile_pin(run, git, it, **_):
    git.pin(run.name, it["name"], it["object"])
    run.finish(it["op"], ref=it["name"])
    return f"{it['op']} pin {it['name']}: repeated"


def _reconcile_patch(run, git, it, **_):
    write_durable(os.path.join(run.path, it["path"]), git.full_patch(it["base"], it["candidate"]))
    run.finish(it["op"], path=it["path"])
    return f"{it['op']} patch {it['path']}: regenerated from the pinned trees"


def _reconcile_close(run, git, it, **_):
    run.close_directory(os.path.join(run.path, it["dir"]))
    run.finish(it["op"], dir=it["dir"])
    return f"{it['op']} close {it['dir']}: hashed again"


def _reconcile_revert(run, git, it, crash, **_):
    made = git.revert_commits(it["commits"], it["op"], run.state["run_id"], it["since"], crash=crash)
    run.finish(it["op"], reverts=made)
    return f"{it['op']} reopen: {len(made)} revert commits are on the branch"


def _reconcile_orphan(it, stop_orphans, grace_s):
    identity, task = it.get("process"), it["task"]
    if is_alive(identity):
        if not stop_orphans:
            raise OrphanAlive(identity, task, it.get("invocation_dir", it["op"]))
        if not stop_process_group(identity, grace_s):
            pids = ", ".join(map(str, invocation_pids(identity)))
            raise ReconcileError(f"could not stop invocation {it.get('invocation_dir', it['op'])} "
                                 f"of '{task}' (pids {pids}); a verified process identity is missing or "
                                 "the group did not stop")


CARRIED_CANCELLED = "its group outlived its stop when it was cancelled before release"


def _carried_cancelled(run):
    """An intent the first version-3 runner kept for a reader cancelled before release whose
    group outlived its stop (`cancelled`: the outcome it was settled to). It is that failed
    cleanup, so it becomes what this runner records at once (PROC-25): the intent finished with
    that outcome and an open obligation for its group, which PROC-23's remedies then close. Its
    replay intent stays, as PROC-22's, for its checkout to be removed after the obligation
    closes."""
    done = []
    for it in list(run.state["intents"]):
        kept = it.pop("cancelled", None)
        if kept is None or it["kind"] == "replay":
            continue
        it["reconciled"] = True              # the cancel time was not recorded: no times
        run.finish(it["op"], **dict(kept, cleanup={
            "status": "open", "group": it.get("process"), "error": CARRIED_CANCELLED}))
        done.append(f"{it['op']} {it['kind']} of '{it['task']}': cancelled before release; its "
                    "group's cleanup is open")
    return done


def _reconcile_agent(run, git, it, stop_orphans, grace_s, **_):
    _reconcile_orphan(it, stop_orphans, grace_s)
    task = it["task"]
    inv = os.path.join(run.path, it["invocation_dir"])
    outcome = os.path.join(inv, "outcome.json")
    # Completion is unknown, so it is never success: the engine had not recorded an outcome.
    os.makedirs(inv, exist_ok=True)
    from . import budgets, agents
    # What the call used is read from the provider's own record when there is one.
    since = agents._epoch(it.get("at"))
    usage = agents.partial_usage(it.get("agent_kind", ""), inv, since) if since is not None else {}
    result = agents.AgentResult(agents.INTERRUPTED, usage=usage,
                                usage_source="provider-record" if usage else "unknown",
                                limits=agents.partial_limits(it.get("agent_kind", ""), inv, since) or None)
    profile = _profile_of(run, it.get("agent"))
    if profile and profile.get("kind") == it.get("agent_kind"):
        agents.make(it["agent"], profile).priced(result)
    extra = {k: v for k, v in result.outcome().items() if k in ("limits", "estimated_usd", "estimated_counts")}
    write_durable(outcome, dump_json({"status": "interrupted",
                                      "reason": "the runner stopped while this call was running",
                                      "usage": usage, "usage_source": result.usage_source, **extra}))
    budgets.settle(run.state, task, it.get("reservation", 0), result, it.get("token_reservation", 0),
                   call={"invocation": it["invocation_dir"]}, token_remainder=it.get("token_remainder", 0))
    run.state["tasks"][task]["session_id"] = None        # the session is abandoned
    run.finish(it["op"], status="interrupted", usage=usage)
    return (f"{it['op']} agent call of '{task}': marked interrupted; its session is abandoned"
            + (f"; it had used {usage['tokens_in']} tokens in, {usage['tokens_out']} out" if usage
               else "; its usage is unknown"))


def _profile_of(run, name):
    """The frozen [agents.NAME] profile of the run, or None (an intent recorded before intents
    named their profile, or a profile a replan removed)."""
    try:
        return read_json(os.path.join(run.path, "workflow.expanded.json"))["agents"].get(name) if name else None
    except (OSError, ValueError, KeyError):
        return None


def _reconcile_command(run, git, it, stop_orphans, grace_s, **_):
    _reconcile_orphan(it, stop_orphans, grace_s)
    task = it["task"]
    run.finish(it["op"], result="interrupted")
    return f"{it['op']} command of '{task}': marked interrupted; verification must run again"


def _reconcile_branch(run, git, it, **_):
    name = it["name"]
    if git.current_branch() != name:
        if git.branch_exists(name):
            raise ReconcileError(f"the run branch '{name}' exists but is not checked out")
        git.create_and_checkout_run_branch(name)
    run.finish(it["op"], branch=name)
    return f"{it['op']} run branch {name}: checked out"


def _reconcile_replay(run, git, it, **_):
    """The acceptance replay's throwaway checkout is removed; `verify` runs again whole. Only a
    directory the runner named as its own is removed."""
    path = it.get("dir", "")
    if os.path.basename(path).startswith("code-smith-replay-") and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    run.finish(it["op"], result="interrupted")
    return f"{it['op']} acceptance replay of '{it['task']}': its checkout is removed; verification runs again"


def _reconcile_replan(run, git, it, crash, **_):
    from . import replan
    try:
        return replan.apply(run, git, it, crash)
    except replan.Refused as exc:
        raise ReconcileError(str(exc)) from exc


_RECONCILERS = {"replan": _reconcile_replan, "branch": _reconcile_branch,
                "commit": _reconcile_commit, "restore": _reconcile_restore, "pin": _reconcile_pin,
                "patch": _reconcile_patch, "close": _reconcile_close, "revert": _reconcile_revert,
                "agent": _reconcile_agent, "command": _reconcile_command,
                "decision": _reconcile_decision, "recover": _reconcile_recover,
                "replay": _reconcile_replay}


def branch_disposition(info, state):
    """For a finished run on its own branch: is its last accepted commit in the branch it came
    from, and in that branch's upstream? None when there is nothing to say or git cannot tell.
    Observed, never decided: nothing in the engine reads it."""
    if state["status"] != "done" or info.get("branch_mode") == "current":
        return None
    commits = [state["tasks"][t].get("commit") for t in state["order"]]
    commits = [c for c in commits if c]
    target = info.get("original_branch")
    if not commits or not target:
        return None
    try:
        git = gitops.Git(info["git_toplevel"])
    except gitops.GitError:
        return None
    last = commits[-1]

    def contains(ref):
        if git.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).returncode:
            return None
        return git.run("merge-base", "--is-ancestor", last, ref, check=False).returncode == 0

    merged = contains("refs/heads/" + target)
    if merged is None:
        return None
    res = git.run("rev-parse", "--abbrev-ref", "--symbolic-full-name", target + "@{upstream}",
                  check=False)
    upstream = res.stdout.decode("utf-8", "replace").strip() if res.returncode == 0 else ""
    return {"commit": last, "target": target, "merged": merged, "upstream": upstream or None,
            "pushed": contains("refs/remotes/" + upstream) if merged and upstream else None}
