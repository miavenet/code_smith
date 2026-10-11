"""Git facts and git effects. This module decides nothing: it reports and it does what it is told.

Everything rests on one primitive, `snapshot()`: the whole work tree hashed into a git tree id,
through a scratch index so the owner's index is never touched (05, Git). All paths are relative to
the top of the repository and are handled NUL-separated, so odd names are safe.

The runner never rewrites history, never discards the owner's changes wholesale and never talks to
a remote; the only git subcommands used are the ones spelled out in this file (GIT-04).
"""

import base64
import os
import shutil
import stat
import subprocess
import tempfile

GITLINK = "160000"
ZERO = "0" * 40
REF_PREFIX = "refs/code-smith/"

# Every git argv this module runs is appended here when it is a list. Tests use it (GIT-04).
TRACE = None


class GitError(Exception):
    """A git command failed."""

    def __init__(self, argv, returncode, stderr):
        self.argv, self.returncode, self.stderr = argv, returncode, stderr
        super().__init__(f"git {' '.join(argv)} failed ({returncode}): {stderr.strip()}")


class RestoreError(Exception):
    """A restore could not be completed or verified. An environment failure: stop, leave the tree."""

    def __init__(self, message, paths=()):
        self.paths = list(paths)
        super().__init__(message + ("".join(f"\n  {p}" for p in self.paths)))


class CommitRefused(Exception):
    """The tree that would be committed is not the verified candidate, or the branch tip moved."""


class RevertConflict(Exception):
    """A revert during a reopen conflicted, or could not start because the index or the work tree
    has changes of the owner's. A conflicting revert was aborted; nothing further was changed."""

    def __init__(self, commit, detail):
        self.commit = commit
        super().__init__(f"reverting {commit} conflicts: {detail.strip()}")


def _no_crash(point):
    return None


def _text(data):
    return data.decode("utf-8", errors="surrogateescape")


def _bytes(text):
    return text.encode("utf-8", errors="surrogateescape")


def find_toplevel(path):
    """The nearest directory at or above `path` that holds `.git`, or ''."""
    top = os.path.abspath(path)
    while not os.path.exists(os.path.join(top, ".git")):
        parent = os.path.dirname(top)
        if parent == top:
            return ""
        top = parent
    return top


class Git:
    def __init__(self, root):
        self.top = find_toplevel(root)
        if not self.top:
            raise GitError(["rev-parse"], 128, f"'{root}' is not inside a git repository")
        self.top = os.path.realpath(self.top)
        # The run record's directory, set by whoever works on a run: a restore never touches it.
        self.runs_dir = None

    # -- running git -------------------------------------------------------------------------

    def env(self, index=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C", GIT_OPTIONAL_LOCKS="0")
        if index:
            env["GIT_INDEX_FILE"] = index
        return env

    def run(self, *args, index=None, stdin=None, check=True, extra_env=None):
        # The owner pointed the runner at this repository, so trust exactly it. The runner's own
        # operations never trigger the owner's hooks.
        argv = ["-c", f"safe.directory={self.top}", "-c", "core.hooksPath=/dev/null",
                "-c", "core.quotepath=false", "-C", self.top, *args]
        if TRACE is not None:
            TRACE.append(list(args))
        env = self.env(index)
        if extra_env:
            env.update(extra_env)
        res = subprocess.run(["git", *argv], input=stdin, capture_output=True, env=env)
        if check and res.returncode != 0:
            raise GitError(list(args), res.returncode, _text(res.stderr))
        return res

    def out(self, *args, **kw):
        return _text(self.run(*args, **kw).stdout).strip()

    def identity_env(self):
        """Commits need an author. Use the repository's; fall back to a fixed one."""
        env = {}
        if not self.out("config", "user.name", check=False):
            env.update(GIT_AUTHOR_NAME="code-smith", GIT_COMMITTER_NAME="code-smith")
        if not self.out("config", "user.email", check=False):
            env.update(GIT_AUTHOR_EMAIL="code-smith@localhost",
                       GIT_COMMITTER_EMAIL="code-smith@localhost")
        return env

    # -- facts -------------------------------------------------------------------------------

    def head(self):
        return self.out("rev-parse", "--verify", "HEAD")

    def tree_of(self, commit):
        return self.out("rev-parse", "--verify", f"{commit}^{{tree}}")

    def current_branch(self):
        """The checked-out branch, or None when HEAD is detached."""
        res = self.run("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return _text(res.stdout).strip() or None if res.returncode == 0 else None

    def is_clean(self):
        """No staged change, no unstaged change, no untracked file that git does not ignore."""
        return self.dirty_paths() == []

    def dirty_paths(self):
        raw = self.run("status", "--porcelain", "-z", "--untracked-files=all").stdout
        return [_text(e) for e in raw.split(b"\0") if e]

    def git_path(self, name):
        """Where git keeps `name` for this work tree. Asked once per name: the answer depends only
        on the repository's layout, and the lease check reads a dozen of these at every step."""
        cache = self.__dict__.setdefault("_git_paths", {})
        if name not in cache:
            path = self.out("rev-parse", "--git-path", name)
            cache[name] = path if os.path.isabs(path) else os.path.join(self.top, path)
        return cache[name]

    # -- snapshots ---------------------------------------------------------------------------

    def snapshot(self, index_file):
        """Tree id of the whole work tree: tracked files and untracked files git does not ignore.

        `index_file` is a scratch index that is reused between calls, so git's stat cache spares
        re-hashing unchanged files. The real index and the work tree are not touched.
        """
        if not os.path.exists(index_file):
            os.makedirs(os.path.dirname(index_file), exist_ok=True)
        # Start from HEAD each time: `add -A` never re-adds a tracked file that .gitignore also
        # matches once a deletion dropped it from the index. A one-tree `--reset` keeps the stat
        # data of every entry that still matches HEAD, so unchanged files are not hashed again.
        self.run("read-tree", "--reset", "HEAD", index=index_file)
        self.run("add", "-A", "--", ".", index=index_file)
        return self.out("write-tree", index=index_file)

    def embedded_repositories(self):
        """Directories below the top that hold their own `.git`. `git add` would turn them into
        submodule entries, or fail outright when they have no commit, so look before a snapshot.
        One that git ignores is left alone: `git add` never sees it, and it is usually a build
        directory's fetched dependency (CMake FetchContent clones into the build tree)."""
        found = []
        runs_override = os.environ.get("CODE_SMITH_RUNS_DIR", "")
        if runs_override:
            runs_override = os.path.realpath(os.path.join(self.top, os.path.expanduser(runs_override)))
        for dirpath, dirnames, filenames in os.walk(self.top):
            rel = os.path.relpath(dirpath, self.top)
            if rel == ".":
                dirnames[:] = [d for d in dirnames if d not in (".git", ".runs")]
                continue
            if runs_override and os.path.realpath(dirpath) == runs_override:
                dirnames[:] = []                          # a relocated runs directory (--runs-dir)
                continue
            if ".git" in dirnames or ".git" in filenames:
                found.append(rel)
                dirnames[:] = []
        ignored = self.check_ignored([rel + "/" for rel in found])
        return sorted(rel for rel in found if rel + "/" not in ignored)

    def gitlinks(self, tree):
        """Paths in `tree` that are submodule entries or embedded repositories (mode 160000)."""
        return [path for path, (mode, _sha) in self.ls_tree(tree).items() if mode == GITLINK]

    def remove_embedded(self, rel):
        """Remove an embedded repository an attempt left behind, then the directories it emptied."""
        full = self._inside(rel)
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RestoreError("not a directory, so not an embedded repository", [rel])
        if not os.path.lexists(os.path.join(full, ".git")):
            raise RestoreError("holds no .git, so it is not an embedded repository", [rel])
        self._check_parents(rel)
        shutil.rmtree(full)
        self._prune_empty_parents(rel)

    def ls_tree(self, tree):
        """path -> (mode, sha) for every entry of `tree`, recursively."""
        raw = self.run("ls-tree", "-r", "-z", "--full-tree", tree).stdout
        entries = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            meta, path = item.split(b"\t", 1)
            mode, _type, sha = _text(meta).split(" ")
            entries[_text(path)] = (mode, sha)
        return entries

    def changed_paths(self, tree_a, tree_b):
        """What differs between two trees: a list of (status, path, old_mode, new_mode).

        Status is A, D, M or T. Renames are not detected, so every path stands for itself.
        """
        raw = self.run("diff-tree", "-r", "-z", "--no-renames", tree_a, tree_b).stdout
        fields = raw.split(b"\0")
        changes = []
        i = 0
        while i + 1 < len(fields):
            meta = _text(fields[i])
            if not meta.startswith(":"):
                i += 1
                continue
            old_mode, new_mode, _old, _new, status = meta[1:].split(" ")
            changes.append((status[0], _text(fields[i + 1]), old_mode, new_mode))
            i += 2
        return changes

    def check_ignored(self, paths):
        """path -> 'source:line: pattern' for each of `paths` that git ignores."""
        if not paths:
            return {}
        res = self.run("check-ignore", "-v", "-z", "--stdin",
                       stdin=b"".join(_bytes(p) + b"\0" for p in paths), check=False)
        if res.returncode not in (0, 1):
            raise GitError(["check-ignore"], res.returncode, _text(res.stderr))
        fields = [_text(f) for f in res.stdout.split(b"\0")]
        ignored = {}
        for i in range(0, len(fields) - 3, 4):
            source, line, pattern, path = fields[i:i + 4]
            if pattern and not pattern.startswith("!"):
                ignored[path] = f"{source}:{line}: {pattern}"
        return ignored

    def ignored_paths(self):
        """Every path in the work tree that git ignores, from the top, a wholly ignored directory
        once as `dir/`; the run record is left out. These are the files a clean checkout lacks."""
        raw = self.run("ls-files", "--others", "--ignored", "--exclude-standard", "--directory",
                       "-z").stdout
        return sorted(p for p in (_text(e) for e in raw.split(b"\0") if e)
                      if not self.in_runs_dir(p.rstrip("/")))

    def replay_checkout(self, tree, parent, dest):
        """A throwaway checkout of exactly `tree` at `dest`, in the git state the live gates see:
        a shared clone (no object is copied), the files of `tree` in its work tree, HEAD detached
        at `parent` and the index at `parent`, so the candidate's changes are uncommitted there as
        they are in the work tree. Nothing is written to this repository; its objects are only
        read."""
        env = self.env()

        def git(*args, cwd=None):
            argv = ["git", "-c", "core.hooksPath=/dev/null", *args]
            res = subprocess.run(argv, cwd=cwd, capture_output=True, env=env)
            if res.returncode:
                raise GitError(list(args), res.returncode, _text(res.stderr))
            return _text(res.stdout).strip()

        git("clone", "-q", "--shared", "--no-checkout", "--", self.top, dest)
        git("read-tree", "--reset", "-u", tree, cwd=dest)          # the candidate's files
        git("update-ref", "--no-deref", "HEAD", parent, cwd=dest)
        git("read-tree", parent, cwd=dest)
        # A clone has none of the repository's own settings; the identity is the one a gate that
        # runs git is most likely to need.
        for line in self.out("config", "--get-regexp", r"^user\.(name|email)$", check=False).splitlines():
            key, _, value = line.partition(" ")
            git("config", key, value, cwd=dest)

    # -- pinned refs -------------------------------------------------------------------------

    def pin(self, run, name, obj):
        """Keep `obj` (a tree or a commit) safe from garbage collection. Idempotent."""
        ref = f"{REF_PREFIX}{run}/{name}"
        self.run("update-ref", ref, obj)
        return ref

    def pins(self, run=None):
        """ref -> object id, for one run or for all of them."""
        prefix = f"{REF_PREFIX}{run}/" if run else REF_PREFIX
        raw = self.out("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return dict(line.split(" ", 1) for line in raw.splitlines() if line)

    def pinned_runs(self):
        return sorted({ref[len(REF_PREFIX):].split("/", 1)[0] for ref in self.pins()})

    def unpin_run(self, run):
        refs = sorted(self.pins(run))
        for ref in refs:
            self.run("update-ref", "-d", ref)
        return refs

    # -- restore ------------------------------------------------------------------------

    def _inside(self, rel):
        parts = rel.split("/")
        if not rel or os.path.isabs(rel) or any(p in ("", ".", "..") for p in parts) \
                or parts[0] == ".git":
            raise RestoreError("refusing a path that is not plainly inside the repository", [rel])
        if self.in_runs_dir(rel):
            raise RestoreError("refusing a path in the run record", [rel])
        return os.path.join(self.top, *parts)

    def in_runs_dir(self, rel):
        """Is `rel` (from the top) the runs directory, inside it, or a directory holding it? The
        runner's own restores must never remove or rewrite the record (W-02)."""
        if not self.runs_dir:
            return False
        runs = os.path.relpath(os.path.realpath(self.runs_dir), self.top).replace(os.sep, "/")
        if runs == ".." or runs.startswith("../"):
            return False
        return rel == runs or rel.startswith(runs + "/") or runs.startswith(rel + "/")

    def _check_parents(self, rel):
        """Every parent must be a real directory inside the repository. A parent that has become a
        symbolic link is removed as a link, never followed."""
        parts = rel.split("/")[:-1]
        current = self.top
        for part in parts:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return                          # git creates the missing directories itself
            if stat.S_ISLNK(info.st_mode):
                os.unlink(current)
                return
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(current)              # a file where a directory must be
                return

    def _prune_empty_parents(self, rel):
        parent = os.path.dirname(os.path.join(self.top, *rel.split("/")))
        while parent != self.top and parent.startswith(self.top + os.sep):
            try:
                os.rmdir(parent)
            except OSError:
                return
            parent = os.path.dirname(parent)

    def _remove(self, rel):
        full = self._inside(rel)
        self._check_parents(rel)
        try:
            info = os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            return
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            shutil.rmtree(full)                 # never follows links
        else:
            os.unlink(full)
        self._prune_empty_parents(rel)

    def restore(self, target_tree, paths, expected_tree=None, index_file=None, crash=_no_crash):
        """Put `paths` back to what `target_tree` holds, by git type and mode.

        Paths the target does not have are removed, and the directories that empties are pruned.
        The rest are recreated by `git checkout-index --force` from an index loaded with the
        target: git unlinks first, so a link is replaced and never written through, and the mode
        comes from the tree. With `expected_tree`, the result is verified by snapshot.
        Running it again gives the same result, which is how a crash in the middle is repaired.
        """
        # Restoring ignore rules may expose untracked files. Only those already ignored
        # before this restore may be added to its scope; unrelated changes must be refused.
        ignored_before = set()
        if expected_tree == target_tree and any(os.path.basename(p) == ".gitignore" for p in paths):
            ignored_before = {_text(p) for p in self.run(
                "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.split(b"\0") if p}
        target = self.ls_tree(target_tree)
        present = [p for p in paths if p in target]
        absent = [p for p in paths if p not in target]
        refused = [p for p in present if target[p][0] == GITLINK]
        if refused:
            raise RestoreError("submodule entries are not supported", refused)
        try:
            # Deepest first, so a directory that replaced a file is emptied before it is pruned.
            for rel in sorted(absent, key=lambda p: (-p.count("/"), p)):
                self._remove(rel)
            crash("restore:after-removals")
            for rel in present:
                self._inside(rel)
                self._check_parents(rel)
            if present:
                with tempfile.TemporaryDirectory(prefix="code-smith-") as tmp:
                    index = os.path.join(tmp, "index")
                    self.run("read-tree", target_tree, index=index)
                    self.run("checkout-index", "--force", "-z", "--stdin", index=index,
                             stdin=b"".join(_bytes(p) + b"\0" for p in present))
        except (OSError, GitError) as exc:
            raise RestoreError(f"the restore could not be completed: {exc}", paths) from exc
        crash("restore:before-verify")
        if expected_tree is not None:
            if index_file is None:
                raise ValueError("verifying a restore needs the run's scratch index")
            now = self.snapshot(index_file)
            revealed = [p for _s, p, _o, _n in self.changed_paths(expected_tree, now)
                        if p in ignored_before]
            if revealed:
                self.restore(target_tree, revealed)
                now = self.snapshot(index_file)
            if now != expected_tree:
                wrong = [p for _s, p, _o, _n in self.changed_paths(expected_tree, now)]
                raise RestoreError(f"after the restore the tree is {now}, expected "
                                   f"{expected_tree}; these paths differ:", wrong)

    # -- recovery artifacts ------------------------------------------------------------------

    # The owner's diff settings (noprefix, color, textconv, an external diff program) never shape
    # a patch that must apply or a diff a reviewer reads, and never run inside the runner.
    PLAIN_DIFF = ("--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/",
                  "--dst-prefix=b/", "--submodule=short")

    def plain_diff(self, *args):
        """`git diff` of two trees that no attribute and no repository setting can shape.
        Attributes decide what git calls binary (`* -diff` turns every text file into `Binary files
        differ`, zero lines), and they come from places outside the trees being compared: the
        user's `core.attributesFile`, the system file, `.git/info/attributes`, and the work tree's
        `.gitattributes`, which the author edits. So the diff runs in a scratch bare repository
        that borrows this one's objects: it has no work tree and no info/attributes, its HEAD is
        unborn (a bare repository's attribute tree), `core.attributesFile` is /dev/null and the
        system file is off. Binary is then git's own content test, a NUL in the first 8000 bytes,
        the same for the same two trees on every machine."""
        objects = os.path.realpath(os.path.join(self.top, self.out("rev-parse", "--git-path", "objects")))
        with tempfile.TemporaryDirectory(prefix="code-smith-diff-") as scratch:
            env = self.env()
            env.update(GIT_ATTR_NOSYSTEM="1", GIT_DIR=scratch)
            subprocess.run(["git", "init", "-q", "--bare", scratch], capture_output=True, env=env,
                           check=True)
            with open(os.path.join(scratch, "objects", "info", "alternates"), "w") as fh:
                fh.write(objects + "\n")
            argv = ["-c", "core.attributesFile=/dev/null", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.quotepath=false", "diff", *args]
            if TRACE is not None:
                TRACE.append(["diff", *args])
            res = subprocess.run(["git", *argv], cwd=scratch, capture_output=True, env=env)
        if res.returncode != 0:
            raise GitError(["diff", *args], res.returncode, _text(res.stderr))
        return res.stdout

    def full_patch(self, base_tree, candidate_tree):
        """The complete, binary-capable patch from base to candidate. For recovery."""
        return self.plain_diff(*self.PLAIN_DIFF, "--binary", "--full-index", "--no-renames",
                               base_tree, candidate_tree)

    def review_diff(self, base_tree, candidate_tree):
        """The readable text diff. For reading only; cap it with `cap_diff`."""
        return _text(self.plain_diff(*self.PLAIN_DIFF, "--no-renames", base_tree, candidate_tree))

    def numstat(self, base_tree, candidate_tree):
        """[(path, lines)] for every file that differs from base to candidate: the measure of the
        diff budget. The flags are fixed so that the same two trees always give the same
        counts, whatever the user's configuration: renames found at 50% similarity with no limit,
        the myers algorithm, no external driver or textconv, no submodule ignored. Lines are added
        plus deleted; a binary file counts zero lines, and a rename is one entry, its new path.
        Binary is decided by content alone, never by attributes (`plain_diff`)."""
        raw = self.plain_diff(*self.PLAIN_DIFF, "--numstat", "-z", "-M50%", "-l0",
                              "--diff-algorithm=myers", "--no-relative", "--ignore-submodules=none",
                              base_tree, candidate_tree)
        fields = raw.split(b"\0")
        out = []
        i = 0
        while i < len(fields):
            if not fields[i]:
                i += 1
                continue
            added, deleted, path = _text(fields[i]).split("\t", 2)
            if not path:                         # a rename: the old path, then the new one
                path = _text(fields[i + 2])
                i += 2
            lines = 0 if added == "-" else int(added) + int(deleted)
            out.append((path, lines))
            i += 1
        return out

    def apply_patch(self, patch_file, check_only=False):
        """Put set-aside work back into the work tree (`retry --apply-patch`). The index is not
        touched. With `check_only`, only say whether it would apply."""
        args = ["apply", "--whitespace=nowarn"] + (["--check"] if check_only else [])
        self.run(*args, patch_file)

    # -- branches ----------------------------------------------------------------------------

    def branch_exists(self, name):
        return self.run("show-ref", "--verify", "--quiet", f"refs/heads/{name}",
                        check=False).returncode == 0

    def create_and_checkout_run_branch(self, name):
        """Create `name` at HEAD and check it out. The tree is clean and identical, so nothing in
        it changes. Returns the branch that was checked out before (None if detached)."""
        before = self.current_branch()
        if self.branch_exists(name):
            raise GitError(["checkout", "-b", name], 128, f"branch '{name}' already exists")
        self.run("checkout", "-q", "-b", name)
        return before

    # -- what a producer transaction owns in git (W-01) --------------------------------------

    # Files under the git directory that change what a snapshot sees or how a file is read, with
    # no trace in the work tree. Kept as base64 so any bytes survive the JSON state.
    INFO_FILES = ("info/exclude", "info/attributes")
    # An operation git leaves half-done across commands. Putting HEAD back under one would decide
    # it for its author, so the runner stops instead.
    IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply",
                   "BISECT_LOG")

    def head_ref(self):
        """The full name of the checked-out branch (`refs/heads/x`), or None when detached."""
        res = self.run("symbolic-ref", "-q", "HEAD", check=False)
        return _text(res.stdout).strip() or None if res.returncode == 0 else None

    def ref(self, name):
        """The commit a ref names, or None when it does not exist (an unborn or deleted branch)."""
        res = self.run("rev-parse", "--verify", "-q", name + "^{commit}", check=False)
        return _text(res.stdout).strip() or None if res.returncode == 0 else None

    def in_progress(self):
        return [name for name in self.IN_PROGRESS if os.path.lexists(self.git_path(name))]

    def info_files(self):
        """name -> base64 of the file's bytes, or None where it does not exist."""
        found = {}
        for name in self.INFO_FILES:
            try:
                with open(self.git_path(name), "rb") as fh:
                    found[name] = base64.b64encode(fh.read()).decode("ascii")
            except FileNotFoundError:
                found[name] = None
        return found

    def put_info_file(self, name, data):
        """Write back what `info_files` read: the bytes, or no file at all."""
        path = self.git_path(name)
        if data is None:
            if os.path.lexists(path):
                os.unlink(path)
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.islink(path):
            os.unlink(path)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))

    def put_head_back(self, branch, commit):
        """The branch at `commit` and checked out, the index at its tree, the work tree untouched:
        what the author left in the files stays there, to be judged like any candidate. `branch`
        None means HEAD was detached at `commit`."""
        if branch:
            self.run("update-ref", "-m", "code-smith: put the run branch back", branch, commit)
            self.run("symbolic-ref", "HEAD", branch)
        else:
            self.run("update-ref", "--no-deref", "HEAD", commit)
        self.sync_index()

    # -- the commit recipe --------------------------------------------------------------

    def tree_with(self, base_tree, source_tree, paths):
        """`base_tree` with each of `paths` taken from `source_tree`; a path absent from
        `source_tree` is absent from the result. Computed in a scratch index; the real index and
        the work tree are not touched. Returns a tree id."""
        src = self.ls_tree(source_tree)
        lines = []
        for rel in paths:
            if rel in src:
                mode, sha = src[rel]
                lines.append(_bytes(f"{mode} {sha}\t{rel}") + b"\0")
            else:
                lines.append(_bytes(f"0 {ZERO}\t{rel}") + b"\0")
        with tempfile.TemporaryDirectory(prefix="code-smith-") as tmp:
            index = os.path.join(tmp, "index")
            self.run("read-tree", base_tree, index=index)
            if lines:
                self.run("update-index", "-z", "--index-info", index=index, stdin=b"".join(lines))
            return self.out("write-tree", index=index)

    def build_commit_tree(self, candidate_tree, paths):
        """HEAD's tree with the task's `paths` taken from the candidate. Must equal the candidate:
        by then every change outside the task's `writes` has been reverted."""
        tree = self.tree_with(self.tree_of("HEAD"), candidate_tree, paths)
        if tree != candidate_tree:
            extra = [p for _s, p, _o, _n in self.changed_paths(tree, candidate_tree)]
            raise CommitRefused("the tree to commit is not the verified candidate; paths outside "
                                "the task differ: " + ", ".join(extra))
        return tree

    @staticmethod
    def commit_message(subject, run_id, task_id, op_id, extra_trailer=""):
        trailers = [f"Run: {run_id}", f"Task: {task_id}", f"Operation: {op_id}"]
        if extra_trailer:
            trailers.append(extra_trailer.strip())
        return subject.strip() + "\n\n" + "\n".join(trailers) + "\n"

    def commit_candidate(self, candidate_tree, paths, subject, run_id, task_id, op_id,
                         expected_parent, extra_trailer="", crash=_no_crash):
        """One commit of exactly the candidate, then make the real index follow it.

        The caller records the intent first. `crash` is called at the named points between the
        steps, so tests can stop the recipe anywhere; `resume` repairs each of those stops.
        """
        if self.head() != expected_parent:
            raise CommitRefused(f"the branch tip is {self.head()}, expected {expected_parent}")
        crash("commit:before-tree")
        tree = self.build_commit_tree(candidate_tree, paths)
        message = self.commit_message(subject, run_id, task_id, op_id, extra_trailer)
        commit = self.out("commit-tree", tree, "-p", expected_parent, "-F", "-",
                          stdin=_bytes(message), extra_env=self.identity_env())
        crash("commit:after-commit-object")
        try:
            self.run("update-ref", "-m", f"code-smith: accept {task_id}", "HEAD", commit,
                     expected_parent)
        except GitError as exc:
            raise CommitRefused(f"the branch tip moved: {exc.stderr.strip()}") from exc
        crash("commit:after-update-ref")
        self.sync_index()
        crash("commit:after-index-sync")
        return commit

    def sync_index(self):
        """Make the real index describe HEAD. Writes nothing to the work tree. Without it the index
        still describes the old tip: status shows phantom changes and a later revert refuses.
        A one-tree `--reset` keeps the owner's skip-worktree bits (sparse checkout) and stat data."""
        self.run("read-tree", "--reset", "HEAD")

    def trailers(self, commit):
        body = self.out("log", "-1", "--format=%B", commit)
        found = {}
        for line in body.splitlines():
            key, sep, value = line.partition(": ")
            if sep and key in ("Run", "Task", "Operation", "Reverts"):
                found[key] = value.strip()
        return found

    def find_operation(self, commit, op_id):
        """Does `commit` carry this operation id?"""
        return self.trailers(commit).get("Operation") == op_id

    def commit_files(self, commit):
        raw = self.run("diff-tree", "-r", "-z", "--no-renames", "--name-only", "--root",
                       "--no-commit-id", commit).stdout
        return [_text(p) for p in raw.split(b"\0") if p]

    # -- reverts for --reopen ------------------------------------------------------

    def revert_in_progress(self):
        return os.path.exists(self.git_path("REVERT_HEAD"))

    def revert_commits(self, commits, op_id, run_id, since, crash=_no_crash):
        """Undo `commits` (newest first) with new commits. Resumable: a half-done revert is aborted,
        and a revert already on the branch is recognised by its operation id `<op_id>/<n>`.

        `since` is the branch tip recorded in the intent; only commits after it are searched.
        Returns the list of revert commits, in the order of `commits`.

        Each revert is staged in the real index and committed from it, so a revert starts only
        from a tracked tree and index that match HEAD: the owner's changes are never swept into a
        revert commit, nor thrown away by aborting one (RevertConflict, nothing changed).
        """
        if self.revert_in_progress():
            self.run("revert", "--abort")
        done = {}
        for sha in self.out("rev-list", f"{since}..HEAD").splitlines():
            op = self.trailers(sha).get("Operation", "")
            if op.startswith(op_id + "/"):
                done[op] = sha
        made = []
        for n, commit in enumerate(commits, 1):
            step = f"{op_id}/{n}"
            if step in done:
                made.append(done[step])
                continue
            changed = self.run("status", "--porcelain", "-z", "--no-renames", "--untracked-files=no").stdout
            if changed:
                raise RevertConflict(commit, "the index or the work tree has changes of its own: "
                                     + ", ".join(_text(e[3:]) for e in changed.split(b"\0") if e)
                                     + "; commit or stash them, then resume")
            res = self.run("revert", "--no-commit", commit, check=False)
            if res.returncode != 0:
                if self.revert_in_progress():
                    self.run("revert", "--abort")
                raise RevertConflict(commit, _text(res.stderr))
            crash(f"revert:{n}:staged")
            subject = self.out("log", "-1", "--format=%s", commit)
            message = (f"Revert \"{subject}\"\n\nThis reverts commit {commit}.\n\n"
                       f"Run: {run_id}\nOperation: {step}\nReverts: {commit}\n")
            self.run("commit", "-q", "--allow-empty", "-F", "-", stdin=_bytes(message),
                     extra_env=self.identity_env())
            made.append(self.head())
            crash(f"revert:{n}:committed")
        return made


def cap_diff(text, cap_bytes, full_path=""):
    """Cut a text diff at a file boundary so it fits `cap_bytes`, with a marker that names what was
    left out. A pure function: the same diff and cap always give the same text."""
    if len(text.encode("utf-8", errors="surrogateescape")) <= cap_bytes:
        return text
    sections, current = [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))

    def name(section):
        lines = section.splitlines()
        for prefix in ("+++ b/", "--- a/"):         # a deleted file has only the second
            for line in lines:
                if line.startswith(prefix):
                    return line[len(prefix):]
        return lines[0].split(" b/", 1)[-1]          # a binary or mode-only change

    kept, omitted, used = [], [], 0
    for section in sections:
        size = len(section.encode("utf-8", errors="surrogateescape"))
        if not omitted and used + size <= cap_bytes:
            kept.append(section)
            used += size
        else:
            omitted.append(name(section))
    marker = (f"[diff truncated: {len(kept)} of {len(sections)} files shown. Omitted: "
              + ", ".join(omitted) + "."
              + (f" The full diff is in {full_path}." if full_path else "") + "]\n")
    return "".join(kept) + marker
